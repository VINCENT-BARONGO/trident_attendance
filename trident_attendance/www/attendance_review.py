import re

import frappe
from frappe import _
from frappe.utils import add_days, get_datetime, get_time, getdate, nowdate

from trident_attendance.checkin_rules import (
	ALREADY_MARKED_PREFIX,
	DAY_PREFIXES,
	evaluate_day,
	is_blocking,
	strip_prefixes,
)
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
	is_viewer,
	last_complete_date,
	scope_filters,
	split_reasons,
)

no_cache = 1

STATUS_FILTERS = ["Pending", "Auto-Released", "Released", "Marked", "Rejected", "All"]
QUEUE_DAYS = 30
OPEN_STATUSES = (STATUS_PENDING, STATUS_RELEASED, STATUS_AUTO_RELEASED)

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
	"Face: No Match": _("Live face did not match the profile photo. Compare the two photos, then release or reject."),
	"Face:": _("Face could not be verified (no profile photo / no face detected). Usually safe to release."),
	"Supervisor not allowed on project": _("The user who posted this is not in the project's Users table. Add them, or move the punch."),
	"Outside geofence": _("Punch was outside the project's radius (plus tolerance)."),
	"No GPS on punch": _("Handset sent no location."),
	"Project has no GPS": _("Set Site Latitude / Longitude on the project."),
	"Supervisor has no Employee record": _("Link the supervisor's User to an Employee (Employee > User ID)."),
	"Missing OUT": _("No OUT punch. Use Add OUT, or reject the day."),
	"Missing IN": _("No IN punch, so hours cannot be computed."),
	"OUT before IN": _("The last OUT is earlier than the first IN. Fix the punch times."),
	"Supervisor not checked in first": _("The supervisor must check themselves in (face Matched) before workers."),
	"Supervisor not checked out first": _("The supervisor must check themselves out before workers."),
	"Mixed projects:": _("Worked at more than one site; the first IN's project is used."),
	"Attendance already marked:": _("An Attendance already exists for this day. Cancel & re-mark rebuilds it from the punches."),
	"Rejected:": _("Rejected by a reviewer."),
}


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/attendance-review"
		raise frappe.Redirect

	# Supervisors hold Employee Checkin read for the app; the review queue is for the office.
	if not is_viewer():
		frappe.throw(_("The attendance review page is for Attendance Admin / HR users."), frappe.PermissionError)

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

	context.projects = frappe.get_all(
		"Project", filters={"status": "Open"}, fields=["name", "project_name"], order_by="project_name"
	)
	project_names = {p.name: (p.project_name or p.name) for p in context.projects}
	context.supervisors = _supervisors()
	supervisor_names = {s.user: s.full_name for s in context.supervisors}

	project_filter = form.get("project") or ""
	if project_filter and project_filter not in project_names:
		project_filter = ""
	supervisor_filter = form.get("supervisor") or ""
	if supervisor_filter and supervisor_filter not in supervisor_names:
		supervisor_filter = ""

	context.view = view
	context.day = day
	context.prev_day = add_days(day, -1)
	context.next_day = add_days(day, 1)
	context.today = getdate(nowdate())
	context.status_filter = status_filter
	context.status_filters = STATUS_FILTERS
	context.project_filter = project_filter
	context.supervisor_filter = supervisor_filter
	context.can_process = is_reviewer()
	context.complete_up_to = last_complete_date(settings)
	context.day_complete = is_day_complete(day, settings)
	context.cutoff = str(get_time(settings.day_cutoff_time or "20:00:00"))[:5]
	context.auto_release = bool(settings.auto_release_clean_punches)
	context.queue_days = QUEUE_DAYS
	# Follow the user's Desk theme; the website itself always renders light.
	context.theme = (frappe.db.get_value("User", frappe.session.user, "desk_theme") or "Light").lower()
	context.project_names = project_names
	context.supervisor_names = supervisor_names

	if view == "queue":
		rows = _queue_rows(settings)
	else:
		start, end = day_bounds(day)
		rows = _rows({"time": ["between", [start, end]]}, settings)

	for r in rows:
		r.time_str = get_datetime(r.time).strftime("%H:%M")
		r.is_pending = r.custom_review_status == STATUS_PENDING and not r.attendance

	days = _group_by_day(rows, status_filter, settings, project_filter, supervisor_filter, project_names, supervisor_names)
	context.days = days
	context.pending_groups = sum(1 for d in days for g in d["groups"] if g["state"] == "Pending")
	context.shown_groups = sum(len(d["groups"]) for d in days)
	context.stats = _stats(settings)

	context.attendance_present = []
	context.attendance_leave = []
	if view == "day":
		attendance = frappe.get_all(
			"Attendance",
			filters={"attendance_date": day, "docstatus": 1},
			fields=["name", "employee", "employee_name", "status", "working_hours", "custom_project", "in_time", "out_time"],
			order_by="status, employee_name",
		)
		context.attendance_present = [a for a in attendance if a.status != "On Leave"]
		context.attendance_leave = [a for a in attendance if a.status == "On Leave"]


def _rows(filters, settings):
	filters = dict(filters)
	filters.update(scope_filters(settings))
	return frappe.get_all("Employee Checkin", filters=filters, fields=PUNCH_FIELDS, order_by="time asc")


def _queue_rows(settings):
	"""All punches of every employee-day that still has a pending punch in the queue window."""
	start, _s_end = day_bounds(add_days(getdate(nowdate()), -QUEUE_DAYS))
	_e_start, end = day_bounds(getdate(nowdate()))
	filters = {
		"custom_review_status": STATUS_PENDING,
		"attendance": ["is", "not set"],
		"time": ["between", [start, end]],
	}
	filters.update(scope_filters(settings))
	pending = frappe.get_all("Employee Checkin", filters=filters, fields=["employee", "time"])
	keys = {(p.employee, getdate(p.time)) for p in pending}
	if not keys:
		return []
	employees = sorted({k[0] for k in keys})
	dates = [k[1] for k in keys]
	d_start, _d_end = day_bounds(min(dates))
	_d_start, d_end = day_bounds(max(dates))
	rows = _rows({"employee": ["in", employees], "time": ["between", [d_start, d_end]]}, settings)
	return [r for r in rows if (r.employee, getdate(r.time)) in keys]


def _supervisors():
	rows = frappe.db.sql(
		"""select distinct pu.user, coalesce(u.full_name, pu.user) as full_name
		from `tabProject User` pu
		join `tabProject` p on p.name = pu.parent
		left join `tabUser` u on u.name = pu.user
		where p.status = 'Open' and ifnull(pu.user, '') != ''
		order by full_name""",
		as_dict=True,
	)
	recent = frappe.get_all(
		"Employee Checkin",
		filters={"creation": [">=", add_days(nowdate(), -QUEUE_DAYS)], **scope_filters()},
		distinct=True,
		pluck="owner",
	)
	known = {r.user for r in rows}
	for owner in recent:
		if owner and owner not in known:
			rows.append(frappe._dict(user=owner, full_name=frappe.db.get_value("User", owner, "full_name") or owner))
	return rows


def _group_by_day(rows, status_filter, settings, project_filter, supervisor_filter, project_names, supervisor_names):
	groups = {}
	for r in rows:
		key = (getdate(r.time), r.employee)
		g = groups.setdefault(
			key,
			{
				"date": key[0],
				"employee": r.employee,
				"employee_name": r.employee_name or r.employee,
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

	employee_images = _employee_images({g["employee"] for g in groups.values()})

	days = {}
	for (day, _employee), g in groups.items():
		punches = g["punches"]
		# Filters apply to whole employee-days, after the day was assembled, so a day is never
		# evaluated on a partial set of punches.
		if project_filter and project_filter not in g["projects"]:
			continue
		if supervisor_filter and supervisor_filter not in g["owners"]:
			continue
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
		elif statuses == {None} or statuses == {""}:
			g["state"] = "Unstaged"
		else:
			g["state"] = "Pending"

		live = [p for p in punches if p.custom_review_status != STATUS_REJECTED]
		open_punches = [p for p in live if p.custom_review_status in OPEN_STATUSES and not p.attendance]
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
		g["suggested_out"] = _suggested_out(g, day, settings) if g["missing_out"] else None
		g["pending_names"] = [p.name for p in punches if p.is_pending]
		g["project_label"] = ", ".join(project_names.get(p, p) for p in g["projects"])
		g["supervisor_label"] = ", ".join(supervisor_names.get(o, o) for o in g["owners"])
		g["image"] = employee_images.get(g["employee"])

		# Instant reasons from the punches that still matter; day reasons recomputed live so an
		# edited punch never shows a stale pairing message.
		source = open_punches if g["state"] == "Pending" else live
		reasons = []
		for p in source:
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
		g["reasons"] = [{"text": r, "blocking": is_blocking(r, settings) if g["state"] == "Pending" else False, "hint": _hint(r)} for r in reasons]
		g["blocking"] = [r["text"] for r in g["reasons"] if r["blocking"]]
		g["can_release"] = g["state"] == "Pending" and bool(g["pending_names"]) and not g["missing_out"] and bool(ins)

		g["existing_attendance"] = g["attendance"]
		for reason in reasons:
			if reason.startswith(ALREADY_MARKED_PREFIX):
				m = re.search(r"marked:\s*([^\s(]+)", reason)
				if m:
					g["existing_attendance"] = m.group(1)

		days.setdefault(day, []).append(g)

	order = {"Pending": 0, "Released": 1, "Marked": 2, "Unstaged": 3, "Rejected": 4}
	out = []
	for day in sorted(days, reverse=True):
		items = sorted(days[day], key=lambda g: (order[g["state"]], g["project_label"], g["employee_name"] or ""))
		out.append(
			{
				"date": day,
				"label": frappe.format(day, {"fieldtype": "Date"}),
				"weekday": day.strftime("%A"),
				"groups": items,
				"pending": sum(1 for g in items if g["state"] == "Pending"),
				"projects": sorted({g["project_label"] for g in items if g["state"] == "Pending"}),
			}
		)
	return out


def _employee_images(employees) -> dict:
	if not employees:
		return {}
	return {
		e.name: e.image
		for e in frappe.get_all("Employee", filters={"name": ["in", list(employees)]}, fields=["name", "image"])
		if e.image
	}


def _suggested_out(g, day, settings):
	"""The supervisor's own OUT on that day/project is the best guess for a missing OUT."""
	first_in = g["first_in"]
	if first_in and first_in.custom_logged_by and first_in.custom_logged_by != first_in.employee:
		start, end = day_bounds(day)
		filters = {
			"employee": first_in.custom_logged_by,
			"log_type": "OUT",
			"time": ["between", [start, end]],
			"custom_review_status": ["!=", STATUS_REJECTED],
		}
		if first_in.custom_site_project:
			filters["custom_site_project"] = first_in.custom_site_project
		t = frappe.db.get_value("Employee Checkin", filters, "max(time)")
		if t:
			return get_datetime(t).strftime("%H:%M")
	return str(get_time(settings.auto_checkout_time or "17:00:00"))[:5]


def _stats(settings) -> dict:
	yesterday = add_days(getdate(nowdate()), -1)
	y_start, y_end = day_bounds(yesterday)
	t_start, t_end = day_bounds(getdate(nowdate()))
	scope = scope_filters(settings)

	def count(extra):
		f = dict(scope)
		f.update(extra)
		return frappe.db.count("Employee Checkin", f)

	rows = frappe.get_all(
		"Employee Checkin",
		filters={**scope, "time": ["between", [y_start, y_end]]},
		fields=["employee", "custom_review_status", "attendance"],
	)
	y_days = {}
	for r in rows:
		y_days.setdefault(r.employee, set()).add("marked" if r.attendance else (r.custom_review_status or "?"))
	marked = sum(1 for s in y_days.values() if "marked" in s)
	pending = sum(1 for s in y_days.values() if STATUS_PENDING in s)
	rejected = sum(1 for s in y_days.values() if s == {STATUS_REJECTED})

	last_run = frappe.get_all(
		"Trident Attendance Run",
		fields=["run_at", "trigger", "marked", "held", "errors"],
		order_by="run_at desc",
		limit=1,
	)
	return {
		"yesterday": {"label": frappe.format(yesterday, {"fieldtype": "Date"}), "punches": len(rows), "days": len(y_days), "marked": marked, "pending": pending, "rejected": rejected},
		"today_punches": count({"time": ["between", [t_start, t_end]]}),
		"last_run": last_run[0] if last_run else None,
	}


def _hint(reason: str) -> str:
	for prefix, hint in REASON_HINTS.items():
		if reason.startswith(prefix):
			return hint
	return ""
