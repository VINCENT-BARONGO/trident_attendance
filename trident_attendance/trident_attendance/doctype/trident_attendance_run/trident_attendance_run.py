import json

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, now_datetime

KEEP_DAYS = 90


class TridentAttendanceRun(Document):
	pass


def record_run(trigger: str, summary: dict, duration: float, user: str | None = None):
	"""Persist one processing run so the Settings page can show whether the pipeline is alive."""
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Trident Attendance Run",
				"run_at": now_datetime(),
				"trigger": trigger,
				"user": user or frappe.session.user,
				"up_to": summary.get("up_to"),
				"days_evaluated": summary.get("days", 0),
				"marked": summary.get("marked", 0),
				"held": summary.get("held", 0),
				"errors": summary.get("errors", 0),
				"absent_marked": summary.get("absent", 0) or 0,
				"duration_seconds": round(duration, 2),
				"summary": json.dumps(summary.get("results", [])[:200], default=str),
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc.name
	except Exception:
		frappe.log_error(title="trident_attendance: could not record run")
		return None


def purge_old_runs():
	cutoff = add_days(now_datetime(), -KEEP_DAYS)
	for name in frappe.get_all("Trident Attendance Run", filters={"run_at": ["<", cutoff]}, pluck="name"):
		frappe.delete_doc("Trident Attendance Run", name, ignore_permissions=True, delete_permanently=True)
