import frappe
from frappe import _
from frappe.model.document import Document


class LocationRequest(Document):
	"""One row each time someone asked where an employee is: who asked, about whom, when, and
	how many of the employee's phones were asked. The phone shows nothing when it is asked, so
	this row is the only trace of a request: it is written only by
	`trident_attendance.locate.request_location`, never changed afterwards, and not deleted."""

	def before_insert(self):
		if not self.flags.from_request_location:
			frappe.throw(_("A Location Request is only made by asking for a person's location."), frappe.PermissionError)

	def validate(self):
		if not self.is_new():
			frappe.throw(_("A Location Request is a record of what was asked and cannot be changed."), frappe.PermissionError)

	def on_trash(self):
		frappe.throw(_("A Location Request is a record of what was asked and cannot be deleted."), frappe.PermissionError)
