"""ERPNext's own errors (Error Log), summarised for IT Help's error watch in the web app.

Reading Error Log needs System Manager, which the IT Help login must not have. This method answers only a
summary: per kind of error (its method/title), how many since a time, the first and last time, the
document it was about, and the LAST LINE of the latest traceback (the exception and its message). The
developer opens the ERP for the full traceback. Read-only.

Allowed: System Manager, or the Helpdesk "Agent" role (the IT Help integration login has it).
"""

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

ROLES = {"System Manager", "Agent"}
MAX_KINDS = 50
SCAN = 5000  # newest rows looked at per call: a flood cannot make the call slow


def _last_line(trace: str) -> str:
	lines = [x.strip() for x in (trace or "").strip().splitlines() if x.strip()]
	return (lines[-1] if lines else "")[:300]


@frappe.whitelist(methods=["GET"])
def error_summary(since: str | None = None) -> dict:
	"""{"now", "since", "kinds": [{"title", "count", "first", "last", "reference_doctype",
	"reference_name", "message"}]}, the busiest kinds first. ``since`` defaults to an hour ago and is
	capped at 7 days back."""
	if not (ROLES & set(frappe.get_roles())):
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	now = now_datetime()
	floor = frappe.utils.add_days(now, -7)
	start = get_datetime(since) if since else frappe.utils.add_to_date(now, hours=-1)
	start = max(start, floor)
	rows = frappe.get_all(
		"Error Log",
		filters={"creation": [">", start]},
		fields=["method", "error", "reference_doctype", "reference_name", "creation"],
		order_by="creation desc",
		limit_page_length=SCAN,
		ignore_permissions=True,
	)
	kinds: dict[str, dict] = {}
	for r in rows:  # newest first: the first row of a kind is its latest
		title = (r.method or "").strip()[:140] or _last_line(r.error)[:140] or "(no title)"
		k = kinds.get(title)
		if k is None:
			kinds[title] = {
				"title": title, "count": 0, "first": r.creation, "last": r.creation,
				"reference_doctype": r.reference_doctype or "", "reference_name": r.reference_name or "",
				"message": _last_line(r.error),
			}
			k = kinds[title]
		k["count"] += 1
		k["first"] = r.creation
	out = sorted(kinds.values(), key=lambda k: (-k["count"], k["title"]))[:MAX_KINDS]
	for k in out:
		k["first"], k["last"] = str(k["first"]), str(k["last"])
	return {"now": str(now), "since": str(start), "scanned": len(rows), "kinds": out}
