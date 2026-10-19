"""Asking where a clocked-in employee is.

The employee app reports a person's position to Traccar while they are on shift. Someone
allowed to may ask for a position now: the person's phone is sent a push that carries data
alone and sends one position to Traccar. The ERP never holds the position itself.

The one who asked is then told where that is, in words (`location_answer`): the site, when the
position is inside a project's geofence, or else the place's name from OpenStreetMap. Never the
coordinates. The ERP reads the position from Traccar for that answer and keeps nothing of it.

The rules:

- Only while the person is clocked in: the attendance hub's latest punch for them
  (`shift.is_clocked_in`, the Shift Signal) is an IN of the last 18 hours. Nobody can be
  located before they clock in, after they clock out, or on a day off. Employee Checkin is
  not looked at: check-ins reach the ERP at night, so during the day it holds none of that day.
- Only by the role named in Trident Attendance Settings > Role that may locate people, or by a
  System Manager. No role named = System Managers only.
- The person is told up front, in the phone app, that the office may ask for their location
  while they are clocked in; a single request shows them nothing; every request is recorded.
- The record is the `Location Request` row: who asked about whom, and when. It is the only
  trace of a request, so it is written before the phone is asked, by this module alone, and
  cannot be changed afterwards.
- One request per employee a minute, whoever asks.
"""

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import frappe
import requests
from frappe import _
from frappe.utils import cint, flt, get_datetime, get_system_timezone, now_datetime
from hrms.hr.utils import get_distance_between_coordinates

from trident_attendance import push
from trident_attendance.shift import is_clocked_in
from trident_attendance.utils import get_settings, require_signed_in

REQUEST_DOCTYPE = "Location Request"
ROLE_FIELD = "locate_role"
ALWAYS_ALLOWED_ROLE = "System Manager"
# Seconds before the same employee can be asked about again, by anyone.
REQUEST_INTERVAL = 60

# The answer to a request can be read for this long after it was made, by the one who made it.
ANSWER_WINDOW = timedelta(minutes=3)
# A position Traccar received this long before the request still counts as the phone's answer:
# the phone's and the ERP's clocks need not agree to the second.
ANSWER_SLACK = timedelta(seconds=5)
# How old a fix the phone may answer with. Asked for "the current location", Android hands
# back one it already has when it is recent enough, so the fix can be older than the request
# while Traccar received it after. Traccar's `from` is on the fix time, so it is asked this
# much further back and the answer is told by when Traccar received it.
ANSWER_FIX_AGE = timedelta(minutes=15)
# A project without a radius of its own is a 200 m geofence, as for punches (checkin_rules).
DEFAULT_RADIUS = 200
HTTP_TIMEOUT = 8
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
# OpenStreetMap's public geocoder takes at most one request a second from an application.
NOMINATIM_INTERVAL_S = 1.0
NOMINATIM_LAST_CALL = "trident_attendance:nominatim_last_call"


def _may_locate(user: str) -> bool:
	roles = set(frappe.get_roles(user))
	if ALWAYS_ALLOWED_ROLE in roles:
		return True
	role = get_settings().get(ROLE_FIELD)
	return bool(role) and role in roles


def _asked_a_moment_ago(employee: str) -> bool:
	since = now_datetime() - timedelta(seconds=REQUEST_INTERVAL)
	return bool(frappe.db.exists(REQUEST_DOCTYPE, {"employee": employee, "requested_at": [">", since]}))


@frappe.whitelist(methods=["GET"])
def may_locate():
	"""Whether the signed-in user may ask for a person's location. For the Employee form, which
	shows "Locate now" by it; `request_location` checks again."""
	return _may_locate(require_signed_in())


@frappe.whitelist(methods=["POST"])
def request_location(employee):
	"""Asks the employee's phones for one position now and records who asked. The phone shows
	nothing. The answer is `{"request", "employee", "employee_name", "devices"}`; the position
	itself goes from the phone to Traccar, not through the ERP.

	Refused with a permission error for a caller without the role, and with a validation error
	that says which when the employee is not active, has no user, has no phone registered, is
	not clocked in, or was asked about less than a minute ago."""
	user = require_signed_in()
	if not _may_locate(user):
		frappe.throw(_("You may not ask for a person's location."), frappe.PermissionError)

	employee = str(employee or "").strip()
	fields = ["name", "employee_name", "status", "user_id"]
	row = frappe.db.get_value("Employee", employee, fields, as_dict=True) if employee else None
	if not row:
		frappe.throw(_("No such employee: {0}").format(employee), frappe.ValidationError)
	who = row.employee_name or row.name
	if row.status != "Active":
		frappe.throw(_("{0} is not an active employee.").format(who), frappe.ValidationError)
	if not row.user_id:
		frappe.throw(_("{0} has no ERP user, so no phone can be asked.").format(who), frappe.ValidationError)
	if not push.push_enabled():
		frappe.throw(_("Push notifications are switched off in Trident Attendance Settings."), frappe.ValidationError)
	devices = push.enabled_devices(row.user_id)
	if not devices:
		frappe.throw(_("{0} has no phone registered.").format(who), frappe.ValidationError)
	if not is_clocked_in(row.name):
		frappe.throw(_("{0} is not clocked in.").format(who), frappe.ValidationError)
	if _asked_a_moment_ago(row.name):
		frappe.throw(_("{0} was asked a moment ago. Try again in a minute.").format(who), frappe.ValidationError)

	request = frappe.new_doc(REQUEST_DOCTYPE)
	request.update(
		{
			"employee": row.name,
			"employee_name": row.employee_name,
			"requested_by": user,
			"requested_at": now_datetime(),
			"device_count": devices,
		}
	)
	request.flags.from_request_location = True
	request.flags.ignore_permissions = True
	request.insert()

	data = {"request": request.name, "employee": row.name, "url": "/ess/"}
	push.instruct_user(row.user_id, "locate", data, push.LOCATE_TTL)
	return {"request": request.name, "employee": row.name, "employee_name": row.employee_name, "devices": devices}


@frappe.whitelist(methods=["GET"])
def location_answer(request):
	"""Where the phone said it is, for the one who made `request` (a Location Request), in its
	first three minutes. The Employee form asks every few seconds after "Locate now".

	`{"status": ...}`, where the status is:

	- `found`: also `site` and `site_name` (a project whose geofence it is inside, else None),
	  `place` (the place's name, from OpenStreetMap, when it is at no site), `attribution`
	  (OpenStreetMap's credit, shown with a place), `at` (the position's time, site time),
	  `minutes_ago` and `accuracy_m` (None when the phone did not say). Never the coordinates.
	- `waiting`: the phone has not answered yet.
	- `not_set_up`: no Traccar address in Trident Attendance Settings; the position is in
	  Traccar alone.
	- `no_device`: Traccar has no device for this employee (its Identifier is the Employee ID).
	- `traccar_error`: Traccar could not be read; `message` says how.

	Nothing of the position is stored, logged or cached: it is read, put into words and
	answered."""
	user = require_signed_in()
	if not _may_locate(user):
		frappe.throw(_("You may not ask for a person's location."), frappe.PermissionError)
	row = frappe.db.get_value(
		REQUEST_DOCTYPE, str(request or "").strip(), ["employee", "requested_by", "requested_at"], as_dict=True
	)
	if not row or row.requested_by != user:
		frappe.throw(_("No such location request of yours."), frappe.PermissionError)
	asked = get_datetime(row.requested_at)
	if now_datetime() - asked > ANSWER_WINDOW:
		frappe.throw(_("This location request is over. Press Locate now again."), frappe.ValidationError)

	settings = get_settings()
	base = (settings.get("traccar_url") or "").strip().rstrip("/")
	if not base:
		return {"status": "not_set_up"}
	try:
		position = _latest_position(base, settings, row.employee, _site_to_utc(asked - ANSWER_SLACK))
	except _NoDevice:
		return {"status": "no_device"}
	except _TraccarError as e:
		return {"status": "traccar_error", "message": str(e)}
	if position is None:
		return {"status": "waiting"}
	return {"status": "found", **_describe(position)}


class _NoDevice(Exception):
	pass


class _TraccarError(Exception):
	"""Said to the one who asked, so it names what went wrong and nothing of the position."""


def _site_tz() -> ZoneInfo:
	return ZoneInfo(get_system_timezone())


def _site_to_utc(when: datetime) -> datetime:
	return when.replace(tzinfo=_site_tz()).astimezone(timezone.utc)


def _parse_time(text) -> datetime | None:
	try:
		when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
	except (TypeError, ValueError):
		return None
	return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _traccar_auth(settings) -> dict:
	"""Basic with the Traccar user and password; with no user, the password field holds a
	Traccar API token."""
	username = (settings.get("traccar_username") or "").strip()
	secret = settings.get_password("traccar_password", raise_exception=False) or ""
	if username:
		return {"auth": (username, secret)}
	return {"headers": {"Authorization": f"Bearer {secret}"}} if secret else {}


def _traccar_get(base, settings, path, params):
	try:
		r = requests.get(f"{base}{path}", params=params, timeout=HTTP_TIMEOUT, **_traccar_auth(settings))
	except requests.RequestException as e:
		raise _TraccarError(_("Traccar could not be reached ({0}).").format(type(e).__name__)) from None
	if r.status_code in (401, 403):
		raise _TraccarError(_("Traccar refused the login in Trident Attendance Settings."))
	if r.status_code != 200:
		raise _TraccarError(_("Traccar answered HTTP {0}.").format(r.status_code))
	try:
		return r.json()
	except ValueError:
		raise _TraccarError(_("Traccar's answer could not be read.")) from None


def _latest_position(base, settings, employee, since_utc: datetime) -> dict | None:
	"""The newest position Traccar received from the employee's device since `since_utc`, or None.
	Its fix may be older (ANSWER_FIX_AGE)."""
	devices = _traccar_get(base, settings, "/api/devices", {"uniqueId": employee})
	device = next((d for d in devices or [] if str(d.get("uniqueId")) == employee), None)
	if not device:
		raise _NoDevice()
	now_utc = datetime.now(timezone.utc)
	params = {
		"deviceId": device["id"],
		"from": (since_utc - ANSWER_FIX_AGE).isoformat().replace("+00:00", "Z"),
		"to": (now_utc + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
	}
	latest = None
	for p in _traccar_get(base, settings, "/api/positions", params) or []:
		received = _parse_time(p.get("serverTime")) or _parse_time(p.get("fixTime"))
		if received is None or received < since_utc or not (flt(p.get("latitude")) or flt(p.get("longitude"))):
			continue
		if latest is None or received > latest[0]:
			latest = (received, p)
	return latest[1] if latest else None


def _describe(position: dict) -> dict:
	lat, lon = flt(position.get("latitude")), flt(position.get("longitude"))
	fixed = _parse_time(position.get("fixTime")) or _parse_time(position.get("serverTime"))
	accuracy = flt(position.get("accuracy"))
	answer = {
		"site": None,
		"site_name": None,
		"place": None,
		"attribution": None,
		"at": fixed.astimezone(_site_tz()).strftime("%Y-%m-%d %H:%M") if fixed else None,
		"minutes_ago": max(0, int((datetime.now(timezone.utc) - fixed).total_seconds() // 60)) if fixed else None,
		"accuracy_m": int(round(accuracy)) if accuracy > 0 else None,
	}
	site = _site_at(lat, lon)
	if site:
		answer["site"], answer["site_name"] = site.name, site.project_name or site.name
		return answer
	answer["place"] = _place_name(lat, lon)
	if answer["place"]:
		answer["attribution"] = OSM_ATTRIBUTION
	return answer


def _site_at(lat: float, lon: float):
	"""The open project whose geofence holds the point, the nearest when several do. The fence
	is the one punches are held by: the project's radius plus the settings' tolerance. A project
	with a radius of 0 has no fence and is never "here"."""
	tolerance = cint(get_settings().get("geofence_tolerance_meters"))
	best = None
	for p in frappe.get_all(
		"Project",
		filters={"status": "Open"},
		fields=["name", "project_name", "custom_site_latitude", "custom_site_longitude", "custom_geofence_radius_meters"],
	):
		if not (flt(p.custom_site_latitude) and flt(p.custom_site_longitude)):
			continue
		radius = DEFAULT_RADIUS if p.custom_geofence_radius_meters is None else cint(p.custom_geofence_radius_meters)
		if radius <= 0:
			continue
		distance = get_distance_between_coordinates(flt(p.custom_site_latitude), flt(p.custom_site_longitude), lat, lon)
		if distance <= radius + tolerance and (best is None or distance < best[0]):
			best = (distance, p)
	return best[1] if best else None


def _place_name(lat: float, lon: float) -> str | None:
	"""The place's name from OpenStreetMap's geocoder: "Mombasa Road, Syokimau, Machakos". None
	when it cannot be had. Kept to OpenStreetMap's rules: one request a second at most, from an
	application that names itself."""
	_wait_for_nominatim()
	try:
		r = requests.get(
			NOMINATIM_URL,
			params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 17, "addressdetails": 1},
			headers={
				"User-Agent": f"trident_attendance/locate ({frappe.utils.get_url()})",
				"Accept-Language": "en",
			},
			timeout=HTTP_TIMEOUT,
		)
		data = r.json() if r.status_code == 200 else None
	except (requests.RequestException, ValueError):
		data = None
	if not data or data.get("error"):
		return None
	return _words_for(data.get("address") or {}) or None


def _words_for(address: dict) -> str:
	"""A road, the area and the town, as far as OpenStreetMap knows them, without repeats."""
	groups = (
		("road", "pedestrian", "footway", "path", "amenity", "building"),
		("neighbourhood", "suburb", "quarter", "hamlet", "village", "residential"),
		("town", "city", "municipality", "county", "state_district", "state"),
	)
	words = []
	for keys in groups:
		value = next((address[k] for k in keys if address.get(k)), None)
		if value and value not in words:
			words.append(value)
	return ", ".join(words)


def _wait_for_nominatim():
	cache = frappe.cache()
	last = flt(cache.get_value(NOMINATIM_LAST_CALL))
	pause = NOMINATIM_INTERVAL_S - (time.time() - last)
	if pause > 0:
		time.sleep(min(pause, NOMINATIM_INTERVAL_S))
	cache.set_value(NOMINATIM_LAST_CALL, time.time(), expires_in_sec=60)
