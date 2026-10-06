"""Hub-only endpoints behind the employee app's sign-in without an ERP password.

The attendance hub checks the ID number and the face on its own server, then asks this site for
an ERP OAuth token for that employee's own user. Nothing here trusts a phone, and nothing here
answers anyone but the hub's login (Trident Attendance Settings > Hub Service User).

A caller that is not the hub gets a 403 (`exc_type: HubOnlyError`). Every other refusal is an
ordinary answer, `{"ok": False, "reason": <code>, "message": ...}`, so the hub can tell the
cases apart without reading English.
"""

import secrets
import string
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

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

REASONS = {
	"invalid_kind": "kind must be 'own' or 'helper'.",
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


def _refuse(reason: str) -> dict:
	return {"ok": False, "reason": reason, "message": _(REASONS[reason])}


def _employee_row(employee, for_update=False):
	name = str(employee or "").strip()
	if not name:
		return None
	fields = ["name", "employee_name", "status", "user_id", "image"]
	if frappe.get_meta("Employee").has_field(APP_ACCESS_FIELD):
		fields.append(APP_ACCESS_FIELD)
	return frappe.db.get_value("Employee", name, fields, as_dict=True, for_update=for_update)


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


@frappe.whitelist()
def get_employee_for_verification(id_number):
	"""The one Active employee with this ID number, and whether the hub may verify them.

	Two Active employees sharing a number are refused (`ambiguous`), never the first of them:
	the answer decides whose records a face unlocks. `can_verify` is false with a `reason` when
	a token would be refused anyway, so the hub can say so before it asks for a face.
	"""
	_require_hub()
	id_number = str(id_number or "").strip()
	if not id_number:
		return _refuse("id_number_required")
	id_field = employee_id_field()
	names = frappe.get_all(
		"Employee", filters={"status": "Active", id_field: id_number}, pluck="name", limit_page_length=2
	)
	if len(names) > 1:
		return _refuse("ambiguous")
	if not names:
		left = frappe.db.exists("Employee", {id_field: id_number})
		return _refuse("employee_inactive" if left else "not_found")

	row = _employee_row(names[0])
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


@frappe.whitelist(methods=["POST"])
def issue_employee_token(employee, kind, device=None):
	"""An ERP OAuth token for the employee's own user, for the hub to hand to a verified phone.

	`own`: an hour's access token with a refresh token, after revoking every token the user
	holds (one phone per employee; a new phone replaces the old one). `helper`: a short access
	token with no refresh token, for a borrowed phone; the employee's own phone stays signed in.
	"""
	_require_hub()
	if kind not in (KIND_OWN, KIND_HELPER):
		return _refuse("invalid_kind")
	# Locked so two phones registering at once cannot both end up with a live token.
	row = _employee_row(employee, for_update=True)
	if not row:
		return _refuse("employee_not_found")
	reason = _refusal_for(row, _user_state(row.user_id))
	if reason:
		return _refuse(reason)
	settings = get_settings()
	client, scopes, reason = _token_client(settings)
	if reason:
		return _refuse(reason)

	user = row.user_id
	revoked = 0
	if kind == KIND_OWN:
		revoked = _revoke_all(user)
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
