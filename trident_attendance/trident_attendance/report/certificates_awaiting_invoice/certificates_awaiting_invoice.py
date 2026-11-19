"""Certificates Awaiting Invoice: every valuation the client has certified that no invoice has been raised
for yet, oldest first, with the days it has waited since the certificate reached the office.

It is the Credit Controller's work list: the KRA is an invoice within the set number of days of the
certificate. Valuations still waiting for the client can be shown too, for the coordinators' follow-up."""

import frappe
from frappe import _
from frappe.utils import date_diff, getdate, nowdate

LATE_AFTER_DAYS = 5


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
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 100},
		{"fieldname": "submitted_on", "label": _("Submitted"), "fieldtype": "Date", "width": 100},
		{"fieldname": "certificate_received_on", "label": _("Certificate Received"), "fieldtype": "Date", "width": 130},
		{"fieldname": "amount", "label": _("Amount"), "fieldtype": "Currency", "width": 130},
		{"fieldname": "days_waiting", "label": _("Days Waiting"), "fieldtype": "Int", "width": 100},
		{"fieldname": "late", "label": _("Late"), "fieldtype": "Check", "width": 60},
		{"fieldname": "follow_ups", "label": _("Follow-ups"), "fieldtype": "Int", "width": 90},
	]


def rows(filters):
	statuses = ["Certified", "Submitted"] if filters.include_not_certified else ["Certified"]
	conditions = {"status": ["in", statuses]}
	for field in ("project", "customer", "company"):
		if filters.get(field):
			conditions[field] = filters.get(field)
	# get_list, not get_all: the caller's permissions and User Permissions (a project) apply.
	certs = frappe.get_list(
		"Payment Certificate",
		filters=conditions,
		fields=["name", "project", "project_name", "customer", "valuation_no", "status", "submitted_on", "certified_on",
		        "certificate_received_on", "certified_amount", "valuation_amount"],
		order_by="submitted_on asc",
		limit_page_length=0,
	)
	counts = {}
	if certs:
		for row in frappe.get_all("Payment Certificate Follow Up", filters={"parent": ["in", [c.name for c in certs]]}, fields=["parent"]):
			counts[row.parent] = counts.get(row.parent, 0) + 1
	today = getdate(nowdate())
	out = []
	for c in certs:
		start = (c.certificate_received_on or c.certified_on) if c.status == "Certified" else c.submitted_on
		waiting = date_diff(today, start) if start else 0
		out.append(
			{
				**c,
				"amount": c.certified_amount if c.status == "Certified" else c.valuation_amount,
				"days_waiting": waiting,
				"late": int(c.status == "Certified" and waiting > LATE_AFTER_DAYS),
				"follow_ups": counts.get(c.name, 0),
			}
		)
	out.sort(key=lambda r: -r["days_waiting"])
	return out
