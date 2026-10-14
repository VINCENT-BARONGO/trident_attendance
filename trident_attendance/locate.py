"""Asking where a clocked-in employee is.

The employee app reports a person's position to Traccar while they are on shift. Someone
allowed to may ask for a position now: the person's phone is sent a push that carries data
alone and sends one position to Traccar. The ERP never holds the position itself.

The rules:

- Only while the person is clocked in: the attendance hub's latest punch for them
  (`shift.is_clocked_in`, the Shift Signal) is an IN of the last 18 hours. Nobody can be
  located before they clock in, after they clock out, or on a day off. Employee Checkin is
  not looked at: check-ins reach the ERP at night, so during the day it holds none of that day.
- Only by the role named in Trident Attendance Settings > Role that may locate people, or by a
  System Manager. No role named = System Managers only.
- The person is told up front, in the phone app, that the office may ask for their location
  while they are clocked in; a single request shows them nothing; every request is recorded.
- The record is the `Location Request` row: who asked about whom, and when. It is the only
  trace of a request, so it is written before the phone is asked, by this module alone, and
  cannot be changed afterwards.
- One request per employee a minute, whoever asks.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import now_datetime

from trident_attendance import push
from trident_attendance.shift import is_clocked_in
from trident_attendance.utils import get_settings, require_signed_in

REQUEST_DOCTYPE = "Location Request"
ROLE_FIELD = "locate_role"
ALWAYS_ALLOWED_ROLE = "System Manager"
# Seconds before the same employee can be asked about again, by anyone.
REQUEST_INTERVAL = 60


def _may_locate(user: str) -> bool:
	roles = set(frappe.get_roles(user))
	if ALWAYS_ALLOWED_ROLE in roles:
		return True
	role = get_settings().get(ROLE_FIELD)
	return bool(role) and role in roles


def _asked_a_moment_ago(employee: str) -> bool:
	since = now_datetime() - timedelta(seconds=REQUEST_INTERVAL)
	return bool(frappe.db.exists(REQUEST_DOCTYPE, {"employee": employee, "requested_at": [">", since]}))


@frappe.whitelist(methods=["GET"])
def may_locate():
	"""Whether the signed-in user may ask for a person's location. For the Employee form, which
	shows "Locate now" by it; `request_location` checks again."""
	return _may_locate(require_signed_in())


@frappe.whitelist(methods=["POST"])
def request_location(employee):
	"""Asks the employee's phones for one position now and records who asked. The phone shows
	nothing. The answer is `{"request", "employee", "employee_name", "devices"}`; the position
	itself goes from the phone to Traccar, not through the ERP.

	Refused with a permission error for a caller without the role, and with a validation error
	that says which when the employee is not active, has no user, has no phone registered, is
	not clocked in, or was asked about less than a minute ago."""
	user = require_signed_in()
	if not _may_locate(user):
		frappe.throw(_("You may not ask for a person's location."), frappe.PermissionError)

	employee = str(employee or "").strip()
	fields = ["name", "employee_name", "status", "user_id"]
	row = frappe.db.get_value("Employee", employee, fields, as_dict=True) if employee else None
	if not row:
		frappe.throw(_("No such employee: {0}").format(employee), frappe.ValidationError)
	who = row.employee_name or row.name
	if row.status != "Active":
		frappe.throw(_("{0} is not an active employee.").format(who), frappe.ValidationError)
	if not row.user_id:
		frappe.throw(_("{0} has no ERP user, so no phone can be asked.").format(who), frappe.ValidationError)
	if not push.push_enabled():
		frappe.throw(_("Push notifications are switched off in Trident Attendance Settings."), frappe.ValidationError)
	devices = push.enabled_devices(row.user_id)
	if not devices:
		frappe.throw(_("{0} has no phone registered.").format(who), frappe.ValidationError)
	if not is_clocked_in(row.name):
		frappe.throw(_("{0} is not clocked in.").format(who), frappe.ValidationError)
	if _asked_a_moment_ago(row.name):
		frappe.throw(_("{0} was asked a moment ago. Try again in a minute.").format(who), frappe.ValidationError)

	request = frappe.new_doc(REQUEST_DOCTYPE)
	request.update(
		{
			"employee": row.name,
			"employee_name": row.employee_name,
			"requested_by": user,
			"requested_at": now_datetime(),
			"device_count": devices,
		}
	)
	request.flags.from_request_location = True
	request.flags.ignore_permissions = True
	request.insert()

	data = {"request": request.name, "employee": row.name, "url": "/ess/"}
	push.instruct_user(row.user_id, "locate", data, push.LOCATE_TTL)
	return {"request": request.name, "employee": row.name, "employee_name": row.employee_name, "devices": devices}
