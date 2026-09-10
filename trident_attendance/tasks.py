"""Turning app check-ins into Attendance, plus scheduled maintenance.

Runs as app hooks (not Server Scripts) so it behaves the same on Frappe Cloud, where Server
Scripts are disabled bench-wide.

A "day" is an employee's in-scope punches on one calendar date. Once the day is complete
(yesterday, or today after the cutoff time) the day rules run; a clean day is released and
marked straight away; anything with a blocking reason waits on /attendance-review.
Working hours are first IN to last OUT, using hrms's own calculate_working_hours so the
numbers match what hrms would have produced for a shift.
"""

import time as _time

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, getdate, now_datetime, nowdate
from frappe.utils.synchronization import LockTimeoutError, filelock

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
	NO_SUPERVISOR_EMPLOYEE,
	OUT_BEFORE_IN,
	blocking_reasons,
	evaluate_day,
	evaluate_face,
	evaluate_project,
	is_internal,
)
from trident_attendance.trident_attendance.doctype.trident_attendance_run.trident_attendance_run import record_run
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
	"latitude",
	"longitude",
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
REJECTED_PREFIX = "Attendance rejected:"
# Held days older than this stop being re-evaluated every hour; a reviewer can still act on them.
LOOKBACK_DAYS = 45
RUN_LOCK = "trident_attendance_finalise"


# ---------------------------------------------------------------------------
# Scheduled entry points
# ---------------------------------------------------------------------------


def finalise_days(force_today: bool = False, up_to=None, trigger: str = "Scheduler") -> dict:
	"""Hourly. Evaluate every complete day that still has pending punches."""
	started = _time.monotonic()
	settings = get_settings()
	end_date = getdate(up_to) if up_to else last_complete_date(settings, force_today)
	summary = {"up_to": str(end_date), "days": 0, "marked": 0, "held": 0, "errors": 0, "results": []}

	try:
		with filelock(RUN_LOCK, timeout=5):
			_finalise_days(settings, end_date, summary, commit_each=(trigger == "Scheduler"))
	except LockTimeoutError:
		summary["skipped"] = _("Another processing run is still going.")
		return summary

	record_run(trigger, summary, _time.monotonic() - started)
	frappe.db.commit()  # nosemgrep
	return summary


def _finalise_days(settings, end_date, summary, commit_each):
	filters = {
		"custom_review_status": STATUS_PENDING,
		"attendance": ["is", "not set"],
		"time": ["between", [f"{add_days(end_date, -LOOKBACK_DAYS)} 00:00:00", f"{end_date} 23:59:59"]],
	}
	filters.update(scope_filters(settings))
	pending = frappe.get_all("Employee Checkin", filters=filters, fields=["employee", "time"], order_by="employee, time")
	days = sorted({(p.employee, getdate(p.time)) for p in pending})
	summary["days"] = len(days)

	for employee, day in days:
		result = finalise_group(employee, day, settings)
		summary["results"].append(result)
		if result["status"] == "error":
			summary["errors"] += 1
			if result.get("fatal"):
				summary["aborted"] = result.get("message")
				break
		elif result["status"] in ("marked", "held"):
			summary[result["status"]] += 1
		if commit_each:
			frappe.db.commit()  # nosemgrep -- scheduler job; keep progress if a later group fails

	if cint(settings.mark_absent_without_punches):
		# Never absent-mark today: the offline queue may still deliver punches.
		absent_day = min(end_date, add_days(getdate(nowdate()), -1))
		summary["absent"] = mark_absent_without_punches(absent_day, settings)


def purge_attendance_photos():
	"""Daily. Drop face photos older than the retention window; keep the check-in row."""
	settings = get_settings()
	retention = cint(settings.photo_retention_days) or 30
	cutoff = add_days(now_datetime(), -retention)
	deleted = 0
	batch = 200

	while True:
		files = frappe.db.sql(
			"""select f.name, f.attached_to_name
			from `tabFile` f
			join `tabEmployee Checkin` c on c.name = f.attached_to_name
			where f.attached_to_doctype = 'Employee Checkin' and c.time < %s
			limit %s""",
			(cutoff, batch),
			as_dict=True,
		)
		if not files:
			break
		for f in files:
			frappe.delete_doc("File", f.name, ignore_permissions=True, delete_permanently=True)
			deleted += 1
		for name in {f.attached_to_name for f in files}:
			frappe.db.set_value("Employee Checkin", name, "custom_attendance_photo", None, update_modified=False)
		frappe.db.commit()  # nosemgrep
		if len(files) < batch:
			break

	orphans = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Employee Checkin",
			"attached_to_name": ["is", "not set"],
			"creation": ["<", cutoff],
		},
		pluck="name",
		limit=batch,
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


def get_day_logs(employee: str, day, settings=None, include_rejected: bool = False, for_update: bool = False) -> list:
	start, end = day_bounds(day)
	filters = {"employee": employee, "time": ["between", [start, end]]}
	if not include_rejected:
		filters["custom_review_status"] = ["!=", STATUS_REJECTED]
	filters.update(scope_filters(settings or get_settings()))
	# for_update serialises the hourly job against a reviewer releasing the same day; the
	# values are read under the lock, so neither side works from a stale snapshot.
	return frappe.get_all(
		"Employee Checkin", filters=filters, fields=PUNCH_FIELDS, order_by="time asc", for_update=for_update
	)


def finalise_group(employee: str, day, settings=None, released_by: str | None = None, force: bool = False) -> dict:
	"""Evaluate one employee-day. Releases + marks it when clean (or when a reviewer forces it)."""
	settings = settings or get_settings()
	day = getdate(day)
	base = {"employee": employee, "date": str(day)}
	savepoint = "trident_finalise_group"
	frappe.db.savepoint(savepoint)
	try:
		logs = get_day_logs(employee, day, settings, for_update=True)
		logs = _heal(logs)
		open_logs = [l for l in logs if l.custom_review_status in OPEN_STATUSES and not l.attendance]
		if not open_logs:
			return {**base, "status": "nothing", "message": _("No open check-ins on this day.")}

		if _rebuild_if_late_punches(employee, day, logs, settings):
			logs = get_day_logs(employee, day, settings)
			open_logs = [l for l in logs if l.custom_review_status in OPEN_STATUSES and not l.attendance]

		instant = {l.name: _refresh_instant(l, settings) for l in open_logs}
		day_reasons = evaluate_day(logs, day, settings)
		blocking = _blocking_for(instant, day_reasons, settings)

		if cint(settings.auto_checkout) and not released_by and MISSING_OUT in day_reasons and not [b for b in blocking if b != MISSING_OUT]:
			if _create_auto_checkout(open_logs, day, settings):
				logs = get_day_logs(employee, day, settings)
				open_logs = [l for l in logs if l.custom_review_status in OPEN_STATUSES and not l.attendance]
				instant = {l.name: _refresh_instant(l, settings) for l in open_logs}
				day_reasons = evaluate_day(logs, day, settings)
				blocking = _blocking_for(instant, day_reasons, settings)

		if blocking and not force:
			_write_reasons(open_logs, instant, day_reasons, status=STATUS_PENDING)
			return {**base, "status": "held", "reasons": blocking}

		if not force and not cint(settings.auto_release_clean_punches):
			_write_reasons(open_logs, instant, day_reasons, status=STATUS_PENDING)
			return {**base, "status": "held", "reasons": [_("Automatic release is switched off")]}

		already = [r for r in day_reasons if r.startswith(ALREADY_MARKED_PREFIX)]
		if already:
			_write_reasons(open_logs, instant, day_reasons, status=STATUS_PENDING)
			return {**base, "status": "held", "reasons": already}

		# A forced release still needs a usable IN/OUT pair; hours cannot be invented.
		unpaired = [r for r in day_reasons if r in PAIRING_REASONS]
		if unpaired:
			_write_reasons(open_logs, instant, day_reasons, status=STATUS_PENDING)
			return {
				**base,
				"status": "held",
				"reasons": unpaired,
				"message": _("Add the missing punch (Add OUT) or reject the day before releasing it."),
			}

		status = STATUS_RELEASED if released_by else STATUS_AUTO_RELEASED
		_write_reasons(open_logs, instant, day_reasons, status=status, reviewer=released_by or "Administrator")
		result = mark_day(open_logs, day, settings, day_reasons=day_reasons, instant=instant)
		return {**base, **result}
	except frappe.QueryDeadlockError as e:
		_rollback(savepoint)
		return {**base, "status": "error", "message": str(e), "fatal": True}
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


def _heal(logs):
	"""Restore the two invariants a stale Desk save can break."""
	for l in logs:
		if l.attendance and l.custom_review_status != STATUS_MARKED:
			frappe.db.set_value("Employee Checkin", l.name, "custom_review_status", STATUS_MARKED)
			l.custom_review_status = STATUS_MARKED
		elif not l.attendance and l.custom_review_status in (STATUS_RELEASED, STATUS_AUTO_RELEASED):
			frappe.db.set_value("Employee Checkin", l.name, "custom_review_status", STATUS_PENDING)
			l.custom_review_status = STATUS_PENDING
	return logs


def _rebuild_if_late_punches(employee, day, logs, settings) -> bool:
	"""A punch that arrives after this app already marked the day: cancel ours and rebuild."""
	linked = [l for l in logs if l.attendance]
	fresh = [l for l in logs if not l.attendance and l.custom_review_status in OPEN_STATUSES]
	if not linked or not fresh:
		return False
	attendance_name = linked[0].attendance
	att = frappe.db.get_value("Attendance", attendance_name, ["docstatus", "leave_type", "status"], as_dict=True)
	if not att or att.docstatus != 1 or att.leave_type or att.status == "On Leave":
		return False
	doc = frappe.get_doc("Attendance", attendance_name)
	doc.flags.ignore_permissions = True
	doc.add_comment("Comment", _("Cancelled by Trident Attendance: a later check-in arrived for this day; rebuilding."))
	doc.cancel()
	return True


def _refresh_instant(log, settings) -> list[str]:
	"""Instant reasons re-evaluated on every pass, so fixing a project's GPS or a supervisor's
	Employee link clears the reason without touching the punch."""
	if is_internal(log):
		return []
	reasons = evaluate_face(log) + evaluate_project(log, settings)
	if not log.custom_logged_by:
		reasons.append(NO_SUPERVISOR_EMPLOYEE)
	# evaluate_project stores the distance on the dict; persist it when it changed.
	distance = log.get("custom_distance_from_site")
	if distance is not None:
		frappe.db.set_value("Employee Checkin", log.name, "custom_distance_from_site", distance, update_modified=False)
	return reasons


def mark_day(logs: list, day, settings=None, day_reasons=None, instant=None) -> dict:
	"""Create + submit one Attendance for these punches and link them."""
	settings = settings or get_settings()
	day_reasons = list(day_reasons or [])
	instant = instant or {}
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
		_write_reasons(logs, instant, kept + [reason], status=STATUS_PENDING, clear_reviewer=True)
		return {"status": "held", "reasons": [reason]}
	except frappe.ValidationError as e:
		# hrms refused the Attendance (inactive employee, date before joining, ...): hold with the message.
		_rollback(savepoint)
		frappe.clear_messages()
		reason = f"{REJECTED_PREFIX} {str(e)[:140]}"
		kept = [r for r in day_reasons if not r.startswith(REJECTED_PREFIX)]
		_write_reasons(logs, instant, kept + [reason], status=STATUS_PENDING, clear_reviewer=True)
		return {"status": "held", "reasons": [reason]}

	for l in logs:
		frappe.db.set_value("Employee Checkin", l.name, {"attendance": attendance.name, "custom_review_status": STATUS_MARKED})

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


def _blocking_for(instant: dict, day_reasons, settings) -> list[str]:
	reasons = []
	for rs in instant.values():
		for r in rs:
			if r not in reasons:
				reasons.append(r)
	for r in day_reasons:
		if r not in reasons:
			reasons.append(r)
	return blocking_reasons(reasons, settings)


def _write_reasons(logs, instant: dict, day_reasons, status: str, reviewer: str | None = None, clear_reviewer: bool = False):
	for l in logs:
		own = instant.get(l.name)
		if own is None:
			# Not re-evaluated this pass (e.g. duplicate path): keep whatever instant reasons it had.
			own = [r for r in split_reasons(l.custom_hold_reasons) if not r.startswith(DAY_PREFIXES + (REJECTED_PREFIX,))]
		reasons = join_reasons(list(own) + list(day_reasons))
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
			frappe.db.set_value("Employee Checkin", l.name, values)
		l.custom_hold_reasons = reasons
		l.custom_review_status = status


def _create_auto_checkout(open_logs, day, settings) -> bool:
	ins = [l for l in open_logs if l.log_type == "IN"]
	if not ins:
		return False
	first_in = ins[0]
	out_time = combine_datetime(day, settings.auto_checkout_time or "17:00:00")
	if out_time <= get_datetime(first_in.time) or out_time > now_datetime():
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
	doc.flags.trident_internal = True
	doc.insert()
	return doc
