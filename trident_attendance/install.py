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
	adopt_site_id_field()


def after_migrate():
	ensure_standard_perms()


# Where a site that predates this app is likely to keep the national ID number, in the order
# tried. `national_id` is CSF KE's (mandatory there).
SITE_ID_FIELDS = ("national_id",)


def adopt_site_id_field():
	"""Point `Employee ID Number Field` at the field the site already fills.

	The fixture always adds an empty `custom_id_number`. On a site whose Employees carry the
	number elsewhere every scan would end at "Employee not found" until someone found the
	setting, so it is chosen here -- only while the setting is untouched, `custom_id_number`
	is empty on every Active Employee and the other field is not. A site that fills
	`custom_id_number` is never changed. Safe to run repeatedly.
	"""
	from trident_attendance.utils import DEFAULT_ID_FIELD

	settings = "Trident Attendance Settings"
	if not frappe.db.exists("DocType", settings):
		return None
	if (frappe.db.get_single_value(settings, "employee_id_field") or DEFAULT_ID_FIELD) != DEFAULT_ID_FIELD:
		return None
	meta = frappe.get_meta("Employee")
	if meta.has_field(DEFAULT_ID_FIELD) and frappe.db.exists(
		"Employee", {"status": "Active", DEFAULT_ID_FIELD: ["is", "set"]}
	):
		return None
	for field in SITE_ID_FIELDS:
		if meta.has_field(field) and frappe.db.exists("Employee", {"status": "Active", field: ["is", "set"]}):
			frappe.db.set_single_value(settings, "employee_id_field", field)
			frappe.logger("trident_attendance").info(f"Employee ID Number Field set to {field}")
			return field
	return None


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
