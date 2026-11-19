"""Tender Performance: the year's tenders by month, against the two yearly targets the Contract Procurement
Manager is measured on - value of tenders submitted and value of work won.

One row per month of the chosen year, then the summary reads the year to date against the targets. The
targets are filters, so the owner can change them without a code change; the defaults are KSh 3 billion
submitted and KSh 700 million won."""

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

DEFAULT_SUBMITTED_TARGET = 3_000_000_000
DEFAULT_WON_TARGET = 700_000_000
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	year = int(filters.year or getdate(nowdate()).year)
	data, totals = rows(year, filters)
	return columns(), data, None, None, summary(totals, filters)


def columns():
	return [
		{"fieldname": "month", "label": _("Month"), "fieldtype": "Data", "width": 110},
		{"fieldname": "submitted", "label": _("Tenders Submitted"), "fieldtype": "Int", "width": 130},
		{"fieldname": "submitted_value", "label": _("Value Submitted"), "fieldtype": "Currency", "width": 150},
		{"fieldname": "on_time", "label": _("On Time"), "fieldtype": "Int", "width": 90},
		{"fieldname": "late", "label": _("After Closing"), "fieldtype": "Int", "width": 110},
		{"fieldname": "won", "label": _("Won"), "fieldtype": "Int", "width": 70},
		{"fieldname": "lost", "label": _("Lost"), "fieldtype": "Int", "width": 70},
		{"fieldname": "won_value", "label": _("Value Won"), "fieldtype": "Currency", "width": 150},
	]


def rows(year, filters):
	conditions = {"submitted_on": ["between", [f"{year}-01-01", f"{year}-12-31"]], "status": ["in", ["Submitted", "Won", "Lost"]]}
	for field in ("company", "prepared_by"):
		if filters.get(field):
			conditions[field] = filters.get(field)
	tenders = frappe.get_list(
		"Tender",
		filters=conditions,
		fields=["name", "submitted_on", "submitted_on_time", "status", "tender_value", "value_won", "result_date"],
		limit_page_length=0,
	)
	months = [
		{"month": _(m), "submitted": 0, "submitted_value": 0.0, "on_time": 0, "late": 0, "won": 0, "lost": 0, "won_value": 0.0}
		for m in MONTHS
	]
	for t in tenders:
		m = months[getdate(t.submitted_on).month - 1]
		m["submitted"] += 1
		m["submitted_value"] += flt(t.tender_value)
		m["on_time" if t.submitted_on_time else "late"] += 1
		# A win is counted in the month the result came, when that is in the same year; else where it was sent.
		if t.status in ("Won", "Lost"):
			when = getdate(t.result_date) if t.result_date and getdate(t.result_date).year == year else getdate(t.submitted_on)
			r = months[when.month - 1]
			if t.status == "Won":
				r["won"] += 1
				r["won_value"] += flt(t.value_won)
			else:
				r["lost"] += 1
	totals = {k: sum(m[k] for m in months) for k in ("submitted", "submitted_value", "on_time", "late", "won", "lost", "won_value")}
	return months, totals


def summary(totals, filters):
	submitted_target = flt(filters.submitted_target) or DEFAULT_SUBMITTED_TARGET
	won_target = flt(filters.won_target) or DEFAULT_WON_TARGET
	decided = totals["won"] + totals["lost"]

	def pct(part, whole):
		return round(100 * part / whole, 1) if whole else 0

	def colour(value, target):
		return "Green" if value >= target else "Red"

	return [
		{"label": _("Value submitted"), "value": totals["submitted_value"], "datatype": "Currency",
		 "indicator": colour(totals["submitted_value"], submitted_target)},
		{"label": _("Of the submitted target"), "value": pct(totals["submitted_value"], submitted_target), "datatype": "Percent",
		 "indicator": colour(totals["submitted_value"], submitted_target)},
		{"label": _("Value won"), "value": totals["won_value"], "datatype": "Currency", "indicator": colour(totals["won_value"], won_target)},
		{"label": _("Of the won target"), "value": pct(totals["won_value"], won_target), "datatype": "Percent",
		 "indicator": colour(totals["won_value"], won_target)},
		{"label": _("Submitted on time"), "value": pct(totals["on_time"], totals["submitted"]), "datatype": "Percent", "indicator": "Blue"},
		{"label": _("Win rate (decided tenders)"), "value": pct(totals["won"], decided), "datatype": "Percent", "indicator": "Blue"},
	]
