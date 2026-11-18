"""Retention Due: retention the clients are holding that has not been claimed back, by the date it can be
claimed. Retention is the company's money; it does not come back unless it is asked for.

By default it lists what is already due and what falls due in the next 60 days; "Show all" lists every
unclaimed retention, including those with no release date entered (which is itself the gap to close)."""

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate, nowdate


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return columns(), rows(filters)


def columns():
	return [
		{"fieldname": "name", "label": _("Certificate"), "fieldtype": "Link", "options": "Payment Certificate", "width": 150},
		{"fieldname": "project", "label": _("Project"), "fieldtype": "Link", "options": "Project", "width": 130},
		{"fieldname": "project_name", "label": _("Project Name"), "fieldtype": "Data", "width": 200},
		{"fieldname": "customer", "label": _("Customer"), "fieldtype": "Link", "options": "Customer", "width": 160},
		{"fieldname": "valuation_no", "label": _("Valuation"), "fieldtype": "Int", "width": 80},
		{"fieldname": "retention_amount", "label": _("Retention Held"), "fieldtype": "Currency", "width": 130},
		{"fieldname": "retention_release_date", "label": _("Due On"), "fieldtype": "Date", "width": 100},
		{"fieldname": "days_overdue", "label": _("Days Overdue"), "fieldtype": "Int", "width": 100},
		{"fieldname": "state", "label": _("State"), "fieldtype": "Data", "width": 130},
	]


def rows(filters):
	conditions = {"retention_amount": [">", 0], "retention_invoice": ["is", "not set"], "status": ["!=", "Rejected"]}
	for field in ("project", "customer", "company"):
		if filters.get(field):
			conditions[field] = filters.get(field)
	certs = frappe.get_list(
		"Payment Certificate",
		filters=conditions,
		fields=["name", "project", "project_name", "customer", "valuation_no", "retention_amount", "retention_release_date"],
		limit_page_length=0,
	)
	today = getdate(nowdate())
	horizon = add_days(today, int(filters.days_ahead or 60))
	out = []
	for c in certs:
		due = getdate(c.retention_release_date) if c.retention_release_date else None
		if not due:
			state = _("No due date entered")
		elif due <= today:
			state = _("Due now")
		elif due <= horizon:
			state = _("Falls due soon")
		else:
			state = _("Not yet due")
		if not filters.show_all and state == _("Not yet due"):
			continue
		if not filters.show_all and not due:
			continue
		out.append({**c, "days_overdue": max(date_diff(today, due), 0) if due else 0, "state": state})
	out.sort(key=lambda r: (r["retention_release_date"] is None, r["retention_release_date"] or today))
	return out
