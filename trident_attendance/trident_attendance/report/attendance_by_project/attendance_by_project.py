import frappe
from frappe import _
from frappe.utils import add_days, getdate, nowdate


def execute(filters=None):
	filters = frappe._dict(filters or {})
	to_date = getdate(filters.to_date or nowdate())
	from_date = getdate(filters.from_date) if filters.from_date else add_days(to_date, -6)

	conditions = {"docstatus": 1, "attendance_date": ["between", [from_date, to_date]]}
	if filters.project:
		conditions["custom_project"] = filters.project
	if filters.status:
		conditions["status"] = filters.status
	if filters.employee:
		conditions["employee"] = filters.employee

	rows = frappe.get_all(
		"Attendance",
		filters=conditions,
		fields=[
			"name",
			"custom_project",
			"employee",
			"employee_name",
			"attendance_date",
			"status",
			"in_time",
			"out_time",
			"working_hours",
			"custom_mixed_projects",
			"late_entry",
			"early_exit",
		],
		order_by="custom_project, employee_name, attendance_date",
	)

	project_names = {r.custom_project for r in rows if r.custom_project}
	titles = {}
	if project_names:
		titles = dict(
			frappe.get_all(
				"Project", filters={"name": ["in", list(project_names)]}, fields=["name", "project_name"], as_list=True
			)
		)

	data = []
	for r in rows:
		data.append(
			{
				"project": r.custom_project,
				"project_name": titles.get(r.custom_project, ""),
				"employee": r.employee,
				"employee_name": r.employee_name,
				"attendance_date": r.attendance_date,
				"status": r.status,
				"in_time": r.in_time,
				"out_time": r.out_time,
				"working_hours": r.working_hours,
				"present": 1 if r.status in ("Present", "Work From Home") else 0,
				"half_day": 1 if r.status == "Half Day" else 0,
				"absent": 1 if r.status == "Absent" else 0,
				"mixed": r.custom_mixed_projects,
				"attendance": r.name,
			}
		)

	return get_columns(), data


def get_columns():
	return [
		{"label": _("Project"), "fieldname": "project", "fieldtype": "Link", "options": "Project", "width": 130},
		{"label": _("Project Name"), "fieldname": "project_name", "fieldtype": "Data", "width": 180},
		{"label": _("Employee"), "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 120},
		{"label": _("Employee Name"), "fieldname": "employee_name", "fieldtype": "Data", "width": 180},
		{"label": _("Date"), "fieldname": "attendance_date", "fieldtype": "Date", "width": 100},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 90},
		{"label": _("In"), "fieldname": "in_time", "fieldtype": "Datetime", "width": 150},
		{"label": _("Out"), "fieldname": "out_time", "fieldtype": "Datetime", "width": 150},
		{"label": _("Hours"), "fieldname": "working_hours", "fieldtype": "Float", "precision": 2, "width": 80},
		{"label": _("Present"), "fieldname": "present", "fieldtype": "Int", "width": 80},
		{"label": _("Half Day"), "fieldname": "half_day", "fieldtype": "Int", "width": 80},
		{"label": _("Absent"), "fieldname": "absent", "fieldtype": "Int", "width": 80},
		{"label": _("Multiple Sites"), "fieldname": "mixed", "fieldtype": "Check", "width": 90},
		{"label": _("Attendance"), "fieldname": "attendance", "fieldtype": "Link", "options": "Attendance", "width": 160},
	]
