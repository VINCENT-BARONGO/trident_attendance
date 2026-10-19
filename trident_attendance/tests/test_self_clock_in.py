import frappe
from frappe.tests.utils import FrappeTestCase

from trident_attendance.employee_access import APP_ACCESS_FIELD
from trident_attendance.self_clock_in import FIELD, get_my_photo, permission
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
		helpers.set_settings(self_clock_in=None)
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


class TestGetMyPhoto(FrappeTestCase):
	"""The app's face check before a punch it takes itself: the person's own photo, only while
	they may clock in on their phone."""

	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		self.addCleanup(self.forget_response)
		frappe.set_user("Administrator")
		emp, self.user = helpers.make_self_service_employee("Tokphoto")
		self.employee = emp.name
		helpers.set_settings(self_clock_in="Ticked employees")
		frappe.db.set_value("Employee", self.employee, {FIELD: "Allowed", APP_ACCESS_FIELD: 1})

	@staticmethod
	def forget_response():
		# A served photo stays on frappe.local.response; the next test must not find it there.
		for key in ("type", "filecontent", "filename"):
			frappe.local.response.pop(key, None)

	def with_photo(self, employee=None):
		frappe.set_user("Administrator")
		file = helpers.attach_photo(employee or self.employee)
		self.addCleanup(frappe.delete_doc, "File", file.name, force=True, ignore_permissions=True)

	def test_own_photo_bytes(self):
		self.with_photo()
		frappe.set_user(self.user)
		get_my_photo()
		self.assertEqual(frappe.local.response.filecontent, helpers.JPEG)
		self.assertEqual(frappe.local.response.type, "download")
		# The read runs as Administrator; the caller must be put back.
		self.assertEqual(frappe.session.user, self.user)

	def test_no_photo_is_not_found(self):
		frappe.set_user(self.user)
		with self.assertRaises(frappe.DoesNotExistError):
			get_my_photo()

	def test_refused_when_not_allowed_to_clock_in(self):
		self.with_photo()
		frappe.db.set_value("Employee", self.employee, FIELD, "Not allowed")
		frappe.set_user(self.user)
		with self.assertRaises(frappe.PermissionError):
			get_my_photo()

	def test_refused_without_app_access(self):
		self.with_photo()
		frappe.db.set_value("Employee", self.employee, APP_ACCESS_FIELD, 0)
		frappe.set_user(self.user)
		with self.assertRaises(frappe.PermissionError):
			get_my_photo()

	def test_takes_no_employee_from_the_caller(self):
		# Someone else's photo is never the answer: there is no argument to ask for it with.
		other, _user = helpers.make_self_service_employee("Tokphotoother")
		self.with_photo(other.name)
		frappe.set_user(self.user)
		with self.assertRaises(TypeError):
			get_my_photo(employee=other.name)
		with self.assertRaises(frappe.DoesNotExistError):
			get_my_photo()

	def test_guest_is_refused(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			get_my_photo()
