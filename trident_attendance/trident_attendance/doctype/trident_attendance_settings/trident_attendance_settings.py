import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, get_time


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
		if cint(self.auto_checkout) and self.auto_checkout_time and self.day_cutoff_time:
			if get_time(self.auto_checkout_time) > get_time(self.day_cutoff_time):
				frappe.throw(_("Auto Checkout Time must not be later than Day Cutoff Time, or the OUT would be created in the future."))
		if not cint(self.hold_on_missing_out) and not cint(self.auto_checkout):
			frappe.msgprint(
				_("Days with a missing OUT will be released with 0 hours: neither 'Hold on Missing OUT' nor 'Auto Checkout' is on."),
				indicator="orange",
			)
