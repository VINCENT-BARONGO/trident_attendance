import frappe

SETTINGS = "Trident Attendance Settings"
FIELD = "locate_role"
DEFAULT_ROLE = "Attendance Admin"


def execute():
	"""`Role that may locate people` starts as Attendance Admin on a site whose settings were
	saved before the field existed: a field's default is only given to settings never saved.
	Once, and only while nothing is stored for the field: a site that empties it afterwards
	(System Managers only) keeps it empty."""
	if not frappe.db.exists("Role", DEFAULT_ROLE):
		return
	# `Singles` has no name column: counted, not looked up.
	if frappe.db.count("Singles", {"doctype": SETTINGS, "field": FIELD}):
		return
	if not frappe.db.count("Singles", {"doctype": SETTINGS}):
		return
	frappe.db.set_single_value(SETTINGS, FIELD, DEFAULT_ROLE)
