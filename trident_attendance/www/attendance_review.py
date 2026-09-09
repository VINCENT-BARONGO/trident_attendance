import re

import frappe
from frappe import _
from frappe.utils import add_days, get_datetime, get_time, getdate, nowdate

from trident_attendance.checkin_rules import ALREADY_MARKED_PREFIX, DAY_PREFIXES, evaluate_day, is_blocking, strip_prefixes
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
QUEUE_DAYS = 30

PUNCH_FIELDS = [
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
]

REASON_HINTS = {
	"Face: No Match": _("Live face did not match the profile photo. Check the photo, then release or reject."),
	"Face:": _("Face could not be verified (no profile photo / no face detected). Usually safe to release."),
	"Supervisor not allowed on project": _("The user who posted this is not in the project's Users table. Add them, or move the punch."),
	"Outside geofence": _("Punch was outside the project's radius (+ tolerance)."),
	"No GPS on punch": _("Handset sent no location."),
	"Project has no GPS": _("Set Site Latitude / Longitude on the project."),
	"Supervisor has no Employee record": _("Link the supervisor's User to an Employee (Employee > User ID)."),
	"Missing OUT": _("No OUT punch. Use Add OUT, or reject the day."),
	"Missing IN": _("No IN punch, so hours cannot be computed."),
	"OUT before IN": _("The last OUT is earlier than the first IN. Fix the punch times."),
	"Supervisor not checked in first": _("The supervisor must check themselves in (face Matched) before workers."),
	"Supervisor not checked out first": _("The supervisor must check themselves out before workers."),
	"Mixed projects:": _("Worked at more than one site; the first IN's project is used."),
	"Attendance already marked:": _("An Attendance already exists for this day. Cancel & re-mark to rebuild it from the punches."),
	"Rejected:": _("Rejected by a reviewer."),
}


def get_context(context):
	# Sits beside the Desk but uses the same session, so ERP credentials just work.
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/attendance-review"
		raise frappe.Redirect

	if not frappe.has_permission("Employee Checkin", "read"):
		frappe.throw(_("You are not permitted to view attendance check-ins."), frappe.PermissionError)

	settings = get_settings()
	form = frappe.form_dict
	view = "day" if form.get("view") == "day" or form.get("date") else "queue"
	try:
		day = getdate(form.get("date") or nowdate())
	except Exception:
		day = getdate(nowdate())
	status_filter = form.get("status") or ("Pending" if view == "queue" else "All")
	if status_filter not in STATUS_FILTERS:
		status_filter = "All"

	context.view = view
	context.day = day
	context.prev_day = add_days(day, -1)
	context.next_day = add_days(day, 1)
	context.today = getdate(nowdate())
	context.status_filter = status_filter
	context.status_filters = STATUS_FILTERS
	context.project_filter = form.get("project") or ""
	context.supervisor_filter = form.get("supervisor") or ""
	context.can_process = is_reviewer()
	context.complete_up_to = last_complete_date(settings)
	context.day_complete = is_day_complete(day, settings)
	context.cutoff = str(get_time(settings.day_cutoff_time or "20:00:00"))[:5]
	context.auto_release = bool(settings.auto_release_clean_punches)
	context.queue_days = QUEUE_DAYS

	if view == "queue":
		rows = _queue_rows(settings, context)
	else:
		start, end = day_bounds(day)
		rows = _rows({"time": ["between", [start, end]]}, settings, context)

	for r in rows:
		r.time_str = get_datetime(r.time).strftime("%H:%M")
		r.is_pending = r.custom_review_status == STATUS_PENDING and not r.attendance

	context.days = _group_by_day(rows, status_filter, settings)
	context.pending_groups = sum(1 for d in context.days for g in d["groups"] if g["state"] == "Pending")
	context.shown_groups = sum(len(d["groups"]) for d in context.days)

	context.attendance = []
	if view == "day":
		context.attendance = frappe.get_all(
			"Attendance",
			filters={"attendance_date": day, "docstatus": 1},
			fields=["name", "employee", "employee_name", "status", "working_hours", "custom_project", "in_time", "out_time"],
			order_by="status, employee_name",
		)
		context.attendance_present = [a for a in context.attendance if a.status not in ("On Leave",)]
		context.attendance_leave = [a for a in context.attendance if a.status == "On Leave"]

	context.projects = frappe.get_all(
		"Project", filters={"status": "Open"}, fields=["name", "project_name"], order_by="project_name"
	)
	context.supervisors = sorted({r.owner for r in rows if r.owner})


def _rows(filters, settings, context):
	filters = dict(filters)
	filters.update(scope_filters(settings))
	if context.project_filter:
		filters["custom_site_project"] = context.project_filter
	if context.supervisor_filter:
		filters["owner"] = context.supervisor_filter
	return frappe.get_all("Employee Checkin", filters=filters, fields=PUNCH_FIELDS, order_by="time asc")


def _queue_rows(settings, context):
	"""Every punch belonging to an employee-day that still has something pending, last N days."""
	start, _ = day_bounds(add_days(getdate(nowdate()), -QUEUE_DAYS))
	_, end = day_bounds(getdate(nowdate()))
	rows = _rows({"time": ["between", [start, end]]}, settings, context)
	pending_keys = {(r.employee, getdate(r.time)) for r in rows if r.custom_review_status == STATUS_PENDING and not r.attendance}
	return [r for r in rows if (r.employee, getdate(r.time)) in pending_keys]


def _group_by_day(rows, status_filter, settings):
	groups = {}
	for r in rows:
		key = (getdate(r.time), r.employee)
		g = groups.setdefault(
			key,
			{
				"date": key[0],
				"employee": r.employee,
				"employee_name": r.employee_name,
				"punches": [],
				"projects": [],
				"supervisors": [],
				"owners": [],
			},
		)
		g["punches"].append(r)
		if r.custom_site_project and r.custom_site_project not in g["projects"]:
			g["projects"].append(r.custom_site_project)
		if r.custom_logged_by and r.custom_logged_by not in g["supervisors"]:
			g["supervisors"].append(r.custom_logged_by)
		if r.owner and r.owner not in g["owners"]:
			g["owners"].append(r.owner)

	wanted = None
	if status_filter != "All":
		wanted = {
			"Pending": {STATUS_PENDING},
			"Auto-Released": {STATUS_AUTO_RELEASED},
			"Released": {STATUS_RELEASED},
			"Marked": {STATUS_MARKED},
			"Rejected": {STATUS_REJECTED},
		}[status_filter]

	days = {}
	for (day, _employee), g in groups.items():
		punches = g["punches"]
		statuses = {p.custom_review_status for p in punches}
		if wanted and not (statuses & wanted):
			continue

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

		# Instant reasons come from the rows; day reasons are recomputed live so an edited
		# punch never shows a stale pairing message.
		reasons = []
		for p in punches:
			for reason in strip_prefixes(split_reasons(p.custom_hold_reasons), DAY_PREFIXES):
				if reason not in reasons:
					reasons.append(reason)
		if g["state"] == "Pending" and live:
			try:
				for reason in evaluate_day(live, day, settings):
					if reason not in reasons:
						reasons.append(reason)
			except Exception:
				frappe.log_error(title="attendance-review: live day evaluation failed")
		g["reasons"] = [{"text": r, "blocking": is_blocking(r, settings), "hint": _hint(r)} for r in reasons]
		g["blocking"] = [r["text"] for r in g["reasons"] if r["blocking"]]

		g["existing_attendance"] = g["attendance"]
		for reason in reasons:
			if reason.startswith(ALREADY_MARKED_PREFIX):
				m = re.search(r"marked:\s*([^\s(]+)", reason)
				if m:
					g["existing_attendance"] = m.group(1)

		days.setdefault(day, []).append(g)

	order = {"Pending": 0, "Released": 1, "Marked": 2, "Rejected": 3}
	out = []
	for day in sorted(days, reverse=True):
		items = sorted(days[day], key=lambda g: (order[g["state"]], g["employee_name"] or ""))
		out.append(
			{
				"date": day,
				"label": frappe.format(day, {"fieldtype": "Date"}),
				"weekday": day.strftime("%A"),
				"groups": items,
				"pending": sum(1 for g in items if g["state"] == "Pending"),
			}
		)
	return out


def _hint(reason: str) -> str:
	for prefix, hint in REASON_HINTS.items():
		if reason.startswith(prefix):
			return hint
	return ""
