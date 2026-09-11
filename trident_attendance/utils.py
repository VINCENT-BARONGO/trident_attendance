"""Small helpers shared by the check-in pipeline, API and reports."""

from datetime import datetime, time as dtime, timedelta

import frappe
from frappe.utils import add_days, cint, get_datetime, get_time, getdate, now_datetime, nowdate

SETTINGS_DOCTYPE = "Trident Attendance Settings"
REVIEWER_ROLES = {"Attendance Admin", "System Manager"}
VIEWER_ROLES = REVIEWER_ROLES | {"HR Manager", "HR User"}
SUPERVISOR_ROLES = {"Attendance Marking", "Attendance Admin", "System Manager"}

# Punches this app creates itself carry this prefix so the instant rules skip them.
INTERNAL_SOURCE_PREFIX = "trident_attendance/"

STATUS_PENDING = "Pending Review"
STATUS_AUTO_RELEASED = "Auto-Released"
STATUS_RELEASED = "Released"
STATUS_MARKED = "Marked"
STATUS_REJECTED = "Rejected"


def get_settings():
	return frappe.get_cached_doc(SETTINGS_DOCTYPE)


def in_scope(doc, settings=None) -> bool:
	"""Only punches from the mobile app (or everything, per settings) enter the review pipeline."""
	settings = settings or get_settings()
	if settings.staged_sources == "All sources":
		return True
	return bool(doc.get("custom_app_source"))


def scope_filters(settings=None) -> dict:
	settings = settings or get_settings()
	if settings.staged_sources == "All sources":
		return {}
	return {"custom_app_source": ["is", "set"]}


def employee_for_user(user: str | None) -> str | None:
	if not user or user in ("Guest", "Administrator"):
		return None
	return frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")


def is_reviewer(user: str | None = None) -> bool:
	return bool(REVIEWER_ROLES & set(frappe.get_roles(user)))


def is_viewer(user: str | None = None) -> bool:
	return bool(VIEWER_ROLES & set(frappe.get_roles(user)))


# Custom child table behind Project > Allowed Users on the live site. The office assigns
# supervisors there; few are in the standard Project > Users table.
ALLOWED_USERS_DOCTYPE = "Project Allowed User"


def assigned_projects(user: str | None = None) -> set[str]:
	"""Projects a user is assigned to: Project > Allowed Users (when the site has that table)
	or the standard Project > Users. Roles never widen this."""
	user = user or frappe.session.user
	names = set(frappe.get_all("Project User", filters={"user": user, "parenttype": "Project"}, pluck="parent"))
	if frappe.db.exists("DocType", ALLOWED_USERS_DOCTYPE):
		names.update(
			frappe.get_all(ALLOWED_USERS_DOCTYPE, filters={"user": user, "parenttype": "Project"}, pluck="parent")
		)
	return names


def day_bounds(day) -> tuple[str, str]:
	day = getdate(day)
	return f"{day} 00:00:00", f"{day} 23:59:59"


def last_complete_date(settings=None, force_today: bool = False):
	"""Latest calendar date whose punches may be turned into Attendance."""
	settings = settings or get_settings()
	today = getdate(nowdate())
	if force_today:
		return today
	cutoff = get_time(settings.day_cutoff_time or "20:00:00")
	if now_datetime().time() >= cutoff:
		return today
	return add_days(today, -1)


def is_day_complete(day, settings=None) -> bool:
	return getdate(day) <= last_complete_date(settings)


def split_reasons(text: str | None) -> list[str]:
	return [r.strip() for r in (text or "").splitlines() if r.strip()]


def join_reasons(reasons) -> str | None:
	seen, out = set(), []
	for r in reasons:
		if r and r not in seen:
			seen.add(r)
			out.append(r)
	return "\n".join(out) or None


def combine_datetime(day, t) -> datetime:
	if isinstance(t, str):
		t = get_time(t)
	if isinstance(t, timedelta):
		# MariaDB returns Time columns as timedelta.
		return datetime.combine(getdate(day), dtime(0, 0)) + t
	if isinstance(t, dtime):
		return datetime.combine(getdate(day), t)
	return get_datetime(t)


def cbool(value) -> bool:
	return bool(cint(value))
