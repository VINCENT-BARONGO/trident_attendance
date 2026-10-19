import frappe
from frappe.tests.utils import FrappeTestCase

from trident_attendance.own_records import apply_own_leave, get_own_photo, get_own_records, withdraw_own_leave
from trident_attendance.tests import helpers

PROJECT_FIELD = "custom_project"


def make_project(name):
	doc = frappe.get_doc({"doctype": "Project", "project_name": name, "company": frappe.db.get_value("Company", {}, "name")})
	return doc.insert(ignore_permissions=True).name


class OwnRecordsCase(FrappeTestCase):
	"""A supervisor as the site sets one up: allowed one project, on every doctype, under
	strict user permissions; their own Employee record names no project."""

	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		strict = frappe.db.get_single_value("System Settings", "apply_strict_user_permissions")
		self.addCleanup(frappe.db.set_single_value, "System Settings", "apply_strict_user_permissions", strict)
		frappe.db.set_single_value("System Settings", "apply_strict_user_permissions", 1)
		self.employee, self.user = helpers.make_self_service_employee("Tokown")
		self.employee = self.employee.name
		self.other, self.other_user = helpers.make_self_service_employee("Tokother")
		self.other = self.other.name
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": self.user,
				"allow": "Project",
				"for_value": make_project("Tok Test Site"),
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)

	def as_employee(self):
		frappe.set_user(self.user)

	def names(self, doctype="Employee", **kwargs):
		return [row["name"] for row in get_own_records(doctype, **kwargs)]


class TestOwnRecords(OwnRecordsCase):
	def test_own_employee_is_read_past_the_project_permission(self):
		if not frappe.get_meta("Employee").has_field(PROJECT_FIELD):
			self.skipTest("this site's Employee has no project field")
		self.as_employee()
		self.assertEqual(frappe.get_list("Employee", filters={"user_id": self.user}, pluck="name"), [])
		rows = get_own_records("Employee", fields=["name", "employee_name", "user_id", "status"])
		self.assertEqual([row["name"] for row in rows], [self.employee])
		self.assertEqual(rows[0]["user_id"], self.user)

	def test_never_another_persons_record(self):
		self.as_employee()
		self.assertEqual(self.names(), [self.employee])
		self.assertEqual(self.names(filters=[["name", "=", self.other]]), [])
		self.assertEqual(self.names(filters=[["user_id", "=", self.other_user]]), [])
		self.assertEqual(self.names(filters={"name": self.other}), [])

	def test_own_checkins_only(self):
		for employee in (self.employee, self.other):
			frappe.get_doc(
				{"doctype": "Employee Checkin", "employee": employee, "time": "2026-01-05 08:00:00", "log_type": "IN"}
			).insert(ignore_permissions=True)
		self.as_employee()
		rows = get_own_records(
			"Employee Checkin",
			fields=["name", "employee", "time", "log_type"],
			filters=[["time", "between", ["2026-01-05 00:00:00", "2026-01-05 23:59:59"]]],
			order_by="time asc",
		)
		self.assertEqual([row["employee"] for row in rows], [self.employee])

	def test_fields_are_what_every_reader_may_see(self):
		self.as_employee()
		row = get_own_records("Employee", fields=["name", "no_such_field", "status", "employee_name; drop table"])[0]
		self.assertEqual(set(row), {"name", "status"})
		meta = frappe.get_meta("Employee")
		permitted = set(meta.get_permitted_fieldnames(user=self.user, permission_type="read"))
		kept_back = [f.fieldname for f in meta.fields if f.fieldtype == "Data" and f.fieldname not in permitted]
		if kept_back:
			self.assertNotIn(kept_back[0], get_own_records("Employee", fields=["name", kept_back[0]])[0])

	def test_what_is_refused(self):
		self.as_employee()
		self.assertRaises(frappe.PermissionError, get_own_records, "Salary Slip")
		self.assertRaises(frappe.PermissionError, get_own_records, "User")
		self.assertRaises(frappe.ValidationError, get_own_records, "Employee", filters=[["name", "=", "x", "y"]])
		self.assertRaises(frappe.ValidationError, get_own_records, "Employee", filters=[["no_such_field", "=", 1]])
		self.assertRaises(frappe.ValidationError, get_own_records, "Employee", filters=[["name", "regexp", "."]])
		self.assertRaises(frappe.ValidationError, get_own_records, "Employee", order_by="name; drop table x")
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, get_own_records, "Employee")

	def test_a_user_with_no_employee_has_no_records(self):
		user = helpers.make_user(f"tokloose@{helpers.TEST_DOMAIN}", roles=["Employee"]).name
		frappe.set_user(user)
		self.assertEqual(get_own_records("Employee"), [])
		self.assertEqual(get_own_records("Employee Checkin"), [])


class TestOwnPhoto(OwnRecordsCase):
	"""My HR's avatar: the person's own photo, past the permissions that hide the file."""

	def setUp(self):
		super().setUp()
		self.addCleanup(self.forget_response)

	@staticmethod
	def forget_response():
		for key in ("type", "filecontent", "filename"):
			frappe.local.response.pop(key, None)

	def with_photo(self, employee):
		frappe.set_user("Administrator")
		file = helpers.attach_photo(employee)
		self.addCleanup(frappe.delete_doc, "File", file.name, force=True, ignore_permissions=True)

	def test_own_photo_bytes(self):
		self.with_photo(self.employee)
		self.as_employee()
		get_own_photo()
		self.assertEqual(frappe.local.response.filecontent, helpers.JPEG)
		self.assertEqual(frappe.local.response.type, "download")
		# The read runs as Administrator; the caller must be put back.
		self.assertEqual(frappe.session.user, self.user)

	def test_never_another_persons_photo(self):
		self.with_photo(self.other)
		self.as_employee()
		with self.assertRaises(frappe.DoesNotExistError):
			get_own_photo()

	def test_no_employee_or_guest(self):
		user = helpers.make_user(f"tokloose@{helpers.TEST_DOMAIN}", roles=["Employee"]).name
		frappe.set_user(user)
		self.assertRaises(frappe.DoesNotExistError, get_own_photo)
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, get_own_photo)


class TestOwnLeave(OwnRecordsCase):
	def setUp(self):
		super().setUp()
		self.leave_type = "Tok Test Unpaid"
		if not frappe.db.exists("Leave Type", self.leave_type):
			frappe.get_doc({"doctype": "Leave Type", "leave_type_name": self.leave_type, "is_lwp": 1}).insert(
				ignore_permissions=True
			)
		# HRMS counts the days of a leave against the employee's holiday list.
		holidays = frappe.get_doc(
			{"doctype": "Holiday List", "holiday_list_name": "Tok Test Holidays", "from_date": "2026-01-01", "to_date": "2026-12-31"}
		).insert(ignore_permissions=True)
		for employee in (self.employee, self.other):
			helpers.give_holiday_list(employee, holidays.name)

	def apply(self, day="2026-11-03", **kwargs):
		return apply_own_leave(self.leave_type, day, day, **kwargs)

	def test_apply_is_for_the_caller_and_open(self):
		self.as_employee()
		out = self.apply(description="Tok test")
		self.assertEqual(out["employee"], self.employee)
		self.assertEqual((out["status"], out["docstatus"]), ("Open", 0))
		self.assertEqual(self.names("Leave Application"), [out["name"]])
		# HRMS still decides: the same day twice is refused.
		self.assertRaises(frappe.ValidationError, self.apply)

	def test_withdraw_own_open_application_only(self):
		self.as_employee()
		mine = self.apply()["name"]
		frappe.set_user(self.other_user)
		theirs = self.apply()["name"]
		self.assertRaises(frappe.DoesNotExistError, withdraw_own_leave, mine)
		self.as_employee()
		self.assertRaises(frappe.DoesNotExistError, withdraw_own_leave, theirs)
		self.assertRaises(frappe.DoesNotExistError, withdraw_own_leave, "no-such-application")
		self.assertEqual(withdraw_own_leave(mine)["status"], "Cancelled")
		self.assertRaises(frappe.ValidationError, withdraw_own_leave, mine)
		self.assertEqual(frappe.db.get_value("Leave Application", theirs, "status"), "Open")

	def test_who_may_not(self):
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, self.apply)
		self.assertRaises(frappe.PermissionError, withdraw_own_leave, "x")
		user = helpers.make_user(f"tokloose@{helpers.TEST_DOMAIN}").name
		frappe.set_user(user)
		self.assertRaises(frappe.PermissionError, self.apply)
