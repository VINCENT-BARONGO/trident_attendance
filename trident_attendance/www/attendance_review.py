import frappe
from frappe import _
from frappe.utils import add_days, getdate, nowdate

no_cache = 1


def get_context(context):
	# Sits beside the Desk but uses the same session, so ERP credentials just work.
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/attendance-review"
		raise frappe.Redirect

	if not frappe.has_permission("Employee Checkin", "read"):
		frappe.throw(_("You are not permitted to view attendance check-ins."), frappe.PermissionError)

	day = getdate(frappe.form_dict.get("date") or nowdate())
	context.day = day
	context.prev_day = add_days(day, -1)
	context.next_day = add_days(day, 1)
	context.today = getdate(nowdate())

	context.rows = frappe.get_all(
		"Employee Checkin",
		filters={"time": ["between", [f"{day} 00:00:00", f"{day} 23:59:59"]]},
		fields=[
			"name", "employee", "employee_name", "log_type", "time", "shift",
			"custom_site_project", "custom_face_match_result", "custom_face_match_score",
			"custom_attendance_photo", "attendance", "skip_auto_attendance",
		],
		order_by="time asc",
	)

	context.attendance = frappe.get_all(
		"Attendance",
		filters={"attendance_date": day},
		fields=["name", "employee_name", "status", "working_hours"],
		order_by="employee_name",
	)

	# Drives whether the buttons render at all; the endpoints re-check server-side.
	context.can_process = frappe.has_permission("Attendance", "create")
	context.held = len([r for r in context.rows if r.skip_auto_attendance and not r.attendance])
	context.ready = len([r for r in context.rows if not r.skip_auto_attendance and not r.attendance])
	context.done = len([r for r in context.rows if r.attendance])
