import frappe
from frappe.tests.utils import FrappeTestCase

from trident_attendance.employee_access import APP_ACCESS_FIELD
from trident_attendance.self_clock_in import FIELD, permission
from trident_attendance.tests import helpers


class TestSelfClockInPermission(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		emp, self.user = helpers.make_self_service_employee("Tokself")
		self.employee = emp.name

	def ask(self, mode, choice="", app_access=1):
		frappe.set_user("Administrator")
		helpers.set_settings(self_clock_in=mode)
		frappe.db.set_value("Employee", self.employee, {FIELD: choice, APP_ACCESS_FIELD: app_access})
		frappe.set_user(self.user)
		return permission()

	def test_off_by_default_and_when_off(self):
		frappe.set_user(self.user)
		self.assertEqual(permission()["reason"], "off")
		self.assertEqual(self.ask("Off", "Allowed")["reason"], "off")

	def test_ticked_employees_only_those_allowed(self):
		self.assertEqual(self.ask("Ticked employees")["reason"], "not_allowed")
		answer = self.ask("Ticked employees", "Allowed")
		self.assertTrue(answer["allowed"])
		self.assertEqual((answer["employee"], answer["user"]), (self.employee, self.user))
		self.assertEqual(self.ask("Ticked employees", "Not allowed")["reason"], "not_allowed")

	def test_everyone_but_those_not_allowed(self):
		self.assertTrue(self.ask("Everyone")["allowed"])
		self.assertTrue(self.ask("Everyone", "Allowed")["allowed"])
		self.assertEqual(self.ask("Everyone", "Not allowed")["reason"], "not_allowed")

	def test_never_without_app_access(self):
		self.assertEqual(self.ask("Everyone", "Allowed", app_access=0)["reason"], "app_access_off")

	def test_a_user_with_no_employee_is_told_so(self):
		frappe.set_user("Administrator")
		other = helpers.make_user(f"tokself-nobody@{helpers.TEST_DOMAIN}").name
		helpers.set_settings(self_clock_in="Everyone")
		frappe.set_user(other)
		answer = permission()
		self.assertEqual((answer["allowed"], answer["reason"]), (False, "no_employee"))

	def test_guest_is_refused(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			permission()
