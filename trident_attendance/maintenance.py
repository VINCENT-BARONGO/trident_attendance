"""Clean-ups a System Manager runs by hand. Each one is a dry run unless `apply` is set.

From the browser console on the Desk:

	frappe.call({method: "trident_attendance.maintenance.close_stale_reviews", args: {apply: 0}})

or on the bench:

	bench --site <site> execute trident_attendance.maintenance.close_stale_reviews --kwargs "{'apply': 0}"
"""

from collections import Counter

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime, nowdate

from trident_attendance import work_day
from trident_attendance.checkin_rules import ALREADY_MARKED_PREFIX, REJECTED_PREFIX
from trident_attendance.tasks import LOOKBACK_DAYS
from trident_attendance.utils import (
	STATUS_MARKED,
	STATUS_PENDING,
	STATUS_REJECTED,
	get_settings,
	join_reasons,
	scope_filters,
	split_reasons,
)

# Every reason written here carries TAG, so reopen_closed_reviews finds exactly these rows.
TAG = "clean-up"
SETTLED = ("Present", "Half Day", "Work From Home")
LINKED_REASON = "Closed in " + TAG + ": the day already has {0} ({1})"
IMPORTED_REASON = f"{REJECTED_PREFIX} closed in {TAG}: never reviewed, older than the review window"
CHUNK = 500


@frappe.whitelist(methods=["POST"])
def close_stale_reviews(before=None, apply=0, employee=None) -> dict:
	"""Clear the Pending Review check-ins nobody will act on, among those before `before`
	(default: a week ago), for one `employee` or everyone. Two kinds are closed:

	- linked: the punch's work day already has a submitted Present / Half Day / Work From Home
	  Attendance, most often one HR marked by hand before the app went live. The day is settled,
	  so the hourly run holds the punch as "Attendance already marked" for good. It is linked to
	  that Attendance and Marked, as if the app had marked the day; cancelling the Attendance puts
	  it back in review (attendance_hooks.before_cancel), so the day can still be rebuilt from it.
	- rejected: no hold reason at all and older than the hourly run's look-back: a check-in
	  imported before the app was installed, never evaluated and never going to be.

	Everything else (a missing OUT, a geofence hold, a day marked Absent or On Leave that has
	punches) is left for a reviewer and only counted.
	"""
	_require_system_manager()
	apply = cint(apply)
	today = getdate(nowdate())
	before = getdate(before) if before else add_days(today, -7)
	window_start = add_days(today, -LOOKBACK_DAYS)

	filters = {"custom_review_status": STATUS_PENDING, "attendance": ["is", "not set"], "time": ["<", before]}
	filters.update(scope_filters(get_settings()))
	if employee:
		filters["employee"] = employee
	rows = frappe.get_all(
		"Employee Checkin",
		filters=filters,
		fields=["name", "employee", "time", "custom_hold_reasons"],
		order_by="time",
	)
	work_day.clear_cache()
	day_of = {r.name: getdate(work_day.work_date_of(r.employee, r.time)) for r in rows}
	settled = _settled_days({(r.employee, day_of[r.name]) for r in rows})

	out = {"apply": apply, "before": str(before), "checked": len(rows), "linked": 0, "rejected": 0, "left": 0}
	linked_by_month, rejected_by_month, left_reasons = Counter(), Counter(), Counter()
	rejected = []
	for r in rows:
		day = day_of[r.name]
		reasons = split_reasons(r.custom_hold_reasons)
		attendance = settled.get((r.employee, day))
		if attendance:
			out["linked"] += 1
			linked_by_month[str(day)[:7]] += 1
			if apply:
				frappe.db.set_value(
					"Employee Checkin",
					r.name,
					{
						"attendance": attendance.name,
						"custom_review_status": STATUS_MARKED,
						"custom_hold_reasons": join_reasons(
							[*reasons, LINKED_REASON.format(attendance.name, attendance.status)]
						),
						"custom_reviewed_by": frappe.session.user,
						"custom_reviewed_on": now_datetime(),
					},
				)
		elif not reasons and day < window_start:
			out["rejected"] += 1
			rejected_by_month[str(day)[:7]] += 1
			rejected.append(r.name)
		else:
			out["left"] += 1
			left_reasons[_kind(reasons[0]) if reasons else _("no reason yet")] += 1

	if apply:
		for i in range(0, len(rejected), CHUNK):
			frappe.db.set_value(
				"Employee Checkin",
				{"name": ["in", rejected[i : i + CHUNK]]},
				{
					"custom_review_status": STATUS_REJECTED,
					"custom_hold_reasons": IMPORTED_REASON,
					"custom_reviewed_by": frappe.session.user,
					"custom_reviewed_on": now_datetime(),
				},
			)
	out["linked_by_month"] = dict(sorted(linked_by_month.items()))
	out["rejected_by_month"] = dict(sorted(rejected_by_month.items()))
	out["left_by_reason"] = dict(left_reasons.most_common(10))
	return out


@frappe.whitelist(methods=["POST"])
def reopen_closed_reviews(apply=0) -> dict:
	"""Undo close_stale_reviews: every check-in it closed goes back to Pending Review, unlinked,
	without the reason it added. A dry run unless `apply`."""
	_require_system_manager()
	rows = frappe.get_all(
		"Employee Checkin",
		filters={
			"custom_hold_reasons": ["like", f"%{TAG}%"],
			"custom_review_status": ["in", [STATUS_MARKED, STATUS_REJECTED]],
		},
		fields=["name", "custom_review_status", "custom_hold_reasons"],
	)
	if cint(apply):
		for r in rows:
			values = {
				"custom_review_status": STATUS_PENDING,
				"custom_hold_reasons": join_reasons(
					[x for x in split_reasons(r.custom_hold_reasons) if TAG not in x]
				),
				"custom_reviewed_by": None,
				"custom_reviewed_on": None,
			}
			if r.custom_review_status == STATUS_MARKED:
				values["attendance"] = None
			frappe.db.set_value("Employee Checkin", r.name, values)
	return {"apply": cint(apply), "reopened": len(rows)}


def _require_system_manager():
	# Not frappe.only_for: that lets everyone through while tests run, so no test could hold it.
	if "System Manager" not in frappe.get_roles():
		frappe.throw(_("Only a System Manager can run this clean-up."), frappe.PermissionError)


def _kind(reason: str) -> str:
	"""A hold reason without its particulars, to count alike ones together:
	"Attendance already marked: HR-ATT-2026-1 (Absent)" -> "Attendance already marked: Absent",
	"Outside geofence (990m, allowed 400m)" -> "Outside geofence"."""
	if reason.startswith(ALREADY_MARKED_PREFIX):
		return f"{ALREADY_MARKED_PREFIX} {reason.rsplit('(', 1)[-1].rstrip(')')}"
	return reason.split("(")[0].strip()[:60]


def _settled_days(pairs: set) -> dict:
	"""{(employee, date): Attendance} for the submitted Present / Half Day / WFH ones among them."""
	if not pairs:
		return {}
	days = [d for _e, d in pairs]
	rows = frappe.get_all(
		"Attendance",
		filters={
			"employee": ["in", list({e for e, _d in pairs})],
			"attendance_date": ["between", [min(days), max(days)]],
			"docstatus": 1,
			"status": ["in", SETTLED],
		},
		fields=["name", "employee", "attendance_date", "status"],
	)
	return {
		(a.employee, getdate(a.attendance_date)): a
		for a in rows
		if (a.employee, getdate(a.attendance_date)) in pairs
	}
