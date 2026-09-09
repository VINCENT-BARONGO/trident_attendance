import re

import frappe
from frappe import _
from frappe.utils import add_days, flt, get_datetime, get_time, getdate, nowdate

from trident_attendance.checkin_rules import ALREADY_MARKED_PREFIX, MISSING_OUT
from trident_attendance.utils import (
	STATUS_AUTO_RELEASED,
	STATUS_MARKED,
	STATUS_PENDING,
	STATUS_REJECTED,
	STATUS_RELEASED,
	day_bounds,
	get_settings,
	is_day_complete,
	is_reviewer,
	last_complete_date,
	scope_filters,
	split_reasons,
)

no_cache = 1

STATUS_FILTERS = ["Pending", "Auto-Released", "Released", "Marked", "Rejected", "All"]
OPEN = (STATUS_PENDING, STATUS_RELEASED, STATUS_AUTO_RELEASED)


def get_context(context):
	# Sits beside the Desk but uses the same session, so ERP credentials just work.
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/attendance-review"
		raise frappe.Redirect

	if not frappe.has_permission("Employee Checkin", "read"):
		frappe.throw(_("You are not permitted to view attendance check-ins."), frappe.PermissionError)

	settings = get_settings()
	form = frappe.form_dict
	try:
		day = getdate(form.get("date") or nowdate())
	except Exception:
		day = getdate(nowdate())
	status_filter = form.get("status") or "Pending"
	if status_filter not in STATUS_FILTERS:
		status_filter = "Pending"

	context.day = day
	context.prev_day = add_days(day, -1)
	context.next_day = add_days(day, 1)
	context.today = getdate(nowdate())
	context.status_filter = status_filter
	context.status_filters = STATUS_FILTERS
	context.project_filter = form.get("project") or ""
	context.supervisor_filter = form.get("supervisor") or ""
	context.can_process = is_reviewer()
	context.day_complete = is_day_complete(day, settings)
	context.complete_up_to = last_complete_date(settings)
	context.cutoff = str(get_time(settings.day_cutoff_time or "20:00:00"))[:5]
	context.auto_release = bool(settings.auto_release_clean_punches)

	start, end = day_bounds(day)
	filters = {"time": ["between", [start, end]]}
	filters.update(scope_filters(settings))
	if context.project_filter:
		filters["custom_site_project"] = context.project_filter
	if context.supervisor_filter:
		filters["owner"] = context.supervisor_filter

	rows = frappe.get_all(
		"Employee Checkin",
		filters=filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"log_type",
			"time",
			"owner",
			"custom_site_project",
			"custom_logged_by",
			"custom_face_match_result",
			"custom_face_match_score",
			"custom_distance_from_site",
			"custom_attendance_photo",
			"custom_review_status",
			"custom_hold_reasons",
			"custom_reviewed_by",
			"custom_reviewed_on",
			"custom_app_source",
			"attendance",
		],
		order_by="employee_name asc, time asc",
	)

	context.counts = {s: 0 for s in (STATUS_PENDING, STATUS_AUTO_RELEASED, STATUS_RELEASED, STATUS_MARKED, STATUS_REJECTED)}
	for r in rows:
		r.reasons = split_reasons(r.custom_hold_reasons)
		r.time_str = get_datetime(r.time).strftime("%H:%M:%S")
		r.is_pending = r.custom_review_status == STATUS_PENDING and not r.attendance
		if r.custom_review_status in context.counts:
			context.counts[r.custom_review_status] += 1

	context.groups = _group(rows, status_filter)
	context.total = len(rows)
	context.shown = sum(len(g["punches"]) for g in context.groups)

	context.attendance = frappe.get_all(
		"Attendance",
		filters={"attendance_date": day, "docstatus": 1},
		fields=["name", "employee", "employee_name", "status", "working_hours", "custom_project", "in_time", "out_time"],
		order_by="employee_name",
	)

	context.projects = frappe.get_all(
		"Project", filters={"status": "Open"}, fields=["name", "project_name"], order_by="project_name"
	)
	context.supervisors = sorted({r.owner for r in rows if r.owner})


def _group(rows, status_filter):
	groups = {}
	for r in rows:
		g = groups.setdefault(
			r.employee,
			{
				"employee": r.employee,
				"employee_name": r.employee_name,
				"punches": [],
				"projects": [],
				"reasons": [],
				"supervisors": [],
			},
		)
		g["punches"].append(r)
		if r.custom_site_project and r.custom_site_project not in g["projects"]:
			g["projects"].append(r.custom_site_project)
		for reason in r.reasons:
			if reason not in g["reasons"]:
				g["reasons"].append(reason)
		if r.custom_logged_by and r.custom_logged_by not in g["supervisors"]:
			g["supervisors"].append(r.custom_logged_by)

	out = []
	for g in groups.values():
		punches = g["punches"]
		statuses = {p.custom_review_status for p in punches}
		g["attendance"] = next((p.attendance for p in punches if p.attendance), None)
		if g["attendance"]:
			g["state"] = "Marked"
		elif STATUS_PENDING in statuses:
			g["state"] = "Pending"
		elif statuses & {STATUS_RELEASED, STATUS_AUTO_RELEASED}:
			g["state"] = "Released"
		elif statuses == {STATUS_REJECTED}:
			g["state"] = "Rejected"
		else:
			g["state"] = "Pending"

		if status_filter != "All":
			wanted = {
				"Pending": {STATUS_PENDING},
				"Auto-Released": {STATUS_AUTO_RELEASED},
				"Released": {STATUS_RELEASED},
				"Marked": {STATUS_MARKED},
				"Rejected": {STATUS_REJECTED},
			}[status_filter]
			if not (statuses & wanted):
				continue

		live = [p for p in punches if p.custom_review_status != STATUS_REJECTED]
		ins = [p for p in live if p.log_type == "IN"]
		outs = [p for p in live if p.log_type == "OUT"]
		g["first_in"] = ins[0] if ins else None
		g["last_out"] = outs[-1] if outs else None
		g["hours"] = None
		if g["first_in"] and g["last_out"]:
			delta = get_datetime(g["last_out"].time) - get_datetime(g["first_in"].time)
			g["hours"] = round(delta.total_seconds() / 3600, 2)
		g["missing_out"] = bool(ins) and not outs
		g["add_out_ref"] = g["first_in"].name if g["first_in"] else None
		g["pending_names"] = [p.name for p in punches if p.is_pending]

		g["existing_attendance"] = g["attendance"]
		for reason in g["reasons"]:
			if reason.startswith(ALREADY_MARKED_PREFIX):
				m = re.search(r"marked:\s*([^\s(]+)", reason)
				if m:
					g["existing_attendance"] = m.group(1)
		out.append(g)

	out.sort(key=lambda g: ({"Pending": 0, "Released": 1, "Marked": 2, "Rejected": 3}[g["state"]], g["employee_name"] or ""))
	return out
