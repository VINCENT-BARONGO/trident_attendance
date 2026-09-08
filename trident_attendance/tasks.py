"""Scheduled maintenance for the Trident attendance data.

Lives in an installed app rather than a Server Script on purpose: Server Scripts
require `server_script_enabled` in common_site_config.json, which is bench-level
config that Frappe Cloud controls and disables on shared plans. A scheduler_events
hook has no such restriction, so this runs the same on local and on the cloud.
"""

import frappe
from frappe.utils import add_days, now_datetime

# Face photos are storage-heavy and are only needed while a check-in could still be
# disputed. The Employee Checkin row itself (who/when/where/score) is kept forever.
PHOTO_RETENTION_DAYS = 30


def purge_attendance_photos():
	"""Delete attendance face photos older than the retention window.

	Keyed on the check-in's own `time`, not the upload time, so a handset that syncs
	days late does not effectively earn a longer retention than one that syncs at once.
	"""
	cutoff = add_days(now_datetime(), -PHOTO_RETENTION_DAYS)
	deleted = 0

	old_checkins = frappe.get_all(
		"Employee Checkin", filters={"time": ["<", cutoff]}, pluck="name"
	)

	if old_checkins:
		files = frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": "Employee Checkin",
				"attached_to_name": ["in", old_checkins],
			},
			fields=["name", "attached_to_name"],
		)
		for f in files:
			frappe.delete_doc(
				"File", f.name, ignore_permissions=True, delete_permanently=True
			)
			deleted += 1

		for name in {f.attached_to_name for f in files}:
			frappe.db.set_value(
				"Employee Checkin", name, "custom_attendance_photo", None,
				update_modified=False,
			)

	# Uploads whose parent check-in was deleted would otherwise sit on disk forever.
	orphans = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Employee Checkin",
			"attached_to_name": ["is", "not set"],
			"creation": ["<", cutoff],
		},
		pluck="name",
	)
	for name in orphans:
		frappe.delete_doc("File", name, ignore_permissions=True, delete_permanently=True)
		deleted += 1

	if deleted:
		frappe.logger("trident_attendance").info(
			f"Purged {deleted} attendance photo(s) older than {PHOTO_RETENTION_DAYS} days"
		)
	return deleted
