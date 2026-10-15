"""Whether a person is clocked in right now, as the attendance hub says at punch time.

Every punch, from a phone or a site terminal, goes to the attendance hub first, and the hub
writes Employee Checkins to the ERP in a batch at night. During the day the ERP therefore has
no check-in of that day, and nothing here reads Employee Checkin. The hub calls `punched`
right after it accepts a punch instead.

`punched` does two things:

- It keeps the employee's latest punch in a `Shift Signal` row, one per employee: IN or OUT,
  and when. `is_clocked_in` reads it; asking for a person's location (`locate`) depends on it.
- It tells the employee's own phones, with a push that carries data alone and shows nothing,
  that they are clocked in or out. The employee app reports the person's position to Traccar
  from an "in" until an "out", wherever the punch was made.
"""

import functools
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone, now_datetime

from trident_attendance import push
from trident_attendance.employee_access import _require_hub
from trident_attendance.utils import require_signed_in

SIGNAL_DOCTYPE = "Shift Signal"
LOG_TYPES = {"IN": "in", "OUT": "out"}
SOURCE_LENGTH = 40
PLACE_LENGTH = 80
# Where the "Clocked in" notification opens: My HR's Attendance.
CLOCK_URL = "/ess/attendance"

# An IN older than this is kept but not sent to the phone: the hub can pass a punch on late,
# and a late IN must not start the phone's reporting. An OUT is sent however late it is.
RECENT_PUNCH = timedelta(minutes=15)
# A clock may run ahead; `api.sync_checkin` takes a punch up to this far in the future too.
PUNCH_MAX_AHEAD = timedelta(minutes=10)
# An IN older than this no longer means "clocked in": the OUT never came.
CLOCKED_IN_FOR = timedelta(hours=18)


def _require_hub_or_system_manager():
	"""The hub's own login, by the check of the hub-only sign-in methods
	(`employee_access._require_hub`: the Hub Service User of Trident Attendance Settings,
	holding Attendance Admin or System Manager; anyone else gets its 403 `HubOnlyError`). A
	System Manager may call it too."""
	user = require_signed_in()
	if "System Manager" in frappe.get_roles(user):
		return
	_require_hub()


def _answer(pushed: int = 0, reason: str | None = None, ok: bool = True) -> dict:
	answer = {"ok": ok, "pushed": pushed}
	if reason:
		answer["reason"] = reason
	return answer


def _refuse(reason: str) -> dict:
	return _answer(0, reason, ok=False)


def _site_timezone() -> ZoneInfo:
	return ZoneInfo(get_system_timezone())


def _punch_time(at) -> datetime | None:
	"""`at` as the ERP keeps times: in the site's time zone, without an offset, to the second.
	ISO 8601 ("2026-10-13T07:58:00+03:00", "...Z") or the ERP's own "2026-10-13 07:58:00"; a
	time without an offset is taken as the site's. None when it is no time."""
	text = "" if at is None else str(at).strip()
	when = None
	if text:
		try:
			when = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1] in "Zz" else text)
		except ValueError:
			try:
				when = get_datetime(text)
			except Exception:
				when = None
	if not isinstance(when, datetime):
		return None
	if when.tzinfo:
		when = when.astimezone(_site_timezone()).replace(tzinfo=None)
	return when.replace(microsecond=0)


def _iso(when: datetime) -> str:
	"""ISO 8601 with the site's offset from UTC ("2026-10-13T07:58:00+03:00")."""
	return when.replace(microsecond=0, tzinfo=_site_timezone()).isoformat()


def _record(employee: str, log_type: str, when: datetime, source: str | None) -> str | None:
	"""Keeps the punch as the employee's Shift Signal when it is newer than the one kept: None.
	Otherwise why not: "duplicate" for a punch of the same time as the one kept, "older" for
	one from before it, arriving late."""
	values = {"log_type": log_type, "at": when, "source": source, "received_at": now_datetime()}
	row = frappe.db.get_value(SIGNAL_DOCTYPE, {"employee": employee}, ["name", "at"], as_dict=True, for_update=True)
	if not row:
		signal = frappe.new_doc(SIGNAL_DOCTYPE)
		signal.update({"employee": employee, **values})
		signal.flags.from_punched = True
		signal.flags.ignore_permissions = True
		try:
			signal.insert()
			return None
		except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
			# Another call for this employee got there first; its row is compared like any other.
			row = frappe.db.get_value(SIGNAL_DOCTYPE, {"employee": employee}, ["name", "at"], as_dict=True, for_update=True)
			if not row:
				raise
	if row.at and get_datetime(row.at) >= when:
		return "duplicate" if get_datetime(row.at) == when else "older"
	signal = frappe.get_doc(SIGNAL_DOCTYPE, row.name)
	signal.update(values)
	signal.flags.from_punched = True
	signal.flags.ignore_permissions = True
	signal.save()
	return None


def _never_answers_with_an_error_status_for_one_punch(method):
	"""The hub reads a 404 or a 417 from this method as "this ERP does not have it" and stops
	calling it for the whole company for an hour, and Frappe answers 417 for every validation
	error. So nothing about one person or one punch is raised: it is answered with HTTP 200 and
	a reason. Something unexpected is a 500, logged as its type. Only a caller who is not the
	hub gets an error of their own (403)."""

	@functools.wraps(method)
	def guarded(*args, **kwargs):
		try:
			return method(*args, **kwargs)
		except frappe.PermissionError:
			raise
		except Exception as e:
			frappe.db.rollback()
			trace = "".join(traceback.format_tb(e.__traceback__))
			frappe.log_error(title="trident_attendance: shift.punched failed", message=f"{type(e).__name__}\n{trace}")
			frappe.local.response.http_status_code = 500
			return _refuse("server_error")

	return guarded


@frappe.whitelist(methods=["POST"])
@_never_answers_with_an_error_status_for_one_punch
def punched(employee=None, log_type=None, at=None, source=None, place=None):
	"""From the attendance hub, right after it accepts a punch: `employee` (the Employee ID)
	clocked `log_type` (IN or OUT) at `at`, on `source` ("terminal", "phone"; a label only), at
	`place` (where, as people call it; optional, hub 0.36.0).

	A punch made in the last 15 minutes is also told to the person as a notification they see:
	"Clocked in" / "Clocked out", "07:52 · Isinya factory", which opens My HR's Attendance. So
	someone who scanned at a terminal knows it was taken.

	The punch becomes the employee's Shift Signal when it is newer than the one kept. Then the
	employee's own phones are told with the data-only "shift" push, when the punch was made in
	the last 15 minutes or is an OUT. An IN that arrives later than that is kept but not sent:
	it must not start the phone's reporting hours after the fact. An OUT is sent however late,
	because a phone left reporting is worse than an OUT told twice.

	The answer is HTTP 200 whatever is wrong with the person or the punch (see
	`_never_answers_with_an_error_status_for_one_punch`):

	- `{"ok": true, "pushed": n}`: kept, and n phones were told.
	- `{"ok": true, "pushed": 0, "reason": "stale"}`: an IN older than 15 minutes; kept, not sent.
	- `{"ok": false, "reason": ..., "pushed": 0}`, where the reason is one of:
	  `bad_log_type`, `unknown_employee`, `inactive`, `bad_time` (no time, or more than 10
	  minutes ahead): nothing is kept.
	  `duplicate` (the punch kept already has this time), `older` (a punch from before the one
	  kept, arriving late): nothing changes and nothing is sent.
	  `no_user`, `no_device`, `push_disabled`, `push_failed`: the signal is kept, nobody is told.

	A caller who is not the hub: permission error (403)."""
	_require_hub_or_system_manager()
	log_type = str(log_type or "").strip().upper()
	if log_type not in LOG_TYPES:
		return _refuse("bad_log_type")
	employee = str(employee or "").strip()
	row = frappe.db.get_value("Employee", employee, ["name", "user_id", "status"], as_dict=True) if employee else None
	if not row:
		return _refuse("unknown_employee")
	if row.status != "Active":
		return _refuse("inactive")
	when = _punch_time(at)
	now = now_datetime()
	if not when or when > now + PUNCH_MAX_AHEAD:
		return _refuse("bad_time")
	source = str(source or "").strip()[:SOURCE_LENGTH] or None
	place = " ".join(str(place or "").split())[:PLACE_LENGTH]

	not_kept = _record(row.name, log_type, when, source)
	if not_kept:
		return _refuse(not_kept)
	if log_type == "IN" and when < now - RECENT_PUNCH:
		return _answer(0, "stale")
	if not row.user_id:
		return _refuse("no_user")
	if not push.push_enabled():
		return _refuse("push_disabled")
	try:
		data = {"shift": LOG_TYPES[log_type], "at": _iso(when), "employee": row.name}
		pushed = push.instruct_user(row.user_id, "shift", data, push.SHIFT_TTL)
	except Exception as e:
		# The signal is kept whatever happens to the push; the hub is not asked to try again.
		frappe.log_error(title="trident_attendance: shift push failed", message=type(e).__name__)
		return _refuse("push_failed")
	if when >= now - RECENT_PUNCH:
		try:
			title = "Clocked in" if log_type == "IN" else "Clocked out"
			body = f"{when:%H:%M} · {place}" if place else f"{when:%H:%M}"
			push.notify_user(row.user_id, title, body, CLOCK_URL, "clock")
		except Exception as e:
			# Only the person's notice is lost; the tracking instruction above stands.
			frappe.log_error(title="trident_attendance: clock notification failed", message=type(e).__name__)
	return _answer(pushed) if pushed else _refuse("no_device")


def is_clocked_in(employee: str) -> bool:
	"""Whether the employee's Shift Signal is an IN made in the last 18 hours. Employee
	Checkin is not looked at: check-ins reach the ERP at night, so during the day it holds
	none of that day. An IN older than 18 hours is a shift whose OUT never came."""
	row = frappe.db.get_value(SIGNAL_DOCTYPE, {"employee": employee}, ["log_type", "at"], as_dict=True)
	if not row or row.log_type != "IN" or not row.at:
		return False
	return get_datetime(row.at) >= now_datetime() - CLOCKED_IN_FOR


# ---------------------------------------------------------------------------
# Employee doc events
# ---------------------------------------------------------------------------


def on_employee_trash(doc, method=None):
	frappe.db.delete(SIGNAL_DOCTYPE, {"employee": doc.name})


def before_employee_rename(doc, method, old, new, merge=False):
	"""Frappe moves the signal's employee to the new ID itself. When two employees are merged
	the old one's signal is dropped first: there is one row per employee."""
	if merge:
		frappe.db.delete(SIGNAL_DOCTYPE, {"employee": old})
