"""Hub-only endpoints behind the employee app's sign-in without an ERP password.

An employee proves who they are with an ID number, a 6-digit PIN and a one-time code sent to
their phone. The attendance hub passes each step on to this site, which checks the PIN and the
code and then hands the hub an ERP OAuth token for that employee's own user. Nothing here
trusts a phone, and nothing here answers anyone but the hub's login (Trident Attendance
Settings > Hub Service User), apart from HR's PIN reset at the end.

A caller that is not the hub gets a 403 (`exc_type: HubOnlyError`). Every other refusal is an
ordinary answer, `{"ok": False, "reason": <code>, "message": ...}`, so the hub can tell the
cases apart without reading English.

The older face check (the hub matches a face, then asks for a token) is still here, switched
off unless Trident Attendance Settings > Allow Face Sign-in is ticked.
"""

import functools
import secrets
import string
import traceback
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from trident_attendance import employee_contact, employee_pin
from trident_attendance.employee_pin import PURPOSE_SET_PIN, PURPOSE_SIGN_IN, PURPOSES
from trident_attendance.utils import employee_id_field, get_settings, is_reviewer

APP_ACCESS_FIELD = "custom_app_access"

KIND_OWN = "own"
KIND_HELPER = "helper"

OWN_TOKEN_SECONDS = 3600
DEFAULT_HELPER_MINUTES = 10
# A borrowed-phone token is meant to be short. A typo in the setting must not make it a long one.
MAX_HELPER_MINUTES = 60

TOKEN_SCOPES = ("openid", "all")
TOKEN_ALPHABET = string.ascii_letters + string.digits
TOKEN_LENGTH = 40

# A user holding anything beyond these is staff with more than their own records to read (a
# supervisor, a manager, a System Manager) and signs in with a password as before. The last
# three are the roles Frappe gives every user by itself.
SELF_SERVICE_ROLES = frozenset({"Employee", "Employee Self Service", "All", "Guest", "Desk User"})

# Who may reset an employee's PIN from the Employee form.
PIN_RESET_ROLES = frozenset({"HR Manager", "HR User", "Attendance Admin", "System Manager"})

REASONS = {
	"invalid_kind": "kind must be 'own' or 'helper'.",
	"invalid_purpose": "purpose must be 'sign_in' or 'set_pin'.",
	"id_number_required": "ID number is required.",
	"not_found": "No Active employee has this ID number.",
	"ambiguous": "More than one Active employee has this ID number.",
	"employee_not_found": "Employee not found.",
	"employee_inactive": "The employee is not Active.",
	"app_access_off": "App access is not enabled for this employee.",
	"no_photo": "The employee has no photo.",
	"no_user": "The employee has no linked user.",
	"user_disabled": "The employee's user is disabled.",
	"user_has_other_roles": "The employee's user holds roles beyond self service.",
	"oauth_client_not_set": "Employee Token OAuth Client is not set in Trident Attendance Settings.",
	"oauth_client_missing": "The Employee Token OAuth Client does not exist.",
	"oauth_client_no_scopes": "The Employee Token OAuth Client allows neither 'openid' nor 'all'.",
	"face_sign_in_off": "Face sign-in is switched off in Trident Attendance Settings.",
	"pin_required": "PIN is required.",
	"pin_not_set": "The employee has not chosen a PIN yet.",
	"wrong_pin": "Wrong PIN.",
	"pin_locked": "Too many wrong PINs. The PIN is locked for a while.",
	"pin_invalid": "The PIN must be exactly 6 digits.",
	"pin_too_simple": "The PIN is too easy to guess.",
	"no_contact": "The employee has no mobile number and no e-mail address of their own.",
	"too_many_codes": "Too many codes were sent to this employee. Try again later.",
	"send_failed": "The code could not be sent.",
	"no_code": "No code is waiting for this employee, or it has expired.",
	"wrong_code": "Wrong code.",
	"too_many_code_tries": "Too many wrong codes. Ask for a new code.",
	"server_error": "Something went wrong on the ERP.",
}


class HubOnlyError(frappe.PermissionError):
	"""The caller is not the hub's service user."""


def is_hub(user: str | None = None) -> bool:
	"""Whether `user` is the hub's login: the one user named in the settings, and still a
	reviewer (the trust `is_relayed` already places in the hub's account).

	A reviewer role alone is not enough here. Every office reviewer holds it, and these methods
	hand out sign-ins as other people.
	"""
	user = user or frappe.session.user
	hub_user = get_settings().get("hub_service_user")
	if not hub_user or user in ("Guest", "Administrator"):
		return False
	return user == hub_user and is_reviewer(user)


def _require_hub():
	if not is_hub():
		frappe.throw(_("Only the attendance hub can call this."), HubOnlyError)


def _refuse(reason: str, **extra) -> dict:
	return {"ok": False, "reason": reason, "message": _(REASONS[reason]), **extra}


def _text(value) -> str:
	return "" if value is None else str(value).strip()


def _keeps_secrets_out_of_the_error_log(method):
	"""An unexpected error is answered with a 500 here, not raised.

	Frappe writes every local variable of a failed request to the Error Log, its own frames
	included, and in these methods those are a PIN and a code. What is logged instead is where
	it failed and the kind of error.
	"""

	@functools.wraps(method)
	def guarded(*args, **kwargs):
		try:
			return method(*args, **kwargs)
		except frappe.PermissionError:
			raise
		except Exception as e:
			frappe.db.rollback()
			trace = "".join(traceback.format_tb(e.__traceback__))
			frappe.log_error(
				title=f"Employee sign-in failed in {method.__name__}", message=f"{type(e).__name__}\n{trace}"
			)
			frappe.local.response.http_status_code = 500
			return _refuse("server_error")

	return guarded


def _employee_row(employee, for_update=False):
	name = _text(employee)
	if not name:
		return None
	meta = frappe.get_meta("Employee")
	fields = ["name", "employee_name", "status", "user_id", "image", *employee_contact.CONTACT_FIELDS]
	fields += [f for f in (APP_ACCESS_FIELD, employee_id_field()) if meta.has_field(f)]
	return frappe.db.get_value("Employee", name, fields, as_dict=True, for_update=for_update)


def _find_employee(id_number) -> tuple[str | None, str | None]:
	"""(employee, refusal reason) for an ID number: the one Active employee holding it.

	Two Active employees sharing a number are refused (`ambiguous`), never the first of them:
	the answer decides whose records a sign-in unlocks.
	"""
	id_field = employee_id_field()
	names = frappe.get_all(
		"Employee", filters={"status": "Active", id_field: id_number}, pluck="name", limit_page_length=2
	)
	if len(names) > 1:
		return None, "ambiguous"
	if not names:
		left = frappe.db.exists("Employee", {id_field: id_number})
		return None, "employee_inactive" if left else "not_found"
	return names[0], None


def _user_state(user: str | None) -> dict:
	"""What the token rules need to know about an employee's linked user."""
	enabled = frappe.db.get_value("User", user, "enabled") if user else None
	exists = enabled is not None
	extra = sorted(set(frappe.get_roles(user)) - SELF_SERVICE_ROLES) if exists else []
	return {
		"has_user": exists,
		"user_enabled": bool(exists and cint(enabled)),
		# Administrator holds every role, so it lands here too.
		"self_service_only": exists and not extra and user not in ("Administrator", "Guest"),
	}


def _refusal_for(row, state, need_photo=False) -> str | None:
	"""Why this employee cannot be issued a token, or None. The first reason that applies."""
	if row.status != "Active":
		return "employee_inactive"
	if not cint(row.get(APP_ACCESS_FIELD)):
		return "app_access_off"
	if need_photo and not row.image:
		return "no_photo"
	if not state["has_user"]:
		return "no_user"
	if not state["user_enabled"]:
		return "user_disabled"
	if not state["self_service_only"]:
		return "user_has_other_roles"
	return None


def _face_sign_in_off() -> dict | None:
	"""The refusal for a face-era method while the setting is off. Without it the hub's login
	could still be issued a token with no PIN and no code."""
	if cint(get_settings().get("allow_face_sign_in")):
		return None
	return _refuse("face_sign_in_off")


@frappe.whitelist()
def get_employee_for_verification(id_number):
	"""The one Active employee with this ID number, and whether the hub may verify them by face.

	`can_verify` is false with a `reason` when a token would be refused anyway, so the hub can
	say so before it asks for a face.
	"""
	_require_hub()
	off = _face_sign_in_off()
	if off:
		return off
	id_number = _text(id_number)
	if not id_number:
		return _refuse("id_number_required")
	employee, reason = _find_employee(id_number)
	if reason:
		return _refuse(reason)

	row = _employee_row(employee)
	state = _user_state(row.user_id)
	reason = _refusal_for(row, state, need_photo=True)
	return {
		"ok": True,
		"employee": row.name,
		"employee_name": row.employee_name,
		"has_photo": bool(row.image),
		"has_user": state["has_user"],
		"user_enabled": state["user_enabled"],
		"app_access": bool(cint(row.get(APP_ACCESS_FIELD))),
		"can_verify": reason is None,
		"reason": reason,
	}


@frappe.whitelist()
def get_verification_photo(employee):
	"""An Active employee's reference photo (Employee.image) as raw image bytes, for the hub's
	face match. Read the way `api.get_employee_photo` reads it."""
	from trident_attendance.api import _serve_photo

	_require_hub()
	off = _face_sign_in_off()
	if off:
		return off
	row = _employee_row(employee)
	if not row or row.status != "Active":
		raise frappe.DoesNotExistError(_("No Active employee {0}.").format(employee))
	_serve_photo("Employee", row.name, "image")


def _new_token() -> str:
	return "".join(secrets.choice(TOKEN_ALPHABET) for _i in range(TOKEN_LENGTH))


def _token_client(settings) -> tuple[str | None, str | None, str | None]:
	"""(client, scopes, refusal reason). The scopes are "openid all", less what the client
	does not allow."""
	client = settings.get("employee_token_client")
	if not client:
		return None, None, "oauth_client_not_set"
	client_scopes = frappe.db.get_value("OAuth Client", client, "scopes")
	if client_scopes is None:
		return None, None, "oauth_client_missing"
	scopes = [s for s in TOKEN_SCOPES if s in client_scopes.split()]
	if not scopes:
		return None, None, "oauth_client_no_scopes"
	return client, " ".join(scopes), None


def helper_token_seconds(settings=None) -> int:
	minutes = cint((settings or get_settings()).get("helper_token_minutes")) or DEFAULT_HELPER_MINUTES
	return min(max(minutes, 1), MAX_HELPER_MINUTES) * 60


def _revoke_all(user: str) -> int:
	"""Ends every sign-in the user holds through OAuth, whichever client it was issued to.

	The web app gets a token of its own when the phone passes the ERP's sign-in step, so
	revoking the phone's alone leaves My HR readable. A code not yet exchanged would become one
	more token, so those go too.
	"""
	active = {"user": user, "status": "Active"}
	count = frappe.db.count("OAuth Bearer Token", active)
	if count:
		frappe.db.set_value("OAuth Bearer Token", active, "status", "Revoked")
	codes = {"user": user, "validity": "Valid"}
	if frappe.db.exists("OAuth Authorization Code", codes):
		frappe.db.set_value("OAuth Authorization Code", codes, "validity", "Invalid")
	return count


def _token_refusal(row, settings) -> tuple[str | None, str | None, str | None]:
	"""(client, scopes, refusal reason): the employee's own state first, then the OAuth client."""
	reason = _refusal_for(row, _user_state(row.user_id))
	if reason:
		return None, None, reason
	return _token_client(settings)


def _issue_token(row, kind, device, settings, client, scopes, revoke_all=False) -> dict:
	"""The token for an employee who has passed `_token_refusal`. The caller holds the
	Employee's row lock, so two phones registering at once cannot both end up with a live token.

	`own`: an hour's access token with a refresh token, after revoking every token the user
	holds (one phone per employee; a new phone replaces the old one). `helper`: a short access
	token with no refresh token, for a borrowed phone; the employee's own phone stays signed in
	unless `revoke_all` is given.
	"""
	user = row.user_id
	revoked = _revoke_all(user) if revoke_all or kind == KIND_OWN else 0
	if kind == KIND_OWN:
		expires_in, refresh_token = OWN_TOKEN_SECONDS, _new_token()
	else:
		expires_in, refresh_token = helper_token_seconds(settings), None

	token = frappe.new_doc("OAuth Bearer Token")
	token.update(
		{
			"client": client,
			"user": user,
			"scopes": scopes,
			"access_token": _new_token(),
			"refresh_token": refresh_token,
			"expires_in": expires_in,
			"expiration_time": now_datetime() + timedelta(seconds=expires_in),
			"status": "Active",
		}
	)
	token.flags.ignore_permissions = True
	token.insert()
	frappe.logger("trident_attendance").info(
		f"Employee token issued: {row.name} ({user}), kind {kind}, device {str(device or '-')[:140]}, "
		f"{revoked} earlier token(s) revoked"
	)
	return {
		"ok": True,
		"access_token": token.access_token,
		"refresh_token": refresh_token,
		"expires_in": expires_in,
		"user": user,
		"employee": row.name,
	}


@frappe.whitelist(methods=["POST"])
def issue_employee_token(employee, kind, device=None):
	"""An ERP OAuth token for the employee's own user, for the hub to hand to a phone whose
	face it has matched. `own` and `helper` as in `_issue_token`."""
	_require_hub()
	off = _face_sign_in_off()
	if off:
		return off
	if kind not in (KIND_OWN, KIND_HELPER):
		return _refuse("invalid_kind")
	row = _employee_row(employee, for_update=True)
	if not row:
		return _refuse("employee_not_found")
	settings = get_settings()
	client, scopes, reason = _token_refusal(row, settings)
	if reason:
		return _refuse(reason)
	return _issue_token(row, kind, device, settings, client, scopes)


def _pin_refusal(row, state, pin) -> dict | None:
	"""Checks the PIN for a sign-in. A wrong one is counted; the fifth in an hour locks the PIN
	for an hour, and while it is locked the right PIN is refused too."""
	if not pin:
		return _refuse("pin_required")
	if not employee_pin.has_pin(row.name):
		return _refuse("pin_not_set")
	wait = employee_pin.pin_lock_wait(state)
	if wait:
		return _refuse("pin_locked", retry_after=wait)
	if employee_pin.pin_matches(row.name, pin):
		employee_pin.note_right_pin(state)
		return None
	wait = employee_pin.note_wrong_pin(state)
	if wait:
		frappe.logger("trident_attendance").info(f"Employee PIN locked: {row.name}, {wait} s")
		return _refuse("pin_locked", retry_after=wait)
	return _refuse("wrong_pin")


@frappe.whitelist(methods=["POST"])
@_keeps_secrets_out_of_the_error_log
def begin_employee_sign_in(id_number, purpose, pin=None):
	"""First step: finds the employee, checks the PIN (`sign_in`) or not (`set_pin`, for a
	first time or a forgotten PIN), and sends a one-time code to the employee's phone or e-mail.

	A new code replaces the one before it. The code is not in the answer; only where it went,
	masked.
	"""
	_require_hub()
	id_number, pin = _text(id_number), _text(pin)
	if not id_number:
		return _refuse("id_number_required")
	if purpose not in PURPOSES:
		return _refuse("invalid_purpose")
	employee, reason = _find_employee(id_number)
	if reason:
		return _refuse(reason)
	row = _employee_row(employee, for_update=True)
	# No code goes to somebody who could not be given a token for it.
	reason = _token_refusal(row, get_settings())[2]
	if reason:
		return _refuse(reason)

	state = employee_pin.load_state(row.name)
	if purpose == PURPOSE_SIGN_IN:
		refusal = _pin_refusal(row, state, pin)
		if refusal:
			return refusal
	channel, address = employee_contact.choose(row)
	if not channel:
		return _refuse("no_contact")
	wait = employee_pin.send_wait(state)
	if wait:
		return _refuse("too_many_codes", retry_after=wait)

	code = employee_pin.new_code(state, purpose)
	try:
		employee_contact.send_code(channel, address, code, employee_pin.CODE_SECONDS // 60)
	except employee_contact.SendError as e:
		employee_pin.unsend_code(state)
		frappe.logger("trident_attendance").warning(f"Employee code not sent: {row.name}, {channel}, {e}")
		return _refuse("send_failed")
	frappe.logger("trident_attendance").info(f"Employee code sent: {row.name}, {purpose}, {channel}")
	return {
		"ok": True,
		"employee": row.name,
		"employee_name": row.employee_name,
		"channel": channel,
		"destination": employee_contact.mask(channel, address),
		"expires_in": employee_pin.CODE_SECONDS,
	}


@frappe.whitelist(methods=["POST"])
@_keeps_secrets_out_of_the_error_log
def complete_employee_sign_in(employee, purpose, code, kind, new_pin=None, device=None):
	"""Second step: takes the code and answers with the token, as `_issue_token` makes it.

	`set_pin` also takes the PIN the employee chose, saves it, lifts a PIN lock and revokes
	every earlier token of the employee whatever the `kind`: it is the way back in after a
	forgotten PIN, so whoever held the old one is signed out. A new PIN that breaks the rules
	is refused before the code is looked at, so the same code can be sent again with a better
	one.
	"""
	_require_hub()
	code, new_pin = _text(code), _text(new_pin)
	if kind not in (KIND_OWN, KIND_HELPER):
		return _refuse("invalid_kind")
	if purpose not in PURPOSES:
		return _refuse("invalid_purpose")
	row = _employee_row(employee, for_update=True)
	if not row:
		return _refuse("employee_not_found")
	settings = get_settings()
	client, scopes, reason = _token_refusal(row, settings)
	if reason:
		return _refuse(reason)

	state = employee_pin.load_state(row.name)
	if not employee_pin.has_code(state, purpose):
		return _refuse("no_code")
	setting_pin = purpose == PURPOSE_SET_PIN
	if setting_pin:
		reason = employee_pin.pin_problem(new_pin, row.get(employee_id_field()))
		if reason:
			return _refuse(reason)
	reason, tries_left = employee_pin.check_code(state, purpose, code)
	if reason == "wrong_code":
		return _refuse(reason, tries_left=tries_left)
	if reason:
		return _refuse(reason)

	if setting_pin:
		employee_pin.set_pin(row.name, new_pin)
		employee_pin.clear_pin_lock(state)
		frappe.logger("trident_attendance").info(f"Employee PIN set: {row.name}")
	result = _issue_token(row, kind, device, settings, client, scopes, revoke_all=setting_pin)
	result.update({"employee_name": row.employee_name, "pin_set": setting_pin})
	return result


@frappe.whitelist(methods=["POST"])
def revoke_employee_tokens(employee):
	"""Revokes every Active OAuth token of the employee's user and returns how many.

	Works whatever the employee's status or app access: taking access away must not depend on
	the things that grant it.
	"""
	_require_hub()
	row = _employee_row(employee)
	if not row:
		return _refuse("employee_not_found")
	revoked = _revoke_all(row.user_id) if row.user_id else 0
	frappe.logger("trident_attendance").info(
		f"Employee tokens revoked: {row.name} ({row.user_id or 'no user'}), {revoked} token(s)"
	)
	return {"ok": True, "employee": row.name, "user": row.user_id, "revoked": revoked}


# ---------------------------------------------------------------------------
# HR, from the Employee form. A role decides here, not being the hub.
# ---------------------------------------------------------------------------


def _require_pin_reset_role() -> set:
	roles = set(frappe.get_roles())
	if frappe.session.user == "Guest" or not PIN_RESET_ROLES & roles:
		frappe.throw(_("You are not allowed to manage employees' app PINs."), frappe.PermissionError)
	return roles


def _pin_reset_row(employee, for_update=False):
	"""The employee, for a caller who holds a PIN reset role and may open that employee. A
	System Manager has no permission on Employee of its own, so there the role is enough."""
	roles = _require_pin_reset_role()
	row = _employee_row(employee, for_update=for_update)
	if row and "System Manager" not in roles and not frappe.has_permission("Employee", "read", doc=row.name):
		frappe.throw(_("You are not allowed to open this employee."), frappe.PermissionError)
	return row


@frappe.whitelist(methods=["POST"])
def reset_employee_pin(employee):
	"""Clears the employee's PIN and any pending code, lifts the PIN lock and revokes every
	token. The employee then chooses a new PIN in the app ("First time here, or forgot your
	PIN?"). Works whatever the employee's status or app access, like `revoke_employee_tokens`.
	"""
	row = _pin_reset_row(employee, for_update=True)
	if not row:
		return _refuse("employee_not_found")
	had_pin = employee_pin.has_pin(row.name)
	employee_pin.reset(employee_pin.load_state(row.name))
	revoked = _revoke_all(row.user_id) if row.user_id else 0
	frappe.logger("trident_attendance").info(
		f"Employee PIN reset by {frappe.session.user}: {row.name}, {revoked} token(s) revoked"
	)
	return {"ok": True, "employee": row.name, "had_pin": had_pin, "revoked": revoked}


@frappe.whitelist()
def get_employee_pin_status(employee):
	"""Whether the employee has chosen an app PIN, and whether it is locked. For the Employee
	form; the same roles as the reset."""
	row = _pin_reset_row(employee)
	if not row:
		return _refuse("employee_not_found")
	state = employee_pin.load_state(row.name, for_update=False)
	return {
		"ok": True,
		"employee": row.name,
		"pin_set": employee_pin.has_pin(row.name),
		"pin_locked": bool(employee_pin.pin_lock_wait(state)),
	}
