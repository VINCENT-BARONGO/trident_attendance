import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, nowdate
from hrms.hr.doctype.attendance.attendance import mark_attendance

from trident_attendance import maintenance
from trident_attendance.tests import helpers
from trident_attendance.utils import STATUS_MARKED, STATUS_PENDING, STATUS_REJECTED

SOURCE = "TPL-FieldApp-test/tok"


def checkin(employee, day, reasons=None):
	"""An IN at 08:00 on `day`, waiting in review with these hold reasons."""
	doc = frappe.get_doc(
		{
			"doctype": "Employee Checkin",
			"employee": employee,
			"log_type": "IN",
			"time": f"{day} 08:00:00",
			"custom_app_source": SOURCE,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.set_value(
		"Employee Checkin",
		doc.name,
		{"custom_review_status": STATUS_PENDING, "custom_hold_reasons": reasons, "attendance": None},
	)
	return doc.name


class CloseStaleReviewsCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		helpers.set_settings(staged_sources="All sources")
		self.employee = helpers.make_employee("Tokclean").name
		today = getdate(nowdate())
		self.before = add_days(today, -5)
		marked_day, absent_day = add_days(today, -10), add_days(today, -12)
		self.att = mark_attendance(self.employee, marked_day, "Present")
		absent = mark_attendance(self.employee, absent_day, "Absent")
		# The day HR marked by hand: held as already marked, for good.
		self.on_marked = checkin(
			self.employee, marked_day, f"Attendance already marked: {self.att} (Present)"
		)
		# Imported before the app: no reason, older than the hourly run looks.
		self.imported = checkin(self.employee, add_days(today, -60))
		# Left for a reviewer: punches on an Absent day, and a day with a missing OUT.
		self.on_absent = checkin(self.employee, absent_day, f"Attendance already marked: {absent} (Absent)")
		self.missing_out = checkin(self.employee, add_days(today, -14), "Missing OUT")
		# Newer than `before`: not looked at.
		self.recent = checkin(self.employee, add_days(today, -2), "Missing OUT")

	def run_it(self, **kwargs):
		return maintenance.close_stale_reviews(before=self.before, employee=self.employee, **kwargs)

	def state(self, name):
		return tuple(frappe.db.get_value("Employee Checkin", name, ["custom_review_status", "attendance"]))

	def reasons(self, name):
		return frappe.db.get_value("Employee Checkin", name, "custom_hold_reasons")

	def test_a_dry_run_counts_and_writes_nothing(self):
		res = self.run_it()
		self.assertEqual((res["checked"], res["linked"], res["rejected"], res["left"]), (4, 1, 1, 2))
		self.assertEqual(res["left_by_reason"], {"Attendance already marked: Absent": 1, "Missing OUT": 1})
		for name in (self.on_marked, self.imported, self.on_absent, self.missing_out, self.recent):
			self.assertEqual(self.state(name), (STATUS_PENDING, None))

	def test_apply_links_settled_days_rejects_old_imports_and_leaves_the_rest(self):
		self.run_it(apply=1)
		self.assertEqual(self.state(self.on_marked), (STATUS_MARKED, self.att))
		self.assertIn(
			f"{maintenance.TAG}: the day already has {self.att} (Present)", self.reasons(self.on_marked)
		)
		self.assertEqual(self.state(self.imported), (STATUS_REJECTED, None))
		self.assertEqual(self.reasons(self.imported), maintenance.IMPORTED_REASON)
		for name in (self.on_absent, self.missing_out, self.recent):
			self.assertEqual(self.state(name), (STATUS_PENDING, None))

	def test_cancelling_the_attendance_puts_a_linked_punch_back_in_review(self):
		self.run_it(apply=1)
		frappe.get_doc("Attendance", self.att).cancel()
		status, attendance = self.state(self.on_marked)
		self.assertEqual(status, STATUS_PENDING)
		self.assertFalse(attendance)  # hrms clears the link to ""

	def test_reopen_undoes_it(self):
		self.run_it(apply=1)
		self.assertEqual(maintenance.reopen_closed_reviews()["reopened"], 2)  # a dry run
		self.assertEqual(self.state(self.on_marked), (STATUS_MARKED, self.att))
		maintenance.reopen_closed_reviews(apply=1)
		self.assertEqual(self.state(self.on_marked), (STATUS_PENDING, None))
		self.assertEqual(self.reasons(self.on_marked), f"Attendance already marked: {self.att} (Present)")
		self.assertEqual(self.state(self.imported), (STATUS_PENDING, None))
		self.assertIsNone(self.reasons(self.imported))

	def test_only_a_system_manager_may_run_it(self):
		user = helpers.make_user(f"tokclean-admin@{helpers.TEST_DOMAIN}", roles=("Attendance Admin",))
		frappe.set_user(user.name)
		with self.assertRaises(frappe.PermissionError):
			self.run_it()
		with self.assertRaises(frappe.PermissionError):
			maintenance.reopen_closed_reviews()
