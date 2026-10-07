import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, get_time


class TridentAttendanceSettings(Document):
	def validate(self):
		self.employee_id_field = (self.employee_id_field or "").strip() or None
		if self.employee_id_field and not frappe.get_meta("Employee").has_field(self.employee_id_field):
			frappe.throw(_("Employee has no field named {0}.").format(self.employee_id_field))
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
		self.validate_employee_access()
		if not cint(self.hold_on_missing_out) and not cint(self.auto_checkout):
			frappe.msgprint(
				_("Days with a missing OUT will be released with 0 hours: neither 'Hold on Missing OUT' nor 'Auto Checkout' is on."),
				indicator="orange",
			)

	def validate_employee_access(self):
		"""Who the hub is and which client its tokens are for decide who can be signed in as an
		employee. An Attendance Admin or HR Manager can save the rest of this page; naming
		themselves here would let them issue those sign-ins. The face sign-in switch is kept
		with them: on, it takes the PIN and the code out of the way."""
		before = self.get_doc_before_save()
		for field in ("hub_service_user", "employee_token_client", "allow_face_sign_in"):
			if (self.get(field) or None) == ((before.get(field) if before else None) or None):
				continue
			if "System Manager" not in frappe.get_roles():
				frappe.throw(
					_("Only a System Manager can change {0}.").format(self.meta.get_label(field)),
					frappe.PermissionError,
				)
		# Empty on a site that had these settings before the field existed.
		self.helper_token_minutes = cint(self.helper_token_minutes) or 10
		if not 1 <= self.helper_token_minutes <= 60:
			frappe.throw(_("{0} must be between 1 and 60.").format(self.meta.get_label("helper_token_minutes")))
