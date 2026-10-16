import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_datetime, getdate, nowdate

from trident_attendance import tasks, work_day
from trident_attendance.employee_access import HubOnlyError
from trident_attendance.own_shift import COMMENT, OwnShiftError, get_own_shift, set_own_shift, shifts
from trident_attendance.tests import helpers

NIGHT = "Tok Night"
DAY = "Tok Day"
SOURCE = "TPL-FieldApp-test/tok"


def make_shift_type(name, start, end, after_min=60):
	if frappe.db.exists("Shift Type", name):
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Shift Type",
			"start_time": start,
			"end_time": end,
			"begin_check_in_before_shift_start_time": 60,
			"allow_check_out_after_shift_end_time": after_min,
		}
	)
	return doc.insert(ignore_permissions=True, set_name=name).name


def assign(employee, shift_type, start, end=None):
	doc = frappe.get_doc(
		{
			"doctype": "Shift Assignment",
			"employee": employee,
			"company": frappe.db.get_value("Employee", employee, "company"),
			"shift_type": shift_type,
			"start_date": start,
			"end_date": end,
			"status": "Active",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def punch(employee, log_type, when):
	doc = frappe.get_doc(
		{
			"doctype": "Employee Checkin",
			"employee": employee,
			"log_type": log_type,
			"time": when,
			"custom_app_source": SOURCE,
		}
	)
	doc.flags.ignore_permissions = True
	return doc.insert().name


def today():
	return getdate(nowdate())


class ShiftCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		work_day.clear_cache()
		self.addCleanup(work_day.clear_cache)
		make_shift_type(NIGHT, "19:00:00", "07:00:00")
		make_shift_type(DAY, "08:00:00", "17:00:00")
		helpers.set_settings(own_shift_enabled=1, own_shift_types="")
		emp, self.user = helpers.make_self_service_employee("Tokshift")
		self.employee = emp.name

	def own(self):
		return [
			(s["shift_type"], s["start_date"], s["end_date"], s["mine"])
			for s in get_own_shift()["shifts"]
		]


class TestSetOwnShift(ShiftCase):
	def test_nights_ahead_are_a_submitted_assignment_with_a_note(self):
		frappe.set_user(self.user)
		start, end = add_days(today(), 1), add_days(today(), 5)
		answer = set_own_shift(str(start), str(end), NIGHT)
		self.assertEqual(self.own(), [(NIGHT, str(start), str(end), True)])
		doc = frappe.get_doc("Shift Assignment", answer["assignment"])
		self.assertEqual((doc.docstatus, doc.employee, doc.owner), (1, self.employee, self.user))
		self.assertTrue(frappe.db.exists("Comment", {"reference_name": doc.name, "content": ["like", f"{COMMENT}%"]}))
		self.assertIn(NIGHT, [c["name"] for c in answer["choices"]])
		self.assertTrue(next(c for c in answer["choices"] if c["name"] == NIGHT)["overnight"])

	def test_a_change_inside_own_nights_keeps_the_days_either_side(self):
		frappe.set_user(self.user)
		set_own_shift(str(add_days(today(), 1)), str(add_days(today(), 10)), NIGHT)
		set_own_shift(str(add_days(today(), 4)), str(add_days(today(), 6)), DAY)
		d = lambda n: str(add_days(today(), n))  # noqa: E731
		self.assertEqual(
			sorted(self.own(), key=lambda r: r[1]),
			[(NIGHT, d(1), d(3), True), (DAY, d(4), d(6), True), (NIGHT, d(7), d(10), True)],
		)

	def test_no_shift_type_goes_back_to_the_usual_shift(self):
		frappe.set_user(self.user)
		set_own_shift(str(add_days(today(), 1)), str(add_days(today(), 3)), NIGHT)
		set_own_shift(str(add_days(today(), 1)), str(add_days(today(), 3)), None)
		self.assertEqual(self.own(), [])

	def refused(self, *args, message=None):
		with self.assertRaises(OwnShiftError) as caught:
			set_own_shift(*args)
		if message:
			self.assertIn(message, str(caught.exception))

	def test_the_rules(self):
		frappe.set_user(self.user)
		t = today()
		self.refused(str(add_days(t, -1)), str(add_days(t, 2)), NIGHT, message="from today on")
		self.refused(str(add_days(t, 3)), str(add_days(t, 1)), NIGHT, message="before the first")
		self.refused(str(add_days(t, 1)), str(add_days(t, 32)), NIGHT, message="at most 31 days")
		self.refused(str(add_days(t, 60)), str(add_days(t, 63)), NIGHT, message="days ahead")
		self.refused(str(add_days(t, 1)), str(add_days(t, 2)), "No Such Shift", message="not a shift you can choose")

	def test_only_the_listed_shifts(self):
		helpers.set_settings(own_shift_types=NIGHT)
		frappe.set_user(self.user)
		self.assertEqual([c["name"] for c in get_own_shift()["choices"]], [NIGHT])
		self.refused(str(add_days(today(), 1)), str(add_days(today(), 2)), DAY, message="not a shift you can choose")

	def test_turned_off(self):
		helpers.set_settings(own_shift_enabled=0)
		frappe.set_user(self.user)
		self.assertFalse(get_own_shift()["enabled"])
		self.refused(str(add_days(today(), 1)), str(add_days(today(), 2)), NIGHT, message="turned off")

	def test_hr_shifts_are_never_overwritten(self):
		assign(self.employee, DAY, add_days(today(), 3), add_days(today(), 4))
		frappe.set_user(self.user)
		self.refused(str(add_days(today(), 1)), str(add_days(today(), 5)), NIGHT, message="HR has set")
		self.assertEqual(self.own(), [(DAY, str(add_days(today(), 3)), str(add_days(today(), 4)), False)])

	def test_today_is_locked_once_it_has_a_punch(self):
		punch(self.employee, "IN", get_datetime(f"{today()} 00:30:00"))
		frappe.set_user(self.user)
		self.assertTrue(get_own_shift()["today_locked"])
		self.refused(str(today()), str(add_days(today(), 2)), NIGHT, message="already clocked today")
		set_own_shift(str(add_days(today(), 1)), str(add_days(today(), 2)), NIGHT)

	def test_last_nights_clock_out_this_morning_does_not_lock_today(self):
		assign(self.employee, NIGHT, add_days(today(), -1), add_days(today(), -1))
		punch(self.employee, "OUT", get_datetime(f"{today()} 00:30:00"))
		frappe.set_user(self.user)
		self.assertFalse(get_own_shift()["today_locked"])
		set_own_shift(str(today()), str(add_days(today(), 2)), NIGHT)

	def test_a_stranger_is_refused(self):
		user = helpers.make_user(f"tokstranger@{helpers.TEST_DOMAIN}").name
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			get_own_shift()


class TestShiftsForTheHub(ShiftCase):
	def test_an_employee_may_not_read_everyone_s_shifts(self):
		frappe.set_user(self.user)
		with self.assertRaises((HubOnlyError, frappe.PermissionError)):
			shifts(str(today()), str(add_days(today(), 7)))

	def test_types_and_assignments(self):
		assign(self.employee, NIGHT, add_days(today(), 1), add_days(today(), 3))
		answer = shifts(str(today()), str(add_days(today(), 7)))
		night = next(t for t in answer["shift_types"] if t["name"] == NIGHT)
		self.assertEqual((night["start"], night["end"], night["overnight"], night["after_min"]), ("19:00:00", "7:00:00", True, 60))
		mine = [a for a in answer["assignments"] if a["employee"] == self.employee]
		self.assertEqual(mine, [{
			"employee": self.employee,
			"attendance_device_id": None,
			"shift_type": NIGHT,
			"start_date": str(add_days(today(), 1)),
			"end_date": str(add_days(today(), 3)),
		}])


	def test_default_shifts_are_sent(self):
		frappe.db.set_value("Employee", self.employee, "default_shift", DAY)
		answer = shifts(str(today()), str(add_days(today(), 7)))
		mine = [d for d in answer["defaults"] if d["employee"] == self.employee]
		self.assertEqual(mine, [{"employee": self.employee, "attendance_device_id": None, "shift_type": DAY}])

	def test_public_holidays_in_the_range_are_sent_weekly_offs_are_not(self):
		day = add_days(today(), 2)
		holidays = frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": "Tok Holidays",
				"from_date": add_days(today(), -30),
				"to_date": add_days(today(), 60),
				"holidays": [
					{"holiday_date": day, "description": "<b>Tok Day</b>"},
					{"holiday_date": add_days(today(), 3), "description": "Sunday", "weekly_off": 1},
					{"holiday_date": add_days(today(), 40), "description": "Later"},
				],
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("Employee", self.employee, "holiday_list", holidays.name)
		answer = shifts(str(today()), str(add_days(today(), 7)))
		self.assertIn({"date": str(day), "name": "Tok Day"}, answer["holidays"])
		self.assertNotIn(str(add_days(today(), 3)), [h["date"] for h in answer["holidays"]])
		self.assertNotIn(str(add_days(today(), 40)), [h["date"] for h in answer["holidays"]])


class TestNightWorkDay(ShiftCase):
	"""A night shift's 07:00 clock-out belongs to the night it ends."""

	def setUp(self):
		super().setUp()
		helpers.set_settings(
			staged_sources="All sources",
			auto_release_clean_punches=1,
			auto_checkout=0,
			require_supervisor_checkin_first=0,
			require_supervisor_checkout_first=0,
			require_supervisor_face_match=0,
			hold_on_face_mismatch=0,
			hold_on_face_unverified=0,
			hold_on_outside_geofence=0,
			hold_on_no_gps=0,
			hold_on_missing_out=1,
			enforce_project_user_list=0,
		)
		self.night = add_days(today(), -5)
		assign(self.employee, NIGHT, self.night, self.night)

	def test_where_a_punch_belongs(self):
		n = self.night
		self.assertEqual(work_day.work_date_of(self.employee, f"{n} 19:02:00"), n)
		self.assertEqual(work_day.work_date_of(self.employee, f"{add_days(n, 1)} 07:05:00"), n)
		self.assertEqual(work_day.work_date_of(self.employee, f"{add_days(n, 1)} 08:30:00"), add_days(n, 1))
		start, end = work_day.bounds(self.employee, n)
		self.assertEqual((start, end), (f"{n} 00:00:00", f"{add_days(n, 1)} 08:00:00"))
		self.assertEqual(work_day.bounds(self.employee, add_days(n, 1))[0], f"{add_days(n, 1)} 08:00:01")
		self.assertTrue(work_day.is_complete(self.employee, n))
		self.assertFalse(work_day.is_complete(self.employee, n, now=get_datetime(f"{add_days(n, 1)} 07:30:00")))

	def test_the_night_is_one_attendance_of_twelve_hours(self):
		n = self.night
		punch(self.employee, "IN", get_datetime(f"{n} 19:00:00"))
		punch(self.employee, "OUT", get_datetime(f"{add_days(n, 1)} 07:00:00"))
		# The job's own grouping, without its commit (finalise_days commits; tests roll back).
		summary = {"days": 0, "marked": 0, "held": 0, "errors": 0, "results": []}
		tasks._finalise_days(tasks.get_settings(), add_days(n, 1), summary, commit_each=False)
		mine = [(r["date"], r["status"]) for r in summary["results"] if r["employee"] == self.employee]
		self.assertEqual(mine, [(str(n), "marked")])
		rows = frappe.get_all(
			"Attendance",
			filters={"employee": self.employee, "docstatus": 1},
			fields=["attendance_date", "working_hours", "status"],
		)
		self.assertEqual([(getdate(r.attendance_date), round(r.working_hours, 1), r.status) for r in rows], [(n, 12.0, "Present")])

	def test_without_a_shift_the_same_punches_are_two_broken_days(self):
		frappe.db.delete("Shift Assignment", {"employee": self.employee})
		work_day.clear_cache()
		n = self.night
		self.assertEqual(work_day.work_date_of(self.employee, f"{add_days(n, 1)} 07:00:00"), add_days(n, 1))

	def test_the_review_queue_groups_the_night_as_one_day(self):
		from trident_attendance.www import attendance_review

		n = self.night
		a = punch(self.employee, "IN", get_datetime(f"{n} 19:00:00"))
		b = punch(self.employee, "OUT", get_datetime(f"{add_days(n, 1)} 07:00:00"))
		rows = frappe.get_all("Employee Checkin", filters={"name": ["in", [a, b]]}, fields=["employee", "time"])
		self.assertEqual({attendance_review._work_date(r) for r in rows}, {n})

