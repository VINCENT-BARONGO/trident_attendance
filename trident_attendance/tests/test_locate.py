from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import frappe
from frappe.utils import get_datetime, now_datetime

from trident_attendance import locate, push, shift
from trident_attendance.locate import REQUEST_DOCTYPE, location_answer, may_locate, request_location
from trident_attendance.tests import helpers
from trident_attendance.tests.test_push import TOKEN_A, TOKEN_C, TriggerCase, devices
from trident_attendance.tests.test_shift import ago, iso
from trident_attendance.utils import SETTINGS_DOCTYPE


def requests(**filters):
	return frappe.get_all(
		REQUEST_DOCTYPE,
		filters=filters,
		fields=["name", "employee", "employee_name", "requested_by", "requested_at", "device_count"],
		order_by="creation",
	)


class LocateCase(TriggerCase):
	"""Push on, a device for each of the two employees, the queue captured. Attendance Admin is
	the role that may locate; employee A is clocked in. Signed in as an Attendance Admin."""

	def setUp(self):
		super().setUp()
		self.locator = helpers.make_user(f"toklocator@{helpers.TEST_DOMAIN}", roles=["Attendance Admin"]).name
		self.manager = helpers.make_user(f"toksys@{helpers.TEST_DOMAIN}", roles=["System Manager"]).name
		self.hr = helpers.make_user(f"tokhr@{helpers.TEST_DOMAIN}", roles=["HR User"]).name
		helpers.set_settings(locate_role="Attendance Admin")
		self.clock(self.employee_a, "IN")
		frappe.set_user(self.locator)

	def clock(self, employee, log_type, **delta):
		"""The hub's punch for the employee, `delta` ago (a minute when not given)."""
		user = frappe.session.user
		frappe.set_user("Administrator")
		shift.punched(employee, log_type, iso(ago(**(delta or {"minutes": 1}))))
		frappe.set_user(user)
		self.jobs()

	def assertNotAsked(self, employee=None):
		self.assertEqual(requests(**({"employee": employee} if employee else {})), [])
		self.assertEqual(self.jobs(), [])


class TestRequestLocation(LocateCase):
	def test_the_answer_the_record_and_the_push(self):
		before = now_datetime().replace(microsecond=0)
		answer = request_location(self.employee_a)
		rows = requests()
		self.assertEqual(len(rows), 1)
		row = rows[0]
		name = frappe.db.get_value("Employee", self.employee_a, "employee_name")
		self.assertEqual(answer, {"request": row.name, "employee": self.employee_a, "employee_name": name, "devices": 1})
		# Who asked about whom, and when.
		self.assertEqual((row.employee, row.employee_name, row.requested_by, row.device_count), (self.employee_a, name, self.locator, 1))
		self.assertTrue(before <= get_datetime(row.requested_at) <= now_datetime() + timedelta(seconds=1))
		self.assertTrue(row.name.startswith("LOC-"))

		jobs = self.jobs()
		self.assertEqual(len(jobs), 1)
		job = jobs[0]
		data = {"request": row.name, "employee": self.employee_a, "url": "/ess/"}
		self.assertEqual(
			(job.method, job.queue, job.enqueue_after_commit, job.kind, job.data, job.ttl),
			("trident_attendance.push._send_data_to_devices", "short", True, "locate", data, 120),
		)
		self.assertEqual(job.devices, [r.name for r in devices(user=self.user_a)])
		for shown in ("title", "body"):
			self.assertNotIn(shown, job)

		# What Firebase is sent: data alone, so the phone shows nothing; high priority; given up
		# on after two minutes.
		self.queue.jobs = [job]
		self.queue.run()
		self.assertEqual(
			[p.payload for p in self.posted],
			[
				{
					"message": {
						"token": TOKEN_A,
						"data": {"kind": "locate", "request": row.name, "employee": self.employee_a, "url": "/ess/"},
						"android": {"priority": "HIGH", "ttl": "120s"},
					}
				}
			],
		)
		message = self.posted[0].payload["message"]
		self.assertNotIn("notification", message)
		self.assertNotIn("notification", message["android"])

	def test_every_enabled_phone_is_asked(self):
		self.register(self.user_a, TOKEN_C)
		self.register(self.user_a, "tok-test-device-token-dddddddddddddddddddddddddddddddddddddddd")
		frappe.db.set_value(push.DEVICE_DOCTYPE, {"token": TOKEN_C}, "enabled", 0)
		frappe.set_user(self.locator)
		answer = request_location(self.employee_a)
		self.assertEqual(answer["devices"], 2)
		self.assertEqual(requests()[0].device_count, 2)
		self.assertEqual(sorted(self.jobs()[0].devices), sorted(r.name for r in devices(user=self.user_a, enabled=1)))

	def test_the_role_is_required(self):
		for user in (self.hr, self.user_a, self.user_b):
			frappe.set_user(user)
			self.assertRaises(frappe.PermissionError, request_location, self.employee_a)
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, request_location, self.employee_a)
		frappe.set_user("Administrator")
		self.assertNotAsked()

	def test_the_role_named_in_the_settings_decides(self):
		frappe.set_user("Administrator")
		helpers.set_settings(locate_role="HR User")
		frappe.set_user(self.locator)
		self.assertRaises(frappe.PermissionError, request_location, self.employee_a)
		frappe.set_user(self.hr)
		self.assertEqual(request_location(self.employee_a)["devices"], 1)
		self.assertEqual([r.requested_by for r in requests()], [self.hr])

	def test_no_role_named_leaves_system_managers(self):
		frappe.set_user("Administrator")
		helpers.set_settings(locate_role=None)
		for user in (self.locator, self.hr):
			frappe.set_user(user)
			self.assertRaises(frappe.PermissionError, request_location, self.employee_a)
		frappe.set_user(self.manager)
		self.assertEqual(request_location(self.employee_a)["devices"], 1)

	def test_a_system_manager_always_may(self):
		frappe.set_user("Administrator")
		helpers.set_settings(locate_role="HR User")
		frappe.set_user(self.manager)
		self.assertEqual(request_location(self.employee_a)["employee"], self.employee_a)
		self.assertEqual([r.requested_by for r in requests()], [self.manager])

	def test_asking_about_oneself_is_like_any_other_request(self):
		frappe.set_user("Administrator")
		frappe.get_doc("User", self.user_a).add_roles("Attendance Admin")
		frappe.set_user(self.user_a)
		self.assertEqual(request_location(self.employee_a)["devices"], 1)
		self.assertEqual([(r.employee, r.requested_by) for r in requests()], [(self.employee_a, self.user_a)])

	def test_not_clocked_in(self):
		# No punch today at all.
		self.assertRaisesRegex(frappe.ValidationError, "is not clocked in", request_location, self.employee_b)
		# Clocked out.
		self.clock(self.employee_a, "OUT", seconds=20)
		self.assertRaisesRegex(frappe.ValidationError, "is not clocked in", request_location, self.employee_a)
		# An IN whose OUT never came: more than 18 hours old.
		self.clock(self.employee_b, "IN", hours=18, minutes=5)
		self.assertRaisesRegex(frappe.ValidationError, "is not clocked in", request_location, self.employee_b)
		self.assertNotAsked()
		# Clocked in again.
		self.clock(self.employee_a, "IN", seconds=5)
		self.assertEqual(request_location(self.employee_a)["devices"], 1)

	def test_clocked_in_some_hours_ago(self):
		self.clock(self.employee_b, "IN", hours=17)
		self.assertEqual(request_location(self.employee_b)["employee"], self.employee_b)

	def test_an_employee_checkin_does_not_make_someone_clocked_in(self):
		frappe.set_user("Administrator")
		frappe.get_doc(
			{"doctype": "Employee Checkin", "employee": self.employee_b, "time": ago(minutes=5), "log_type": "IN"}
		).insert(ignore_permissions=True)
		frappe.set_user(self.locator)
		self.assertRaisesRegex(frappe.ValidationError, "is not clocked in", request_location, self.employee_b)

	def test_no_phone_registered(self):
		frappe.db.set_value(push.DEVICE_DOCTYPE, {"token": TOKEN_A}, "enabled", 0)
		self.assertRaisesRegex(frappe.ValidationError, "has no phone registered", request_location, self.employee_a)
		frappe.db.delete(push.DEVICE_DOCTYPE, {"user": self.user_a})
		self.assertRaisesRegex(frappe.ValidationError, "has no phone registered", request_location, self.employee_a)
		self.assertNotAsked()

	def test_no_user(self):
		frappe.db.set_value("Employee", self.employee_a, "user_id", None)
		self.assertRaisesRegex(frappe.ValidationError, "has no ERP user", request_location, self.employee_a)
		self.assertNotAsked()

	def test_an_inactive_employee(self):
		for status in ("Left", "Inactive", "Suspended"):
			frappe.db.set_value("Employee", self.employee_a, "status", status)
			self.assertRaisesRegex(frappe.ValidationError, "is not an active employee", request_location, self.employee_a)
		self.assertNotAsked()

	def test_an_unknown_employee(self):
		for employee in ("HR-EMP-TOK-NOBODY", "", None):
			self.assertRaisesRegex(frappe.ValidationError, "No such employee", request_location, employee)
		self.assertNotAsked()

	def test_push_switched_off(self):
		frappe.set_user("Administrator")
		helpers.set_settings(push_enabled=0)
		frappe.set_user(self.locator)
		self.assertRaisesRegex(frappe.ValidationError, "switched off", request_location, self.employee_a)
		self.assertNotAsked()

	def test_each_refusal_is_a_validation_error_not_a_permission_error(self):
		frappe.db.set_value(push.DEVICE_DOCTYPE, {"token": TOKEN_A}, "enabled", 0)
		for employee in (self.employee_a, self.employee_b, "HR-EMP-TOK-NOBODY"):
			try:
				request_location(employee)
			except frappe.PermissionError:
				self.fail("a permission error")
			except frappe.ValidationError:
				pass
			else:
				self.fail("not refused")

	def test_one_request_per_employee_a_minute(self):
		first = request_location(self.employee_a)["request"]
		self.assertEqual(len(self.jobs()), 1)
		self.assertRaisesRegex(frappe.ValidationError, "asked a moment ago", request_location, self.employee_a)
		# Whoever asks.
		frappe.set_user(self.manager)
		self.assertRaisesRegex(frappe.ValidationError, "asked a moment ago", request_location, self.employee_a)
		self.assertEqual([r.name for r in requests()], [first])
		self.assertEqual(self.jobs(), [])
		# Another employee is not held back by it.
		self.clock(self.employee_b, "IN")
		self.assertEqual(request_location(self.employee_b)["employee"], self.employee_b)
		self.assertEqual(len(self.jobs()), 1)
		# A minute later the first can be asked about again.
		frappe.db.set_value(REQUEST_DOCTYPE, first, "requested_at", now_datetime() - timedelta(seconds=61), update_modified=False)
		second = request_location(self.employee_a)["request"]
		self.assertNotEqual(second, first)
		self.assertEqual([(r.name, r.requested_by) for r in requests(employee=self.employee_a)], [(first, self.locator), (second, self.manager)])
		self.assertEqual(len(self.jobs()), 1)

	def test_post_only_and_not_for_guests(self):
		self.assertIn(request_location, frappe.whitelisted)
		self.assertNotIn(request_location, frappe.guest_methods)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[request_location], ["POST"])


class TestMayLocate(LocateCase):
	def test_by_role(self):
		expected = {self.locator: True, self.manager: True, "Administrator": True, self.hr: False, self.user_a: False}
		for user, allowed in expected.items():
			frappe.set_user(user)
			self.assertIs(may_locate(), allowed, user)
		frappe.set_user("Administrator")
		helpers.set_settings(locate_role="HR User")
		for user, allowed in {self.locator: False, self.hr: True, self.manager: True}.items():
			frappe.set_user(user)
			self.assertIs(may_locate(), allowed, user)
		frappe.set_user("Administrator")
		helpers.set_settings(locate_role=None)
		for user, allowed in {self.locator: False, self.hr: False, self.manager: True}.items():
			frappe.set_user(user)
			self.assertIs(may_locate(), allowed, user)

	def test_guest(self):
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, may_locate)

	def test_a_get_and_not_for_guests(self):
		self.assertIn(may_locate, frappe.whitelisted)
		self.assertNotIn(may_locate, frappe.guest_methods)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[may_locate], ["GET"])


class TestLocateRoleSetting(LocateCase):
	def test_only_a_system_manager_changes_the_role(self):
		"""Attendance Admins and HR Managers save this page; naming a role of their own here
		would let them ask where anyone is."""
		frappe.set_user(self.locator)
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.locate_role = "Employee"
		self.assertRaises(frappe.PermissionError, settings.save)
		frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, "locate_role"), "Attendance Admin")
		# The rest of the page is still theirs.
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.photo_retention_days = 31
		settings.save()
		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, "locate_role"), "Attendance Admin")
		frappe.set_user(self.manager)
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.locate_role = "HR User"
		settings.save()
		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, "locate_role"), "HR User")

	def test_the_field(self):
		field = frappe.get_meta(SETTINGS_DOCTYPE).get_field("locate_role")
		self.assertEqual((field.fieldtype, field.options, field.label, field.default), ("Link", "Role", "Role that may locate people", "Attendance Admin"))
		self.assertTrue(frappe.db.exists("Role", "Attendance Admin"))


class TestLocationRequestRows(LocateCase):
	def test_only_made_by_asking(self):
		frappe.set_user("Administrator")
		row = frappe.get_doc(
			{"doctype": REQUEST_DOCTYPE, "employee": self.employee_a, "requested_by": "Administrator", "requested_at": now_datetime()}
		)
		self.assertRaises(frappe.PermissionError, row.insert)
		self.assertEqual(requests(), [])

	def test_never_changed_or_deleted(self):
		name = request_location(self.employee_a)["request"]
		frappe.set_user("Administrator")
		row = frappe.get_doc(REQUEST_DOCTYPE, name)
		row.requested_by = self.hr
		self.assertRaises(frappe.PermissionError, row.save)
		self.assertRaises(frappe.PermissionError, frappe.delete_doc, REQUEST_DOCTYPE, name)
		self.assertEqual([(r.name, r.requested_by) for r in requests()], [(name, self.locator)])

	def test_who_reads_the_rows(self):
		meta = frappe.get_meta(REQUEST_DOCTYPE)
		self.assertEqual(sorted(p.role for p in meta.permissions), ["HR Manager", "System Manager"])
		for permission in meta.permissions:
			self.assertEqual(
				(permission.read, permission.write, permission.create, permission.delete), (1, 0, 0, 0), permission.role
			)
		self.assertFalse(meta.track_changes)
		for field in meta.fields:
			if field.fieldtype not in frappe.model.no_value_fields:
				self.assertEqual(field.read_only, 1, field.fieldname)
		# The person who asks does not get to read the record by asking.
		frappe.set_user(self.user_a)
		self.assertFalse(frappe.has_permission(REQUEST_DOCTYPE, "read"))


TRACCAR = "https://traccar.tok-test.example"
# A site and a point well away from it (about 5 km north), in the South Atlantic: no project of
# the site the tests run on is near.
SITE = (-30.500000, 10.500000)
AWAY = (-30.455000, 10.500000)


class _Answer:
	def __init__(self, status, body):
		self.status_code, self._body = status, body

	def json(self):
		return self._body


class FakeWeb:
	"""Traccar with one device for `employee` and the given positions, and OpenStreetMap's
	geocoder. `calls` keeps what was asked, to show what was (and was not) asked."""

	def __init__(self, employee, positions=(), traccar_status=200, device=True, address=None):
		self.employee, self.positions, self.traccar_status = employee, list(positions), traccar_status
		self.device = device
		self.address = address if address is not None else {"road": "Mombasa Road", "suburb": "Syokimau", "county": "Machakos"}
		self.calls = []

	def get(self, url, params=None, **kwargs):
		self.calls.append((url, dict(params or {}), kwargs))
		if url.startswith(TRACCAR):
			if self.traccar_status != 200:
				return _Answer(self.traccar_status, {})
			if url.endswith("/api/devices"):
				return _Answer(200, [{"id": 7, "uniqueId": self.employee}] if self.device else [])
			return _Answer(200, self.positions)
		return _Answer(200, {"address": self.address})

	def asked(self, part):
		return [c for c in self.calls if part in c[0]]


def position(lat_lon, seconds_ago=5, accuracy=12.0, fix_seconds_ago=None):
	now = datetime.now(timezone.utc)
	at = (now - timedelta(seconds=seconds_ago)).isoformat()
	fixed = (now - timedelta(seconds=seconds_ago if fix_seconds_ago is None else fix_seconds_ago)).isoformat()
	return {"latitude": lat_lon[0], "longitude": lat_lon[1], "fixTime": fixed, "serverTime": at, "accuracy": accuracy}


class TestLocationAnswer(LocateCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		helpers.set_settings(traccar_url=TRACCAR + "/", traccar_username="erp-locate", geofence_tolerance_meters=0)
		frappe.get_doc(
			{
				"doctype": "Project",
				"project_name": "Tok Test Kingswood",
				"status": "Open",
				"company": frappe.db.get_value("Employee", self.employee_a, "company"),
				"custom_site_latitude": SITE[0],
				"custom_site_longitude": SITE[1],
				"custom_geofence_radius_meters": 200,
			}
		).insert(ignore_permissions=True)
		frappe.set_user(self.locator)
		self.request = request_location(self.employee_a)["request"]
		self.jobs()

	def answer(self, web, request=None):
		with patch.object(locate.requests, "get", web.get), patch.object(locate, "_wait_for_nominatim", lambda: None):
			return location_answer(request or self.request)

	def test_at_a_site_it_names_the_site(self):
		web = FakeWeb(self.employee_a, [position(SITE)])
		answer = self.answer(web)
		self.assertEqual((answer["status"], answer["site_name"], answer["place"]), ("found", "Tok Test Kingswood", None))
		self.assertEqual((answer["minutes_ago"], answer["accuracy_m"]), (0, 12))
		self.assertEqual(web.asked("openstreetmap"), [], "a site needs no place name")
		# Traccar is asked for this employee's device, with the login in the settings.
		self.assertEqual(web.asked("/api/devices")[0][1], {"uniqueId": self.employee_a})
		self.assertEqual(web.asked("/api/devices")[0][2]["auth"][0], "erp-locate")

	def test_at_no_site_it_names_the_place_and_never_the_coordinates(self):
		web = FakeWeb(self.employee_a, [position(AWAY)])
		answer = self.answer(web)
		self.assertEqual(answer["status"], "found")
		self.assertIsNone(answer["site"])
		self.assertEqual(answer["place"], "Mombasa Road, Syokimau, Machakos")
		self.assertEqual(answer["attribution"], "© OpenStreetMap contributors")
		for value in answer.values():
			self.assertNotIn("10.5", str(value))
		self.assertNotIn("latitude", answer)
		osm = web.asked("openstreetmap")
		self.assertEqual(len(osm), 1)
		self.assertIn("trident_attendance", osm[0][2]["headers"]["User-Agent"], "OpenStreetMap asks apps to name themselves")

	def test_the_nearest_of_two_sites_that_hold_the_point(self):
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "Project",
				"project_name": "Tok Test Next Door",
				"status": "Open",
				"company": frappe.db.get_value("Employee", self.employee_a, "company"),
				"custom_site_latitude": SITE[0] + 0.0015,
				"custom_site_longitude": SITE[1],
				"custom_geofence_radius_meters": 300,
			}
		).insert(ignore_permissions=True)
		frappe.set_user(self.locator)
		self.assertEqual(self.answer(FakeWeb(self.employee_a, [position(SITE)]))["site_name"], "Tok Test Kingswood")

	def test_a_site_without_a_fence_or_closed_is_not_here(self):
		frappe.set_user("Administrator")
		name = frappe.db.get_value("Project", {"project_name": "Tok Test Kingswood"})
		frappe.db.set_value("Project", name, "custom_geofence_radius_meters", 0)
		frappe.set_user(self.locator)
		self.assertIsNone(self.answer(FakeWeb(self.employee_a, [position(SITE)]))["site"])
		frappe.set_user("Administrator")
		frappe.db.set_value("Project", name, {"custom_geofence_radius_meters": 200, "status": "Completed"})
		frappe.set_user(self.locator)
		self.assertIsNone(self.answer(FakeWeb(self.employee_a, [position(SITE)]))["site"])

	def test_waiting_until_the_phone_answers(self):
		# Only a position from before the request: the phone has not answered yet.
		web = FakeWeb(self.employee_a, [position(SITE, seconds_ago=600)])
		self.assertEqual(self.answer(web), {"status": "waiting"})
		web.positions.append(position(AWAY, seconds_ago=1))
		self.assertEqual(self.answer(web)["place"], "Mombasa Road, Syokimau, Machakos")

	def test_an_answer_with_a_fix_from_before_the_request(self):
		# Android answered "where are you now" with a fix it already had: taken before the
		# request, received by Traccar after it. Traccar's `from` is on the fix time.
		web = FakeWeb(self.employee_a, [position(AWAY, seconds_ago=1, fix_seconds_ago=50)])
		answer = self.answer(web)
		self.assertEqual((answer["status"], answer["minutes_ago"]), ("found", 0))
		asked_from = datetime.fromisoformat(web.asked("/api/positions")[0][1]["from"].replace("Z", "+00:00"))
		self.assertLess(asked_from, datetime.now(timezone.utc) - timedelta(minutes=14))

	def test_the_newest_answer_wins(self):
		web = FakeWeb(self.employee_a, [position(SITE, seconds_ago=1), position(AWAY, seconds_ago=3)])
		self.assertEqual(self.answer(web)["site_name"], "Tok Test Kingswood")

	def test_no_place_name_to_be_had(self):
		answer = self.answer(FakeWeb(self.employee_a, [position(AWAY)], address={}))
		self.assertEqual((answer["status"], answer["place"], answer["attribution"]), ("found", None, None))

	def test_traccar_not_set_up_no_device_and_refusing(self):
		self.assertEqual(self.answer(FakeWeb(self.employee_a, device=False)), {"status": "no_device"})
		answer = self.answer(FakeWeb(self.employee_a, traccar_status=401))
		self.assertEqual(answer["status"], "traccar_error")
		self.assertIn("refused the login", answer["message"])
		frappe.set_user("Administrator")
		helpers.set_settings(traccar_url="")
		frappe.set_user(self.locator)
		web = FakeWeb(self.employee_a, [position(SITE)])
		self.assertEqual(self.answer(web), {"status": "not_set_up"})
		self.assertEqual(web.calls, [])

	def test_only_the_one_who_asked_and_only_for_three_minutes(self):
		web = FakeWeb(self.employee_a, [position(SITE)])
		frappe.set_user(self.manager)
		self.assertRaises(frappe.PermissionError, self.answer, web)
		frappe.set_user(self.hr)
		self.assertRaises(frappe.PermissionError, self.answer, web)
		frappe.set_user(self.locator)
		self.assertRaises(frappe.PermissionError, self.answer, web, "LOC-NOBODY")
		frappe.db.set_value(REQUEST_DOCTYPE, self.request, "requested_at", now_datetime() - timedelta(minutes=4))
		self.assertRaisesRegex(frappe.ValidationError, "is over", self.answer, web)
		self.assertEqual(web.calls, [])

	def test_nothing_of_the_position_is_kept(self):
		before = frappe.get_all(REQUEST_DOCTYPE, fields=["*"])
		logged = set(frappe.get_all("Error Log", pluck="name"))     # whatever earlier tests left there
		self.answer(FakeWeb(self.employee_a, [position(AWAY)]))
		self.assertEqual(frappe.get_all(REQUEST_DOCTYPE, fields=["*"]), before)
		self.assertEqual(set(frappe.get_all("Error Log", pluck="name")) - logged, set())
