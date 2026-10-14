import frappe
from frappe import _
from frappe.model.document import Document


class ShiftSignal(Document):
	"""One row per employee: their latest punch as the attendance hub reported it (IN or OUT,
	and when). Written only by `trident_attendance.shift.punched`."""

	def validate(self):
		if not self.flags.from_punched:
			frappe.throw(_("A Shift Signal is kept by the attendance hub's punches and is not edited."), frappe.PermissionError)
