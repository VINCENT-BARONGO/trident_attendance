"""Everything that silently breaks the field-attendance flow, in one list."""

import frappe
from frappe import _
from frappe.utils import cint, flt

EXPECTED_TIMEZONE = "Africa/Nairobi"


def execute(filters=None):
	rows = []
	rows += supervisor_checks()
	rows += employee_checks()
	rows += project_checks()
	rows += site_checks()
	return get_columns(), rows


LIST_TYPES = {"DocType"}


def row(category, record_type, record, problem, fix):
	if record_type in LIST_TYPES:
		link = f'<a href="/app/{frappe.scrub(record).replace("_", "-")}">{frappe.utils.escape_html(record)}</a>'
	elif record_type == "Custom Field":
		link = f'<a href="/app/custom-field/{frappe.utils.quote(record)}">{frappe.utils.escape_html(record)}</a>'
	else:
		link = f'<a href="/app/{frappe.scrub(record_type).replace("_", "-")}/{frappe.utils.quote(record)}">{frappe.utils.escape_html(record)}</a>'
	return {"category": category, "record_type": record_type, "record": link, "problem": problem, "fix": fix}


def supervisor_checks():
	rows = []
	users = frappe.get_all(
		"Project User",
		filters={"parenttype": "Project"},
		fields=["user", "parent"],
	)
	open_projects = set(frappe.get_all("Project", filters={"status": "Open"}, pluck="name"))
	supervisors = sorted({u.user for u in users if u.parent in open_projects and u.user})
	if not supervisors:
		rows.append(
			row(
				_("Supervisors"),
				"DocType",
				"Project",
				_("No Open project has anyone in its Users table"),
				_("Add each site supervisor's User to the project's Users table; the app only lists those projects"),
			)
		)
		return rows

	for user in supervisors:
		enabled, full_name = frappe.db.get_value("User", user, ["enabled", "full_name"]) or (0, user)
		roles = set(frappe.get_roles(user))
		employee = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
		if not cint(enabled):
			rows.append(row(_("Supervisors"), "User", user, _("User is disabled"), _("Enable the user or remove them from the project's Users table")))
		if "Attendance Marking" not in roles:
			rows.append(row(_("Supervisors"), "User", user, _("Missing the Attendance Marking role: cannot post check-ins"), _("Add the Attendance Marking role to the user")))
		if not employee:
			rows.append(
				row(
					_("Supervisors"),
					"User",
					user,
					_("No active Employee is linked to this user: check-ins cannot be attributed to the supervisor and the supervisor cannot check themselves in"),
					_("Set Employee > User ID to {0}").format(user),
				)
			)
		else:
			image, id_number = frappe.db.get_value("Employee", employee, ["image", "custom_id_number"])
			if not image:
				rows.append(row(_("Supervisors"), "Employee", employee, _("No profile photo: the supervisor's own face match can never be 'Matched'"), _("Upload a clear face photo on the Employee record")))
			if not id_number:
				rows.append(row(_("Supervisors"), "Employee", employee, _("No National ID number: the supervisor cannot scan themselves in"), _("Fill Employee > National ID Number")))
	return rows


def employee_checks():
	rows = []
	employees = frappe.get_all(
		"Employee",
		filters={"status": "Active", "custom_project": ["is", "set"]},
		fields=["name", "employee_name", "image", "custom_id_number", "custom_project"],
	)
	for e in employees:
		if not e.custom_id_number:
			rows.append(row(_("Site employees"), "Employee", e.name, _("No National ID number: cannot be found by the ID scan"), _("Fill Employee > National ID Number")))
		if not e.image:
			rows.append(row(_("Site employees"), "Employee", e.name, _("No profile photo: face verification reports 'No Baseline Photo'"), _("Upload a clear face photo on the Employee record")))
	dupes = frappe.db.sql(
		"""select custom_id_number, count(*) n from `tabEmployee`
		where status='Active' and ifnull(custom_id_number,'')!='' group by custom_id_number having n > 1""",
		as_dict=True,
	)
	for d in dupes:
		rows.append(row(_("Site employees"), "DocType", "Employee", _("National ID {0} is on {1} active employees: the app refuses the scan").format(d.custom_id_number, d.n), _("Fix the duplicate ID numbers")))
	return rows


def project_checks():
	rows = []
	projects = frappe.get_all(
		"Project",
		filters={"status": "Open"},
		fields=["name", "project_name", "custom_site_latitude", "custom_site_longitude", "custom_geofence_radius_meters"],
	)
	with_users = set(frappe.get_all("Project User", filters={"parenttype": "Project"}, pluck="parent"))
	for p in projects:
		if p.name not in with_users:
			continue
		if not (flt(p.custom_site_latitude) and flt(p.custom_site_longitude)):
			rows.append(row(_("Projects"), "Project", p.name, _("No site GPS coordinates: the app blocks scanning at this site"), _("Set Site Latitude / Longitude on the project")))
		elif cint(p.custom_geofence_radius_meters) <= 0:
			rows.append(row(_("Projects"), "Project", p.name, _("Geofence radius is 0"), _("Set Geofence Radius (metres), typically 200")))
	return rows


def site_checks():
	rows = []
	if cint(frappe.db.get_single_value("HR Settings", "allow_geolocation_tracking")):
		rows.append(row(_("Site"), "HR Settings", "HR Settings", _("Allow Geolocation Tracking is on: hrms rejects check-ins that have no GPS"), _("Switch it off; this app does its own geofence check")))

	tz = frappe.db.get_single_value("System Settings", "time_zone")
	if tz != EXPECTED_TIMEZONE:
		rows.append(row(_("Site"), "System Settings", "System Settings", _("Time zone is {0}; the app posts handset-local times").format(tz), _("Set System Settings > Time Zone to {0}").format(EXPECTED_TIMEZONE)))

	has_create = frappe.db.exists("Custom DocPerm", {"parent": "Attendance", "role": "Attendance Marking", "create": 1}) or frappe.db.exists(
		"DocPerm", {"parent": "Attendance", "role": "Attendance Marking", "create": 1}
	)
	if has_create:
		rows.append(row(_("Site"), "Role", "Attendance Marking", _("This role can create Attendance directly (used by the office Attendance Tool). Reviewing check-ins requires the separate Attendance Admin role."), _("Informational")))

	for name in ("Employee Checkin-custom_project", "Employee Checkin-custom_mobile_app_data"):
		if frappe.db.exists("Custom Field", name):
			rows.append(row(_("Site"), "Custom Field", name, _("Legacy field duplicates one this app owns (custom_site_project / Mobile App Data)"), _("Delete the Custom Field once nothing else reads it")))

	settings = frappe.get_cached_doc("Trident Attendance Settings")
	if not settings.day_cutoff_time:
		rows.append(row(_("Site"), "Trident Attendance Settings", "Trident Attendance Settings", _("Day Cutoff Time is empty"), _("Set it, e.g. 20:00")))

	auto_shifts = frappe.get_all("Shift Type", filters={"enable_auto_attendance": 1}, pluck="name")
	if auto_shifts and settings.staged_sources == "All sources":
		rows.append(row(_("Site"), "DocType", "Shift Type", _("Auto attendance is enabled on {0} while this app stages all sources: both may try to mark the same day").format(", ".join(auto_shifts)), _("Disable auto attendance on the shift or set Staged Sources back to 'Mobile app only'")))
	return rows


def get_columns():
	return [
		{"label": _("Category"), "fieldname": "category", "fieldtype": "Data", "width": 130},
		{"label": _("Type"), "fieldname": "record_type", "fieldtype": "Data", "width": 110},
		{"label": _("Record"), "fieldname": "record", "fieldtype": "HTML", "width": 240},
		{"label": _("Problem"), "fieldname": "problem", "fieldtype": "Data", "width": 420},
		{"label": _("Fix"), "fieldname": "fix", "fieldtype": "Data", "width": 360},
	]
