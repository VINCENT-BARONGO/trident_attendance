"""Health numbers for the Settings page and the review page header.

Everything here is read-only and cheap enough to call on every page load.
"""

from collections import Counter

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, getdate, now_datetime, nowdate

from trident_attendance.utils import (
	STATUS_MARKED,
	STATUS_PENDING,
	STATUS_REJECTED,
	day_bounds,
	get_settings,
	is_reviewer,
	last_complete_date,
	scope_filters,
	split_reasons,
)

MONITOR_ROLES = {"Attendance Admin", "System Manager", "HR Manager", "HR User"}


def _bucket(reason: str) -> str:
	"""Collapse variable parts of a reason so the stats can group them."""
	if reason.startswith("Face:") and reason != "Face: No Match":
		return "Face: Unverified"
	for prefix in ("Outside geofence", "Mixed projects", "Attendance already marked", "Rejected", "Supervisor not checked in first", "Supervisor not checked out first"):
		if reason.startswith(prefix):
			return prefix
	return reason


def _require_monitor():
	if not (MONITOR_ROLES & set(frappe.get_roles())):
		frappe.throw(_("Not permitted."), frappe.PermissionError)


@frappe.whitelist()
def get_pipeline_stats(days: int = 7) -> dict:
	_require_monitor()
	settings = get_settings()
	days = max(1, min(cint(days) or 7, 90))
	today = getdate(nowdate())
	window_start, _ws_end = day_bounds(add_days(today, -(days - 1)))
	_we_start, window_end = day_bounds(today)
	today_start, today_end = day_bounds(today)
	scope = scope_filters(settings)

	def count(extra):
		f = dict(scope)
		f.update(extra)
		return frappe.db.count("Employee Checkin", f)

	pending_rows = frappe.get_all(
		"Employee Checkin",
		filters={**scope, "custom_review_status": STATUS_PENDING, "attendance": ["is", "not set"]},
		fields=["employee", "time", "custom_hold_reasons"],
	)
	reasons = Counter()
	for r in pending_rows:
		for reason in split_reasons(r.custom_hold_reasons):
			reasons[_bucket(reason)] += 1
	pending_days = len({(r.employee, getdate(r.time)) for r in pending_rows})
	oldest_pending = min((getdate(r.time) for r in pending_rows), default=None)

	last_punch = frappe.db.get_value(
		"Employee Checkin", scope or {"name": ["is", "set"]}, ["max(creation)"], as_dict=False
	)

	runs = frappe.get_all(
		"Trident Attendance Run",
		fields=["name", "run_at", "trigger", "user", "up_to", "days_evaluated", "marked", "held", "errors", "absent_marked", "duration_seconds"],
		order_by="run_at desc",
		limit=8,
	)
	last_run = runs[0] if runs else None
	last_scheduler_run = next((r for r in runs if r.trigger == "Scheduler"), None)

	job_types = frappe.get_all("Scheduled Job Type", filters={"method": ["like", "trident_attendance.tasks.%"]}, fields=["name", "method", "stopped", "last_execution"])
	scheduler_disabled = False
	try:
		from frappe.utils.scheduler import is_scheduler_disabled

		scheduler_disabled = bool(is_scheduler_disabled())
	except Exception:
		pass

	error_count = frappe.db.count(
		"Error Log",
		{"creation": [">=", add_days(now_datetime(), -1)], "method": ["like", "%trident_attendance%"]},
	)

	marked_window = frappe.db.count(
		"Employee Checkin",
		{**scope, "custom_review_status": STATUS_MARKED, "custom_reviewed_on": ["between", [window_start, window_end]]},
	)
	attendance_window = frappe.db.count(
		"Attendance",
		{"docstatus": 1, "attendance_date": ["between", [getdate(window_start), today]], "in_time": ["is", "set"], "custom_project": ["is", "set"]},
	)

	warnings = []
	if scheduler_disabled:
		warnings.append(_("The site scheduler is disabled: nothing will be processed automatically."))
	if not job_types:
		warnings.append(_("Scheduled job types for this app are missing: run bench migrate."))
	elif any(cint(j.stopped) for j in job_types):
		warnings.append(_("A Trident Attendance scheduled job is stopped in Scheduled Job Type."))
	if last_scheduler_run and get_datetime(last_scheduler_run.run_at) < add_days(now_datetime(), -1):
		warnings.append(_("The hourly job has not run for over 24 hours."))
	if not last_run:
		warnings.append(_("No processing run has been recorded yet."))
	if error_count:
		warnings.append(_("{0} error(s) logged in the last 24 hours (see Error Log).").format(error_count))
	if oldest_pending and (today - oldest_pending).days > 3:
		warnings.append(_("Oldest pending check-in is from {0}.").format(frappe.format(oldest_pending, {"fieldtype": "Date"})))
	if not cint(settings.auto_release_clean_punches):
		warnings.append(_("Automatic release is off: every day needs a reviewer."))

	return {
		"as_of": str(now_datetime()),
		"settings": {
			"staged_sources": settings.staged_sources,
			"day_cutoff_time": str(settings.day_cutoff_time or "20:00:00")[:5],
			"auto_release": cint(settings.auto_release_clean_punches),
			"auto_checkout": cint(settings.auto_checkout),
			"mark_absent": cint(settings.mark_absent_without_punches),
			"complete_up_to": str(last_complete_date(settings)),
		},
		"punches": {
			"today": count({"creation": ["between", [today_start, today_end]]}),
			"window": count({"creation": ["between", [window_start, window_end]]}),
			"last_received": str(last_punch[0]) if last_punch and last_punch[0] else None,
			"supervisors_today": len(
				frappe.get_all("Employee Checkin", filters={**scope, "creation": ["between", [today_start, today_end]]}, distinct=True, pluck="owner")
			),
		},
		"review": {
			"pending_punches": len(pending_rows),
			"pending_days": pending_days,
			"oldest_pending": str(oldest_pending) if oldest_pending else None,
			"top_reasons": reasons.most_common(8),
			"rejected_window": count({"custom_review_status": STATUS_REJECTED, "custom_reviewed_on": ["between", [window_start, window_end]]}),
			"marked_punches_window": marked_window,
			"attendance_from_app_window": attendance_window,
		},
		"scheduler": {
			"disabled": scheduler_disabled,
			"jobs": job_types,
			"last_run": last_run,
			"last_scheduler_run": last_scheduler_run,
			"runs": runs,
		},
		"errors_24h": error_count,
		"warnings": warnings,
		"window_days": days,
		"can_run": is_reviewer(),
	}
