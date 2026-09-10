"""Hold reasons for check-ins.

Every rule returns a stable, human-readable reason string. Reasons are stored newline-separated
in `Employee Checkin.custom_hold_reasons` and are matched by prefix in `is_blocking()`, so the
wording here is part of the contract with the review page and the Staging Log report.

Instant rules run when the punch arrives (or when its project is set); day rules run once the
day is complete and look at all of an employee's punches for that date together.
"""

from datetime import date

import frappe
from frappe.utils import cint, flt, getdate

from hrms.hr.utils import get_distance_between_coordinates

from trident_attendance.utils import (
	INTERNAL_SOURCE_PREFIX,
	STATUS_REJECTED,
	day_bounds,
	join_reasons,
	scope_filters,
	split_reasons,
)

FACE_NO_MATCH = "Face: No Match"
FACE_PREFIX = "Face:"
GEOFENCE_PREFIX = "Outside geofence"
NO_GPS_PUNCH = "No GPS on punch"
NO_GPS_PROJECT = "Project has no GPS"
NOT_ALLOWED_ON_PROJECT = "Supervisor not allowed on project"
NO_SUPERVISOR_EMPLOYEE = "Supervisor has no Employee record"
MISSING_IN = "Missing IN"
MISSING_OUT = "Missing OUT"
OUT_BEFORE_IN = "OUT before IN"
SUPERVISOR_NOT_IN = "Supervisor not checked in first"
SUPERVISOR_NOT_OUT = "Supervisor not checked out first"
MIXED_PREFIX = "Mixed projects:"
ALREADY_MARKED_PREFIX = "Attendance already marked:"
REJECTED_PREFIX = "Rejected:"

PROJECT_PREFIXES = (GEOFENCE_PREFIX, NO_GPS_PUNCH, NO_GPS_PROJECT, NOT_ALLOWED_ON_PROJECT)
DAY_PREFIXES = (
	MISSING_IN,
	MISSING_OUT,
	OUT_BEFORE_IN,
	SUPERVISOR_NOT_IN,
	SUPERVISOR_NOT_OUT,
	MIXED_PREFIX,
	ALREADY_MARKED_PREFIX,
)


def is_blocking(reason: str, settings) -> bool:
	if reason.startswith(FACE_NO_MATCH):
		return cint(settings.hold_on_face_mismatch) == 1
	if reason.startswith(FACE_PREFIX):
		return cint(settings.hold_on_face_unverified) == 1
	if reason.startswith(GEOFENCE_PREFIX):
		return cint(settings.hold_on_outside_geofence) == 1
	if reason in (NO_GPS_PUNCH, NO_GPS_PROJECT):
		return cint(settings.hold_on_no_gps) == 1
	if reason == NOT_ALLOWED_ON_PROJECT:
		return cint(settings.enforce_project_user_list) == 1
	if reason == NO_SUPERVISOR_EMPLOYEE:
		return cint(settings.require_supervisor_checkin_first) == 1 or cint(settings.require_supervisor_checkout_first) == 1
	if reason == MISSING_OUT:
		return cint(settings.hold_on_missing_out) == 1
	if reason.startswith(SUPERVISOR_NOT_IN):
		return cint(settings.require_supervisor_checkin_first) == 1
	if reason.startswith(SUPERVISOR_NOT_OUT):
		return cint(settings.require_supervisor_checkout_first) == 1
	if reason.startswith(MIXED_PREFIX):
		return False
	# Missing IN, OUT before IN, Attendance already marked, Rejected and anything unknown.
	return True


def blocking_reasons(reasons, settings) -> list[str]:
	return [r for r in reasons if is_blocking(r, settings)]


def strip_prefixes(reasons, prefixes) -> list[str]:
	return [r for r in reasons if not r.startswith(prefixes)]


def is_internal(doc) -> bool:
	return (doc.get("custom_app_source") or "").startswith(INTERNAL_SOURCE_PREFIX)


# ---------------------------------------------------------------------------
# Instant rules (single punch)
# ---------------------------------------------------------------------------


def evaluate_face(doc) -> list[str]:
	result = doc.get("custom_face_match_result")
	if not result or result in ("Matched", "Skipped"):
		return []
	if result == "No Match":
		return [FACE_NO_MATCH]
	return [f"{FACE_PREFIX} {result}"]


def evaluate_project(doc, settings) -> list[str]:
	"""Rules that need the site project: geofence and the supervisor's project membership."""
	project = doc.get("custom_site_project")
	if not project:
		doc.custom_distance_from_site = None
		return []

	reasons = []
	owner = doc.get("owner") or frappe.session.user
	if owner not in ("Administrator",) and not frappe.db.exists(
		"Project User", {"parent": project, "parenttype": "Project", "user": owner}
	):
		reasons.append(NOT_ALLOWED_ON_PROJECT)

	lat, lon, radius = frappe.db.get_value(
		"Project",
		project,
		["custom_site_latitude", "custom_site_longitude", "custom_geofence_radius_meters"],
	) or (None, None, None)

	if not (flt(lat) and flt(lon)):
		doc.custom_distance_from_site = None
		reasons.append(NO_GPS_PROJECT)
		return reasons

	if not (flt(doc.get("latitude")) and flt(doc.get("longitude"))):
		doc.custom_distance_from_site = None
		reasons.append(NO_GPS_PUNCH)
		return reasons

	distance = get_distance_between_coordinates(flt(lat), flt(lon), flt(doc.latitude), flt(doc.longitude))
	doc.custom_distance_from_site = round(distance, 1)
	if radius is not None and cint(radius) <= 0:
		# Radius 0 on the project means "no geofence at this site".
		return reasons
	allowed = cint(radius or 200) + cint(settings.geofence_tolerance_meters)
	if distance > allowed:
		reasons.append(f"{GEOFENCE_PREFIX} ({round(distance)}m, allowed {allowed}m)")
	return reasons


def evaluate_instant(doc, settings) -> list[str]:
	if is_internal(doc):
		return []
	reasons = evaluate_face(doc) + evaluate_project(doc, settings)
	reasons += [r for r in (doc.flags.trident_reasons or []) if r not in reasons]
	return reasons


def refresh_project_reasons(doc, settings) -> str | None:
	kept = strip_prefixes(split_reasons(doc.get("custom_hold_reasons")), PROJECT_PREFIXES)
	return join_reasons(kept + evaluate_project(doc, settings))


# ---------------------------------------------------------------------------
# Day rules (all punches of one employee on one date)
# ---------------------------------------------------------------------------


def evaluate_day(logs: list, day: date, settings) -> list[str]:
	"""`logs` are the employee's non-rejected in-scope punches for `day`, ordered by time."""
	if not logs:
		return []
	reasons = []
	ins = [l for l in logs if l.log_type == "IN"]
	outs = [l for l in logs if l.log_type == "OUT"]

	if not ins:
		reasons.append(MISSING_IN)
	if not outs:
		reasons.append(MISSING_OUT)
	if ins and outs and outs[-1].time < ins[0].time:
		reasons.append(OUT_BEFORE_IN)

	projects = []
	for l in logs:
		if l.get("custom_site_project") and l.custom_site_project not in projects:
			projects.append(l.custom_site_project)
	if len(projects) > 1:
		reasons.append(f"{MIXED_PREFIX} {', '.join(projects)}")

	existing = frappe.db.get_value(
		"Attendance",
		{"employee": logs[0].employee, "attendance_date": getdate(day), "docstatus": ["<", 2]},
		["name", "status"],
		as_dict=True,
	)
	if existing:
		reasons.append(f"{ALREADY_MARKED_PREFIX} {existing.name} ({existing.status})")

	reasons += _supervisor_reasons(ins, outs, day, settings)
	return reasons


def _supervisor_reasons(ins, outs, day, settings) -> list[str]:
	reasons = []
	if ins and cint(settings.require_supervisor_checkin_first):
		first_in = ins[0]
		if _needs_supervisor(first_in) and not _supervisor_punch_exists(first_in, "IN", day, settings):
			reasons.append(f"{SUPERVISOR_NOT_IN} ({first_in.custom_logged_by})")
	if outs and cint(settings.require_supervisor_checkout_first):
		last_out = outs[-1]
		if _needs_supervisor(last_out) and not _supervisor_punch_exists(last_out, "OUT", day, settings):
			reasons.append(f"{SUPERVISOR_NOT_OUT} ({last_out.custom_logged_by})")
	return reasons


def _needs_supervisor(log) -> bool:
	supervisor = log.get("custom_logged_by")
	# No supervisor resolved (reported separately) or the supervisor punching themselves.
	return bool(supervisor) and supervisor != log.employee and not is_internal(log)


def _supervisor_punch_exists(log, log_type: str, day, settings) -> bool:
	start, _end = day_bounds(day)
	filters = {
		"employee": log.custom_logged_by,
		"log_type": log_type,
		"time": ["between", [start, log.time]],
		"custom_review_status": ["!=", STATUS_REJECTED],
	}
	if log.get("custom_site_project"):
		filters["custom_site_project"] = log.custom_site_project
	if cint(settings.require_supervisor_face_match):
		filters["custom_face_match_result"] = "Matched"
	filters.update(scope_filters(settings))
	return bool(frappe.db.exists("Employee Checkin", filters))
