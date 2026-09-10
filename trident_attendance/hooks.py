app_name = "trident_attendance"
app_title = "Trident Attendance"
app_publisher = "Trident Plumbers Ltd"
app_description = "Field attendance: ID scan, face match and geofenced check-ins"
app_email = "vbarongo68@gmail.com"
app_license = "mit"

required_apps = ["erpnext", "hrms"]

# The Custom DocPerm fixture only carries this app's roles; Frappe drops a doctype's standard
# permissions once any Custom DocPerm exists for it, so the standard rows are copied in.
after_install = "trident_attendance.install.after_install"
after_sync = "trident_attendance.install.after_install"
after_migrate = "trident_attendance.install.after_migrate"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
# Everything the mobile app and the review workflow depend on travels with this app, so a
# fresh site is provisioned by `bench install-app trident_attendance`.
#
# Regenerate after changing any of these on a site:
#     bench --site <site> export-fixtures --app trident_attendance
#
# `Employee Checkin-custom_project` / `custom_mobile_app_data` are legacy fields that exist on
# the production site beside the ones this app owns; they are excluded so an export does not
# adopt them.

fixtures = [
	{
		"dt": "Custom Field",
		"filters": [
			["fieldname", "like", "custom_%"],
			["dt", "in", ["Employee", "Employee Checkin", "Project", "HR Settings", "Attendance"]],
			["name", "not in", ["Employee Checkin-custom_project", "Employee Checkin-custom_mobile_app_data"]],
		],
	},
	{
		"dt": "Role",
		"filters": [["name", "in", ["Attendance Marking", "Attendance Admin"]]],
	},
	{
		"dt": "Custom DocPerm",
		"filters": [["role", "in", ["Attendance Marking", "Attendance Admin"]]],
	},
	# NOTE the workspace's `module` is HR, not this app: a workspace is only visible to users
	# holding doctype permissions in its module, and ordinary reviewers hold none in
	# "Trident Attendance". Frappe swallows the PermissionError silently. Do not "correct" it.
	{
		"dt": "Workspace",
		"filters": [["name", "in", ["Field Attendance"]]],
	},
]

# ---------------------------------------------------------------------------
# Controller override
# ---------------------------------------------------------------------------
# Frappe validates Link/Select values before any doc_event runs, on insert and on save. The
# mobile app can post values that fail that check; the override repairs them first so a punch
# is never rejected for a fixable field.

override_doctype_class = {
	"Employee Checkin": "trident_attendance.overrides.TridentEmployeeCheckin",
}

# ---------------------------------------------------------------------------
# Document events
# ---------------------------------------------------------------------------

doc_events = {
	"Employee Checkin": {
		"before_insert": "trident_attendance.checkin_hooks.before_insert",
		"validate": "trident_attendance.checkin_hooks.validate",
	},
	"Attendance": {
		"before_insert": "trident_attendance.attendance_hooks.before_insert",
		"after_insert": "trident_attendance.attendance_hooks.after_insert",
		"before_cancel": "trident_attendance.attendance_hooks.before_cancel",
	},
}

# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------
# App hooks rather than Server Scripts: Server Scripts need `server_script_enabled` in
# common_site_config.json, which Frappe Cloud disables on shared plans.

scheduler_events = {
	"hourly": [
		"trident_attendance.tasks.finalise_days",
	],
	"daily": [
		"trident_attendance.tasks.purge_attendance_photos",
		"trident_attendance.trident_attendance.doctype.trident_attendance_run.trident_attendance_run.purge_old_runs",
	],
}
