"""The 16 Oct 2026 security audit: an employee's sign-in only ever sees their own records, ends
with what allowed it, and cannot pick their own leave approver or reshape a day already worked."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime, nowdate

from trident_attendance import own_records
from trident_attendance.employee_access import (
	APP_ACCESS_FIELD,
	_ensure_own_user_permission,
	_revoke_all,
	on_employee_update,
)
from trident_attendance.own_shift import _has_punches_since
from trident_attendance.tests import helpers


class TestAccessHardening(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		emp, self.user = helpers.make_self_service_employee("Tokharden")
		self.employee = emp.name

	def _token(self):
		client = helpers.make_oauth_client()
		frappe.get_doc(
			{
				"doctype": "OAuth Bearer Token",
				"client": client,
				"user": self.user,
				"scopes": "all openid",
				"access_token": frappe.generate_hash(length=30),
				"expires_in": 3600,
				"expiration_time": frappe.utils.add_to_date(now_datetime(), hours=1),
				"status": "Active",
			}
		).insert(ignore_permissions=True)

	def _active_tokens(self):
		return frappe.db.count("OAuth Bearer Token", {"user": self.user, "status": "Active"})

	def test_the_user_permission_that_narrows_an_employee_to_their_own_is_made_once(self):
		frappe.db.delete("User Permission", {"user": self.user})
		_ensure_own_user_permission(self.user, self.employee)
		_ensure_own_user_permission(self.user, self.employee)
		rows = frappe.get_all(
			"User Permission",
			filters={"user": self.user, "allow": "Employee"},
			fields=["for_value", "apply_to_all_doctypes"],
		)
		self.assertEqual(rows, [{"for_value": self.employee, "apply_to_all_doctypes": 1}])

	def test_leaving_or_losing_app_access_ends_the_sign_in(self):
		for change in ({"status": "Left", "relieving_date": nowdate()}, {APP_ACCESS_FIELD: 0}):
			frappe.db.set_value("Employee", self.employee, {"status": "Active", APP_ACCESS_FIELD: 1})
			self._token()
			self.assertEqual(self._active_tokens(), 1)
			doc = frappe.get_doc("Employee", self.employee)
			doc._doc_before_save = frappe.get_doc("Employee", self.employee)
			doc.update(change)
			on_employee_update(doc)
			self.assertEqual(self._active_tokens(), 0, change)

	def test_an_unrelated_change_keeps_the_sign_in(self):
		self._token()
		doc = frappe.get_doc("Employee", self.employee)
		doc._doc_before_save = frappe.get_doc("Employee", self.employee)
		doc.first_name = "Renamed"
		on_employee_update(doc)
		self.assertEqual(self._active_tokens(), 1)
		_revoke_all(self.user)

	def test_the_callers_leave_approver_is_not_taken(self):
		frappe.db.set_value("Employee", self.employee, "leave_approver", None)
		self.assertIsNone(own_records._leave_approver_for(self.employee) or None)

	def test_a_punch_signalled_today_locks_today(self):
		if not frappe.db.exists("DocType", "Shift Signal"):
			self.skipTest("no Shift Signal on this site")
		today = frappe.utils.getdate(nowdate())
		self.assertFalse(_has_punches_since(self.employee, today))
		from trident_attendance.shift import _record

		_record(self.employee, "IN", now_datetime(), "terminal")  # as the hub's punch signal writes it
		self.assertTrue(_has_punches_since(self.employee, today))
