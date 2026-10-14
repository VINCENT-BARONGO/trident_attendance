from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone, now_datetime

from trident_attendance import push, shift
from trident_attendance.employee_access import HubOnlyError
from trident_attendance.shift import SIGNAL_DOCTYPE, punched
from trident_attendance.tests import helpers
from trident_attendance.tests.test_push import TOKEN_A, TOKEN_B, TriggerCase, devices

HUB = f"tokhub@{helpers.TEST_DOMAIN}"


def refused(reason) -> dict:
	"""The answer for a punch that was not taken, or about which nobody was told: never an
	error status, which the hub would read as "this ERP does not have the method"."""
	return {"ok": False, "reason": reason, "pushed": 0}


def ago(**delta):
	"""A time before now, to the second, as the ERP keeps it."""
	return now_datetime().replace(microsecond=0) - timedelta(**delta)


def iso(when) -> str:
	"""What the phone is told: the site's time with its offset from UTC."""
	return when.replace(tzinfo=ZoneInfo(get_system_timezone())).isoformat()


def signal(employee):
	return frappe.db.get_value(
		SIGNAL_DOCTYPE, {"employee": employee}, ["name", "employee", "log_type", "at", "source", "received_at"], as_dict=True
	)


class ShiftCase(TriggerCase):
	"""Push on, a device for each of the two employees, the queue captured, and the hub's own
	user named in the settings and signed in."""

	def jobs(self):
		"""This app's jobs only: making a test user also queues Frappe's own create_contact."""
		return [job for job in super().jobs() if not str(job.get("method") or "").startswith("frappe.")]

	def setUp(self):
		super().setUp()
		helpers.make_user(HUB, roles=["Attendance Admin"])
		helpers.set_settings(hub_service_user=HUB)
		frappe.set_user(HUB)


class TestPunched(ShiftCase):
	def test_a_recent_in_is_kept_and_sent(self):
		when = ago(minutes=2)
		self.assertEqual(punched(self.employee_a, "IN", iso(when), source="terminal"), {"ok": True, "pushed": 1})
		row = signal(self.employee_a)
		self.assertEqual((row.name, row.employee, row.log_type, row.source), (self.employee_a, self.employee_a, "IN", "terminal"))
		self.assertEqual(get_datetime(row.at), when)
		self.assertTrue(row.received_at)
		jobs = self.jobs()
		self.assertEqual(len(jobs), 1)
		job = jobs[0]
		data = {"shift": "in", "at": iso(when), "employee": self.employee_a}
		self.assertEqual(
			(job.method, job.queue, job.enqueue_after_commit, job.kind, job.data, job.ttl),
			("trident_attendance.push._send_data_to_devices", "short", True, "shift", data, 600),
		)
		self.assertEqual(job.devices, [r.name for r in devices(user=self.user_a)])
		# What Firebase is sent: data alone, high priority, given up on after ten minutes.
		self.queue.jobs = [job]
		self.queue.run()
		self.assertEqual(
			[p.payload for p in self.posted],
			[
				{
					"message": {
						"token": TOKEN_A,
						"data": {"kind": "shift", "shift": "in", "at": iso(when), "employee": self.employee_a},
						"android": {"priority": "HIGH", "ttl": "600s"},
					}
				}
			],
		)
		self.assertNotIn("notification", self.posted[0].payload["message"])

	def test_an_out_is_sent(self):
		punched(self.employee_a, "IN", iso(ago(minutes=9)))
		self.jobs()
		when = ago(minutes=1)
		self.assertEqual(punched(self.employee_a, "out", iso(when), source="phone"), {"ok": True, "pushed": 1})
		job = self.jobs()[0]
		self.assertEqual((job.kind, job.ttl), ("shift", 600))
		self.assertEqual(job.data, {"shift": "out", "at": iso(when), "employee": self.employee_a})
		row = signal(self.employee_a)
		self.assertEqual((row.log_type, row.source, get_datetime(row.at)), ("OUT", "phone", when))
		self.assertEqual(frappe.db.count(SIGNAL_DOCTYPE, {"employee": self.employee_a}), 1)

	def test_a_stale_in_is_kept_but_not_sent(self):
		when = ago(minutes=16)
		self.assertEqual(punched(self.employee_a, "IN", iso(when)), {"ok": True, "pushed": 0, "reason": "stale"})
		self.assertEqual(self.jobs(), [])
		row = signal(self.employee_a)
		self.assertEqual((row.log_type, get_datetime(row.at)), ("IN", when))
		# Just inside the fifteen minutes it is sent.
		self.assertEqual(punched(self.employee_a, "IN", iso(ago(minutes=14))), {"ok": True, "pushed": 1})
		self.assertEqual(len(self.jobs()), 1)

	def test_a_late_out_is_sent_all_the_same(self):
		self.assertEqual(punched(self.employee_a, "IN", iso(ago(hours=9)))["reason"], "stale")
		self.assertEqual(self.jobs(), [])
		when = ago(hours=3)
		self.assertEqual(punched(self.employee_a, "OUT", iso(when)), {"ok": True, "pushed": 1})
		jobs = self.jobs()
		self.assertEqual(len(jobs), 1)
		self.assertEqual(jobs[0].data, {"shift": "out", "at": iso(when), "employee": self.employee_a})
		self.assertEqual(signal(self.employee_a).log_type, "OUT")

	def test_the_same_punch_again_does_nothing_more(self):
		when = ago(minutes=1)
		self.assertEqual(punched(self.employee_a, "IN", iso(when), source="terminal"), {"ok": True, "pushed": 1})
		received = signal(self.employee_a).received_at
		self.assertEqual(len(self.jobs()), 1)
		for again in (iso(when), str(when), iso(when).replace("T", " ")):
			self.assertEqual(punched(self.employee_a, "IN", again, source="terminal"), refused("duplicate"))
			self.assertEqual(punched(self.employee_a, "in", again), refused("duplicate"))
		self.assertEqual(self.jobs(), [])
		row = signal(self.employee_a)
		self.assertEqual((row.log_type, row.source, row.received_at), ("IN", "terminal", received))
		self.assertEqual(frappe.db.count(SIGNAL_DOCTYPE, {"employee": self.employee_a}), 1)

	def test_an_older_punch_arriving_late_changes_nothing(self):
		newer = ago(minutes=1)
		punched(self.employee_a, "IN", iso(newer), source="phone")
		self.jobs()
		# The OUT of the shift before, passed on late: it would stop the reporting just started.
		self.assertEqual(punched(self.employee_a, "OUT", iso(ago(minutes=5)), source="terminal"), refused("older"))
		self.assertEqual(punched(self.employee_a, "IN", iso(ago(hours=20))), refused("older"))
		self.assertEqual(self.jobs(), [])
		row = signal(self.employee_a)
		self.assertEqual((row.log_type, row.source, get_datetime(row.at)), ("IN", "phone", newer))

	def test_each_employee_has_their_own_signal(self):
		punched(self.employee_a, "IN", iso(ago(minutes=3)))
		punched(self.employee_b, "OUT", iso(ago(minutes=2)))
		self.assertEqual((signal(self.employee_a).log_type, signal(self.employee_b).log_type), ("IN", "OUT"))
		jobs = self.jobs()
		self.assertEqual([j.devices for j in jobs], [[r.name for r in devices(user=u)] for u in (self.user_a, self.user_b)])

	def test_the_time_is_read_in_every_form(self):
		site = ZoneInfo(get_system_timezone())
		aware = datetime(2026, 10, 13, 4, 58, 0, tzinfo=timezone.utc)
		local = aware.astimezone(site).replace(tzinfo=None)
		for text in (
			"2026-10-13T04:58:00Z",
			"2026-10-13T04:58:00+00:00",
			"2026-10-13T06:58:00+02:00",
			"2026-10-13T04:58:00.250Z",
			"2026-10-13 04:58:00+00:00",
		):
			self.assertEqual(shift._punch_time(text), local, text)
		# Without an offset it is the site's own time.
		for text in ("2026-10-13 07:58:00", "2026-10-13T07:58:00", "2026-10-13 07:58:00.123456", " 2026-10-13 07:58:00 "):
			self.assertEqual(shift._punch_time(text), datetime(2026, 10, 13, 7, 58, 0), text)
		self.assertEqual(shift._punch_time(datetime(2026, 10, 13, 7, 58, 0, 5)), datetime(2026, 10, 13, 7, 58, 0))
		for bad in (None, "", "   ", "yesterday morning", "25:99", {}):
			self.assertIsNone(shift._punch_time(bad), bad)

	def test_a_punch_that_cannot_be_taken_is_answered_not_raised(self):
		"""Frappe answers 417 for a validation error, and the hub reads a 417 as "no such
		method" for the whole company. Each of these is an ordinary answer with a reason."""
		now = iso(ago(minutes=1))
		for log_type in (None, "", "BREAK", "INOUT", "1"):
			self.assertEqual(punched(self.employee_a, log_type, now), refused("bad_log_type"), log_type)
		for employee in (None, "", "HR-EMP-TOK-NOBODY"):
			self.assertEqual(punched(employee, "IN", now), refused("unknown_employee"), employee)
		for at in (None, "", "not a time"):
			self.assertEqual(punched(self.employee_a, "IN", at), refused("bad_time"), at)
		# A clock a little ahead is taken; a punch from the future is not.
		self.assertEqual(punched(self.employee_a, "IN", iso(ago(minutes=-11))), refused("bad_time"))
		self.assertEqual(punched(), refused("bad_log_type"))
		self.assertIsNone(signal(self.employee_a))
		self.assertEqual(self.jobs(), [])
		self.assertEqual(self.errors_logged, [])
		self.assertNotEqual(frappe.local.response.get("http_status_code"), 417)
		self.assertEqual(punched(self.employee_a, " in ", iso(ago(minutes=-5))), {"ok": True, "pushed": 1})

	def test_an_inactive_employee(self):
		frappe.db.set_value("Employee", self.employee_a, "status", "Left")
		self.assertEqual(punched(self.employee_a, "IN", iso(ago(minutes=1))), refused("inactive"))
		self.assertIsNone(signal(self.employee_a))
		self.assertEqual(self.jobs(), [])

	def test_something_unexpected_is_a_500_not_a_417(self):
		"""A validation error from somewhere inside would be a 417 if it were let through. Once
		only: the method rolls the transaction back, and the test's records go with it."""
		with patch("trident_attendance.shift._record", side_effect=frappe.ValidationError("a rule somewhere")):
			self.assertEqual(punched(self.employee_a, "IN", iso(ago(minutes=1))), refused("server_error"))
		self.assertEqual(frappe.local.response.http_status_code, 500)
		self.assertEqual(len(self.errors_logged), 1)
		self.assertIn("trident_attendance: shift.punched failed\nValidationError", self.errors_logged[0])
		self.assertEqual(self.jobs(), [])

	def test_the_source_is_a_short_label(self):
		punched(self.employee_a, "IN", iso(ago(minutes=1)), source="  " + "t" * 60)
		self.assertEqual(signal(self.employee_a).source, "t" * 40)
		punched(self.employee_b, "IN", iso(ago(minutes=1)), source="   ")
		self.assertIsNone(signal(self.employee_b).source)

	def test_nobody_to_tell_is_not_an_error(self):
		# No user.
		frappe.set_user("Administrator")
		employee = helpers.make_employee("Tokthree").name
		frappe.set_user(HUB)
		self.assertEqual(punched(employee, "IN", iso(ago(minutes=1))), refused("no_user"))
		self.assertEqual(signal(employee).log_type, "IN")
		# A user with no phone, and one whose only phone Firebase no longer knows.
		frappe.db.delete(push.DEVICE_DOCTYPE, {"user": self.user_a})
		frappe.db.set_value(push.DEVICE_DOCTYPE, {"token": TOKEN_B}, "enabled", 0)
		for employee in (self.employee_a, self.employee_b):
			self.assertEqual(punched(employee, "IN", iso(ago(minutes=1))), refused("no_device"))
			self.assertEqual(signal(employee).log_type, "IN")
		self.assertEqual(self.jobs(), [])

	def test_push_switched_off_still_keeps_the_signal(self):
		frappe.set_user("Administrator")
		helpers.set_settings(push_enabled=0)
		frappe.set_user(HUB)
		self.assertEqual(punched(self.employee_a, "IN", iso(ago(minutes=1))), refused("push_disabled"))
		self.assertEqual(signal(self.employee_a).log_type, "IN")
		self.assertEqual(self.jobs(), [])

	def test_a_push_that_cannot_be_queued_does_not_fail_the_call(self):
		with patch("trident_attendance.push.instruct_user", side_effect=RuntimeError("queue down")):
			self.assertEqual(punched(self.employee_a, "IN", iso(ago(minutes=1))), refused("push_failed"))
		self.assertEqual(signal(self.employee_a).log_type, "IN")
		self.assertEqual(len(self.errors_logged), 1)
		self.assertIn("trident_attendance: shift push failed\nRuntimeError", self.errors_logged[0])

	def test_an_employee_checkin_sends_nothing(self):
		"""Check-ins reach the ERP at night: inserting one is not a punch being made."""
		frappe.set_user("Administrator")
		frappe.get_doc(
			{"doctype": "Employee Checkin", "employee": self.employee_a, "time": ago(minutes=1), "log_type": "IN"}
		).insert(ignore_permissions=True)
		self.assertEqual(self.jobs(), [])
		self.assertIsNone(signal(self.employee_a))
		hooks = frappe.get_hooks("doc_events").get("Employee Checkin", {}).get("after_insert", [])
		self.assertEqual([hook for hook in hooks if hook.startswith("trident_attendance.")], [])


class TestWhoMayCall(ShiftCase):
	def refused(self, user):
		frappe.set_user(user)
		self.assertRaises(frappe.PermissionError, punched, self.employee_a, "IN", iso(ago(minutes=1)))
		# Before anything is looked at: a bad punch from a stranger is still a 403.
		self.assertRaises(frappe.PermissionError, punched, "HR-EMP-TOK-NOBODY", "BREAK", "not a time")
		frappe.set_user("Administrator")
		self.assertIsNone(signal(self.employee_a))
		self.assertEqual(self.jobs(), [])

	def test_the_hub(self):
		self.assertEqual(punched(self.employee_a, "IN", iso(ago(minutes=1))), {"ok": True, "pushed": 1})

	def test_a_system_manager(self):
		manager = helpers.make_user(f"toksys@{helpers.TEST_DOMAIN}", roles=["System Manager"]).name
		for user in (manager, "Administrator"):
			frappe.set_user(user)
			self.assertEqual(punched(self.employee_a, "IN", iso(ago(seconds=30))), {"ok": True, "pushed": 1})
			frappe.db.delete(SIGNAL_DOCTYPE, {"employee": self.employee_a})

	def test_everyone_else_is_refused(self):
		# The hub's role without being the user named in the settings; HR; the employee themselves.
		reviewer = helpers.make_user(f"tokreviewer@{helpers.TEST_DOMAIN}", roles=["Attendance Admin"]).name
		hr = helpers.make_user(f"tokhr@{helpers.TEST_DOMAIN}", roles=["HR Manager", "HR User"]).name
		for user in (reviewer, hr, self.user_a, "Guest"):
			self.refused(user)
		# The same refusal as the hub-only sign-in methods give.
		frappe.set_user(reviewer)
		self.assertRaises(HubOnlyError, punched, self.employee_a, "IN", iso(ago(minutes=1)))

	def test_the_named_user_without_the_role_is_refused(self):
		plain = helpers.make_user(f"tokplain@{helpers.TEST_DOMAIN}", roles=["HR User"]).name
		frappe.set_user("Administrator")
		helpers.set_settings(hub_service_user=plain)
		self.refused(plain)
		self.refused(HUB)

	def test_no_hub_named(self):
		frappe.set_user("Administrator")
		helpers.set_settings(hub_service_user=None)
		self.refused(HUB)

	def test_post_only_and_not_for_guests(self):
		self.assertIn(punched, frappe.whitelisted)
		self.assertNotIn(punched, frappe.guest_methods)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[punched], ["POST"])


class TestClockedIn(ShiftCase):
	def test_an_in_of_the_last_eighteen_hours(self):
		self.assertFalse(shift.is_clocked_in(self.employee_a))
		punched(self.employee_a, "IN", iso(ago(minutes=1)))
		self.assertTrue(shift.is_clocked_in(self.employee_a))
		self.assertFalse(shift.is_clocked_in(self.employee_b))
		punched(self.employee_a, "OUT", iso(ago(seconds=10)))
		self.assertFalse(shift.is_clocked_in(self.employee_a))

	def test_an_in_whose_out_never_came(self):
		punched(self.employee_a, "IN", iso(ago(hours=17, minutes=50)))
		self.assertTrue(shift.is_clocked_in(self.employee_a))
		punched(self.employee_b, "IN", iso(ago(hours=18, minutes=10)))
		self.assertFalse(shift.is_clocked_in(self.employee_b))

	def test_employee_checkins_do_not_count(self):
		frappe.set_user("Administrator")
		frappe.get_doc(
			{"doctype": "Employee Checkin", "employee": self.employee_a, "time": ago(minutes=5), "log_type": "IN"}
		).insert(ignore_permissions=True)
		self.assertFalse(shift.is_clocked_in(self.employee_a))


class TestShiftSignalRows(ShiftCase):
	def test_not_edited_by_hand(self):
		punched(self.employee_a, "IN", iso(ago(minutes=1)))
		frappe.set_user("Administrator")
		row = frappe.get_doc(SIGNAL_DOCTYPE, signal(self.employee_a).name)
		row.log_type = "OUT"
		self.assertRaises(frappe.PermissionError, row.save)
		new = frappe.get_doc(
			{"doctype": SIGNAL_DOCTYPE, "employee": self.employee_b, "log_type": "IN", "at": ago(minutes=1)}
		)
		self.assertRaises(frappe.PermissionError, new.insert)
		self.assertEqual(signal(self.employee_a).log_type, "IN")
		self.assertIsNone(signal(self.employee_b))

	def test_who_reads_the_rows(self):
		meta = frappe.get_meta(SIGNAL_DOCTYPE)
		self.assertEqual(sorted(p.role for p in meta.permissions), ["HR Manager", "System Manager"])
		for permission in meta.permissions:
			self.assertEqual((permission.read, permission.write, permission.create), (1, 0, 0), permission.role)
			self.assertEqual(permission.delete, 1 if permission.role == "System Manager" else 0)
		self.assertFalse(meta.track_changes)
		frappe.set_user(self.user_a)
		self.assertFalse(frappe.has_permission(SIGNAL_DOCTYPE, "read"))

	def test_the_signal_goes_with_the_employee(self):
		punched(self.employee_a, "IN", iso(ago(minutes=1)))
		punched(self.employee_b, "IN", iso(ago(minutes=1)))
		shift.on_employee_trash(frappe._dict(name=self.employee_a))
		self.assertIsNone(signal(self.employee_a))
		# Merged into another employee: the old one's row goes; a plain rename keeps it.
		shift.before_employee_rename(frappe._dict(name=self.employee_b), "before_rename", self.employee_b, "X", False)
		self.assertEqual(signal(self.employee_b).log_type, "IN")
		shift.before_employee_rename(frappe._dict(name=self.employee_b), "before_rename", self.employee_b, "X", True)
		self.assertIsNone(signal(self.employee_b))

	def test_the_employee_hooks_are_wired(self):
		events = frappe.get_hooks("doc_events")["Employee"]
		self.assertIn("trident_attendance.shift.on_employee_trash", events["on_trash"])
		self.assertIn("trident_attendance.employee_pin.on_employee_trash", events["on_trash"])
		self.assertIn("trident_attendance.shift.before_employee_rename", events["before_rename"])
