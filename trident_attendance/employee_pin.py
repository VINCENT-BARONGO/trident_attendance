"""The employee's app PIN and one-time code: the rules, where they are kept, and the limits.

The PIN is a hash in Frappe's `__Auth` table, keyed on the Employee. It is not the User's
password and nothing here touches that. Everything else is one row per employee in
`Employee App Sign In`: the pending code (a keyed hash), the wrong-PIN lock and when codes
were sent.

A table and not the cache: every worker reads the same row, and `bench migrate` or
`bench clear-cache` delete every cache key of the site, which would lift a lock and forget the
sending limits. Callers hold the Employee's row lock before they load the state, so two
requests for one employee never work on it at once.
"""

import hashlib
import hmac
import json
import math
import re
import secrets
from datetime import timedelta

import frappe
from frappe.utils import cint, get_datetime, now, now_datetime
from frappe.utils.password import (
	check_password,
	get_encryption_key,
	remove_encrypted_password,
	update_password,
)

STATE_DOCTYPE = "Employee App Sign In"
# A name in `__Auth`, not a field on Employee.
PIN_FIELD = "app_pin"

PURPOSE_SIGN_IN = "sign_in"
PURPOSE_SET_PIN = "set_pin"
PURPOSES = (PURPOSE_SIGN_IN, PURPOSE_SET_PIN)

CODE_SECONDS = 300
CODE_TRIES = 5
# (codes, seconds): 3 in 15 minutes, 10 in 24 hours.
SEND_LIMITS = ((3, 15 * 60), (10, 24 * 3600))

PIN_TRIES = 5
PIN_WINDOW_SECONDS = 3600
PIN_LOCK_SECONDS = 3600

PIN_PATTERN = re.compile(r"[0-9]{6}")

STATE_FIELDS = (
	"code_hash",
	"code_purpose",
	"code_expires_at",
	"code_wrong_tries",
	"codes_sent_at",
	"wrong_pins_at",
	"pin_locked_until",
)
TIME_LISTS = ("codes_sent_at", "wrong_pins_at")


def pin_problem(pin, id_number=None) -> str | None:
	"""Why this cannot be a PIN (`pin_invalid` or `pin_too_simple`), or None. Reads nothing."""
	if not isinstance(pin, str) or not PIN_PATTERN.fullmatch(pin):
		return "pin_invalid"
	digits = [int(c) for c in pin]
	steps = {(b - a) % 10 for a, b in zip(digits, digits[1:], strict=False)}
	# One digit repeated, or a straight run up or down (123456, 987654, also 890123).
	if steps in ({0}, {1}, {9}):
		return "pin_too_simple"
	id_digits = re.sub(r"[^0-9]", "", str(id_number or ""))
	if len(id_digits) >= 6 and pin == id_digits[-6:]:
		return "pin_too_simple"
	return None


def _keyed(*parts) -> str:
	"""A digest only this site can make. Six digits hashed on their own are found by trying
	all million of them; with the site's encryption key mixed in, a copy of the database is
	not enough."""
	return hmac.new(get_encryption_key().encode(), ":".join(parts).encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# PIN
# ---------------------------------------------------------------------------


def has_pin(employee) -> bool:
	return bool(frappe.db.exists("__Auth", {"doctype": "Employee", "name": employee, "fieldname": PIN_FIELD}))


def set_pin(employee, pin):
	update_password(employee, _keyed("pin", pin), doctype="Employee", fieldname=PIN_FIELD)


def pin_matches(employee, pin) -> bool:
	try:
		# The tracker cache is the ERP login's own count of failures, keyed by user.
		check_password(
			employee, _keyed("pin", pin), doctype="Employee", fieldname=PIN_FIELD, delete_tracker_cache=False
		)
	except frappe.AuthenticationError:
		return False
	return True


def clear_pin(employee):
	remove_encrypted_password("Employee", employee, PIN_FIELD)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state(employee, for_update=True):
	"""The employee's row, locked and as last committed. `for_update=False` only looks, and
	may see nothing or a moment ago.

	The row is made first when it is missing. A locking read of a row that is not there locks
	the gap around it, and two employees signing in for the first time would then wait on each
	other until MariaDB gives one of them up as a deadlock.
	"""
	if for_update:
		frappe.db.sql(
			f"""insert ignore into `tab{STATE_DOCTYPE}`
				(`name`, `employee`, `creation`, `modified`, `owner`, `modified_by`, `docstatus`, `idx`)
				values (%(employee)s, %(employee)s, %(now)s, %(now)s, 'Administrator', 'Administrator', 0, 0)""",
			{"employee": employee, "now": now()},
		)
	row = frappe.db.get_value(STATE_DOCTYPE, employee, list(STATE_FIELDS), as_dict=True, for_update=for_update)
	state = frappe._dict(row or {})
	state.employee = employee
	for field in TIME_LISTS:
		state[field] = [get_datetime(t) for t in json.loads(state.get(field) or "[]")]
	return state


def _save(state):
	values = {field: state.get(field) for field in STATE_FIELDS}
	for field in TIME_LISTS:
		values[field] = json.dumps([str(t) for t in state[field]]) if state[field] else None
	frappe.db.set_value(STATE_DOCTYPE, state.employee, values, update_modified=False)


def _since(times, seconds, now) -> list:
	return [t for t in times if t > now - timedelta(seconds=seconds)]


def _seconds_until(moment, now) -> int:
	return max(math.ceil((moment - now).total_seconds()), 0)


# ---------------------------------------------------------------------------
# PIN lock
# ---------------------------------------------------------------------------


def pin_lock_wait(state) -> int:
	"""Seconds until the PIN may be tried again. 0 = not locked."""
	if not state.pin_locked_until:
		return 0
	return _seconds_until(state.pin_locked_until, now_datetime())


def note_wrong_pin(state) -> int:
	"""Counts a wrong PIN. Returns the seconds the PIN is now locked for, or 0."""
	now = now_datetime()
	state.wrong_pins_at = [*_since(state.wrong_pins_at, PIN_WINDOW_SECONDS, now), now]
	locked = len(state.wrong_pins_at) >= PIN_TRIES
	if locked:
		state.pin_locked_until = now + timedelta(seconds=PIN_LOCK_SECONDS)
		if state.code_purpose == PURPOSE_SIGN_IN:
			# Sent after a right PIN a moment ago; the lock covers it too.
			_forget_code(state)
	_save(state)
	return PIN_LOCK_SECONDS if locked else 0


def note_right_pin(state):
	if state.wrong_pins_at:
		state.wrong_pins_at = []
		_save(state)


def clear_pin_lock(state):
	state.wrong_pins_at = []
	state.pin_locked_until = None
	_save(state)


# ---------------------------------------------------------------------------
# One-time code
# ---------------------------------------------------------------------------


def _code_hash(employee, purpose, code) -> str:
	return _keyed("code", employee, purpose, code)


def _forget_code(state):
	state.update({"code_hash": None, "code_purpose": None, "code_expires_at": None, "code_wrong_tries": 0})


def send_wait(state) -> int:
	"""Seconds until another code may be sent to this employee. 0 = now."""
	now = now_datetime()
	wait = 0
	for limit, seconds in SEND_LIMITS:
		recent = _since(state.codes_sent_at, seconds, now)
		if len(recent) >= limit:
			# One more is allowed once the oldest of the last `limit` is out of the window.
			wait = max(wait, _seconds_until(recent[-limit] + timedelta(seconds=seconds), now))
	return wait


def new_code(state, purpose) -> str:
	"""A fresh code for this purpose, in place of any pending one. Only its hash is kept; the
	code itself goes to the employee and nowhere else."""
	now = now_datetime()
	code = f"{secrets.randbelow(10**6):06d}"
	state.update(
		{
			"code_hash": _code_hash(state.employee, purpose, code),
			"code_purpose": purpose,
			"code_expires_at": now + timedelta(seconds=CODE_SECONDS),
			"code_wrong_tries": 0,
		}
	)
	state.codes_sent_at = [*_since(state.codes_sent_at, SEND_LIMITS[-1][1], now), now]
	_save(state)
	return code


def unsend_code(state):
	"""The code could not be sent: it must not work, and it does not count toward the limits.

	Read again first. Sending an e-mail commits, which lets go of the Employee's row lock, so
	another request may have put a newer code in place.
	"""
	current = load_state(state.employee)
	if current.code_hash == state.code_hash:
		_forget_code(current)
	sent_at = state.codes_sent_at[-1]
	current.codes_sent_at = [t for t in current.codes_sent_at if t != sent_at]
	_save(current)


def has_code(state, purpose) -> bool:
	"""Whether a code for this purpose is pending and still in time."""
	return bool(
		state.code_hash
		and state.code_purpose == purpose
		and state.code_expires_at
		and state.code_expires_at > now_datetime()
	)


def check_code(state, purpose, code) -> tuple[str | None, int | None]:
	"""(refusal reason, tries left). (None, None) = the code is right, and now used up."""
	if not has_code(state, purpose):
		return "no_code", None
	if hmac.compare_digest(state.code_hash, _code_hash(state.employee, purpose, str(code or ""))):
		_forget_code(state)
		_save(state)
		return None, None
	tries = cint(state.code_wrong_tries) + 1
	if tries >= CODE_TRIES:
		_forget_code(state)
		_save(state)
		return "too_many_code_tries", None
	state.code_wrong_tries = tries
	_save(state)
	return "wrong_code", CODE_TRIES - tries


def reset(state):
	"""HR's reset: no PIN, no pending code, no lock. When codes were sent is kept, so a reset
	does not hand out more of them."""
	clear_pin(state.employee)
	_forget_code(state)
	clear_pin_lock(state)


# ---------------------------------------------------------------------------
# Employee events
# ---------------------------------------------------------------------------


def on_employee_trash(doc, method=None):
	# Frappe deletes the PIN itself, with every other `__Auth` row of the document.
	frappe.db.delete(STATE_DOCTYPE, {"name": doc.name})


def after_employee_rename(doc, method, old, new, merge=False):
	# Frappe moves the PIN to the new name. The lock and the limits follow it.
	if merge or frappe.db.exists(STATE_DOCTYPE, new):
		frappe.db.delete(STATE_DOCTYPE, {"name": old})
	else:
		frappe.db.set_value(STATE_DOCTYPE, old, {"name": new, "employee": new}, update_modified=False)
