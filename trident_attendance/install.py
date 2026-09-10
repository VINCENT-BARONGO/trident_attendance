"""Install / migrate hooks.

The Custom DocPerm fixture only carries rows for this app's two roles. Frappe ignores a
doctype's standard DocPerms as soon as *any* Custom DocPerm row exists for it, so on a
fresh site the fixture alone would lock System Manager / HR out of Employee Checkin,
Attendance, Employee, Project, File, HR Settings and this app's own doctypes. This copies
the standard rows in beside ours whenever they are missing. Safe to run repeatedly.
"""

import frappe

PERM_FIELDS = (
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"report",
	"export",
	"import",
	"share",
	"print",
	"email",
	"select",
	"if_owner",
)


def after_install():
	ensure_standard_perms()


def after_migrate():
	ensure_standard_perms()


def ensure_standard_perms():
	doctypes = frappe.get_all("Custom DocPerm", distinct=True, pluck="parent")
	own_doctypes = frappe.get_all("DocType", filters={"module": "Trident Attendance"}, pluck="name")
	targets = set(doctypes) & (
		{"Employee Checkin", "Attendance", "Employee", "Project", "File", "HR Settings"} | set(own_doctypes)
	)
	added = 0
	for doctype in sorted(targets):
		existing = {
			(p.role, p.permlevel)
			for p in frappe.get_all("Custom DocPerm", filters={"parent": doctype}, fields=["role", "permlevel"])
		}
		standard = frappe.get_all(
			"DocPerm", filters={"parent": doctype}, fields=["role", "permlevel", *PERM_FIELDS], order_by="idx"
		)
		for p in standard:
			if (p.role, p.permlevel) in existing:
				continue
			doc = frappe.new_doc("Custom DocPerm")
			doc.update({"parent": doctype, "parenttype": "DocType", "parentfield": "permissions"})
			doc.update({k: p.get(k) for k in ("role", "permlevel", *PERM_FIELDS)})
			doc.flags.ignore_permissions = True
			doc.insert()
			added += 1
	if added:
		frappe.clear_cache()
		frappe.logger("trident_attendance").info(f"Copied {added} standard permission row(s) into Custom DocPerm")
	return added
