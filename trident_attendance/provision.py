"""One ERP user per employee, for the employee app's sign-in without a password.

    bench --site <site> execute trident_attendance.provision.provision_ess_users \
        --kwargs "{'domain': 'example.co.ke'}"

That is a dry run: it changes nothing and lists what it would create. Add `'dry_run': 0` to
create the users. Not whitelisted: it is run from the bench by whoever administers the site.
"""

import json
import re
import secrets

import frappe
from frappe.utils import cint, validate_email_address

from trident_attendance.employee_access import APP_ACCESS_FIELD

ESS_ROLE = "Employee Self Service"
DOMAIN_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def provision_ess_users(domain, dry_run=1, company=None, employees=None, enable_app_access=0):
	"""Creates `<employee id>@staff.<domain>` for every Active employee without a linked user.

	The address needs no mailbox: no welcome e-mail is sent and the password is random and kept
	nowhere, since these users only ever sign in with a token the attendance hub asks for.
	`employees` narrows the run to a list of Employee ids, `company` to one company.
	`enable_app_access` also ticks Employee > App Access on the employees it creates a user for.

	An employee who already has a user is skipped and nothing about that user changes, so the
	run can be repeated. Returns one row per employee with what happened.
	"""
	domain = str(domain or "").strip().lower().lstrip("@")
	if not DOMAIN_PATTERN.match(domain):
		frappe.throw(f"'{domain}' is not a domain name (expected something like example.co.ke).")
	dry_run = cint(dry_run)
	enable_app_access = cint(enable_app_access) and frappe.get_meta("Employee").has_field(APP_ACCESS_FIELD)

	filters = {"status": "Active"}
	if company:
		filters["company"] = company
	wanted = _employee_names(employees)
	if wanted:
		filters["name"] = ["in", wanted]
	rows = frappe.get_all(
		"Employee", filters=filters, fields=["name", "employee_name", "user_id"], order_by="name asc"
	)

	out = [
		{"employee": name, "employee_name": None, "user": None, "outcome": "skipped: no such Active employee"}
		for name in sorted(set(wanted) - {r.name for r in rows})
	]
	for row in rows:
		result = {"employee": row.name, "employee_name": row.employee_name, "user": row.user_id}
		if row.user_id:
			result["outcome"] = "skipped: already has a user"
		else:
			result["user"] = email = f"{row.name.lower()}@staff.{domain}"
			if not validate_email_address(email):
				result["outcome"] = "skipped: the employee id does not make a valid address"
			elif frappe.db.exists("User", email):
				# Not ours to change, and not linked to this employee: someone has to look.
				result["outcome"] = "skipped: user exists but is not linked to the employee"
			elif dry_run:
				result["outcome"] = "would create"
			else:
				result["outcome"] = _create(row.name, email, enable_app_access)
		out.append(result)

	return {
		"dry_run": bool(dry_run),
		"domain": domain,
		"created": len([r for r in out if r["outcome"] == "created"]),
		"would_create": len([r for r in out if r["outcome"] == "would create"]),
		"skipped": len([r for r in out if r["outcome"].startswith("skipped")]),
		"failed": len([r for r in out if r["outcome"].startswith("failed")]),
		"employees": out,
	}


def _employee_names(employees) -> list[str]:
	if not employees:
		return []
	if isinstance(employees, str):
		employees = json.loads(employees) if employees.strip().startswith("[") else employees.split(",")
	return [str(e).strip() for e in employees if str(e).strip()]


def _create(employee: str, email: str, enable_app_access) -> str:
	"""One employee's user, whole or not at all: a failure undoes that employee only."""
	frappe.db.savepoint("provision_ess_user")
	try:
		_create_linked_user(employee, email, enable_app_access)
	except Exception as e:
		frappe.db.rollback(save_point="provision_ess_user")
		frappe.clear_messages()
		return f"failed: {str(e)[:200] or type(e).__name__}"
	if not frappe.flags.in_test:
		frappe.db.commit()
	return "created"


def _create_linked_user(employee: str, email: str, enable_app_access):
	emp = frappe.get_doc("Employee", employee)
	names = (emp.employee_name or emp.first_name or employee).split(" ")

	user = frappe.new_doc("User")
	user.update(
		{
			"email": email,
			"first_name": names[0],
			"last_name": " ".join(names[1:]) or None,
			"enabled": 1,
			"send_welcome_email": 0,
			# Nobody is told it; these users get in with a token from the hub.
			"new_password": secrets.token_urlsafe(32),
		}
	)
	user.flags.ignore_permissions = True
	user.flags.no_welcome_mail = True
	user.insert()

	# The link comes before the role. ERPNext takes Employee Self Service off any user no
	# Employee points at, every time the user is saved (validate_employee_role in
	# erpnext/setup/doctype/employee/employee.py, a User validate hook). Saving the Employee
	# is also what adds the Employee role and the user permissions, as it does in the Desk.
	emp.user_id = user.name
	emp.create_user_permission = 1
	if enable_app_access:
		emp.set(APP_ACCESS_FIELD, 1)
	emp.flags.ignore_permissions = True
	emp.save()

	user.reload()
	user.flags.ignore_permissions = True
	user.add_roles(ESS_ROLE)
	if not frappe.db.exists("Has Role", {"parent": user.name, "parenttype": "User", "role": ESS_ROLE}):
		frappe.throw(f"The {ESS_ROLE} role did not stay on {user.name}.")
