"""Controller override for Employee Checkin.

Frappe validates Link and Select values (`_validate_links`, `_validate_selects`) before any
`doc_events` hook runs, on both insert and save. The Android app sometimes posts values that
fail that validation (a supervisor's email in `custom_logged_by`, a face label outside the
Select options). Rejecting the request loses the punch for good -- the app never retries a
FAILED row -- so the values are repaired here, before Frappe looks at them.
"""

import frappe

from hrms.hr.doctype.employee_checkin.employee_checkin import EmployeeCheckin

from trident_attendance.utils import employee_for_user, is_viewer

REASON_NO_SUPERVISOR_EMPLOYEE = "Supervisor has no Employee record"
FACE_ALIASES = {
	"matched": "Matched",
	"match": "Matched",
	"nomatch": "No Match",
	"no_match": "No Match",
	"skipped": "Skipped",
	"skip": "Skipped",
}


class TridentEmployeeCheckin(EmployeeCheckin):
	def _validate_links(self):
		self.flags.trident_reasons = list(self.flags.trident_reasons or [])
		self._normalise_logged_by()
		self._normalise_face_result()
		super()._validate_links()

	def _normalise_logged_by(self):
		value = self.custom_logged_by
		poster = self.owner or frappe.session.user
		trusted = self.flags.trident_internal or self.flags.ignore_permissions or is_viewer()

		if trusted and value and frappe.db.exists("Employee", value):
			return

		# The session user that posted the punch is the supervisor, whatever the client sent;
		# only reviewers/HR may name someone else.
		resolved = employee_for_user(poster)
		if not resolved and trusted and value:
			resolved = employee_for_user(value)

		self.custom_logged_by = resolved
		if not resolved and REASON_NO_SUPERVISOR_EMPLOYEE not in self.flags.trident_reasons:
			self.flags.trident_reasons.append(REASON_NO_SUPERVISOR_EMPLOYEE)

	def _normalise_face_result(self):
		value = self.custom_face_match_result
		if not value:
			return
		field = self.meta.get_field("custom_face_match_result")
		options = [o.strip() for o in (field.options or "").splitlines() if o.strip()] if field else []
		if not options or value in options:
			return
		key = "".join(ch for ch in str(value).lower() if ch.isalnum() or ch == "_")
		alias = FACE_ALIASES.get(key) or FACE_ALIASES.get(key.replace("_", ""))
		if alias:
			self.custom_face_match_result = alias
			return
		reason = f"Face: unrecognised value ({str(value)[:40]})"
		if reason not in self.flags.trident_reasons:
			self.flags.trident_reasons.append(reason)
		self.custom_face_match_result = "Skipped"
