"""Whether the signed-in employee may clock themselves in and out with the employee app.

Asked AS THE EMPLOYEE (their own ERP sign-in from the ID number, PIN and code), by the attendance
hub before it takes such a punch and by the app before it shows the Clock in button. The hub
does the rest (their registered phone or a colleague's through Help a colleague, a clock-in site
or a project's fence, a photo, no fake location) and relays the punch like any phone punch.

Who may is HR's choice, in two places:

- Trident Attendance Settings > Who may clock in on their phone: Off (nobody, the default),
  Ticked employees, or Everyone;
- Employee > Clock In On Own Phone: Allowed, Not allowed, or blank (as the setting says).

With "Ticked employees" only those set to Allowed may; with "Everyone" all but those set to Not
allowed. Only an Active employee with App Access is ever allowed: that is what lets them sign in
without an ERP password in the first place.
"""

import frappe
from frappe import _
from frappe.utils import cint

from trident_attendance.employee_access import APP_ACCESS_FIELD
from trident_attendance.own_records import own_employees
from trident_attendance.utils import get_settings, require_signed_in

FIELD = "custom_self_clock_in"
SETTING = "self_clock_in"
OFF, TICKED, EVERYONE = "Off", "Ticked employees", "Everyone"
ALLOWED, NOT_ALLOWED = "Allowed", "Not allowed"

REASONS = {
	"off": "Clocking in on your phone is not switched on. Clock in at a terminal or with your supervisor.",
	"not_allowed": "You may not clock in on your phone. Clock in at a terminal or with your supervisor.",
	"no_employee": "Your sign-in is not linked to an active employee record.",
	"app_access_off": "App access is not enabled for you. Ask HR.",
}


def decide(employee_row, settings) -> str | None:
	"""The refusal reason for this Employee row, or None when they may."""
	mode = settings.get(SETTING) or OFF
	if mode not in (TICKED, EVERYONE):
		return "off"
	if not cint(employee_row.get(APP_ACCESS_FIELD)):
		return "app_access_off"
	choice = employee_row.get(FIELD) or ""
	if choice == NOT_ALLOWED or (mode == TICKED and choice != ALLOWED):
		return "not_allowed"
	return None


@frappe.whitelist(methods=["GET"])
def permission() -> dict:
	"""{"allowed", "employee", "employee_name", "user", "reason", "message"} for the caller."""
	user = require_signed_in()
	fields = ["name", "employee_name", "status"]
	meta = frappe.get_meta("Employee")
	fields += [f for f in (APP_ACCESS_FIELD, FIELD) if meta.has_field(f)]
	row = None
	for name in own_employees(user):
		candidate = frappe.db.get_value("Employee", name, fields, as_dict=True)
		if candidate and candidate.status == "Active":
			row = candidate
			break
	reason = "no_employee" if row is None else decide(row, get_settings())
	return {
		"allowed": reason is None,
		"employee": row.name if row else None,
		"employee_name": row.employee_name if row else None,
		"user": user,
		"reason": reason,
		"message": _(REASONS[reason]) if reason else "",
	}
