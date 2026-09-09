"""Turning app check-ins into Attendance, plus scheduled maintenance.

Runs as app hooks (not Server Scripts) so it behaves the same on Frappe Cloud, where Server
Scripts are disabled bench-wide.

A "day" is an employee's in-scope punches on one calendar date. Once the day is complete
(yesterday, or today after the cutoff time) the day rules run; a clean day is released and
marked straight away; anything with a blocking reason waits on /attendance-review.
Working hours are first IN to last OUT, using hrms's own calculate_working_hours so the
numbers match what hrms would have produced for a shift.
"""

from datetime import datetime

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, getdate, now_datetime, nowdate

from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
from erpnext.setup.doctype.holiday_list.holiday_list import is_holiday

from hrms.hr.doctype.attendance.attendance import (
	DuplicateAttendanceError,
	OverlappingShiftAttendanceError,
	mark_attendance,
)
from hrms.hr.doctype.employee_checkin.employee_checkin import calculate_working_hours

from trident_attendance.checkin_rules import (
	ALREADY_MARKED_PREFIX,
	DAY_PREFIXES,
	MISSING_IN,
	MISSING_OUT,
	OUT_BEFORE_IN,
	blocking_reasons,
	evaluate_day,
	strip_prefixes,
)
from trident_attendance.utils import (
	INTERNAL_SOURCE_PREFIX,
	STATUS_AUTO_RELEASED,
	STATUS_MARKED,
	STATUS_PENDING,
	STATUS_REJECTED,
	STATUS_RELEASED,
	combine_datetime,
	day_bounds,
	get_settings,
	join_reasons,
	last_complete_date,
	scope_filters,
	split_reasons,
)

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
	"custom_review_status",
	"custom_hold_reasons",
	"custom_app_source",
	"attendance",
]
OPEN_STATUSES = (STATUS_PENDING, STATUS_RELEASED, STATUS_AUTO_RELEASED)
CHECK_IN_OUT_TYPE = "Strictly based on Log Type in Employee Checkin"
HOURS_CALC_TYPE = "First Check-in and Last Check-out"
PAIRING_REASONS = (MISSING_IN, MISSING_OUT, OUT_BEFORE_IN)
# Held days older than this stop being re-evaluated every hour; a reviewer can still act on them.
LOOKBACK_DAYS = 45


# ---------------------------------------------------------------------------
# Scheduled entry points
# ---------------------------------------------------------------------------


def finalise_days(force_today: bool = False, up_to=None) -> dict:
	"""Hourly. Evaluate every complete day that still has pending punches."""
	settings = get_settings()
	end_date = getdate(up_to) if up_to else last_complete_date(settings, force_today)

	filters = {
		"custom_review_status": STATUS_PENDING,
		"attendance": ["is", "not set"],
		"time": ["between", [f"{add_days(end_date, -LOOKBACK_DAYS)} 00:00:00", f"{end_date} 23:59:59"]],
	}
	filters.update(scope_filters(settings))
	pending = frappe.get_all("Employee Checkin", filters=filters, fields=["employee", "time"], order_by="employee, time")

	days = sorted({(p.employee, getdate(p.time)) for p in pending})
	summary = {"up_to": str(end_date), "days": len(days), "marked": 0, "held": 0, "errors": 0, "results": []}

	for employee, day in days:
		result = finalise_group(employee, day, settings)
		summary["results"].append(result)
		if result["status"] == "error":
			summary["errors"] += 1
		elif result["status"] in ("marked", "held"):
			summary[result["status"]] += 1

	frappe.db.commit()  # nosemgrep -- scheduler job; keep progress if absent marking fails

	if cint(settings.mark_absent_without_punches):
		summary["absent"] = mark_absent_without_punches(end_date, settings)
		frappe.db.commit()  # nosemgrep
	return summary


def purge_attendance_photos():
	"""Daily. Drop face photos older than the retention window; keep the check-in row."""
	settings = get_settings()
	retention = cint(settings.photo_retention_days) or 30
	cutoff = add_days(now_datetime(), -retention)
	deleted = 0

	old_checkins = frappe.get_all("Employee Checkin", filters={"time": ["<", cutoff]}, pluck="name")
	if old_checkins:
		files = frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Employee Checkin", "attached_to_name": ["in", old_checkins]},
			fields=["name", "attached_to_name"],
		)
		for f in files:
			frappe.delete_doc("File", f.name, ignore_permissions=True, delete_permanently=True)
			deleted += 1
		for name in {f.attached_to_name for f in files}:
			frappe.db.set_value("Employee Checkin", name, "custom_attendance_photo", None, update_modified=False)

	orphans = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Employee Checkin",
			"attached_to_name": ["is", "not set"],
			"creation": ["<", cutoff],
		},
		pluck="name",
	)
	for name in orphans:
		frappe.delete_doc("File", name, ignore_permissions=True, delete_permanently=True)
		deleted += 1

	if deleted:
		frappe.logger("trident_attendance").info(f"Purged {deleted} attendance photo(s) older than {retention} days")
	return deleted


# ---------------------------------------------------------------------------
# Day evaluation
# ---------------------------------------------------------------------------


def get_day_logs(employee: str, day, settings=None, include_rejected: bool = False) -> list:
	start, end = day_bounds(day)
	filters = {"employee": employee, "time": ["between", [start, end]]}
	if not include_rejected:
		filters["custom_review_status"] = ["!=", STATUS_REJECTED]
	filters.update(scope_filters(settings or get_settings()))
	return frappe.get_all("Employee Checkin", filters=filters, fields=PUNCH_FIELDS, order_by="time asc")


def finalise_group(employee: str, day, settings=None, released_by: str | None = None, force: bool = False) -> dict:
	"""Evaluate one employee-day. Releases + marks it when clean (or when a reviewer forces it)."""
	settings = settings or get_settings()
	day = getdate(day)
	base = {"employee": employee, "date": str(day)}
	savepoint = "trident_finalise_group"
	frappe.db.savepoint(savepoint)
	try:
		logs = get_day_logs(employee, day, settings)
		open_logs = [l for l in logs if l.custom_review_status in OPEN_STATUSES and not l.attendance]
		if not open_logs:
			return {**base, "status": "nothing", "message": _("No open check-ins on this day.")}

		day_reasons = evaluate_day(logs, day, settings)
		blocking = _blocking_for(open_logs, day_reasons, settings)

		if cint(settings.auto_checkout) and not released_by and blocking == [MISSING_OUT]:
			if _create_auto_checkout(open_logs, day, settings):
				logs = get_day_logs(employee, day, settings)
				open_logs = [l for l in logs if l.custom_review_status in OPEN_STATUSES and not l.attendance]
				day_reasons = evaluate_day(logs, day, settings)
				blocking = _blocking_for(open_logs, day_reasons, settings)

		if blocking and not force:
			_write_reasons(open_logs, day_reasons, status=STATUS_PENDING)
			return {**base, "status": "held", "reasons": blocking}

		if not force and not cint(settings.auto_release_clean_punches):
			_write_reasons(open_logs, day_reasons, status=STATUS_PENDING)
			return {**base, "status": "held", "reasons": [_("Automatic release is switched off")]}

		if any(r.startswith(ALREADY_MARKED_PREFIX) for r in day_reasons):
			_write_reasons(open_logs, day_reasons, status=STATUS_PENDING)
			return {**base, "status": "held", "reasons": [r for r in day_reasons if r.startswith(ALREADY_MARKED_PREFIX)]}

		# A forced release still needs a usable IN/OUT pair; hours cannot be invented.
		unpaired = [r for r in day_reasons if r in PAIRING_REASONS]
		if unpaired:
			_write_reasons(open_logs, day_reasons, status=STATUS_PENDING)
			return {
				**base,
				"status": "held",
				"reasons": unpaired,
				"message": _("Add the missing punch (Add OUT) or reject the day before releasing it."),
			}

		status = STATUS_RELEASED if released_by else STATUS_AUTO_RELEASED
		_write_reasons(open_logs, day_reasons, status=status, reviewer=released_by or "Administrator")
		result = mark_day(open_logs, day, settings, day_reasons=day_reasons)
		return {**base, **result}
	except Exception as e:
		_rollback(savepoint)
		frappe.log_error(title=f"trident_attendance: finalise {employee} {day} failed")
		return {**base, "status": "error", "message": str(e)}


def _rollback(savepoint: str):
	# After a lock-wait/deadlock the transaction is already gone and the rollback itself raises.
	try:
		frappe.db.rollback(save_point=savepoint)
	except Exception:
		frappe.log_error(title=f"trident_attendance: rollback to {savepoint} failed")


def mark_day(logs: list, day, settings=None, day_reasons=None) -> dict:
	"""Create + submit one Attendance for these punches and link them."""
	settings = settings or get_settings()
	day_reasons = list(day_reasons or [])
	logs = sorted(logs, key=lambda l: l.time)
	for l in logs:
		l.time = get_datetime(l.time)

	hours, in_time, out_time = calculate_working_hours(logs, CHECK_IN_OUT_TYPE, HOURS_CALC_TYPE)
	hours = max(flt(hours), 0.0)

	status = "Present"
	if flt(settings.absent_threshold_hours) and hours < flt(settings.absent_threshold_hours):
		status = "Absent"
	elif flt(settings.half_day_threshold_hours) and hours < flt(settings.half_day_threshold_hours):
		status = "Half Day"

	projects = []
	for l in logs:
		if l.custom_site_project and l.custom_site_project not in projects:
			projects.append(l.custom_site_project)
	first_in = next((l for l in logs if l.log_type == "IN" and l.custom_site_project), None)
	project = (first_in.custom_site_project if first_in else None) or (projects[0] if projects else None)
	if not project:
		project = frappe.db.get_value("Employee", logs[0].employee, "custom_project")

	savepoint = "trident_mark_day"
	frappe.db.savepoint(savepoint)
	try:
		attendance = frappe.new_doc("Attendance")
		attendance.update(
			{
				"employee": logs[0].employee,
				"attendance_date": getdate(day),
				"status": status,
				"working_hours": hours,
				"in_time": in_time,
				"out_time": out_time,
				"custom_project": project,
				"custom_mixed_projects": 1 if len(projects) > 1 else 0,
				"half_day_status": "Absent" if status == "Half Day" else None,
			}
		)
		attendance.flags.ignore_permissions = True
		attendance.flags.trident_marker = True
		attendance.flags.trident_mixed_projects = projects if len(projects) > 1 else None
		attendance.insert()
		attendance.submit()
	except (DuplicateAttendanceError, OverlappingShiftAttendanceError):
		_rollback(savepoint)
		frappe.clear_messages()
		existing = frappe.db.get_value(
			"Attendance",
			{"employee": logs[0].employee, "attendance_date": getdate(day), "docstatus": ["<", 2]},
			["name", "status"],
			as_dict=True,
		)
		reason = f"{ALREADY_MARKED_PREFIX} {existing.name} ({existing.status})" if existing else f"{ALREADY_MARKED_PREFIX} ?"
		kept = [r for r in day_reasons if not r.startswith(ALREADY_MARKED_PREFIX)]
		_write_reasons(logs, kept + [reason], status=STATUS_PENDING, clear_reviewer=True)
		return {"status": "held", "reasons": [reason]}

	for l in logs:
		frappe.db.set_value(
			"Employee Checkin",
			l.name,
			{"attendance": attendance.name, "custom_review_status": STATUS_MARKED},
			update_modified=False,
		)

	return {
		"status": "marked",
		"attendance": attendance.name,
		"attendance_status": status,
		"working_hours": hours,
		"project": project,
		"in_time": str(in_time) if in_time else None,
		"out_time": str(out_time) if out_time else None,
	}


def mark_absent_without_punches(day, settings=None) -> int:
	"""Optional: Absent for site employees with no punches and no Attendance on a working day."""
	settings = settings or get_settings()
	day = getdate(day)
	start, end = day_bounds(day)
	marked = 0

	employees = frappe.get_all(
		"Employee",
		filters={"status": "Active", "custom_project": ["is", "set"], "date_of_joining": ["<=", day]},
		pluck="name",
	)
	for employee in employees:
		savepoint = "trident_mark_absent"
		frappe.db.savepoint(savepoint)
		try:
			if frappe.db.exists("Attendance", {"employee": employee, "attendance_date": day, "docstatus": ["<", 2]}):
				continue
			filters = {"employee": employee, "time": ["between", [start, end]]}
			filters.update(scope_filters(settings))
			if frappe.db.exists("Employee Checkin", filters):
				continue
			holiday_list = get_holiday_list_for_employee(employee, raise_exception=False)
			if holiday_list and is_holiday(holiday_list, day):
				continue
			# mark_attendance only swallows duplicate/overlap errors; anything else is per-employee.
			if mark_attendance(employee, day, "Absent"):
				marked += 1
		except Exception:
			_rollback(savepoint)
			frappe.clear_messages()
			frappe.log_error(title=f"trident_attendance: absent marking {employee} {day} failed")
	return marked


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _blocking_for(open_logs, day_reasons, settings) -> list[str]:
	reasons = []
	for l in open_logs:
		for r in strip_prefixes(split_reasons(l.custom_hold_reasons), DAY_PREFIXES):
			if r not in reasons:
				reasons.append(r)
	for r in day_reasons:
		if r not in reasons:
			reasons.append(r)
	return blocking_reasons(reasons, settings)


def _write_reasons(logs, day_reasons, status: str, reviewer: str | None = None, clear_reviewer: bool = False):
	for l in logs:
		own = strip_prefixes(split_reasons(l.custom_hold_reasons), DAY_PREFIXES)
		reasons = join_reasons(own + list(day_reasons))
		values = {}
		if reasons != (l.custom_hold_reasons or None):
			values["custom_hold_reasons"] = reasons
		if status != l.custom_review_status:
			values["custom_review_status"] = status
		if reviewer:
			values["custom_reviewed_by"] = reviewer
			values["custom_reviewed_on"] = now_datetime()
		elif clear_reviewer:
			values["custom_reviewed_by"] = None
			values["custom_reviewed_on"] = None
		if values:
			frappe.db.set_value("Employee Checkin", l.name, values, update_modified=False)
		l.custom_hold_reasons = reasons
		l.custom_review_status = status


def _create_auto_checkout(open_logs, day, settings) -> bool:
	ins = [l for l in open_logs if l.log_type == "IN"]
	if not ins:
		return False
	first_in = ins[0]
	out_time = combine_datetime(day, settings.auto_checkout_time or "17:00:00")
	if out_time <= get_datetime(first_in.time):
		return False
	create_internal_punch(
		employee=first_in.employee,
		log_type="OUT",
		time=out_time,
		project=first_in.custom_site_project,
		logged_by=first_in.custom_logged_by,
		source=f"{INTERNAL_SOURCE_PREFIX}auto-checkout",
	)
	return True


def create_internal_punch(employee, log_type, time, project=None, logged_by=None, source=None):
	doc = frappe.new_doc("Employee Checkin")
	doc.update(
		{
			"employee": employee,
			"log_type": log_type,
			"time": get_datetime(time),
			"custom_site_project": project,
			"custom_logged_by": logged_by,
			"custom_app_source": source or f"{INTERNAL_SOURCE_PREFIX}manual",
			"skip_auto_attendance": 1,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc
