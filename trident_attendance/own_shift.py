"""An employee sets their own shift for the days ahead, from My HR in the web app.

Shifts are irregular and many people clock in on a terminal, so nobody chooses a shift at
clock-in. Instead a person says ahead of time "nights from the 20th to the 31st", and the ERP
(work_day.py) and the attendance hub read their punches by it: the 19:00 scan is the IN and the
07:00 scan the next morning the OUT of the same work day. Whoever does not set one keeps the
day shift; that is on them.

What is stored is HRMS's own Shift Assignment, so HR sees and corrects it in the Desk as usual.
The rules that keep it honest are all here, on the server:

- only ahead: from today on, and today only while it has no punch yet. A day with punches is
  never changed, so a late arrival cannot be turned into an on-time night after the fact;
- only the company's Shift Types (the ones listed in Trident Attendance Settings, or all), never
  times typed in;
- a span of at most MAX_SPAN_DAYS, ending at most MAX_AHEAD_DAYS from today;
- never over a shift HR set: HR's assignments are left alone and the request is refused;
- only the caller's own Employee (Employee.user_id), never one named by the caller;
- every change is a submitted or cancelled Shift Assignment with a comment saying the employee
  set it in My HR (the Desk keeps who and when), and at most CHANGES_PER_DAY a day.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, getdate, nowdate

from trident_attendance import work_day
from trident_attendance.own_records import own_employees
from trident_attendance.utils import get_settings, require_signed_in

ASSIGNMENT = "Shift Assignment"
MAX_SPAN_DAYS = 31
MAX_AHEAD_DAYS = 62
CHANGES_PER_DAY = 10
COMMENT = "Set by the employee in My HR"


class OwnShiftError(frappe.ValidationError):
	pass


def _employee(user: str):
	"""The caller's active Employee: name, company, default shift."""
	for name in own_employees(user):
		row = frappe.db.get_value("Employee", name, ["name", "status", "company", "default_shift"], as_dict=True)
		if row and row.status == "Active":
			return row
	frappe.throw(_("Your login is not linked to an active employee record."), frappe.PermissionError)


def _enabled(settings) -> bool:
	return bool(cint(settings.get("own_shift_enabled")))


def choices(settings=None) -> list[str]:
	"""The Shift Types a person may pick: those listed in the settings (one per line), or all."""
	settings = settings or get_settings()
	listed = [line.strip() for line in str(settings.get("own_shift_types") or "").splitlines() if line.strip()]
	existing = frappe.get_all("Shift Type", pluck="name", order_by="name asc")
	return [name for name in existing if name in listed] if listed else existing


def _assignments(employee: str, from_date, to_date) -> list:
	"""Active, submitted assignments of this employee that touch [from_date, to_date]."""
	return frappe.db.sql(
		"""select name, shift_type, start_date, end_date, owner
		from `tabShift Assignment`
		where employee = %(employee)s and docstatus = 1 and status = 'Active'
			and start_date <= %(to)s and (end_date is null or end_date >= %(from)s)
		order by start_date""",
		{"employee": employee, "from": from_date, "to": to_date},
		as_dict=True,
	)


def _has_punches_since(employee: str, day) -> bool:
	"""Whether work day `day` (or a later one) has a punch. This morning's clock-out from last
	night's shift belongs to last night (work_day.py) and does not lock today."""
	day = getdate(day)
	times = frappe.get_all(
		"Employee Checkin", filters={"employee": employee, "time": [">=", f"{day} 00:00:00"]}, pluck="time"
	)
	if any(work_day.work_date_of(employee, t) >= day for t in times):
		return True
	# Punches reach the ERP's Employee Checkin only in the hub's nightly batch; the hub signals
	# each one at once (shift.punched). A punch signalled today locks today too, so a late arrival
	# cannot set a later shift before the night upload.
	signal = frappe.db.get_value("Shift Signal", {"employee": employee}, "at") if frappe.db.exists("DocType", "Shift Signal") else None
	return bool(signal) and work_day.work_date_of(employee, signal) >= day


def _changes_today(user: str) -> int:
	return frappe.db.count(
		"Comment",
		{
			"reference_doctype": ASSIGNMENT,
			"comment_type": "Info",
			"owner": user,
			"content": ["like", f"{COMMENT}%"],
			"creation": [">=", f"{nowdate()} 00:00:00"],
		},
	)


def _note(name: str, what: str):
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": ASSIGNMENT,
			"reference_name": name,
			"content": f"{COMMENT}: {what}",
		}
	).insert(ignore_permissions=True)


def _row(a, user) -> dict:
	return {
		"name": a.name,
		"shift_type": a.shift_type,
		"start_date": str(a.start_date),
		"end_date": str(a.end_date) if a.end_date else None,
		"mine": a.owner == user,
	}


@frappe.whitelist()
def get_own_shift() -> dict:
	"""What My HR's "My shift" page shows: the choices, the rules, and the caller's shifts from
	today to the furthest day they may set."""
	user = require_signed_in()
	settings = get_settings()
	emp = _employee(user)
	today = getdate(nowdate())
	last = add_days(today, MAX_AHEAD_DAYS)
	types = choices(settings)
	times = {
		t.name: t
		for t in frappe.get_all("Shift Type", filters={"name": ["in", types or [""]]}, fields=["name", "start_time", "end_time"])
	}
	return {
		"enabled": _enabled(settings),
		"today": str(today),
		"last_day": str(last),
		"max_span_days": MAX_SPAN_DAYS,
		"today_locked": _has_punches_since(emp.name, today),
		"default_shift": emp.default_shift,
		"choices": [
			{
				"name": name,
				"start": str(times[name].start_time)[:5] if name in times else None,
				"end": str(times[name].end_time)[:5] if name in times else None,
				"overnight": work_day.is_overnight(times.get(name)),
			}
			for name in types
		],
		"shifts": [_row(a, user) for a in _assignments(emp.name, today, last)],
	}


@frappe.whitelist(methods=["POST"])
def set_own_shift(from_date, to_date, shift_type=None) -> dict:
	"""The caller works `shift_type` from `from_date` to `to_date`; no shift_type = back to the
	usual shift for those days. Refused with the reason when a rule says no."""
	user = require_signed_in()
	settings = get_settings()
	if not _enabled(settings):
		frappe.throw(_("Choosing your own shift is turned off."), OwnShiftError)
	emp = _employee(user)
	today = getdate(nowdate())
	try:
		start, end = getdate(from_date), getdate(to_date)
	except Exception:
		frappe.throw(_("Choose the first and the last day."), OwnShiftError)
	shift_type = (shift_type or "").strip() or None

	if start < today:
		frappe.throw(_("A shift can only be set from today on."), OwnShiftError)
	if end < start:
		frappe.throw(_("The last day is before the first."), OwnShiftError)
	if date_diff(end, start) + 1 > MAX_SPAN_DAYS:
		frappe.throw(_("Set at most {0} days at a time.").format(MAX_SPAN_DAYS), OwnShiftError)
	if date_diff(end, today) > MAX_AHEAD_DAYS:
		frappe.throw(_("A shift can be set at most {0} days ahead.").format(MAX_AHEAD_DAYS), OwnShiftError)
	if start == today and _has_punches_since(emp.name, today):
		frappe.throw(_("You have already clocked today, so today's shift stays as it is. Start from tomorrow."), OwnShiftError)
	if shift_type and shift_type not in choices(settings):
		frappe.throw(_("{0} is not a shift you can choose.").format(shift_type), OwnShiftError)
	if _changes_today(user) >= CHANGES_PER_DAY:
		frappe.throw(_("Too many changes today. Try again tomorrow or ask HR."), OwnShiftError)

	touching = _assignments(emp.name, start, end)
	if any(a.owner != user for a in touching):
		frappe.throw(_("HR has set your shift on some of these days. Ask HR to change it."), OwnShiftError)

	# Make room: own assignments are cut back to the days outside [start, end].
	for a in touching:
		_cut(a, start, end)

	made = None
	if shift_type:
		doc = frappe.get_doc(
			{
				"doctype": ASSIGNMENT,
				"employee": emp.name,
				"company": emp.company,
				"shift_type": shift_type,
				"start_date": start,
				"end_date": end,
				"status": "Active",
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		doc.submit()
		_note(doc.name, f"{shift_type} from {start} to {end}")
		made = doc.name
	work_day.clear_cache()
	return {"ok": True, "assignment": made, **get_own_shift()}


def _cut(a, start, end):
	"""Leave this own assignment only on its days before `start` and after `end`."""
	keep_before = a.start_date < start
	keep_after = a.end_date is None or a.end_date > end
	doc = frappe.get_doc(ASSIGNMENT, a.name)
	doc.flags.ignore_permissions = True
	if keep_after:
		rest = frappe.get_doc(
			{
				"doctype": ASSIGNMENT,
				"employee": doc.employee,
				"company": doc.company,
				"shift_type": doc.shift_type,
				"start_date": add_days(end, 1),
				"end_date": a.end_date,
				"status": "Active",
			}
		)
		rest.flags.ignore_permissions = True
	if keep_before:
		# Its earlier days may already have been worked: they stay exactly as they were.
		doc.end_date = add_days(start, -1)
		doc.save()
		_note(doc.name, f"now ends {doc.end_date}")
	else:
		doc.cancel()
		_note(doc.name, "cancelled")
	if keep_after:
		rest.insert()
		rest.submit()
		_note(rest.name, f"{rest.shift_type} from {rest.start_date} to {rest.end_date or 'on'}")


@frappe.whitelist()
def shifts(from_date, to_date) -> dict:
	"""For the attendance hub: every Shift Type with its times, the active shift assignments
	(HR's and employees' own) that touch [from_date, to_date], each active employee's Default
	Shift (what HRMS falls back to on a day without an assignment) and the public holidays in
	the range, so the hub reads punches by the same shifts and days as the ERP. The hub's own
	login (or a System Manager) only."""
	from trident_attendance.shift import _require_hub_or_system_manager

	_require_hub_or_system_manager()
	start, end = getdate(from_date), getdate(to_date)
	if end < start or date_diff(end, start) > 2 * MAX_AHEAD_DAYS:
		frappe.throw(_("Ask for at most {0} days.").format(2 * MAX_AHEAD_DAYS), OwnShiftError)
	types = frappe.get_all(
		"Shift Type",
		fields=[
			"name",
			"start_time",
			"end_time",
			"begin_check_in_before_shift_start_time as before_min",
			"allow_check_out_after_shift_end_time as after_min",
		],
		order_by="name asc",
	)
	rows = frappe.db.sql(
		"""select sa.employee, e.attendance_device_id, sa.shift_type, sa.start_date, sa.end_date
		from `tabShift Assignment` sa join `tabEmployee` e on e.name = sa.employee
		where sa.docstatus = 1 and sa.status = 'Active'
			and sa.start_date <= %(to)s and (sa.end_date is null or sa.end_date >= %(from)s)
		order by sa.employee, sa.start_date""",
		{"from": start, "to": end},
		as_dict=True,
	)
	return {
		"shift_types": [
			{
				"name": t.name,
				"start": str(t.start_time),
				"end": str(t.end_time),
				"overnight": work_day.is_overnight(t),
				"before_min": cint(t.before_min) if t.before_min is not None else work_day.DEFAULT_WINDOW_MIN,
				"after_min": cint(t.after_min) if t.after_min is not None else work_day.DEFAULT_WINDOW_MIN,
			}
			for t in types
		],
		"assignments": [
			{
				"employee": r.employee,
				"attendance_device_id": r.attendance_device_id,
				"shift_type": r.shift_type,
				"start_date": str(r.start_date),
				"end_date": str(r.end_date) if r.end_date else None,
			}
			for r in rows
		],
		"defaults": _defaults(),
		"holidays": _holidays(start, end),
	}


def _defaults() -> list[dict]:
	"""Active employees with a Default Shift: the shift HRMS marks them by when no assignment
	covers a day."""
	return [
		{"employee": e.name, "attendance_device_id": e.attendance_device_id, "shift_type": e.default_shift}
		for e in frappe.get_all(
			"Employee",
			filters={"status": "Active", "default_shift": ["is", "set"]},
			fields=["name", "attendance_device_id", "default_shift"],
			order_by="name asc",
		)
	]


def _holidays(start, end) -> list[dict]:
	"""Public holidays in [start, end] from the Holiday Lists the active employees go by (their
	own, else their company's default). Weekly offs are left out: the hub knows a shift's work
	days itself. One row per date."""
	lists = set(
		frappe.get_all("Employee", filters={"status": "Active", "holiday_list": ["is", "set"]}, pluck="holiday_list")
	)
	# HRMS v16 assigns holiday lists by Holiday List Assignment (to an employee or a company)
	# rather than Employee.holiday_list; v15 has no such doctype.
	if frappe.db.exists("DocType", "Holiday List Assignment"):
		lists.update(frappe.get_all("Holiday List Assignment", filters={"docstatus": 1}, pluck="holiday_list"))
	companies = set(frappe.get_all("Employee", filters={"status": "Active"}, pluck="company"))
	for company in companies:
		default = frappe.db.get_value("Company", company, "default_holiday_list")
		if default:
			lists.add(default)
	if not lists:
		return []
	out: dict[str, str] = {}
	for h in frappe.get_all(
		"Holiday",
		filters={
			"parent": ["in", sorted(lists)],
			"parenttype": "Holiday List",
			"holiday_date": ["between", [start, end]],
			"weekly_off": 0,
		},
		fields=["holiday_date", "description"],
		order_by="holiday_date asc",
	):
		day = str(h.holiday_date)
		out.setdefault(day, frappe.utils.strip_html(h.description or "").strip()[:80] or "Holiday")
	return [{"date": d, "name": n} for d, n in sorted(out.items())]
