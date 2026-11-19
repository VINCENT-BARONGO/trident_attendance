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
	ensure_casual_approval()


def after_migrate():
	ensure_standard_perms()
	ensure_casual_approval()


# ---------------------------------------------------------------------------
# Casual labourers: registered by one role, approved by another
# ---------------------------------------------------------------------------
# The web app's Casual labourers page creates a Casual Labourer as "Pending Approval" for whoever
# holds Casual Registrar; someone with Casual Approver approves it (or rejects it) in My HR >
# Approvals or the ERP, and only then is it Active, which is all the phone's scan finds
# (api.get_casuals). The workflow runs on the Status field itself. A migrate adds whatever of it
# is missing (a state row, a transition) and marks the optional states optional; it removes
# nothing an administrator added.
#
# Active, Left and Blacklisted are optional states: Frappe makes a Workflow Action (an item in
# "Waiting for me", and a push to the phone) only for a step into a state that is not optional,
# so a working casual never waits on anyone; Pending Approval waits on the approver and Rejected
# on the registrar (Send Again).

CASUAL_ROLES = ("Casual Registrar", "Casual Approver")
CASUAL_WORKFLOW = "Casual Labourer Approval"
CASUAL_EDITORS = ("Casual Registrar", "Casual Approver", "HR Manager", "HR User", "Attendance Admin")
CASUAL_STATES = (
	# state, optional (no one waits on a casual in it)
	("Pending Approval", 0),
	("Active", 1),
	("Rejected", 0),
	("Left", 1),
	("Blacklisted", 1),
)
CASUAL_TRANSITIONS = tuple(
	# from, action, to, role, self approval
	[
		("Pending Approval", "Approve", "Active", "Casual Approver", 0),
		("Pending Approval", "Reject", "Rejected", "Casual Approver", 0),
		("Rejected", "Send Again", "Pending Approval", "Casual Registrar", 1),
	]
	+ [("Active", "Deactivate", "Left", r, 1) for r in ("Casual Registrar", "Casual Approver", "HR Manager", "HR User")]
	+ [("Left", "Reactivate", "Active", r, 1) for r in ("Casual Registrar", "Casual Approver", "HR Manager", "HR User")]
	+ [("Active", "Blacklist", "Blacklisted", r, 1) for r in ("Casual Approver", "HR Manager")]
	+ [("Left", "Blacklist", "Blacklisted", r, 1) for r in ("Casual Approver", "HR Manager")]
	+ [("Blacklisted", "Lift Blacklist", "Left", r, 1) for r in ("Casual Approver", "HR Manager")]
)


def ensure_casual_approval():
	"""The two roles, the workflow's states and actions, the workflow, and each of its state rows
	(one per role that may edit) and transitions, each only when missing. Returns True when it
	changed the workflow. Safe to run repeatedly."""
	if not frappe.db.exists("DocType", "Casual Labourer"):
		return False
	for role in CASUAL_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)
	for state, _ in CASUAL_STATES:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": state}).insert(ignore_permissions=True)
	for action in {t[1] for t in CASUAL_TRANSITIONS}:
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": action}).insert(
				ignore_permissions=True
			)
	states = [
		{"state": s, "doc_status": "0", "allow_edit": role, "is_optional_state": optional}
		for s, optional in CASUAL_STATES
		for role in CASUAL_EDITORS
	]
	transitions = [
		{"state": a, "action": act, "next_state": b, "allowed": role, "allow_self_approval": own}
		for a, act, b, role, own in CASUAL_TRANSITIONS
	]
	if not frappe.db.exists("Workflow", CASUAL_WORKFLOW):
		frappe.get_doc(
			{
				"doctype": "Workflow",
				"workflow_name": CASUAL_WORKFLOW,
				"document_type": "Casual Labourer",
				"workflow_state_field": "status",
				"is_active": 1,
				"send_email_alert": 0,
				"states": states,
				"transitions": transitions,
			}
		).insert(ignore_permissions=True)
		frappe.logger("trident_attendance").info(f"Workflow {CASUAL_WORKFLOW} made")
		return True
	wf = frappe.get_doc("Workflow", CASUAL_WORKFLOW)
	have_states = {(d.state, d.allow_edit) for d in wf.states}
	have_moves = {(d.state, d.action, d.next_state, d.allowed) for d in wf.transitions}
	optional = {s for s, o in CASUAL_STATES if o}
	added = [d for d in wf.states if d.state in optional and not d.is_optional_state]
	for d in added:
		# Frappe reads the first row of a state: every row of it must say so (no Workflow Action).
		d.is_optional_state = 1
	added += [wf.append("states", s) for s in states if (s["state"], s["allow_edit"]) not in have_states]
	added += [
		wf.append("transitions", t)
		for t in transitions
		if (t["state"], t["action"], t["next_state"], t["allowed"]) not in have_moves
	]
	if not added:
		return False
	wf.save(ignore_permissions=True)
	frappe.logger("trident_attendance").info(f"Workflow {CASUAL_WORKFLOW}: {len(added)} rows added")
	return True


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
