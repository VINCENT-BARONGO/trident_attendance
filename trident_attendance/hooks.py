app_name = "trident_attendance"
app_title = "Trident Attendance"
app_publisher = "Trident Plumbers Ltd"
app_description = "Field attendance: ID scan, face match and geofenced check-ins"
app_email = "vbarongo68@gmail.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "trident_attendance",
# 		"logo": "/assets/trident_attendance/logo.png",
# 		"title": "Trident Attendance",
# 		"route": "/trident_attendance",
# 		"has_permission": "trident_attendance.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/trident_attendance/css/trident_attendance.css"
# app_include_js = "/assets/trident_attendance/js/trident_attendance.js"

# include js, css files in header of web template
# web_include_css = "/assets/trident_attendance/css/trident_attendance.css"
# web_include_js = "/assets/trident_attendance/js/trident_attendance.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "trident_attendance/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "trident_attendance/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "trident_attendance.utils.jinja_methods",
# 	"filters": "trident_attendance.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "trident_attendance.install.before_install"
# after_install = "trident_attendance.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "trident_attendance.uninstall.before_uninstall"
# after_uninstall = "trident_attendance.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "trident_attendance.utils.before_app_install"
# after_app_install = "trident_attendance.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "trident_attendance.utils.before_app_uninstall"
# after_app_uninstall = "trident_attendance.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "trident_attendance.notifications.get_notification_config"

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["trident_attendance.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"trident_attendance.tasks.all"
# 	],
# 	"daily": [
# 		"trident_attendance.tasks.daily"
# 	],
# 	"hourly": [
# 		"trident_attendance.tasks.hourly"
# 	],
# 	"weekly": [
# 		"trident_attendance.tasks.weekly"
# 	],
# 	"monthly": [
# 		"trident_attendance.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "trident_attendance.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "trident_attendance.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "trident_attendance.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["trident_attendance.utils.before_request"]
# after_request = ["trident_attendance.utils.after_request"]

# Job Events
# ----------
# before_job = ["trident_attendance.utils.before_job"]
# after_job = ["trident_attendance.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"trident_attendance.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []


# ---------------------------------------------------------------------------
# Trident attendance configuration
# ---------------------------------------------------------------------------

# Everything the mobile app and the review workflow depend on travels with this
# app as fixtures, so a fresh site (including trident.frappe.cloud) is provisioned
# by `bench install-app trident_attendance` rather than by recreating ~18 custom
# fields and two roles by hand.
#
# Regenerate after changing any of these on a site:
#     bench --site <site> export-fixtures --app trident_attendance

fixtures = [
	{
		"dt": "Custom Field",
		"filters": [["fieldname", "like", "custom_%"], ["dt", "in", [
			"Employee", "Employee Checkin", "Project", "HR Settings",
		]]],
	},
	{
		"dt": "Role",
		"filters": [["name", "in", ["Attendance Marking", "Attendance Admin"]]],
	},
	{
		"dt": "Custom DocPerm",
		"filters": [["role", "in", ["Attendance Marking", "Attendance Admin"]]],
	},
]

# Replaces the "Purge Attendance Photos" Server Script. An app hook works on
# Frappe Cloud, where Server Scripts are typically disabled bench-wide.
scheduler_events = {
	"daily": [
		"trident_attendance.tasks.purge_attendance_photos",
	],
}
