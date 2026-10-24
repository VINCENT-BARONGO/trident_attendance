"""Casual Attendance: one row per casual labourer per day, from their Casual Checkins.

Casuals are not Employees, so they get no Attendance record; this report is their attendance. A day
with an IN and a later OUT is Present (never "Half day"); a day with only one of them is flagged so
the supervisor can be asked. Hours run from the first IN to the last OUT.
"""

import frappe
from frappe import _
from frappe.utils import add_days, get_datetime, getdate, nowdate

PRESENT = "Present"
NO_OUT = "Missing check-out"
NO_IN = "Missing check-in"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	to_date = getdate(filters.to_date or nowdate())
	from_date = getdate(filters.from_date) if filters.from_date else add_days(to_date, -6)
	return columns(), rows(from_date, to_date, filters)


def columns():
	return [
		{"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 100},
		{"fieldname": "casual_labourer", "label": _("Casual"), "fieldtype": "Link", "options": "Casual Labourer", "width": 110},
		{"fieldname": "full_name", "label": _("Name"), "fieldtype": "Data", "width": 180},
		{"fieldname": "id_number", "label": _("ID Number"), "fieldtype": "Data", "width": 110},
		{"fieldname": "subcontractor_name", "label": _("Subcontractor"), "fieldtype": "Data", "width": 160},
		{"fieldname": "project", "label": _("Project"), "fieldtype": "Link", "options": "Project", "width": 140},
		{"fieldname": "first_in", "label": _("First In"), "fieldtype": "Time", "width": 90},
		{"fieldname": "last_out", "label": _("Last Out"), "fieldtype": "Time", "width": 90},
		{"fieldname": "hours", "label": _("Hours"), "fieldtype": "Float", "precision": 2, "width": 80},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 140},
	]


def rows(from_date, to_date, filters):
	conditions = {"time": ["between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]]}
	if filters.casual_labourer:
		conditions["casual_labourer"] = filters.casual_labourer
	if filters.project:
		conditions["project"] = filters.project
	punches = frappe.get_all(
		"Casual Checkin",
		filters=conditions,
		fields=["casual_labourer", "full_name", "id_number", "log_type", "time", "project"],
		order_by="time asc",
	)
	people = labourers({p.casual_labourer for p in punches})
	days = {}
	for p in punches:
		key = (getdate(p.time), p.casual_labourer)
		day = days.setdefault(key, {"ins": [], "outs": [], "projects": [], "punch": p})
		(day["ins"] if p.log_type == "IN" else day["outs"]).append(get_datetime(p.time))
		if p.project:
			day["projects"].append(p.project)

	out = []
	for (date, casual), day in sorted(days.items(), key=lambda kv: (kv[0][0], kv[1]["punch"].full_name or kv[0][1])):
		who = people.get(casual, {})
		if filters.subcontractor and who.get("subcontractor") != filters.subcontractor:
			continue
		first_in = min(day["ins"]) if day["ins"] else None
		last_out = max(day["outs"]) if day["outs"] else None
		if first_in and last_out and last_out > first_in:
			status, hours = PRESENT, round((last_out - first_in).total_seconds() / 3600, 2)
		else:
			status, hours = (NO_OUT if first_in else NO_IN), None
		if filters.status and filters.status != status:
			continue
		p = day["punch"]
		out.append({
			"date": date,
			"casual_labourer": casual,
			"full_name": who.get("full_name") or p.full_name,
			"id_number": who.get("id_number") or p.id_number,
			"subcontractor_name": who.get("subcontractor_name") or "",
			"project": max(set(day["projects"]), key=day["projects"].count) if day["projects"] else None,
			"first_in": first_in.time() if first_in else None,
			"last_out": last_out.time() if last_out else None,
			"hours": hours,
			"status": status,
		})
	return out


def labourers(names):
	"""{name: {full_name, id_number, subcontractor, subcontractor_name}} for these casuals."""
	if not names:
		return {}
	fields = ["name", "full_name", "id_number"]
	if frappe.get_meta("Casual Labourer").has_field("subcontractor"):
		fields += ["subcontractor", "subcontractor_name"]
	return {r.name: r for r in frappe.get_all("Casual Labourer", filters={"name": ["in", list(names)]}, fields=fields)}
