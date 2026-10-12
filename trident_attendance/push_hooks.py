"""Which HR documents send the employee app a push notification (`hooks.doc_events`).

Each hook catches its own errors and logs the type: a notification that cannot be queued never
stops the Newsletter, Leave Application or Salary Slip from being saved or submitted.
"""

import functools
import html as html_entities
import re
import traceback
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, formatdate, now_datetime

from trident_attendance import push
from trident_attendance.announcements import ANNOUNCED_AT_FIELD, ANNOUNCEMENT_FIELD

# On Leave Application: the status the employee was last told about.
NOTIFIED_STATUS_FIELD = "custom_app_notified_status"
LEAVE_TITLES = {"Approved": "Leave approved", "Rejected": "Leave rejected"}


def _never_blocks_the_save(hook):
	@functools.wraps(hook)
	def guarded(doc, method=None):
		try:
			return hook(doc, method)
		except Exception as e:
			trace = "".join(traceback.format_tb(e.__traceback__))
			frappe.log_error(
				title=f"trident_attendance: push on {doc.doctype} failed", message=f"{type(e).__name__}\n{trace}"
			)

	return guarded


def _employee_user(employee) -> str | None:
	if not employee:
		return None
	return frappe.db.get_value("Employee", employee, "user_id") or None


def _path(*parts) -> str:
	"""A web-app path; a document name is one segment whatever it holds."""
	return "/" + "/".join(quote(str(part), safe="") for part in parts)


@_never_blocks_the_save
def newsletter_on_update(doc, method=None):
	"""Every phone, once, when "Show in employee app" goes from off to on. The time is kept on
	the newsletter: it is the announcement's date, and the reason a box ticked again later
	sends nothing."""
	if not cint(doc.get(ANNOUNCEMENT_FIELD)) or doc.get(ANNOUNCED_AT_FIELD):
		return
	before = doc.get_doc_before_save()
	if before and cint(before.get(ANNOUNCEMENT_FIELD)):
		return
	doc.db_set(ANNOUNCED_AT_FIELD, now_datetime(), update_modified=False)
	push.notify_all(_("New announcement"), doc.subject or doc.name, _path("ess", "announcements", doc.name), "announcement")


@_never_blocks_the_save
def leave_application_status(doc, method=None):
	"""The employee's phones when the status becomes Approved or Rejected, whether that happens
	on save, on submit or after submit. The status told is kept on the document, so the same
	decision is never sent twice."""
	status = doc.get("status")
	if status not in LEAVE_TITLES or doc.get(NOTIFIED_STATUS_FIELD) == status:
		return
	before = doc.get_doc_before_save()
	if before and before.get("status") == status:
		return
	doc.db_set(NOTIFIED_STATUS_FIELD, status, update_modified=False)
	user = _employee_user(doc.get("employee"))
	if not user:
		return
	body = f"{formatdate(doc.from_date)} to {formatdate(doc.to_date)}, {doc.leave_type}"
	push.notify_user(user, _(LEAVE_TITLES[status]), body, "/ess/leave", "leave")


@_never_blocks_the_save
def salary_slip_on_submit(doc, method=None):
	user = _employee_user(doc.get("employee"))
	if not user:
		return
	body = formatdate(doc.start_date, "MMMM yyyy") if doc.get("start_date") else doc.name
	push.notify_user(user, _("Payslip ready"), body, _path("ess", "payslips", doc.name), "payslip")


# ---------------------------------------------------------------------------
# Things waiting for someone
# ---------------------------------------------------------------------------

# The ERP's own notifications (the bell): which ones reach the phone, and under what title.
# Energy points are left out: they are frequent and nobody has to act on them.
NOTIFICATION_TITLES = {
	"Assignment": "Assigned to you",
	"Mention": "You were mentioned",
	"Share": "Shared with you",
	"Alert": "ERP notification",
	"": "ERP notification",
}
BODY_LENGTH = 180


def _plain(text) -> str:
	"""One line of plain text from what the ERP shows as HTML."""
	text = html_entities.unescape(re.sub("<[^>]*>", " ", str(text or "")))
	text = " ".join(text.split())
	return text if len(text) <= BODY_LENGTH else text[: BODY_LENGTH - 1].rstrip() + "…"


@_never_blocks_the_save
def notification_log_after_insert(doc, method=None):
	"""Whatever the ERP puts under a person's bell also goes to their phones: an assignment, a
	mention, a share, an alert rule. The words are the ERP's own."""
	kind = doc.get("type") or ""
	if kind not in NOTIFICATION_TITLES:
		return
	user = doc.get("for_user")
	if not user or user == doc.get("from_user"):
		return
	body = _plain(doc.get("subject")) or _plain(f"{doc.get('document_type') or ''} {doc.get('document_name') or ''}")
	if not body:
		return
	push.notify_user(user, _(NOTIFICATION_TITLES[kind]), body, "/ess/", "erp")


@_never_blocks_the_save
def leave_application_after_insert(doc, method=None):
	"""A new leave request goes to the leave approver named on it: HRMS only sends an e-mail."""
	approver = doc.get("leave_approver")
	if not approver or doc.get("status") != "Open" or approver == _employee_user(doc.get("employee")):
		return
	who = doc.get("employee_name") or doc.get("employee")
	body = _plain(f"{who}: {formatdate(doc.from_date)} to {formatdate(doc.to_date)}, {doc.leave_type}")
	push.notify_user(approver, _("Leave request"), body, "/ess/team", "leave")


@_never_blocks_the_save
def workflow_action_after_insert(doc, method=None):
	"""A document waiting at a workflow step goes to everyone who may act on it and has the app.
	Only people with a registered phone are looked at, so a wide role costs one small query."""
	if doc.get("status") != "Open":
		return
	roles = [row.get("role") for row in (doc.get("permitted_roles") or []) if row.get("role")]
	if doc.get("user"):
		users = {doc.get("user")}
	elif roles:
		with_app = set(frappe.get_all(push.DEVICE_DOCTYPE, filters={"enabled": 1}, pluck="user", distinct=True))
		holders = set(
			frappe.get_all("Has Role", filters={"role": ["in", roles], "parenttype": "User"}, pluck="parent", distinct=True)
		)
		users = with_app & holders
	else:
		return
	users -= {"Administrator", "Guest", frappe.session.user}
	body = _plain(f"{doc.get('reference_doctype')} {doc.get('reference_name')}: {doc.get('workflow_state') or ''}")
	for user in sorted(users):
		push.notify_user(user, _("Waiting for your approval"), body, "/ess/", "erp")
