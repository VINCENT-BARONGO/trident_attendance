"""Endpoints for the attendance review page.

Deliberately thin: the page is a view over stock ERPNext data and the buttons call
hrms's own auto-attendance routine. No parallel schema, no second source of truth --
Employee Checkin stays the raw punch log and Attendance stays the committed record.

Staging works through the stock `skip_auto_attendance` field. The mobile app posts
every punch with it set, and hrms' get_employee_checkins() filters on
skip_auto_attendance = 0, so held punches are invisible to attendance processing
until a reviewer releases them here.
"""

import json

import frappe
from frappe import _


def _require_reviewer():
	"""Both actions admit data into payroll, so both need the same authority.

	Write on Employee Checkin is deliberately NOT the test: the mobile app's own role
	holds that (it patches custom fields after creating a punch), so using it would let
	a supervisor release their own scans from the handset.
	"""
	if not frappe.has_permission("Attendance", "create"):
		frappe.throw(
			_("You are not permitted to release check-ins or create Attendance records."),
			frappe.PermissionError,
		)


@frappe.whitelist()
def release_checkins(names):
	"""Clear skip_auto_attendance so these punches become eligible for Attendance."""
	_require_reviewer()

	if isinstance(names, str):
		names = json.loads(names)
	if not names:
		return {"ok": False, "message": _("Nothing selected.")}

	released = 0
	for name in names:
		# Re-read each row rather than trusting the posted list: the page may be stale,
		# and releasing something already processed would be a silent no-op worth avoiding.
		row = frappe.db.get_value(
			"Employee Checkin", name, ["skip_auto_attendance", "attendance"], as_dict=True
		)
		if not row or row.attendance or not row.skip_auto_attendance:
			continue
		frappe.db.set_value("Employee Checkin", name, "skip_auto_attendance", 0)
		released += 1

	frappe.db.commit()
	return {"ok": True, "released": released}


@frappe.whitelist()
def process_attendance():
	"""Convert released check-ins into Attendance via each shift's own rules."""
	_require_reviewer()

	shifts = frappe.get_all("Shift Type", filters={"enable_auto_attendance": 1}, pluck="name")
	if not shifts:
		return {
			"ok": False,
			"message": _("No Shift Type has auto attendance enabled, so there is nothing to process."),
		}

	before = frappe.db.count("Attendance")
	messages = []
	for name in shifts:
		shift = frappe.get_doc("Shift Type", name)
		result = shift.process_auto_attendance(is_manually_triggered=True)
		messages.append(f"{name}: {result or _('nothing to process')}")

	created = frappe.db.count("Attendance") - before
	return {"ok": True, "created": created, "messages": messages}
