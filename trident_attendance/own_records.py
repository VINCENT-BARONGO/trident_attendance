"""A person's own HR records for the web app's My HR, read past User Permissions.

A site supervisor is given User Permissions on the projects they mark, applied to every
doctype, and the site runs strict user permissions: the ERP then hides from them every record
whose project is blank or another site's, their own Employee record and most of their own
attendance among them. Whose record it is settles this, not which project it names, so these
reads look only at the Employee linked to the signed-in user (`Employee.user_id`) and never
take an employee from the caller. The role must still be allowed to read the doctype, and
only the fields its roles may read (permission levels) are given.

The same permissions refuse their own leave application when they write it (it names no
project), so applying for leave and withdrawing it are here too: for the caller's own
Employee only, by a role that may create one, and through every check HRMS makes of a leave
application (balance, overlap, dates). Approval is untouched.
"""

import re

import frappe
from frappe import _
from frappe.utils import cint, nowdate

from trident_attendance.utils import require_signed_in

# doctype -> the field that names the employee the record belongs to.
OWN_DOCTYPES = {
	"Employee": "name",
	"Attendance": "employee",
	"Employee Checkin": "employee",
	"Leave Application": "employee",
}

OPERATORS = {"=", "!=", "<", "<=", ">", ">=", "in", "not in", "between", "like", "is"}
STANDARD_FIELDS = {"name", "owner", "creation", "modified", "modified_by", "docstatus"}
ORDER_BY = re.compile(r"^([a-z][a-z0-9_]*) (asc|desc)$")

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000


def own_employees(user: str) -> list[str]:
	"""The Employee records linked to this user, the active one first."""
	rows = frappe.get_all("Employee", filters={"user_id": user}, fields=["name", "status"])
	rows.sort(key=lambda r: r.status != "Active")
	return [r.name for r in rows]


def _readable(meta, user) -> set:
	"""The stored fields of the doctype this user's roles may read."""
	permitted = set(meta.get_permitted_fieldnames(user=user, permission_type="read", with_virtual_fields=False))
	stored = {f.fieldname for f in meta.fields if f.fieldtype not in frappe.model.no_value_fields}
	return (permitted & stored) | STANDARD_FIELDS


def _own_filters(meta, readable, filters) -> list:
	"""The caller's conditions, each on a readable field of this doctype with a plain operator."""
	filters = frappe.parse_json(filters) or []
	if isinstance(filters, dict):
		filters = [[field, "=", value] for field, value in filters.items()]
	clean = []
	for condition in filters:
		if not isinstance(condition, (list, tuple)) or len(condition) != 3:
			frappe.throw(_("Not a filter: {0}").format(frappe.as_json(condition)), frappe.ValidationError)
		field, operator, value = condition
		operator = str(operator).lower()
		if not isinstance(field, str) or field not in readable or operator not in OPERATORS:
			frappe.throw(_("Not a filter: {0}").format(frappe.as_json(condition)), frappe.ValidationError)
		clean.append([meta.name, field, operator, value])
	return clean


@frappe.whitelist()
def get_own_records(doctype, fields=None, filters=None, order_by=None, limit=DEFAULT_LIMIT, start=0):
	"""The signed-in person's own records of `doctype`: `fields` of each, narrowed by `filters`
	(`[[field, operator, value], ...]`), in `order_by` ("field asc"). Nothing for a user with
	no Employee record; fields the doctype does not have, or keeps from this person's roles,
	are left out of the rows."""
	user = require_signed_in()
	link = OWN_DOCTYPES.get(doctype)
	if not link:
		frappe.throw(_("Not a record of your own: {0}").format(doctype), frappe.PermissionError)
	employees = own_employees(user)
	if not employees:
		return []
	if not frappe.has_permission(doctype, "read"):
		frappe.throw(_("You may not read {0}.").format(_(doctype)), frappe.PermissionError)

	meta = frappe.get_meta(doctype)
	readable = _readable(meta, user)
	fields = [f for f in (frappe.parse_json(fields) or ["name"]) if isinstance(f, str) and f in readable]
	if "name" not in fields:
		fields.insert(0, "name")
	conditions = _own_filters(meta, readable, filters)
	if order_by:
		match = ORDER_BY.match(str(order_by).strip().lower())
		if not match or match.group(1) not in readable:
			frappe.throw(_("Not an order: {0}").format(order_by), frappe.ValidationError)
		order_by = f"`tab{doctype}`.`{match.group(1)}` {match.group(2)}"

	return frappe.get_all(
		doctype,
		filters=[[doctype, link, "in", employees], *conditions],
		fields=fields,
		order_by=order_by or f"`tab{doctype}`.`modified` desc",
		limit_start=max(cint(start), 0),
		limit_page_length=min(max(cint(limit) or DEFAULT_LIMIT, 1), MAX_LIMIT),
	)


LEAVE = "Leave Application"
LEAVE_ANSWER = ("name", "docstatus", "status", "total_leave_days", "employee")


def _own_employee_for_leave(user, ptype) -> list[str]:
	"""The caller's Employee records, for a role that may `ptype` a leave application."""
	employees = own_employees(user)
	if not employees or not frappe.has_permission(LEAVE, ptype):
		frappe.throw(_("You may not apply for leave here."), frappe.PermissionError)
	return employees


@frappe.whitelist(methods=["POST"])
def apply_own_leave(leave_type, from_date, to_date, half_day=0, half_day_date=None, description=None, leave_approver=None):
	"""An open leave application for the signed-in person, as HRMS would take it from them."""
	employees = _own_employee_for_leave(require_signed_in(), "create")
	doc = frappe.new_doc(LEAVE)
	doc.update(
		{
			"employee": employees[0],
			"leave_type": leave_type,
			"from_date": from_date,
			"to_date": to_date,
			"half_day": 1 if cint(half_day) else 0,
			"half_day_date": half_day_date if cint(half_day) else None,
			"description": description,
			"status": "Open",
			"posting_date": nowdate(),
		}
	)
	# The approver is HR's (the Employee's or the department's), never the caller's choice: a
	# friendly colleague named here would be shared the application to approve.
	approver = _leave_approver_for(employees[0])
	if approver:
		doc.leave_approver = approver
	doc.insert(ignore_permissions=True)
	return {field: doc.get(field) for field in LEAVE_ANSWER}


def _leave_approver_for(employee: str) -> str | None:
	"""HRMS's own answer (the Employee's Leave Approver, else the department's first)."""
	try:
		# v16: the lookup without get_leave_approver's check of who is asking.
		from hrms.hr.doctype.leave_application.leave_application import get_employee_leave_approver
	except ImportError:
		try:
			from hrms.hr.doctype.leave_application.leave_application import (
				get_leave_approver as get_employee_leave_approver,
			)
		except ImportError:
			return frappe.db.get_value("Employee", employee, "leave_approver")
	return get_employee_leave_approver(employee)


@frappe.whitelist(methods=["POST"])
def withdraw_own_leave(name):
	"""Their own application, still open and not yet approved, set to Cancelled. Another
	person's answers exactly like one that does not exist."""
	employees = _own_employee_for_leave(require_signed_in(), "write")
	row = frappe.db.get_value(LEAVE, {"name": name, "employee": ["in", employees]}, ["docstatus", "status"], as_dict=True)
	if not row:
		frappe.throw(_("No such leave application."), frappe.DoesNotExistError)
	if row.docstatus != 0 or row.status != "Open":
		frappe.throw(_("Only an open application that has not been approved yet can be withdrawn."), frappe.ValidationError)
	doc = frappe.get_doc(LEAVE, name)
	doc.status = "Cancelled"
	doc.save(ignore_permissions=True)
	return {field: doc.get(field) for field in LEAVE_ANSWER}
