"""Push notifications to the employee app's phones through Firebase Cloud Messaging (HTTP v1).

A phone registers its FCM token after each sign-in (`register_app_device`) and takes it away at
sign-out. The HR hooks in `push_hooks` and the settings page's test button call `notify_user` /
`notify_all`, which queue one background job per batch of devices; the job POSTs one message
per enabled device, carrying an OAuth2 bearer made from the Firebase service-account key in
Trident Attendance Settings (or site_config `fcm_service_account`).

Nothing here writes a device token, a message body or the key to a log: kinds and counts only.
An unexpected error is logged as its type and where it happened, never with the request's
values, which in these functions include the key.
"""

import functools
import hashlib
import json
import traceback
from datetime import datetime, timezone
from urllib.parse import quote

import frappe
import requests
from frappe import _
from frappe.utils import cint, now_datetime

from trident_attendance.utils import get_settings, require_signed_in

DEVICE_DOCTYPE = "App Device"
PLATFORMS = ("android", "ios")
TOKEN_MIN_LENGTH = 20
TOKEN_MAX_LENGTH = 512

KINDS = ("announcement", "leave", "payslip", "erp", "test")
ANDROID_CHANNEL = "trident_employee"

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
SEND_TIMEOUT = 10
# Devices per background job: the short queue allows 300 s, and a request takes well under 1 s.
BATCH_SIZE = 200

ACCESS_TOKEN_CACHE_KEY = "trident_attendance:fcm_access_token"
# The cached bearer is dropped this long before Google says it expires.
ACCESS_TOKEN_MARGIN = 300
DEFAULT_ACCESS_TOKEN_SECONDS = 3600

# Firebase's words for a token it no longer knows (the app was removed, or the token rotated).
GONE_CODES = ("UNREGISTERED", "NOT_FOUND")

REASONS = {
	"token_invalid": "The device token must be 20 to 512 characters long.",
	"invalid_platform": "platform must be 'android' or 'ios'.",
	"push_disabled": "Push notifications are switched off in Trident Attendance Settings.",
	"no_service_account": "No Firebase service account is set up (Trident Attendance Settings, or site_config fcm_service_account).",
	"server_error": "Something went wrong on the ERP.",
}


def _refuse(reason: str, **extra) -> dict:
	return {"ok": False, "reason": reason, "message": _(REASONS[reason]), **extra}


def _text(value) -> str:
	return "" if value is None else str(value).strip()


def _log():
	return frappe.logger("trident_attendance")


def _keeps_the_key_out_of_the_error_log(method):
	"""Like `employee_access._keeps_secrets_out_of_the_error_log`: Frappe logs every local of a
	failed request, and here one of them may be the service-account key."""

	@functools.wraps(method)
	def guarded(*args, **kwargs):
		try:
			return method(*args, **kwargs)
		except frappe.PermissionError:
			raise
		except Exception as e:
			frappe.db.rollback()
			trace = "".join(traceback.format_tb(e.__traceback__))
			frappe.log_error(title=f"Push failed in {method.__name__}", message=f"{type(e).__name__}\n{trace}")
			frappe.local.response.http_status_code = 500
			return _refuse("server_error")

	return guarded


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


def _token_problem(token: str) -> dict | None:
	if not TOKEN_MIN_LENGTH <= len(token) <= TOKEN_MAX_LENGTH:
		return _refuse("token_invalid")
	return None


@frappe.whitelist(methods=["POST"])
def register_app_device(token, platform="android", app_version=None):
	"""The caller's phone, by its FCM token: inserted, or updated when the token is known. A
	token another user registered moves to the caller (the phone changed hands)."""
	user = require_signed_in()
	token = _text(token)
	refusal = _token_problem(token)
	if refusal:
		return refusal
	platform = _text(platform).lower() or "android"
	if platform not in PLATFORMS:
		return _refuse("invalid_platform")
	values = {
		"user": user,
		"platform": platform,
		"app_version": _text(app_version)[:140] or None,
		"enabled": 1,
		"last_used": now_datetime(),
		"last_error": None,
	}
	existing = frappe.db.get_value(DEVICE_DOCTYPE, {"token": token}, ["name", "user"], as_dict=True)
	if existing:
		frappe.db.set_value(DEVICE_DOCTYPE, existing.name, values)
	else:
		device = frappe.new_doc(DEVICE_DOCTYPE)
		device.update({"token": token, **values})
		device.flags.ignore_permissions = True
		device.insert()
	moved = bool(existing and existing.user != user)
	_log().info(f"App device registered: {user}, {platform}{', moved from another user' if moved else ''}")
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def unregister_app_device(token):
	"""Forgets the caller's phone. Another user's row with that token is left alone."""
	user = require_signed_in()
	token = _text(token)
	refusal = _token_problem(token)
	if refusal:
		return refusal
	frappe.db.delete(DEVICE_DOCTYPE, {"token": token, "user": user})
	_log().info(f"App device unregistered: {user}")
	return {"ok": True}


# ---------------------------------------------------------------------------
# The service account and its bearer
# ---------------------------------------------------------------------------


def push_enabled() -> bool:
	return bool(cint(get_settings().get("push_enabled")))


def service_account() -> dict | None:
	"""The service-account key as a dict: the settings' field, else site_config
	`fcm_service_account` (a dict, or the JSON as a string). None when neither holds one that
	parses and names a project."""
	raw = get_settings().get_password("firebase_service_account", raise_exception=False)
	if not raw:
		raw = frappe.conf.get("fcm_service_account")
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			return None
	if not isinstance(raw, dict):
		return None
	if not all(raw.get(key) for key in ("project_id", "client_email", "private_key")):
		return None
	return raw


def _cache_key(account: dict) -> str:
	# A new key for the same project must not reuse the old key's bearer.
	fingerprint = hashlib.sha256(f"{account['client_email']}:{account.get('private_key_id')}".encode()).hexdigest()
	return f"{ACCESS_TOKEN_CACHE_KEY}:{fingerprint[:16]}"


def _fetch_access_token(account: dict) -> tuple[str, int]:
	"""(bearer, seconds it is good for), from Google's token endpoint. google-auth, which Frappe
	itself depends on, signs the JWT and makes the request."""
	from google.auth.transport.requests import Request
	from google.oauth2 import service_account as google_service_account

	credentials = google_service_account.Credentials.from_service_account_info(account, scopes=[FCM_SCOPE])
	credentials.refresh(Request())
	expires_in = DEFAULT_ACCESS_TOKEN_SECONDS
	if credentials.expiry:
		expires_in = int((credentials.expiry - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds())
	return credentials.token, expires_in


def access_token(account: dict) -> str:
	"""The bearer for this key, from the cache while it has more than `ACCESS_TOKEN_MARGIN`
	seconds left."""
	key = _cache_key(account)
	# `expires`: without it a miss is remembered for the rest of the request.
	cached = frappe.cache().get_value(key, expires=True)
	if cached:
		return cached
	token, expires_in = _fetch_access_token(account)
	keep = expires_in - ACCESS_TOKEN_MARGIN
	if keep > 0:
		frappe.cache().set_value(key, token, expires_in_sec=keep)
	return token


def forget_access_token(account: dict):
	frappe.cache().delete_value(_cache_key(account))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def message(device_token: str, title: str, body: str, url: str, kind: str) -> dict:
	"""The FCM v1 request body. `data.url` is a path on the web app, which the phone opens when
	the notification is tapped."""
	return {
		"message": {
			"token": device_token,
			"notification": {"title": title, "body": body},
			"data": {"url": url, "kind": kind},
			"android": {"priority": "HIGH", "notification": {"channel_id": ANDROID_CHANNEL}},
		}
	}


def _post(url: str, bearer: str, payload: dict):
	"""The one HTTP call to Firebase. Tests replace it."""
	return requests.post(
		url,
		json=payload,
		headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json; UTF-8"},
		timeout=SEND_TIMEOUT,
	)


def _error_code(response) -> str:
	"""What Firebase said: the FcmError code when there is one, else the status, else HTTP n."""
	try:
		error = response.json().get("error") or {}
	except ValueError:
		error = {}
	for detail in error.get("details") or []:
		if isinstance(detail, dict) and detail.get("errorCode"):
			return str(detail["errorCode"])
	return str(error.get("status") or f"HTTP {response.status_code}")


def _send_one(send_url: str, bearer: str, device_token: str, title: str, body: str, url: str, kind: str) -> str | None:
	"""Firebase's error code for this device, or None when the message was accepted."""
	try:
		response = _post(send_url, bearer, message(device_token, title, body, url, kind))
	except requests.RequestException as e:
		# The exception's text can hold the URL; its type is enough.
		return type(e).__name__
	if 200 <= response.status_code < 300:
		return None
	return _error_code(response)


def _send(devices: list, title: str, body: str, url: str, kind: str) -> tuple[int, int, int]:
	"""(sent, failed, disabled) over the named devices that are still enabled."""
	account = service_account()
	if not account:
		_log().warning(f"Push {kind} not sent: no Firebase service account")
		return 0, len(devices), 0
	rows = frappe.get_all(DEVICE_DOCTYPE, filters={"name": ["in", devices], "enabled": 1}, fields=["name", "token"])
	if not rows:
		return 0, 0, 0
	bearer = access_token(account)
	send_url = FCM_SEND_URL.format(project=quote(str(account["project_id"]), safe=""))
	sent = failed = disabled = 0
	for row in rows:
		code = _send_one(send_url, bearer, row.token, title, body, url, kind)
		if code is None:
			sent += 1
			continue
		failed += 1
		values = {"last_error": f"{now_datetime():%Y-%m-%d %H:%M:%S} {code}"[:500]}
		if code in GONE_CODES:
			values["enabled"] = 0
			disabled += 1
		elif code == "UNAUTHENTICATED":
			forget_access_token(account)
		frappe.db.set_value(DEVICE_DOCTYPE, row.name, values, update_modified=False)
	return sent, failed, disabled


def _send_to_devices(devices: list, title: str, body: str, url: str, kind: str):
	"""The background job. Never raises: the key may be among the locals of a failure."""
	try:
		sent, failed, disabled = _send(devices, title, body, url, kind)
	except Exception as e:
		trace = "".join(traceback.format_tb(e.__traceback__))
		frappe.log_error(title=f"trident_attendance: push {kind} failed", message=f"{type(e).__name__}\n{trace}")
		return
	_log().info(f"Push {kind}: {len(devices)} device(s), {sent} sent, {failed} failed, {disabled} disabled")


def _notify(filters: dict, title: str, body: str, url: str, kind: str) -> int:
	if kind not in KINDS:
		raise ValueError(f"unknown push kind {kind!r}")
	if not push_enabled():
		return 0
	devices = frappe.get_all(DEVICE_DOCTYPE, filters={**filters, "enabled": 1}, pluck="name")
	for start in range(0, len(devices), BATCH_SIZE):
		frappe.enqueue(
			"trident_attendance.push._send_to_devices",
			queue="short",
			enqueue_after_commit=True,
			devices=devices[start : start + BATCH_SIZE],
			title=title,
			body=body,
			url=url,
			kind=kind,
		)
	if devices:
		_log().info(f"Push {kind} queued: {len(devices)} device(s)")
	return len(devices)


def notify_user(user: str | None, title: str, body: str, url: str, kind: str) -> int:
	"""Queues the message for every enabled device of `user`. How many devices, 0 when push is
	off or the user has none."""
	if not user or user == "Guest":
		return 0
	return _notify({"user": user}, title, body, url, kind)


def notify_all(title: str, body: str, url: str, kind: str) -> int:
	"""Queues the message for every enabled device on the site."""
	return _notify({}, title, body, url, kind)


@frappe.whitelist(methods=["POST"])
@_keeps_the_key_out_of_the_error_log
def send_test_push():
	"""A test message to the caller's own phones. `devices` is how many were queued."""
	user = require_signed_in()
	if not push_enabled():
		return _refuse("push_disabled")
	if not service_account():
		return _refuse("no_service_account")
	devices = notify_user(user, _("Test notification"), _("Push notifications from the ERP reach this phone."), "/ess/", "test")
	return {"ok": True, "devices": devices}
