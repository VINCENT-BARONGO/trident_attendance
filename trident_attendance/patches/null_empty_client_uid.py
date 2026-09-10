import frappe


def execute():
	"""`custom_client_uid` gets a unique index; empty strings would collide, NULLs do not."""
	if not frappe.db.has_column("Employee Checkin", "custom_client_uid"):
		return
	frappe.db.sql("update `tabEmployee Checkin` set custom_client_uid = NULL where custom_client_uid = ''")
