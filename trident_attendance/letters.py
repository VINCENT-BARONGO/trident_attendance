"""HR letters in the employee app: what HR emails a person also reaches My HR > Letters.

Three ways in, all ending in an Employee App Letter (one per letter and employee):

  * an email sent from the person's Employee record (the form's Email button: warning letters,
    notices, contract renewals) - `communication_after_insert`;
  * an email sent from an Official Letter (PrintHub) to an address that is an employee's;
  * Employee > Send to app (`send_to_app`), for people with no email address too. With "also
    email" ticked it is sent as an ordinary email from the record, which then comes in by the
    first way, so the letter is never made twice.

Only emails sent FROM an Employee record or an Official Letter come in: the ERP's other mail
(invoices, CRM, tickets) never reaches the app, whoever it is addressed to.

The person reads their letters with `list_my_letters` / `get_my_letter` /
`get_my_letter_file`, as themselves: only the letters of the Employee records linked to
their user, whatever they ask for. They hold no permission on Employee App Letter, so the
reads run past it after that check. Opening a letter is recorded on it (first and last time,
how many times): HR sees it on the Employee form (`employee_letters`) and on the letter.

Each new letter sends the person's phones a notification ("New letter from HR") that opens it.
"""

import frappe
from frappe import _
from frappe.utils import cint, get_url, now_datetime, sanitize_html, validate_email_address

from trident_attendance import push
from trident_attendance.announcements import absolute_file_links, summary
from trident_attendance.own_records import own_employees
from trident_attendance.utils import require_signed_in

DOCTYPE = "Employee App Letter"
SENDER_ROLES = ("HR Manager", "HR User", "System Manager")
EMAIL_FIELDS = ("user_id", "prefered_email", "personal_email", "company_email")

DEFAULT_LIMIT = 30
MAX_LIMIT = 100
MAX_SUBJECT = 140
MAX_ATTACHMENTS = 5

LIST_FIELDS = ["name", "subject", "sent_at", "opened_at", "message", "source"]

REASONS = {
	"not_found": "No such letter.",
	"no_subject": "Write a subject.",
	"no_message": "Write the letter, or attach it.",
	"no_employee": "No such employee.",
	"no_email": "This employee has no email address: the letter can only go to the app.",
	"too_many_files": "Attach at most {0} files.",
}


def _refuse(reason: str, *args, **extra) -> dict:
	return {"ok": False, "reason": reason, "message": _(REASONS[reason]).format(*args), **extra}


def _may_send(user=None) -> bool:
	return bool(set(frappe.get_roles(user)) & set(SENDER_ROLES))


def _require_sender():
	if not _may_send():
		frappe.throw(_("Only HR may send letters to the app."), frappe.PermissionError)


# ---------------------------------------------------------------------------
# Making letters
# ---------------------------------------------------------------------------


def _files_of(doctype, name) -> list[dict]:
	return frappe.get_all(
		"File",
		filters={"attached_to_doctype": doctype, "attached_to_name": name, "is_folder": 0},
		fields=["file_url", "file_name", "is_private"],
		order_by="creation asc",
	)


def _attach(letter: str, files: list[dict]):
	"""Copies of the files on the letter: the same stored file, a File row of its own, kept
	private (a letter is personal whatever the original said)."""
	for f in files[:MAX_ATTACHMENTS]:
		if not f.get("file_url"):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_url": f["file_url"],
				"file_name": f.get("file_name") or f["file_url"].rsplit("/", 1)[-1],
				"is_private": 1 if str(f["file_url"]).startswith("/private/") else cint(f.get("is_private")),
				"attached_to_doctype": DOCTYPE,
				"attached_to_name": letter,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()


def make_letter(employee, subject, message, source, sent_by=None, communication=None, files=()) -> str | None:
	"""One Employee App Letter, its files and the push. None when this email already gave the
	employee their letter (a job run twice, a resend)."""
	if communication and frappe.db.exists(DOCTYPE, {"communication": communication, "employee": employee}):
		return None
	emp = frappe.db.get_value("Employee", employee, ["name", "employee_name", "user_id"], as_dict=True)
	if not emp:
		return None
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"employee": emp.name,
			"employee_name": emp.employee_name,
			"subject": (subject or _("Letter from HR")).strip()[:MAX_SUBJECT],
			"message": sanitize_html(str(message or ""), always_sanitize=True),
			"source": source,
			"sent_by": sent_by or frappe.session.user,
			"sent_at": now_datetime(),
			"communication": communication,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	_attach(doc.name, list(files))
	phones = 0
	if emp.user_id:
		phones = push.notify_user(
			emp.user_id, _("New letter from HR"), doc.subject, f"/ess/letters/{doc.name}", "letter"
		)
	doc.db_set("phones_notified", phones, update_modified=False)
	return doc.name


def _addresses(text) -> list[str]:
	out = []
	for part in str(text or "").replace(";", ",").split(","):
		part = part.strip()
		if "<" in part and ">" in part:
			part = part[part.index("<") + 1 : part.index(">")]
		if part and validate_email_address(part):
			out.append(part.lower())
	return out


def employees_with_emails(addresses) -> list[str]:
	"""The employees any of these addresses belong to (their user, preferred, personal or
	company email)."""
	addresses = sorted(set(a.lower() for a in addresses if a))
	if not addresses:
		return []
	found = []
	for field in EMAIL_FIELDS:
		found += frappe.get_all("Employee", filters={field: ["in", addresses]}, pluck="name")
	return sorted(set(found))


def _employees_of(comm) -> tuple[list[str], str]:
	if comm.reference_doctype == "Employee" and comm.reference_name:
		return ([comm.reference_name] if frappe.db.exists("Employee", comm.reference_name) else []), "Email"
	if comm.reference_doctype == "Official Letter":
		to = _addresses(comm.recipients) + _addresses(comm.cc) + _addresses(comm.bcc)
		return employees_with_emails(to), "Official Letter"
	return [], ""


def _is_letter_email(doc) -> bool:
	return (
		doc.get("communication_type") in (None, "", "Communication")
		and doc.get("communication_medium") == "Email"
		and doc.get("sent_or_received") == "Sent"
		and doc.get("reference_doctype") in ("Employee", "Official Letter")
	)


def communication_after_insert(doc, method=None):
	"""An email sent from an Employee record or an Official Letter: a letter in the app too. Made
	after the commit, in the background: the email's attachments are added after this insert,
	and nothing here may hold up or undo the email itself."""
	try:
		if _is_letter_email(doc):
			frappe.enqueue(
				"trident_attendance.letters.letters_from_communication",
				communication=doc.name,
				queue="short",
				enqueue_after_commit=True,
			)
	except Exception:
		frappe.log_error(title="trident_attendance: letter from email could not be queued")


def letters_from_communication(communication):
	if not frappe.db.exists("Communication", communication):
		return
	comm = frappe.get_doc("Communication", communication)
	if not _is_letter_email(comm):
		return
	employees, source = _employees_of(comm)
	if not employees:
		return
	files = _files_of("Communication", comm.name)
	for employee in employees:
		make_letter(
			employee,
			comm.subject,
			comm.content,
			source,
			sent_by=comm.owner if comm.owner not in (None, "Guest") else comm.sender,
			communication=comm.name,
			files=files,
		)
	frappe.db.commit()  # a background job: nothing else commits it


# ---------------------------------------------------------------------------
# HR: Employee > Send to app, and what the form shows
# ---------------------------------------------------------------------------


def _employee_email(employee) -> str | None:
	"""Where to email the letter: the employee's preferred email, else personal, else company.
	Never their ERP login: many (the tpl-emp ones) are not real mailboxes."""
	row = (
		frappe.db.get_value(
			"Employee", employee, ["prefered_email", "personal_email", "company_email"], as_dict=True
		)
		or {}
	)
	for field in ("prefered_email", "personal_email", "company_email"):
		value = (row.get(field) or "").strip()
		if value and validate_email_address(value):
			return value
	return None


def _file_list(attachments) -> list[str]:
	if isinstance(attachments, str):
		attachments = frappe.parse_json(attachments) if attachments.strip().startswith("[") else [attachments]
	return [str(a).strip() for a in (attachments or []) if str(a or "").strip()]


@frappe.whitelist(methods=["POST"])
def send_to_app(employee, subject, message=None, attachments=None, also_email=0):
	"""HR's letter to one employee's app. `attachments`: the file URLs the dialog uploaded.
	`also_email`: sent as an email from the Employee record as well (the letter then comes from
	that email)."""
	_require_sender()
	if not employee or not frappe.db.exists("Employee", employee):
		return _refuse("no_employee")
	subject = (subject or "").strip()
	if not subject:
		return _refuse("no_subject")
	urls = _file_list(attachments)
	if len(urls) > MAX_ATTACHMENTS:
		return _refuse("too_many_files", MAX_ATTACHMENTS)
	if not (message or "").strip() and not urls:
		return _refuse("no_message")
	files = []
	for url in urls:
		row = frappe.db.get_value(
			"File", {"file_url": url}, ["name", "file_url", "file_name", "is_private"], as_dict=True
		)
		if row:
			files.append(row)

	if cint(also_email):
		to = _employee_email(employee)
		if not to:
			return _refuse("no_email")
		from frappe.core.doctype.communication.email import make

		sent = make(
			doctype="Employee",
			name=employee,
			recipients=to,
			subject=subject,
			content=message or "",
			send_email=1,
			attachments=[f.name for f in files],
		)
		return {"ok": True, "emailed": to, "communication": sent.get("name")}

	letter = make_letter(employee, subject, message, "Sent to app", files=files)
	return {"ok": True, "letter": letter}


@frappe.whitelist()
def employee_letters(employee, limit=10):
	"""The Employee form's list: the latest letters, whether each was opened, and the email to
	use (for "also email")."""
	_require_sender()
	rows = frappe.get_all(
		DOCTYPE,
		filters={"employee": employee},
		fields=["name", "subject", "sent_at", "source", "phones_notified", "opened_at", "times_opened"],
		order_by="sent_at desc",
		limit_page_length=min(cint(limit) or 10, MAX_LIMIT),
	)
	return {
		"ok": True,
		"letters": rows,
		"total": frappe.db.count(DOCTYPE, {"employee": employee}),
		"email": _employee_email(employee),
		"has_user": bool(frappe.db.get_value("Employee", employee, "user_id")),
	}


# ---------------------------------------------------------------------------
# The employee: My HR > Letters
# ---------------------------------------------------------------------------


def _mine(user, name):
	"""The letter, if it is one of the signed-in person's; else None (a colleague's and one that
	does not exist answer alike)."""
	if not name:
		return None
	employees = own_employees(user)
	if not employees:
		return None
	row = frappe.db.get_value(
		DOCTYPE,
		{"name": name, "employee": ["in", employees]},
		["name", "employee", "subject", "sent_at", "message", "source", "opened_at", "times_opened"],
		as_dict=True,
	)
	return row


@frappe.whitelist()
def list_my_letters(limit=DEFAULT_LIMIT, offset=0):
	"""The person's letters, newest first: `name`, `subject`, `sent_at`, `summary`, `opened`,
	`files`; and `unread`, how many they have not opened."""
	user = require_signed_in()
	employees = own_employees(user)
	if not employees:
		return {"ok": True, "items": [], "unread": 0}
	limit = min(cint(limit) or DEFAULT_LIMIT, MAX_LIMIT)
	rows = frappe.get_all(
		DOCTYPE,
		filters={"employee": ["in", employees]},
		fields=LIST_FIELDS,
		order_by="sent_at desc, creation desc",
		limit_start=max(cint(offset), 0),
		limit_page_length=limit,
	)
	counts = {}
	if rows:
		for f in frappe.get_all(
			"File",
			filters={"attached_to_doctype": DOCTYPE, "attached_to_name": ["in", [r.name for r in rows]]},
			fields=["attached_to_name"],
		):
			counts[f.attached_to_name] = counts.get(f.attached_to_name, 0) + 1
	return {
		"ok": True,
		"items": [
			{
				"name": r.name,
				"subject": r.subject,
				"sent_at": r.sent_at,
				"summary": summary(r.message or ""),
				"opened": bool(r.opened_at),
				"files": counts.get(r.name, 0),
			}
			for r in rows
		],
		"unread": frappe.db.count(DOCTYPE, {"employee": ["in", employees], "opened_at": ["is", "not set"]}),
	}


@frappe.whitelist(methods=["POST"])
def get_my_letter(name):
	"""One of the person's letters with its `html` and `files`; opening it is recorded (POST:
	it changes the letter)."""
	user = require_signed_in()
	row = _mine(user, name)
	if not row:
		return _refuse("not_found")
	now = now_datetime()
	values = {"last_opened_at": now, "times_opened": cint(row.times_opened) + 1}
	if not row.opened_at:
		values["opened_at"] = now
	frappe.db.set_value(DOCTYPE, row.name, values, update_modified=False)
	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": DOCTYPE, "attached_to_name": row.name},
		fields=["name", "file_name", "file_size"],
		order_by="creation asc",
	)
	return {
		"ok": True,
		"name": row.name,
		"subject": row.subject,
		"sent_at": row.sent_at,
		"html": absolute_file_links(sanitize_html(str(row.message or ""), always_sanitize=True), get_url()),
		"files": [{"id": f.name, "file_name": f.file_name, "size": cint(f.file_size)} for f in files],
	}


@frappe.whitelist(methods=["GET"])
def get_my_letter_file(letter, file):
	"""One file of one of the person's letters, as bytes. Read as Administrator after the check:
	the file is private and the person holds no permission on it."""
	user = require_signed_in()
	row = _mine(user, letter)
	if not row or not file:
		raise frappe.DoesNotExistError(_("No such file."))
	name = frappe.db.get_value(
		"File", {"name": file, "attached_to_doctype": DOCTYPE, "attached_to_name": row.name}
	)
	if not name:
		raise frappe.DoesNotExistError(_("No such file."))
	caller = frappe.session.user
	try:
		frappe.set_user("Administrator")
		doc = frappe.get_doc("File", name)
		content = doc.get_content()
		filename = doc.file_name or name
	finally:
		frappe.set_user(caller)
	if isinstance(content, str):
		content = content.encode()
	frappe.local.response.filename = filename
	frappe.local.response.filecontent = content
	frappe.local.response.type = "download"
