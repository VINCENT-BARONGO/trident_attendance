import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class TridentAttendanceSettings(Document):
	def validate(self):
		for field in ("geofence_tolerance_meters", "photo_retention_days"):
			if cint(self.get(field)) < 0:
				frappe.throw(_("{0} cannot be negative.").format(self.meta.get_label(field)))
		for field in ("half_day_threshold_hours", "absent_threshold_hours"):
			if flt(self.get(field)) < 0:
				frappe.throw(_("{0} cannot be negative.").format(self.meta.get_label(field)))
		if flt(self.absent_threshold_hours) and flt(self.half_day_threshold_hours):
			if flt(self.absent_threshold_hours) > flt(self.half_day_threshold_hours):
				frappe.throw(_("Absent threshold must be lower than the half-day threshold."))
