import re

import frappe
from frappe import _
from frappe.model.document import Document

from trident_attendance.utils import employee_id_field

# What a Casual Approver approved: once a casual is Active (or Left, and may be reactivated) only an
# approver or HR changes these, or a registrar could approve someone and then swap the pay, the
# M-Pesa number or the face the phone matches against.
APPROVED_FIELDS = ("full_name", "id_number", "photo", "subcontractor", "daily_rate", "mpesa_number", "bank_name",
                   "bank_account_no")
APPROVED_STATES = ("Active", "Left")
APPROVER_ROLES = {"Casual Approver", "HR Manager", "System Manager"}


def may_change_approved():
	return frappe.session.user == "Administrator" or bool(APPROVER_ROLES & set(frappe.get_roles()))


def file_before_insert(doc, method=None):
	"""upload_file with fieldname=photo may set the field itself: the same rule as a save."""
	if doc.attached_to_doctype != "Casual Labourer" or doc.attached_to_field not in APPROVED_FIELDS:
		return
	status = frappe.db.get_value("Casual Labourer", doc.attached_to_name, "status")
	if status in APPROVED_STATES and not may_change_approved():
		frappe.throw(_("This casual was approved: only a Casual Approver or HR may change their photo."),
		             frappe.PermissionError)


class CasualLabourer(Document):
	"""A casual labourer who is not an Employee. Registered in the office; the attendance app
	finds them by ID number and matches the face against `photo`."""

	def validate(self):
		self.full_name = " ".join((self.full_name or "").split())
		self._clean_id_number()
		self._check_not_an_employee()
		self._check_approved_fields()
		if self.start_date and self.end_date and self.end_date < self.start_date:
			frappe.throw(_("End Date cannot be before Start Date."))

	def _check_approved_fields(self):
		before = self.get_doc_before_save()
		if before is None or before.status not in APPROVED_STATES or may_change_approved():
			return
		changed = [f for f in APPROVED_FIELDS if (self.get(f) or None) != (before.get(f) or None)]
		if changed:
			frappe.throw(
				_("{0} was approved with these details: only a Casual Approver or HR may change {1}.").format(
					self.full_name, ", ".join(_(self.meta.get_label(f)) for f in changed)
				),
				frappe.PermissionError,
			)

	def _clean_id_number(self):
		# Kept as printed digits only, so it compares with what the app reads off the card.
		digits = re.sub(r"\D", "", self.id_number or "")
		if not 6 <= len(digits) <= 9:
			frappe.throw(_("ID Number must be 6 to 9 digits (old cards 7-8, Maisha cards 9)."))
		self.id_number = digits

	def _check_not_an_employee(self):
		# The app looks an ID number up among Employees and casuals; one number must be one person.
		employee = frappe.db.get_value(
			"Employee", {employee_id_field(): self.id_number, "status": "Active"}, ["name", "employee_name"], as_dict=True
		)
		if employee:
			frappe.throw(
				_("ID Number {0} already belongs to Employee {1} ({2}).").format(
					self.id_number, employee.name, employee.employee_name
				)
			)
