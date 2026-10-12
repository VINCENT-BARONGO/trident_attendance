"""Announcements for the employee app: the Newsletters HR ticked "Show in employee app".

Any signed-in user may read them (the box is the permission, not Newsletter's own rules, which
would keep every employee out). The message is rendered to HTML here, cleaned with Frappe's
sanitiser and handed over with absolute links, so the web app shows it as it is.
"""

import html as html_entities
import re

import frappe
from frappe import _
from frappe.utils import cint, get_url, md_to_html, sanitize_html

from trident_attendance.utils import require_signed_in

ANNOUNCEMENT_FIELD = "custom_app_announcement"
ANNOUNCED_AT_FIELD = "custom_app_announced_at"

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
SUMMARY_LENGTH = 160

FIELDS = ["name", "subject", "content_type", "message", "message_md", "message_html", ANNOUNCED_AT_FIELD, "modified"]

# href="/files/..." and src='/private/files/...': the ERP's own uploads, written relative.
RELATIVE_FILE_LINK = re.compile(r"""((?:href|src)\s*=\s*["'])(/(?:private/)?files/)""", re.IGNORECASE)

REASONS = {
	"not_found": "No such announcement.",
}


def _refuse(reason: str, **extra) -> dict:
	return {"ok": False, "reason": reason, "message": _(REASONS[reason]), **extra}


def render_html(row) -> str:
	"""The newsletter's message as safe HTML: Rich Text as it is, Markdown rendered, HTML as it
	is; then sanitised, with the ERP's own file links made absolute."""
	content_type = row.get("content_type")
	if content_type == "Markdown":
		html = md_to_html(row.get("message_md") or "")
	elif content_type == "HTML":
		html = row.get("message_html") or ""
	else:
		html = row.get("message") or ""
	html = sanitize_html(str(html or ""), always_sanitize=True)
	return absolute_file_links(html)


def absolute_file_links(html: str, base: str | None = None) -> str:
	base = (base or get_url()).rstrip("/")
	return RELATIVE_FILE_LINK.sub(lambda m: f"{m.group(1)}{base}{m.group(2)}", html)


def plain_text(html: str) -> str:
	"""The words of the message. A tag becomes a space, or a heading runs into the paragraph
	after it; what a script or style element holds is not text."""
	text = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", html or "")
	text = html_entities.unescape(re.sub(r"(?s)<[^>]*>", " ", text))
	return re.sub(r"\s+", " ", text).strip()


def summary(html: str, length: int = SUMMARY_LENGTH) -> str:
	"""The first `length` or so characters of the plain text, cut at a word."""
	text = plain_text(html)
	if len(text) <= length:
		return text
	cut = text[:length]
	if " " in cut[length // 2 :]:
		cut = cut[: cut.rfind(" ")]
	return cut.rstrip(" ,;:") + "…"


def shown_at(row):
	return row.get(ANNOUNCED_AT_FIELD) or row.get("modified")


@frappe.whitelist()
def list_announcements(limit=DEFAULT_LIMIT, offset=0):
	"""The announcements, newest first: `name`, `subject`, `shown_at`, `summary`."""
	require_signed_in()
	limit = min(cint(limit) or DEFAULT_LIMIT, MAX_LIMIT)
	offset = max(cint(offset), 0)
	rows = frappe.get_all(
		"Newsletter",
		filters={ANNOUNCEMENT_FIELD: 1},
		fields=FIELDS,
		order_by=f"{ANNOUNCED_AT_FIELD} desc, modified desc",
		limit_start=offset,
		limit_page_length=limit,
	)
	return {
		"ok": True,
		"items": [
			{"name": row.name, "subject": row.subject, "shown_at": shown_at(row), "summary": summary(render_html(row))}
			for row in rows
		],
	}


@frappe.whitelist()
def get_announcement(name):
	"""One announcement with its `html`. A newsletter not marked for the app answers
	`not_found`, exactly like one that does not exist."""
	require_signed_in()
	row = None
	if name:
		row = frappe.db.get_value("Newsletter", {"name": name, ANNOUNCEMENT_FIELD: 1}, FIELDS, as_dict=True)
	if not row:
		return _refuse("not_found")
	return {"ok": True, "name": row.name, "subject": row.subject, "shown_at": shown_at(row), "html": render_html(row)}
