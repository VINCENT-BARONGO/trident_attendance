import frappe
from frappe import _
from frappe.utils import add_days, getdate, nowdate

from trident_attendance.utils import day_bounds, scope_filters


def execute(filters=None):
	filters = frappe._dict(filters or {})
	to_date = getdate(filters.to_date or nowdate())
	from_date = getdate(filters.from_date) if filters.from_date else add_days(to_date, -6)
	start, _end = day_bounds(from_date)
	_start, end = day_bounds(to_date)

	conditions = {"time": ["between", [start, end]]}
	if not filters.include_all_sources:
		conditions.update(scope_filters())
	if filters.project:
		conditions["custom_site_project"] = filters.project
	if filters.review_status:
		conditions["custom_review_status"] = filters.review_status
	if filters.reason:
		conditions["custom_hold_reasons"] = ["like", f"%{filters.reason}%"]
	if filters.supervisor:
		conditions["owner"] = filters.supervisor
	if filters.employee:
		conditions["employee"] = filters.employee

	rows = frappe.get_all(
		"Employee Checkin",
		filters=conditions,
		fields=[
			"name",
			"time",
			"employee",
			"employee_name",
			"log_type",
			"custom_site_project",
			"custom_face_match_result",
			"custom_face_match_score",
			"custom_distance_from_site",
			"custom_review_status",
			"custom_hold_reasons",
			"custom_logged_by",
			"owner",
			"custom_reviewed_by",
			"custom_reviewed_on",
			"attendance",
			"custom_app_source",
			"device_id",
		],
		order_by="time desc",
	)

	data = []
	for r in rows:
		data.append(
			{
				"time": r.time,
				"employee": r.employee,
				"employee_name": r.employee_name,
				"log_type": r.log_type,
				"project": r.custom_site_project,
				"face": r.custom_face_match_result,
				"score": r.custom_face_match_score,
				"distance": r.custom_distance_from_site,
				"review_status": r.custom_review_status,
				"hold_reasons": (r.custom_hold_reasons or "").replace("\n", "; "),
				"supervisor": r.custom_logged_by,
				"owner": r.owner,
				"reviewed_by": r.custom_reviewed_by,
				"reviewed_on": r.custom_reviewed_on,
				"attendance": r.attendance,
				"source": r.custom_app_source or r.device_id,
				"checkin": r.name,
			}
		)
	return get_columns(), data


def get_columns():
	return [
		{"label": _("Time"), "fieldname": "time", "fieldtype": "Datetime", "width": 150},
		{"label": _("Employee"), "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 110},
		{"label": _("Employee Name"), "fieldname": "employee_name", "fieldtype": "Data", "width": 170},
		{"label": _("Type"), "fieldname": "log_type", "fieldtype": "Data", "width": 60},
		{"label": _("Project"), "fieldname": "project", "fieldtype": "Link", "options": "Project", "width": 120},
		{"label": _("Face"), "fieldname": "face", "fieldtype": "Data", "width": 150},
		{"label": _("Score"), "fieldname": "score", "fieldtype": "Float", "precision": 2, "width": 70},
		{"label": _("Distance (m)"), "fieldname": "distance", "fieldtype": "Float", "precision": 0, "width": 90},
		{"label": _("Review Status"), "fieldname": "review_status", "fieldtype": "Data", "width": 120},
		{"label": _("Hold Reasons"), "fieldname": "hold_reasons", "fieldtype": "Data", "width": 320},
		{"label": _("Supervisor"), "fieldname": "supervisor", "fieldtype": "Link", "options": "Employee", "width": 110},
		{"label": _("Posted By"), "fieldname": "owner", "fieldtype": "Link", "options": "User", "width": 160},
		{"label": _("Reviewed By"), "fieldname": "reviewed_by", "fieldtype": "Link", "options": "User", "width": 140},
		{"label": _("Reviewed On"), "fieldname": "reviewed_on", "fieldtype": "Datetime", "width": 150},
		{"label": _("Attendance"), "fieldname": "attendance", "fieldtype": "Link", "options": "Attendance", "width": 150},
		{"label": _("Source"), "fieldname": "source", "fieldtype": "Data", "width": 150},
		{"label": _("Check-in"), "fieldname": "checkin", "fieldtype": "Link", "options": "Employee Checkin", "width": 180},
	]
