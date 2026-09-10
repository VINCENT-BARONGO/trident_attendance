"""Attendance doc events.

These run inside hrms's own attendance savepoints as well as for manual Desk entries, so
anything non-essential is wrapped: an unexpected exception here would abort someone else's
attendance run.
"""

import frappe
from frappe import _

from hrms.hr.doctype.attendance.attendance import DuplicateAttendanceError

from trident_attendance.utils import STATUS_PENDING, day_bounds, get_settings, scope_filters


def before_insert(doc, method=None):
	try:
		if not doc.custom_project:
			doc.custom_project = frappe.db.get_value("Employee", doc.employee, "custom_project")
	except Exception:
		frappe.log_error(title="trident_attendance: set Attendance project failed")

	if doc.status == "Absent" and not doc.leave_type and not doc.flags.trident_marker:
		pending = _pending_punches(doc.employee, doc.attendance_date)
		if pending:
			# Plain raise, not frappe.throw: hrms's mark_attendance swallows this class without
			# clearing message_log, so a throw would leave a stray dialog behind.
			raise DuplicateAttendanceError(
				_("{0} has {1} check-in(s) on {2} awaiting review; release or reject them before marking Absent.").format(
					doc.employee, len(pending), frappe.format(doc.attendance_date, {"fieldtype": "Date"})
				)
			)


def after_insert(doc, method=None):
	projects = doc.flags.trident_mixed_projects
	if not projects:
		return
	try:
		doc.add_comment(
			"Comment",
			_("Worked at more than one site: {0}. Project set from the first check-in.").format(", ".join(projects)),
		)
	except Exception:
		frappe.log_error(title="trident_attendance: mixed-projects comment failed")


def before_cancel(doc, method=None):
	"""hrms clears the link on cancel; put the punches back in front of a reviewer first."""
	try:
		filters = {"attendance": doc.name}
		filters.update(scope_filters(get_settings()))
		for name in frappe.get_all("Employee Checkin", filters=filters, pluck="name"):
			frappe.db.set_value(
				"Employee Checkin",
				name,
				{"custom_review_status": STATUS_PENDING, "custom_reviewed_by": None, "custom_reviewed_on": None},
			)
	except Exception:
		frappe.log_error(title="trident_attendance: re-hold on Attendance cancel failed")


def _pending_punches(employee, day) -> list[str]:
	try:
		start, end = day_bounds(day)
		filters = {
			"employee": employee,
			"custom_review_status": STATUS_PENDING,
			"attendance": ["is", "not set"],
			"time": ["between", [start, end]],
		}
		filters.update(scope_filters(get_settings()))
		return frappe.get_all("Employee Checkin", filters=filters, pluck="name")
	except Exception:
		frappe.log_error(title="trident_attendance: pending punch lookup failed")
		return []
