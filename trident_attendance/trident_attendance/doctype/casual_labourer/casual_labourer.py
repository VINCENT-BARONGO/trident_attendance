import re

import frappe
from frappe import _
from frappe.model.document import Document


class CasualLabourer(Document):
	"""A casual labourer who is not an Employee. Registered in the office; the attendance app
	finds them by ID number and matches the face against `photo`."""

	def validate(self):
		self.full_name = " ".join((self.full_name or "").split())
		self._clean_id_number()
		self._check_not_an_employee()
		if self.start_date and self.end_date and self.end_date < self.start_date:
			frappe.throw(_("End Date cannot be before Start Date."))

	def _clean_id_number(self):
		# Kept as printed digits only, so it compares with what the app reads off the card.
		digits = re.sub(r"\D", "", self.id_number or "")
		if not 6 <= len(digits) <= 9:
			frappe.throw(_("ID Number must be 6 to 9 digits (old cards 7-8, Maisha cards 9)."))
		self.id_number = digits

	def _check_not_an_employee(self):
		# The app looks an ID number up among Employees and casuals; one number must be one person.
		employee = frappe.db.get_value(
			"Employee", {"custom_id_number": self.id_number, "status": "Active"}, ["name", "employee_name"], as_dict=True
		)
		if employee:
			frappe.throw(
				_("ID Number {0} already belongs to Employee {1} ({2}).").format(
					self.id_number, employee.name, employee.employee_name
				)
			)
