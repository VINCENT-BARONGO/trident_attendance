"""Which work day a punch belongs to, for people on a shift that runs past midnight.

A day is a calendar date, 00:00 to 23:59, unless the employee has a Shift Assignment that day
whose Shift Type ends at or before it starts (a night shift, 19:00 to 07:00). Such a work day
runs on to the shift's end the next morning, plus the Shift Type's own "Allow check-out after
shift end time" (HRMS's field, 60 minutes by default). The 07:00 clock-out then belongs to the night it ends, not to the day
it falls on, and the next calendar day starts after that window.

Shift Assignments are HRMS's own records: HR sets them in the Desk, an employee sets their own
in My HR (own_shift.py). Nobody's day changes without one.
"""

from datetime import timedelta

import frappe
from frappe.utils import add_days, cint, get_datetime, getdate, now_datetime

from trident_attendance.utils import combine_datetime, day_bounds

DEFAULT_WINDOW_MIN = 60


def _cache() -> dict:
	"""One answer per (employee, date) for the rest of the request or job."""
	if not hasattr(frappe.local, "trident_work_day"):
		frappe.local.trident_work_day = {}
	return frappe.local.trident_work_day


def clear_cache():
	if hasattr(frappe.local, "trident_work_day"):
		del frappe.local.trident_work_day


def assignment_on(employee: str, day):
	"""The active, submitted Shift Assignment covering this date, with its Shift Type's times;
	None without one."""
	day = getdate(day)
	key = (employee, day)
	cache = _cache()
	if key in cache:
		return cache[key]
	rows = frappe.db.sql(
		"""select sa.name, sa.shift_type, st.start_time, st.end_time,
			st.begin_check_in_before_shift_start_time as before_min,
			st.allow_check_out_after_shift_end_time as after_min
		from `tabShift Assignment` sa join `tabShift Type` st on st.name = sa.shift_type
		where sa.employee = %(employee)s and sa.docstatus = 1 and sa.status = 'Active'
			and sa.start_date <= %(day)s and (sa.end_date is null or sa.end_date >= %(day)s)
		order by sa.start_date desc, sa.creation desc limit 1""",
		{"employee": employee, "day": day},
		as_dict=True,
	)
	cache[key] = rows[0] if rows else None
	return cache[key]


def _seconds(value) -> int:
	if isinstance(value, timedelta):
		return int(value.total_seconds())
	parts = [int(float(p)) for p in str(value or "0:0:0").split(":")] + [0, 0]
	return parts[0] * 3600 + parts[1] * 60 + parts[2]


def is_overnight(row) -> bool:
	return bool(row) and _seconds(row.end_time) <= _seconds(row.start_time)


def night_on(employee: str, day):
	"""The night shift that starts on this date, or None."""
	row = assignment_on(employee, day)
	return row if is_overnight(row) else None


def _window(row, field) -> timedelta:
	value = row.get(field)
	return timedelta(minutes=DEFAULT_WINDOW_MIN if value is None else cint(value))


def night_bounds(row, day):
	"""(first, last) instant of a night work day that starts on `day`."""
	day = getdate(day)
	start = combine_datetime(day, row.start_time) - _window(row, "before_min")
	end = combine_datetime(add_days(day, 1), row.end_time) + _window(row, "after_min")
	return start, end


def bounds(employee: str, day) -> tuple[str, str]:
	"""The first and last instant of this employee's work day `day`, as strings for a query."""
	day = getdate(day)
	start, end = (get_datetime(t) for t in day_bounds(day))
	# The morning still belongs to last night until its check-out window has passed.
	before = night_on(employee, add_days(day, -1))
	if before:
		start = max(start, night_bounds(before, add_days(day, -1))[1] + timedelta(seconds=1))
	# A night that starts today runs on to tomorrow morning. Earlier punches today stay with it
	# (a person on nights who scans in the afternoon is still this work day, not lost).
	night = night_on(employee, day)
	if night:
		end = night_bounds(night, day)[1]
	return str(start), str(end)


def work_date_of(employee: str, time):
	"""The work day a punch at `time` belongs to: the night before when it falls inside that
	night's check-out window, else its own date."""
	time = get_datetime(time)
	day = getdate(time)
	before = night_on(employee, add_days(day, -1))
	if before and time <= night_bounds(before, add_days(day, -1))[1]:
		return add_days(day, -1)
	return day


def is_complete(employee: str, day, now=None) -> bool:
	"""A night work day can be marked only once its check-out window has passed."""
	night = night_on(employee, day)
	if not night:
		return True
	return get_datetime(now or now_datetime()) > night_bounds(night, day)[1]
