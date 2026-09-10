"""Whitelisted endpoints.

Reviewer endpoints back the /attendance-review page and require the Attendance Admin role
(not a doctype permission: on the live site the mobile app's own role also holds Attendance
create, so a permission check could not tell a supervisor from a reviewer).

Supervisor endpoints are what the Android app calls. Everything they return is scoped to the
calling user's own punches and projects.
"""

import base64
import json
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, flt, get_datetime, getdate, now_datetime, nowdate

from trident_attendance import tasks
from trident_attendance.checkin_rules import REJECTED_PREFIX
from trident_attendance.utils import (
	INTERNAL_SOURCE_PREFIX,
	STATUS_PENDING,
	STATUS_REJECTED,
	SUPERVISOR_ROLES,
	combine_datetime,
	day_bounds,
	get_settings,
	is_reviewer,
	join_reasons,
	last_complete_date,
	scope_filters,
	split_reasons,
)


def _require_reviewer():
	if not is_reviewer():
		frappe.throw(_("Only users with the Attendance Admin role can review check-ins."), frappe.PermissionError)


def _require_supervisor():
	if not (SUPERVISOR_ROLES & set(frappe.get_roles())):
		frappe.throw(_("You are not permitted to use the attendance app."), frappe.PermissionError)


def _names(names) -> list[str]:
	if isinstance(names, str):
		try:
			names = json.loads(names) if names.startswith("[") else [names]
		except ValueError:
			frappe.throw(_("Invalid selection."))
	if not isinstance(names, list):
		frappe.throw(_("Invalid selection."))
	return [str(n) for n in names if n][:500]


def _groups(names) -> list[tuple[str, object]]:
	groups = []
	for name in _names(names):
		row = frappe.db.get_value("Employee Checkin", name, ["employee", "time"], as_dict=True)
		if not row:
			continue
		key = (row.employee, getdate(row.time))
		if key not in groups:
			groups.append(key)
	return groups


# ---------------------------------------------------------------------------
# Reviewer endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def release_checkins(names):
	"""Release every open punch of the selected employee-days and mark them now."""
	_require_reviewer()
	groups = _groups(names)
	if not groups:
		return {"ok": False, "message": _("Nothing selected.")}
	settings = get_settings()
	results = [
		tasks.finalise_group(employee, day, settings, released_by=frappe.session.user, force=True)
		for employee, day in groups
	]
	return {"ok": True, "results": results, "marked": len([r for r in results if r["status"] == "marked"])}


@frappe.whitelist(methods=["POST"])
def reject_checkins(names, reason=None):
	reason = (reason or "").strip()[:140]
	_require_reviewer()
	rejected = 0
	for name in _names(names):
		row = frappe.db.get_value("Employee Checkin", name, ["custom_review_status", "attendance", "custom_hold_reasons"], as_dict=True)
		if not row or row.attendance or row.custom_review_status == STATUS_REJECTED:
			continue
		reasons = split_reasons(row.custom_hold_reasons) + [f"{REJECTED_PREFIX} {reason or _('rejected by reviewer')}"]
		frappe.db.set_value(
			"Employee Checkin",
			name,
			{
				"custom_review_status": STATUS_REJECTED,
				"custom_hold_reasons": join_reasons(reasons),
				"custom_reviewed_by": frappe.session.user,
				"custom_reviewed_on": now_datetime(),
			},
		)
		rejected += 1
	return {"ok": True, "rejected": rejected}


@frappe.whitelist(methods=["POST"])
def set_checkin_project(name, project):
	_require_reviewer()
	if not frappe.db.exists("Project", project):
		frappe.throw(_("Project {0} does not exist.").format(project))
	doc = frappe.get_doc("Employee Checkin", name)
	if doc.custom_review_status != STATUS_PENDING or doc.attendance:
		frappe.throw(_("Only check-ins awaiting review can be moved to another project."))
	doc.custom_site_project = project
	doc.flags.ignore_permissions = True
	doc.save()
	return {"ok": True, "hold_reasons": split_reasons(doc.custom_hold_reasons)}


@frappe.whitelist(methods=["POST"])
def add_missing_out(name, time):
	"""Create an OUT punch for the day of the given check-in, then re-evaluate that day."""
	_require_reviewer()
	ref = frappe.db.get_value(
		"Employee Checkin", name, ["employee", "time", "custom_site_project", "custom_logged_by"], as_dict=True
	)
	if not ref:
		frappe.throw(_("Check-in {0} not found.").format(name))
	day = getdate(ref.time)
	try:
		out_time = combine_datetime(day, time) if len(str(time)) <= 8 else get_datetime(time)
	except Exception:
		frappe.throw(_("Enter the OUT time as HH:MM."))
	if out_time <= get_datetime(ref.time):
		frappe.throw(_("The OUT time must be after the check-in at {0}.").format(ref.time))

	tasks.create_internal_punch(
		employee=ref.employee,
		log_type="OUT",
		time=out_time,
		project=ref.custom_site_project,
		logged_by=ref.custom_logged_by,
		source=f"{INTERNAL_SOURCE_PREFIX}manual",
	)
	return {"ok": True, "result": tasks.finalise_group(ref.employee, day, get_settings())}


@frappe.whitelist(methods=["POST"])
def cancel_and_remark(attendance):
	"""Cancel an Attendance and rebuild it from the day's check-ins."""
	_require_reviewer()
	att = frappe.get_doc("Attendance", attendance)
	employee, day = att.employee, getdate(att.attendance_date)
	linked = frappe.db.exists("Employee Checkin", {"attendance": att.name})
	start, end = day_bounds(day)
	has_punches = frappe.db.exists(
		"Employee Checkin", {"employee": employee, "time": ["between", [start, end]], **scope_filters()}
	)
	if att.leave_type or att.status == "On Leave":
		frappe.throw(_("{0} is a leave record; cancel it from the Desk if that is intended.").format(att.name))
	if not (linked or has_punches):
		frappe.throw(_("{0} has no app check-ins to rebuild from.").format(att.name))
	if att.docstatus == 1:
		att.flags.ignore_permissions = True
		att.cancel()
	result = tasks.finalise_group(employee, day, get_settings(), released_by=frappe.session.user, force=True)
	return {"ok": True, "result": result}


@frappe.whitelist(methods=["POST"])
def process_attendance(include_today=0):
	_require_reviewer()
	summary = tasks.finalise_days(force_today=cint(include_today), trigger="Reviewer")
	return {"ok": True, **summary}


# ---------------------------------------------------------------------------
# Supervisor endpoints (mobile app)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_my_projects():
	"""Open projects the calling user is listed on. No fallback to all projects."""
	_require_supervisor()
	fields = [
		"name",
		"project_name",
		"status",
		"custom_site_latitude",
		"custom_site_longitude",
		"custom_geofence_radius_meters",
	]
	if is_reviewer():
		return frappe.get_all("Project", filters={"status": "Open"}, fields=fields, order_by="project_name")
	allowed = frappe.get_all(
		"Project User", filters={"user": frappe.session.user, "parenttype": "Project"}, pluck="parent"
	)
	if not allowed:
		return []
	return frappe.get_all(
		"Project", filters={"name": ["in", allowed], "status": "Open"}, fields=fields, order_by="project_name"
	)


MAX_PHOTO_BYTES = 2 * 1024 * 1024
MAX_PUNCH_AGE_DAYS = 60
IMAGE_SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")


@frappe.whitelist(methods=["POST"])
def sync_checkin(
	client_uid,
	employee,
	log_type,
	time,
	custom_site_project=None,
	custom_id_number_scanned=None,
	custom_mrz_raw=None,
	custom_face_match_result=None,
	custom_face_match_score=None,
	latitude=None,
	longitude=None,
	custom_app_source=None,
	photo_base64=None,
	photo_filename=None,
):
	"""Idempotent single-call ingestion for the mobile app."""
	_require_supervisor()
	if log_type not in ("IN", "OUT"):
		frappe.throw(_("log_type must be IN or OUT."))
	try:
		punch_time = get_datetime(time).replace(microsecond=0)
	except Exception:
		frappe.throw(_("time must be 'YYYY-MM-DD HH:MM:SS'."))
	# Older app builds send no uid; derive a stable one so a retry is still a no-op.
	client_uid = (client_uid or "").strip()[:64] or f"{employee}|{log_type}|{punch_time}"[:64]
	if punch_time > now_datetime() + timedelta(minutes=10):
		frappe.throw(_("Check-in time is in the future."))
	if punch_time < add_days(now_datetime(), -MAX_PUNCH_AGE_DAYS):
		frappe.throw(_("Check-in is older than {0} days.").format(MAX_PUNCH_AGE_DAYS))

	# A supervisor can only ever see their own punches through the replay lookup.
	own = {} if is_reviewer() else {"owner": frappe.session.user}
	existing = frappe.db.get_value(
		"Employee Checkin",
		{"custom_client_uid": client_uid, **own},
		["name", "custom_review_status", "custom_hold_reasons"],
		as_dict=True,
	)
	if not existing:
		# Same punch re-sent without its uid (older app build): treat like a replay too.
		existing = frappe.db.get_value(
			"Employee Checkin",
			{"employee": employee, "log_type": log_type, "time": punch_time, **own},
			["name", "custom_review_status", "custom_hold_reasons"],
			as_dict=True,
		)
	if existing:
		return _sync_response(existing, duplicate=True)

	doc = frappe.new_doc("Employee Checkin")
	doc.update(
		{
			"employee": employee,
			"log_type": log_type,
			"time": punch_time,
			"skip_auto_attendance": 1,
			"custom_client_uid": client_uid,
			"custom_site_project": custom_site_project,
			"custom_id_number_scanned": custom_id_number_scanned,
			"custom_mrz_raw": custom_mrz_raw,
			"custom_face_match_result": custom_face_match_result,
			"custom_face_match_score": flt(custom_face_match_score),
			"latitude": flt(latitude) or None,
			"longitude": flt(longitude) or None,
			"custom_app_source": (custom_app_source or "TPL-FieldApp")[:140],
		}
	)
	doc.insert()

	if photo_base64:
		try:
			_attach_photo(doc, photo_base64)
		except Exception:
			frappe.clear_messages()
			frappe.log_error(title=f"trident_attendance: photo attach failed for {doc.name}")

	return _sync_response(doc, duplicate=False)


def _attach_photo(doc, photo_base64):
	# Android Base64.DEFAULT wraps lines; strip all whitespace before validating.
	raw = "".join(photo_base64.split(",", 1)[-1].split())
	if len(raw) > MAX_PHOTO_BYTES * 4 // 3 + 4:
		frappe.throw(_("Photo is larger than {0} MB.").format(MAX_PHOTO_BYTES // (1024 * 1024)))
	content = base64.b64decode(raw, validate=True)
	if not content.startswith(IMAGE_SIGNATURES):
		frappe.throw(_("Photo must be a JPEG or PNG image."))
	stamp = get_datetime(doc.time).strftime("%Y%m%d_%H%M%S")
	ext = "png" if content.startswith(IMAGE_SIGNATURES[1]) else "jpg"
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"checkin_{doc.employee}_{stamp}.{ext}",
			"attached_to_doctype": "Employee Checkin",
			"attached_to_name": doc.name,
			"attached_to_field": "custom_attendance_photo",
			"is_private": 1,
			"content": content,
		}
	)
	file_doc.flags.ignore_permissions = True
	file_doc.insert()
	frappe.db.set_value("Employee Checkin", doc.name, "custom_attendance_photo", file_doc.file_url, update_modified=False)


def _sync_response(row, duplicate: bool) -> dict:
	return {
		"name": row.name,
		"review_status": row.custom_review_status,
		"hold_reasons": split_reasons(row.custom_hold_reasons),
		"duplicate": duplicate,
	}


HISTORY_FIELDS = [
	"name",
	"employee",
	"employee_name",
	"log_type",
	"time",
	"custom_site_project",
	"custom_logged_by",
	"custom_face_match_result",
	"custom_face_match_score",
	"custom_distance_from_site",
	"custom_review_status",
	"custom_hold_reasons",
	"attendance",
	"custom_app_source",
]

# One report is one request and one response held in the phone's memory. A month across a
# supervisor's sites is already a few thousand punches.
MAX_REPORT_DAYS = 31


@frappe.whitelist()
def get_my_history(from_date=None, to_date=None, project=None):
	"""The caller's own punches with their review outcome, plus a per-day summary."""
	_require_supervisor()
	try:
		to_date = getdate(to_date or nowdate())
		from_date = getdate(from_date) if from_date else add_days(to_date, -14)
	except Exception:
		frappe.throw(_("Dates must be YYYY-MM-DD."))
	if from_date > to_date:
		from_date, to_date = to_date, from_date
	if (to_date - from_date).days > 92:
		from_date = add_days(to_date, -92)
	start, _s_end = day_bounds(from_date)
	_e_start, end = day_bounds(to_date)

	filters = {"time": ["between", [start, end]]}
	filters.update(scope_filters())
	if project:
		filters["custom_site_project"] = project
	if not is_reviewer():
		filters["owner"] = frappe.session.user

	rows = frappe.get_all("Employee Checkin", filters=filters, fields=HISTORY_FIELDS, order_by="time desc")
	days = _enrich_punches(rows)
	return {
		"from_date": str(from_date),
		"to_date": str(to_date),
		"complete_up_to": str(last_complete_date()),
		"punches": rows,
		"days": days,
	}


def _enrich_punches(rows) -> list[dict]:
	"""Adds hold reasons and the Attendance outcome to each punch, and returns per-day totals."""
	attendance_names = [r.attendance for r in rows if r.attendance]
	attendance = {}
	if attendance_names:
		for a in frappe.get_all(
			"Attendance",
			filters={"name": ["in", attendance_names]},
			fields=["name", "status", "working_hours", "in_time", "out_time", "custom_project"],
		):
			attendance[a.name] = a

	days = {}
	for r in rows:
		r.hold_reasons = split_reasons(r.custom_hold_reasons)
		r.attendance_detail = attendance.get(r.attendance)
		key = str(getdate(r.time))
		d = days.setdefault(key, {"date": key, "punches": 0, "employees": set(), "marked": 0, "pending": 0, "rejected": 0})
		d["punches"] += 1
		d["employees"].add(r.employee)
		if r.attendance:
			d["marked"] += 1
		elif r.custom_review_status == STATUS_REJECTED:
			d["rejected"] += 1
		else:
			d["pending"] += 1
	for d in days.values():
		d["employees"] = len(d["employees"])
	return sorted(days.values(), key=lambda d: d["date"], reverse=True)


def _report_projects() -> list[dict]:
	"""Projects whose punches the caller may read: every project they are listed on, open or
	closed, since a finished site's history is still theirs. Reviewers read every project."""
	fields = ["name", "project_name", "status"]
	if is_reviewer():
		return frappe.get_all("Project", fields=fields, order_by="project_name")
	listed = frappe.get_all(
		"Project User", filters={"user": frappe.session.user, "parenttype": "Project"}, pluck="parent"
	)
	if not listed:
		return []
	return frappe.get_all("Project", filters={"name": ["in", listed]}, fields=fields, order_by="project_name")


@frappe.whitelist()
def get_project_report(from_date=None, to_date=None, projects=None):
	"""Every punch on the caller's projects, whoever logged it, for the app's Reports screen.

	get_my_history only returns punches the caller took. This is scoped by project instead: a
	supervisor listed on several sites sees each of them in full, including staff clocked by
	another supervisor, and nothing at all on a site they are not listed on.
	"""
	_require_supervisor()
	to_date = getdate(to_date or nowdate())
	from_date = getdate(from_date) if from_date else to_date
	if from_date > to_date:
		frappe.throw(_("The report start date must be on or before its end date."))
	if date_diff(to_date, from_date) >= MAX_REPORT_DAYS:
		frappe.throw(_("A report can cover at most {0} days.").format(MAX_REPORT_DAYS))

	visible = _report_projects()
	visible_names = [p.name for p in visible]
	requested = _names(projects)
	denied = sorted(set(requested) - set(visible_names))
	if denied:
		frappe.throw(_("You are not listed on {0}.").format(", ".join(denied)), frappe.PermissionError)
	scope = requested or visible_names

	rows = []
	if scope:
		filters = {
			"time": ["between", [day_bounds(from_date)[0], day_bounds(to_date)[1]]],
			"custom_site_project": ["in", scope],
		}
		filters.update(scope_filters())
		rows = frappe.get_all(
			"Employee Checkin", filters=filters, fields=HISTORY_FIELDS + ["owner"], order_by="time desc"
		)

	supervisors = list({r.custom_logged_by for r in rows if r.custom_logged_by})
	supervisor_names = {}
	if supervisors:
		supervisor_names = dict(
			frappe.get_all(
				"Employee", filters={"name": ["in", supervisors]}, fields=["name", "employee_name"], as_list=True
			)
		)
	for r in rows:
		r.logged_by_name = supervisor_names.get(r.custom_logged_by)
		# Say whether the caller took the punch without sending other supervisors' logins.
		r.is_mine = r.pop("owner") == frappe.session.user

	days = _enrich_punches(rows)
	return {
		"from_date": str(from_date),
		"to_date": str(to_date),
		"complete_up_to": str(last_complete_date()),
		"projects": visible,
		"punches": rows,
		"days": days,
	}
