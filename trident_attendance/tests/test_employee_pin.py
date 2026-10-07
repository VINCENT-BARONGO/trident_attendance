import re
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime
from frappe.utils.password import check_password, rename_password

from trident_attendance import employee_contact, employee_pin
from trident_attendance.employee_access import (
	HubOnlyError,
	begin_employee_sign_in,
	complete_employee_sign_in,
	get_employee_pin_status,
	issue_employee_token,
	reset_employee_pin,
)
from trident_attendance.employee_contact import mask_email, mask_mobile, normalise_mobile, own_email
from trident_attendance.employee_pin import STATE_DOCTYPE, pin_problem
from trident_attendance.tests import helpers
from trident_attendance.tests.test_employee_access import HUB, ID_NUMBER, EmployeeAccessCase, accepted, refreshable

PIN = "135790"
OTHER_PIN = "248613"
MOBILE = "0712 345 123"
EMAIL = f"tokone.home@{helpers.TEST_DOMAIN}"
SEND_REQUEST = "frappe.core.doctype.sms_settings.sms_settings.send_request"
TOKEN_KEYS = {
	"ok",
	"access_token",
	"refresh_token",
	"expires_in",
	"user",
	"employee",
	"employee_name",
	"pin_set",
}


class TestPinRules(FrappeTestCase):
	def test_six_digits_and_nothing_else(self):
		for pin in ("", None, "12345", "1234567", "12345a", " 135790", "135790\n", 135790, "１３５７９０", "13 579"):
			self.assertEqual(pin_problem(pin), "pin_invalid", repr(pin))

	def test_good_pins(self):
		for pin in (PIN, OTHER_PIN, "000001", "112233", "121212", "123457", "102030", "998877"):
			self.assertIsNone(pin_problem(pin), pin)
			self.assertIsNone(pin_problem(pin, "29481736"), pin)

	def test_one_digit_repeated(self):
		for digit in "0123456789":
			self.assertEqual(pin_problem(digit * 6), "pin_too_simple")

	def test_straight_runs_up_and_down(self):
		runs = ("012345", "123456", "234567", "345678", "456789", "987654", "876543", "765432", "654321", "543210")
		# Past 9 and round again is still a run.
		wrapped = ("567890", "890123", "901234", "210987", "098765")
		for pin in runs + wrapped:
			self.assertEqual(pin_problem(pin), "pin_too_simple", pin)

	def test_the_end_of_the_id_number(self):
		self.assertEqual(pin_problem("481736", "29481736"), "pin_too_simple")
		self.assertEqual(pin_problem("481736", " 29-481-736 "), "pin_too_simple")
		self.assertEqual(pin_problem("481736", 29481736), "pin_too_simple")
		self.assertEqual(pin_problem("000001", ID_NUMBER), "pin_too_simple")
		self.assertIsNone(pin_problem("294817", "29481736"))
		# Fewer than six digits on the card: nothing to compare.
		self.assertIsNone(pin_problem("048173", "48173"))
		self.assertIsNone(pin_problem("481736", None))


class TestContact(FrappeTestCase):
	def test_kenyan_numbers(self):
		for raw in (
			"0712345123",
			"0712 345 123",
			"0712-345-123",
			"712345123",
			"254712345123",
			"+254712345123",
			"+254 712 345 123",
			"00254712345123",
			"+254 (0) 712 345 123",
			"2540712345123",
		):
			self.assertEqual(normalise_mobile(raw), "254712345123", raw)
		self.assertEqual(normalise_mobile("0112345123"), "254112345123")
		self.assertEqual(normalise_mobile("+254112345123"), "254112345123")

	def test_other_countries_are_left_alone(self):
		for raw, number in (
			("+255 712 345 123", "255712345123"),
			("255712345123", "255712345123"),
			("+44 7911 123456", "447911123456"),
			("+1 (202) 555-0143", "12025550143"),
			("0044 7911 123456", "447911123456"),
		):
			self.assertEqual(normalise_mobile(raw), number, raw)

	def test_not_a_mobile_number(self):
		for raw in ("", None, "  ", "abc", "07123", "0201234567", "07123451234", "12345", "+", "0712345x23", "1" * 16):
			self.assertIsNone(normalise_mobile(raw), raw)

	def test_masks(self):
		self.assertEqual(mask_mobile("254712345123"), "07•• ••• 123")
		self.assertEqual(mask_mobile("254112345123"), "01•• ••• 123")
		self.assertEqual(mask_mobile("447911123456"), "+447•• ••• 456")
		self.assertEqual(mask_email("vbarongo@gmail.com"), "v•••@gmail.com")
		self.assertEqual(mask_email("a@b.co.ke"), "a•••@b.co.ke")

	def test_own_email_order_and_never_a_staff_address(self):
		row = frappe._dict(prefered_email="p@x.example", personal_email="h@x.example", company_email="c@x.example")
		self.assertEqual(own_email(row), "p@x.example")
		row.prefered_email = "hr-emp-00001@staff.example.co.ke"
		self.assertEqual(own_email(row), "h@x.example")
		row.personal_email = " "
		self.assertEqual(own_email(row), "c@x.example")
		row.company_email = "hr-emp-00001@STAFF.example.co.ke"
		self.assertIsNone(own_email(row))
		row.company_email = "not an address"
		self.assertIsNone(own_email(row))
		# Only a domain that starts with it.
		row.company_email = "jane@mystaff.example"
		self.assertEqual(own_email(row), "jane@mystaff.example")


class PinCase(EmployeeAccessCase):
	"""The hub's login, an employee with a mobile number, a site with an SMS gateway. The
	gateway request and `frappe.sendmail` are replaced: nothing leaves the machine."""

	def setUp(self):
		super().setUp()
		frappe.db.set_value("Employee", self.employee, "cell_number", MOBILE)
		helpers.set_sms_gateway()
		self.sms_status = 200
		self.sms = []
		self.mails = []
		self.mail_outcome = "sent"
		self.logged = []
		self.start(patch(SEND_REQUEST, self.fake_sms_request))
		self.start(patch("frappe.sendmail", self.fake_sendmail))
		self.start(patch("frappe.logger", lambda *a, **kw: self))
		self.sms_logs = frappe.db.count("SMS Log")
		self.error_logs = set(frappe.get_all("Error Log", pluck="name"))
		self.clock = None

	def start(self, patcher):
		patcher.start()
		self.addCleanup(patcher.stop)

	# What the code under test logs through frappe.logger(...).
	def info(self, message, *args, **kwargs):
		self.logged.append(str(message))

	warning = error = exception = debug = info

	def fake_sms_request(self, gateway_url, params, headers=None, use_post=False, use_json=False):
		self.sms.append(frappe._dict(url=gateway_url, to=params.get("to"), message=params.get("message")))
		if isinstance(self.sms_status, Exception):
			raise self.sms_status
		return self.sms_status

	def fake_sendmail(self, **kwargs):
		"""Queues as Frappe does, and hands back a queue row whose send() does not send."""
		self.mails.append(frappe._dict(kwargs))
		queued = frappe.get_doc(
			{
				"doctype": "Email Queue",
				"sender": f"erp@{helpers.TEST_DOMAIN}",
				"message": kwargs["message"],
				"status": "Not Sent",
				"recipients": [{"recipient": r} for r in kwargs["recipients"]],
			}
		).insert(ignore_permissions=True)
		outcome = self.mail_outcome

		def send():
			if outcome == "error":
				raise OSError(f"smtp refused: {kwargs['message']}")
			if outcome == "sent":
				frappe.db.set_value("Email Queue", queued.name, "status", "Sent")

		queued.send = send
		return queued

	@contextmanager
	def later(self, seconds):
		"""The PIN and code clock, `seconds` after the test began."""
		self.clock = self.clock or now_datetime()
		with patch("trident_attendance.employee_pin.now_datetime", return_value=self.clock + timedelta(seconds=seconds)):
			yield

	def sent_code(self) -> str:
		message = self.sms[-1].message if self.sms else self.mails[-1].message
		return re.search(r"\b([0-9]{6})\b", message).group(1)

	def another_code(self, code) -> str:
		return f"{(int(code) + 1) % 10**6:06d}"

	def give_pin(self, pin=PIN):
		employee_pin.set_pin(self.employee, pin)

	def begin(self, purpose="sign_in", pin=PIN):
		return begin_employee_sign_in(ID_NUMBER, purpose, pin)

	def begin_ok(self, purpose="sign_in", pin=PIN) -> str:
		"""Begins, and returns the code that was sent."""
		sent = len(self.sms) + len(self.mails)
		result = self.begin(purpose, pin)
		self.assertTrue(result["ok"], result)
		self.assertEqual(len(self.sms) + len(self.mails), sent + 1)
		return self.sent_code()

	def complete(self, code, purpose="sign_in", kind="own", new_pin=None):
		return complete_employee_sign_in(self.employee, purpose, code, kind, new_pin, device="test-phone")

	def state(self):
		return frappe.db.get_value(STATE_DOCTYPE, self.employee, "*", as_dict=True) or frappe._dict()

	def stored_pin(self, doctype="Employee", name=None, fieldname="app_pin"):
		"""(hash, encrypted) as `__Auth` holds it, or None."""
		rows = frappe.db.sql(
			"select `password`, `encrypted` from `__Auth` where `doctype`=%s and `name`=%s and `fieldname`=%s",
			(doctype, name or self.employee, fieldname),
		)
		return rows[0] if rows else None

	def new_error_logs(self):
		names = set(frappe.get_all("Error Log", pluck="name")) - self.error_logs
		return [frappe.db.get_value("Error Log", n, ["method", "error"]) for n in names]

	def assertNothingSent(self):
		self.assertEqual((self.sms, self.mails), ([], []))

	def assertRefusedWith(self, result, reason, **extra):
		self.assertRefused(result, reason)
		self.assertEqual(set(result), {"ok", "reason", "message", *extra})
		for key, value in extra.items():
			self.assertEqual(result[key], value, result)


class TestBegin(PinCase):
	def test_set_pin_sends_a_code_by_sms(self):
		result = self.begin("set_pin", None)
		self.assertEqual(
			result,
			{
				"ok": True,
				"employee": self.employee,
				"employee_name": "Tokone Tok Test",
				"channel": "sms",
				"destination": "07•• ••• 123",
				"expires_in": 300,
			},
		)
		self.assertEqual(len(self.sms), 1)
		self.assertEqual(self.sms[0].url, "https://sms.tok-test.example/send")
		self.assertEqual(self.sms[0].to, "254712345123")
		code = self.sent_code()
		self.assertEqual(
			self.sms[0].message, f"Your employee app code is {code}. It expires in 5 minutes. Do not share it."
		)
		self.assertEqual(self.mails, [])
		# Frappe's own send_sms would have kept the message in an SMS Log.
		self.assertEqual(frappe.db.count("SMS Log"), self.sms_logs)

	def test_only_a_keyed_hash_of_the_code_is_kept(self):
		import hashlib

		code = self.begin_ok("set_pin")
		state = self.state()
		self.assertEqual(state.code_purpose, "set_pin")
		self.assertEqual(state.code_wrong_tries, 0)
		self.assertRegex(state.code_hash, r"^[0-9a-f]{64}$")
		self.assertNotIn(code, " ".join(str(v) for v in state.values()))
		# Not a bare digest of the six digits: that is found by trying all of them.
		self.assertNotEqual(state.code_hash, hashlib.sha256(code.encode()).hexdigest())
		self.assertEqual(frappe.get_meta(STATE_DOCTYPE).permissions, [])

	def test_sign_in_needs_the_right_pin(self):
		self.give_pin()
		result = self.begin("sign_in", f" {PIN} ")
		self.assertTrue(result["ok"], result)
		self.assertEqual((result["channel"], result["expires_in"]), ("sms", 300))
		self.assertEqual(len(self.sms), 1)

	def test_set_pin_takes_no_notice_of_a_pin(self):
		self.give_pin()
		for pin in (None, "000000", OTHER_PIN):
			self.assertTrue(self.begin("set_pin", pin)["ok"])
		self.assertIsNone(self.state().wrong_pins_at)

	def test_id_number_and_purpose(self):
		for id_number in ("", "  ", None):
			self.assertRefusedWith(begin_employee_sign_in(id_number, "set_pin"), "id_number_required")
		for purpose in ("", None, "SIGN_IN", "reset", "sign_in "):
			self.assertRefusedWith(begin_employee_sign_in(ID_NUMBER, purpose, PIN), "invalid_purpose")
		self.assertRefusedWith(begin_employee_sign_in("TOK00000000", "set_pin"), "not_found")
		self.assertNothingSent()

	def test_ambiguous_and_inactive(self):
		frappe.set_user("Administrator")
		twin = helpers.make_employee("Toktwin", id_number=ID_NUMBER).name
		frappe.set_user(HUB)
		self.assertRefusedWith(self.begin("set_pin"), "ambiguous")
		frappe.db.set_value("Employee", twin, "status", "Left")
		self.assertTrue(self.begin("set_pin")["ok"])
		frappe.db.set_value("Employee", self.employee, "status", "Left")
		self.assertRefusedWith(self.begin("set_pin"), "employee_inactive")
		self.assertEqual(len(self.sms), 1)

	def test_an_employee_who_could_not_be_given_a_token_gets_no_code(self):
		self.give_pin()
		cases = (
			("app_access_off", lambda: frappe.db.set_value("Employee", self.employee, "custom_app_access", 0)),
			("user_disabled", lambda: frappe.db.set_value("User", self.user, "enabled", 0)),
			("user_has_other_roles", lambda: frappe.get_doc("User", self.user).add_roles("Attendance Marking")),
			("no_user", lambda: frappe.db.set_value("Employee", self.employee, "user_id", None)),
			("oauth_client_not_set", lambda: helpers.set_settings(employee_token_client=None)),
			("oauth_client_missing", lambda: helpers.set_settings(employee_token_client="tok-no-such-client")),
			("oauth_client_no_scopes", lambda: frappe.db.set_value("OAuth Client", self.client, "scopes", "profile")),
		)
		for reason, change in cases:
			frappe.db.savepoint("tok_case")
			frappe.set_user("Administrator")
			change()
			frappe.set_user(HUB)
			for purpose in ("sign_in", "set_pin"):
				self.assertRefusedWith(self.begin(purpose), reason)
			frappe.db.rollback(save_point="tok_case")
			frappe.clear_cache(user=self.user)
			helpers.set_settings(employee_token_client=self.client)
		self.assertNothingSent()
		self.assertTrue(self.begin()["ok"])

	def test_pin_required_not_set_and_wrong(self):
		self.assertRefusedWith(self.begin("sign_in", PIN), "pin_not_set")
		self.give_pin()
		for pin in (None, "", "  "):
			self.assertRefusedWith(self.begin("sign_in", pin), "pin_required")
		for pin in (OTHER_PIN, "13579", "1357900", "abcdef"):
			self.assertRefusedWith(self.begin("sign_in", pin), "wrong_pin")
		self.assertNothingSent()

	def test_five_wrong_pins_in_an_hour_lock_the_pin_for_an_hour(self):
		self.give_pin()
		with self.later(0):
			for _i in range(4):
				self.assertRefusedWith(self.begin("sign_in", OTHER_PIN), "wrong_pin")
		with self.later(600):
			self.assertRefusedWith(self.begin("sign_in", OTHER_PIN), "pin_locked", retry_after=3600)
		with self.later(601):
			# The right PIN is refused as well, and so is another wrong one.
			self.assertRefusedWith(self.begin("sign_in", PIN), "pin_locked", retry_after=3599)
			self.assertRefusedWith(self.begin("sign_in", OTHER_PIN), "pin_locked", retry_after=3599)
		with self.later(600 + 3599):
			self.assertRefusedWith(self.begin("sign_in", PIN), "pin_locked", retry_after=1)
		self.assertNothingSent()
		with self.later(600 + 3600):
			self.assertTrue(self.begin("sign_in", PIN)["ok"])
			# A clean slate after the lock: one more wrong PIN does not lock it again.
			self.assertRefusedWith(self.begin("sign_in", OTHER_PIN), "wrong_pin")

	def test_wrong_pins_spread_over_more_than_an_hour_do_not_lock(self):
		self.give_pin()
		for seconds in (0, 1000, 2000, 3000, 3601, 4601, 5601, 6601):
			with self.later(seconds):
				self.assertRefusedWith(self.begin("sign_in", OTHER_PIN), "wrong_pin")
		with self.later(6602):
			self.assertRefusedWith(self.begin("sign_in", OTHER_PIN), "pin_locked", retry_after=3600)

	def test_the_right_pin_forgets_the_wrong_ones(self):
		self.give_pin()
		for _round in range(3):
			for _i in range(4):
				self.assertRefusedWith(self.begin("sign_in", OTHER_PIN), "wrong_pin")
			with self.later(0):
				self.begin_ok("sign_in")
			# Each round sends a code; keep clear of the sending limit.
			frappe.db.set_value(STATE_DOCTYPE, self.employee, "codes_sent_at", None)

	def test_a_lock_takes_a_sign_in_code_with_it_but_not_the_way_back_in(self):
		self.give_pin()
		code = self.begin_ok("sign_in")
		for _i in range(5):
			self.begin("sign_in", OTHER_PIN)
		self.assertRefusedWith(self.complete(code), "no_code")
		# Forgot PIN still works while the PIN is locked, and lifts the lock.
		code = self.begin_ok("set_pin")
		self.assertTrue(self.complete(code, "set_pin", new_pin=OTHER_PIN)["ok"])
		self.assertTrue(self.begin("sign_in", OTHER_PIN)["ok"])

	def test_wrong_pins_do_not_cancel_a_pending_set_pin_code(self):
		self.give_pin()
		code = self.begin_ok("set_pin")
		for _i in range(5):
			self.begin("sign_in", OTHER_PIN)
		self.assertTrue(self.complete(code, "set_pin", new_pin=OTHER_PIN)["ok"])

	def test_email_when_there_is_no_mobile_number(self):
		frappe.db.set_value("Employee", self.employee, {"cell_number": None, "personal_email": EMAIL})
		queued = frappe.db.count("Email Queue")
		result = self.begin("set_pin")
		self.assertTrue(result["ok"], result)
		self.assertEqual((result["channel"], result["destination"]), ("email", f"t•••@{helpers.TEST_DOMAIN}"))
		self.assertEqual(self.sms, [])
		self.assertEqual(len(self.mails), 1)
		self.assertEqual(self.mails[0].recipients, [EMAIL])
		code = self.sent_code()
		self.assertIn(code, self.mails[0].message)
		self.assertNotIn(code, self.mails[0].subject)
		# The queue row holds the message, so it does not stay.
		self.assertEqual(frappe.db.count("Email Queue"), queued)
		self.assertFalse(frappe.db.exists("Email Queue Recipient", {"recipient": EMAIL}))
		self.assertTrue(self.complete(code, "set_pin", new_pin=PIN)["ok"])

	def test_which_address(self):
		staff = f"{self.employee.lower()}@staff.{helpers.TEST_DOMAIN}"
		frappe.db.set_value("Employee", self.employee, "cell_number", None)
		for values, expected in (
			({"prefered_email": f"p@{helpers.TEST_DOMAIN}", "personal_email": EMAIL, "company_email": "c@x.example"}, f"p@{helpers.TEST_DOMAIN}"),
			({"prefered_email": staff, "personal_email": EMAIL, "company_email": "c@x.example"}, EMAIL),
			({"prefered_email": staff, "personal_email": None, "company_email": "c@x.example"}, "c@x.example"),
		):
			frappe.db.set_value("Employee", self.employee, values)
			frappe.db.set_value(STATE_DOCTYPE, self.employee, "codes_sent_at", None)
			self.assertEqual(self.begin("set_pin")["channel"], "email")
			self.assertEqual(self.mails[-1].recipients, [expected])

	def test_no_contact(self):
		staff = f"{self.employee.lower()}@staff.{helpers.TEST_DOMAIN}"
		for values in (
			{"cell_number": None},
			{"cell_number": "  "},
			{"cell_number": None, "prefered_email": staff, "company_email": staff},
			{"cell_number": "020 123", "personal_email": "not an address"},
		):
			frappe.db.set_value("Employee", self.employee, values)
			self.assertRefusedWith(self.begin("set_pin"), "no_contact")
		self.assertNothingSent()
		self.assertFalse(self.state().code_hash)

	def test_a_number_that_cannot_take_an_sms_falls_back_to_email(self):
		frappe.db.set_value("Employee", self.employee, {"cell_number": "020 1234567", "personal_email": EMAIL})
		self.assertEqual(self.begin("set_pin")["channel"], "email")

	def test_a_site_without_an_sms_gateway(self):
		helpers.set_sms_gateway(url=None)
		self.assertRefusedWith(self.begin("set_pin"), "send_failed")
		self.assertNothingSent()
		frappe.db.set_value("Employee", self.employee, "personal_email", EMAIL)
		self.assertEqual(self.begin("set_pin")["channel"], "email")

	def test_an_international_number_is_sent_as_it_is(self):
		frappe.db.set_value("Employee", self.employee, "cell_number", "+255 712 345 987")
		result = self.begin("set_pin")
		self.assertEqual(result["destination"], "+255•• ••• 987")
		self.assertEqual(self.sms[0].to, "255712345987")

	def assertSendFailsCleanly(self):
		result = self.begin("set_pin")
		self.assertRefusedWith(result, "send_failed")
		code = self.sent_code()
		state = self.state()
		self.assertFalse(state.code_hash)
		self.assertFalse(state.codes_sent_at)
		self.assertRefusedWith(self.complete(code, "set_pin", new_pin=PIN), "no_code")
		self.assertNotIn(code, " ".join(self.logged))
		return code

	def test_a_gateway_error_leaves_no_code_behind(self):
		for failure in (
			requests.exceptions.HTTPError("500 Server Error for url: https://sms.tok-test.example/send?message=..."),
			requests.exceptions.ConnectionError("no route"),
			302,
			500,
		):
			self.sms_status = failure
			self.assertSendFailsCleanly()
		self.assertIn("Employee code not sent", self.logged[-1])
		self.assertIn("HTTP 500", self.logged[-1])
		# Failed sends are not counted, so the next one that works is not held back.
		self.sms_status = 200
		code = self.begin_ok("set_pin")
		self.assertTrue(self.complete(code, "set_pin", new_pin=PIN)["ok"])

	def test_a_gateway_error_text_is_never_logged(self):
		"""A gateway called with GET has the message, code and all, in the URL of its error."""
		real = self.fake_sms_request

		def failing(gateway_url, params, *args):
			real(gateway_url, params)
			raise requests.exceptions.HTTPError(f"403 for url: {gateway_url}?message={params['message']}")

		with patch(SEND_REQUEST, failing):
			code = self.assertSendFailsCleanly()
		self.assertEqual(self.new_error_logs(), [])
		self.assertNotIn(code, " ".join(self.logged))

	def test_an_email_that_does_not_go_out(self):
		frappe.db.set_value("Employee", self.employee, {"cell_number": None, "personal_email": EMAIL})
		queued = frappe.db.count("Email Queue")
		# "muted": the site's e-mails are switched off, so send() does nothing.
		for outcome in ("error", "muted"):
			self.mail_outcome = outcome
			self.assertSendFailsCleanly()
			# Left in the queue it would be sent later, with a code that no longer works.
			self.assertEqual(frappe.db.count("Email Queue"), queued)
		with patch("frappe.sendmail", lambda **kwargs: []):
			self.assertRefusedWith(self.begin("set_pin"), "send_failed")

	def test_a_failed_send_keeps_the_limits_of_the_sends_that_worked(self):
		with self.later(0):
			for _i in range(3):
				self.begin_ok("set_pin")
			self.assertRefusedWith(self.begin("set_pin"), "too_many_codes", retry_after=900)
		self.assertEqual(len(self.sms), 3)

	def test_three_codes_in_fifteen_minutes(self):
		with self.later(0):
			self.begin_ok("set_pin")
		with self.later(100):
			self.begin_ok("set_pin")
		with self.later(200):
			self.begin_ok("set_pin")
			self.assertRefusedWith(self.begin("set_pin"), "too_many_codes", retry_after=700)
		with self.later(899):
			self.assertRefusedWith(self.begin("set_pin"), "too_many_codes", retry_after=1)
		self.assertEqual(len(self.sms), 3)
		with self.later(900):
			self.begin_ok("set_pin")
			# The second of the first three is still inside the fifteen minutes.
			self.assertRefusedWith(self.begin("set_pin"), "too_many_codes", retry_after=100)

	def test_a_refused_request_for_a_code_keeps_the_pending_one(self):
		with self.later(0):
			for _i in range(2):
				self.begin_ok("set_pin")
			code = self.begin_ok("set_pin")
			self.assertRefused(self.begin("set_pin"), "too_many_codes")
			self.assertTrue(self.complete(code, "set_pin", new_pin=PIN)["ok"])

	def test_ten_codes_in_a_day(self):
		for i in range(10):
			with self.later(i * 480):
				self.begin_ok("set_pin")
		with self.later(10 * 480):
			self.assertRefusedWith(self.begin("set_pin"), "too_many_codes", retry_after=86400 - 4800)
		with self.later(86399):
			self.assertRefusedWith(self.begin("set_pin"), "too_many_codes", retry_after=1)
		self.assertEqual(len(self.sms), 10)
		with self.later(86400):
			self.begin_ok("set_pin")

	def test_the_limit_is_per_employee_and_covers_both_purposes(self):
		self.give_pin()
		frappe.set_user("Administrator")
		other, _user = helpers.make_self_service_employee("Tokother", id_number="TOK90000002", cell_number="0722000111")
		frappe.set_user(HUB)
		with self.later(0):
			self.begin_ok("set_pin")
			self.begin_ok("sign_in")
			self.begin_ok("set_pin")
			self.assertRefused(self.begin("sign_in"), "too_many_codes")
			self.assertTrue(begin_employee_sign_in("TOK90000002", "set_pin")["ok"])
		self.assertEqual(self.sms[-1].to, "254722000111")


class TestComplete(PinCase):
	def web_app_token(self):
		return frappe.get_doc(
			{
				"doctype": "OAuth Bearer Token",
				"client": self.client,
				"user": self.user,
				"scopes": "openid all",
				"access_token": frappe.generate_hash(length=40),
				"refresh_token": frappe.generate_hash(length=40),
				"expires_in": 3600,
			}
		).insert(ignore_permissions=True)

	def test_both_purposes_and_both_kinds(self):
		self.give_pin()
		for purpose in ("sign_in", "set_pin"):
			for kind in ("own", "helper"):
				frappe.db.set_value(STATE_DOCTYPE, self.employee, "codes_sent_at", None)
				code = self.begin_ok(purpose)
				result = self.complete(code, purpose, kind, new_pin=PIN if purpose == "set_pin" else None)
				self.assertTrue(result["ok"], result)
				self.assertEqual(set(result), TOKEN_KEYS)
				self.assertEqual(
					(result["user"], result["employee"], result["employee_name"]),
					(self.user, self.employee, "Tokone Tok Test"),
				)
				self.assertIs(result["pin_set"], purpose == "set_pin")
				self.assertEqual(accepted(result["access_token"]), self.user)
				if kind == "own":
					self.assertEqual(result["expires_in"], 3600)
					self.assertTrue(refreshable(result["refresh_token"]))
				else:
					self.assertEqual(result["expires_in"], 600)
					self.assertIsNone(result["refresh_token"])
					self.assertFalse(frappe.db.get_value("OAuth Bearer Token", result["access_token"], "refresh_token"))

	def test_first_time_then_sign_in(self):
		"""The whole way: choose a PIN with a code, then sign in with the PIN and a code."""
		self.assertFalse(employee_pin.has_pin(self.employee))
		code = self.begin_ok("set_pin", None)
		first = self.complete(code, "set_pin", "own", new_pin=PIN)
		self.assertTrue(first["pin_set"])
		self.assertTrue(employee_pin.has_pin(self.employee))
		code = self.begin_ok("sign_in", PIN)
		second = self.complete(code, "sign_in", "own")
		self.assertFalse(second["pin_set"])
		self.assertEqual(accepted(second["access_token"]), self.user)
		# One phone per employee, as before.
		self.assertIsNone(accepted(first["access_token"]))

	def test_set_pin_revokes_every_earlier_token_for_both_kinds(self):
		self.give_pin()
		for kind in ("own", "helper"):
			frappe.db.set_value(STATE_DOCTYPE, self.employee, "codes_sent_at", None)
			own = self.complete(self.begin_ok("sign_in"), "sign_in", "own")
			helper = self.complete(self.begin_ok("sign_in"), "sign_in", "helper")
			web = self.web_app_token()
			for token in (own["access_token"], helper["access_token"], web.access_token):
				self.assertEqual(accepted(token), self.user)

			new = self.complete(self.begin_ok("set_pin"), "set_pin", kind, new_pin=PIN)
			self.assertTrue(new["ok"], new)
			for token in (own["access_token"], helper["access_token"], web.access_token):
				self.assertIsNone(accepted(token))
			self.assertFalse(refreshable(own["refresh_token"]))
			self.assertFalse(refreshable(web.refresh_token))
			self.assertEqual(accepted(new["access_token"]), self.user)
			self.assertEqual(self.active_tokens(), 1)

	def test_sign_in_revokes_as_the_kinds_always_did(self):
		self.give_pin()
		own = self.complete(self.begin_ok(), "sign_in", "own")
		helper = self.complete(self.begin_ok(), "sign_in", "helper")
		self.assertEqual(accepted(own["access_token"]), self.user)
		self.assertEqual(accepted(helper["access_token"]), self.user)
		frappe.db.set_value(STATE_DOCTYPE, self.employee, "codes_sent_at", None)
		again = self.complete(self.begin_ok(), "sign_in", "own")
		self.assertIsNone(accepted(own["access_token"]))
		self.assertIsNone(accepted(helper["access_token"]))
		self.assertEqual(accepted(again["access_token"]), self.user)

	def test_a_new_pin_replaces_the_old_one(self):
		self.give_pin()
		self.assertTrue(self.complete(self.begin_ok("set_pin"), "set_pin", new_pin=OTHER_PIN)["ok"])
		self.assertRefused(self.begin("sign_in", PIN), "wrong_pin")
		self.assertTrue(self.begin("sign_in", OTHER_PIN)["ok"])

	def test_the_pin_is_kept_as_a_hash_and_is_not_the_users_password(self):
		password_before = self.stored_pin("User", self.user, "password")
		self.complete(self.begin_ok("set_pin"), "set_pin", new_pin=PIN)
		stored = self.stored_pin()
		self.assertTrue(stored[0].startswith("$pbkdf2-sha256$"), stored[0][:16])
		self.assertEqual(stored[1], 0)
		self.assertNotIn(PIN, stored[0])
		# The PIN itself was not what went into the hash, so a copy of the table is not enough.
		from frappe.utils.password import passlibctx

		self.assertFalse(passlibctx.verify(PIN, stored[0]))
		self.assertEqual(self.stored_pin("User", self.user, "password"), password_before)
		self.assertRaises(frappe.AuthenticationError, check_password, self.user, PIN)
		self.assertFalse(frappe.get_meta("Employee").has_field("app_pin"))

	def test_kind_purpose_and_employee(self):
		code = self.begin_ok("set_pin")
		for kind in ("", None, "OWN", "admin"):
			self.assertRefusedWith(self.complete(code, "set_pin", kind, new_pin=PIN), "invalid_kind")
		for purpose in ("", None, "SET_PIN", "reset"):
			self.assertRefusedWith(self.complete(code, purpose, new_pin=PIN), "invalid_purpose")
		for employee in ("TOK-NO-SUCH-EMPLOYEE", None, " "):
			self.assertRefusedWith(
				complete_employee_sign_in(employee, "set_pin", code, "own", PIN), "employee_not_found"
			)
		self.assertFalse(employee_pin.has_pin(self.employee))
		# None of those used the code up.
		self.assertTrue(self.complete(code, "set_pin", new_pin=PIN)["ok"])

	def test_no_code_pending(self):
		self.give_pin()
		for purpose in ("sign_in", "set_pin"):
			self.assertRefusedWith(self.complete("123456", purpose, new_pin=OTHER_PIN), "no_code")
		self.assertEqual(self.active_tokens(), 0)

	def test_a_code_is_for_one_purpose(self):
		"""Otherwise the set_pin code, which needs no PIN, would sign in past the PIN."""
		self.give_pin()
		code = self.begin_ok("set_pin")
		self.assertRefusedWith(self.complete(code, "sign_in"), "no_code")
		code = self.begin_ok("sign_in")
		self.assertRefusedWith(self.complete(code, "set_pin", new_pin=OTHER_PIN), "no_code")
		self.assertEqual(self.active_tokens(), 0)
		self.assertTrue(employee_pin.pin_matches(self.employee, PIN))
		self.assertTrue(self.complete(code, "sign_in")["ok"])

	def test_a_code_is_for_one_employee(self):
		frappe.set_user("Administrator")
		other, _user = helpers.make_self_service_employee("Tokother", id_number="TOK90000002", cell_number="0722000111")
		frappe.set_user(HUB)
		code = self.begin_ok("set_pin")
		begin_employee_sign_in("TOK90000002", "set_pin")
		theirs = self.sent_code()
		if theirs != code:
			result = complete_employee_sign_in(other.name, "set_pin", code, "own", PIN)
			self.assertRefused(result, "wrong_code")
		# The same six digits for two employees are two different hashes.
		self.assertNotEqual(
			employee_pin._code_hash(self.employee, "set_pin", code), employee_pin._code_hash(other.name, "set_pin", code)
		)

	def test_a_code_lasts_five_minutes(self):
		with self.later(0):
			code = self.begin_ok("set_pin")
		with self.later(300):
			self.assertRefusedWith(self.complete(code, "set_pin", new_pin=PIN), "no_code")
		with self.later(0):
			code = self.begin_ok("set_pin")
		with self.later(299):
			self.assertTrue(self.complete(code, "set_pin", new_pin=PIN)["ok"])

	def test_a_code_works_once(self):
		self.give_pin()
		code = self.begin_ok("sign_in")
		self.assertTrue(self.complete(code)["ok"])
		self.assertRefusedWith(self.complete(code), "no_code")
		self.assertFalse(self.state().code_hash)
		self.assertEqual(self.active_tokens(), 1)

	def test_five_wrong_codes(self):
		self.give_pin()
		code = self.begin_ok("sign_in")
		wrong = self.another_code(code)
		for tries_left in (4, 3, 2, 1):
			self.assertRefusedWith(self.complete(wrong), "wrong_code", tries_left=tries_left)
		self.assertRefusedWith(self.complete(wrong), "too_many_code_tries")
		# The right code is too late now.
		self.assertRefusedWith(self.complete(code), "no_code")
		self.assertEqual(self.active_tokens(), 0)

	def test_the_right_code_after_wrong_ones(self):
		code = self.begin_ok("set_pin")
		for wrong in (self.another_code(code), "", None, code + "0"):
			self.assertRefused(self.complete(wrong, "set_pin", new_pin=PIN), "wrong_code")
		self.assertFalse(employee_pin.has_pin(self.employee))
		self.assertEqual(self.state().code_wrong_tries, 4)
		self.assertTrue(self.complete(f" {code} ", "set_pin", new_pin=PIN)["ok"])

	def test_asking_again_replaces_the_code(self):
		with patch("trident_attendance.employee_pin.secrets.randbelow", side_effect=[111111, 222222]):
			first = self.begin_ok("set_pin")
			self.complete("000000", "set_pin", new_pin=PIN)
			second = self.begin_ok("set_pin")
		self.assertEqual((first, second), ("111111", "222222"))
		# The new code starts with all its tries.
		self.assertRefusedWith(self.complete(first, "set_pin", new_pin=PIN), "wrong_code", tries_left=4)
		self.assertTrue(self.complete(second, "set_pin", new_pin=PIN)["ok"])

	def test_codes_are_six_digits_from_the_whole_range(self):
		with patch("trident_attendance.employee_pin.secrets.randbelow", return_value=42) as randbelow:
			self.assertEqual(self.begin_ok("set_pin"), "000042")
		randbelow.assert_called_once_with(10**6)

	def test_a_new_pin_that_breaks_the_rules(self):
		code = self.begin_ok("set_pin")
		for new_pin in (None, "", "12345", "1234567", "12345a"):
			self.assertRefusedWith(self.complete(code, "set_pin", new_pin=new_pin), "pin_invalid")
		# ID_NUMBER ends in 000001.
		for new_pin in ("000000", "123456", "987654", "012345", "000001"):
			self.assertRefusedWith(self.complete(code, "set_pin", new_pin=new_pin), "pin_too_simple")
		self.assertFalse(employee_pin.has_pin(self.employee))
		self.assertEqual(self.active_tokens(), 0)
		# The code is still good and none of its tries were spent.
		self.assertEqual(self.state().code_wrong_tries, 0)
		self.assertTrue(self.complete(code, "set_pin", new_pin=PIN)["ok"])

	def test_sign_in_takes_no_notice_of_new_pin(self):
		self.give_pin()
		self.assertTrue(self.complete(self.begin_ok(), "sign_in", new_pin="000000")["ok"])
		self.assertTrue(employee_pin.pin_matches(self.employee, PIN))

	def test_token_refusals_after_the_code_was_sent(self):
		cases = (
			("employee_inactive", lambda: frappe.db.set_value("Employee", self.employee, "status", "Left")),
			("app_access_off", lambda: frappe.db.set_value("Employee", self.employee, "custom_app_access", 0)),
			("user_disabled", lambda: frappe.db.set_value("User", self.user, "enabled", 0)),
			("user_has_other_roles", lambda: frappe.get_doc("User", self.user).add_roles("Attendance Marking")),
			("no_user", lambda: frappe.db.set_value("Employee", self.employee, "user_id", None)),
			("oauth_client_not_set", lambda: helpers.set_settings(employee_token_client=None)),
			("oauth_client_missing", lambda: helpers.set_settings(employee_token_client="tok-no-such-client")),
			("oauth_client_no_scopes", lambda: frappe.db.set_value("OAuth Client", self.client, "scopes", "profile")),
		)
		code = self.begin_ok("set_pin")
		for reason, change in cases:
			frappe.db.savepoint("tok_case")
			frappe.set_user("Administrator")
			change()
			frappe.set_user(HUB)
			for kind in ("own", "helper"):
				self.assertRefusedWith(self.complete(code, "set_pin", kind, new_pin=PIN), reason)
			frappe.db.rollback(save_point="tok_case")
			frappe.clear_cache(user=self.user)
			helpers.set_settings(employee_token_client=self.client)
		self.assertFalse(employee_pin.has_pin(self.employee))
		self.assertEqual(self.active_tokens(), 0)

	def test_the_face_switch_does_not_matter_here(self):
		self.assertRefused(issue_employee_token(self.employee, "own"), "face_sign_in_off")
		self.assertTrue(self.complete(self.begin_ok("set_pin"), "set_pin", new_pin=PIN)["ok"])


class TestNothingSensitiveLeaves(PinCase):
	def test_answers_logs_and_rows_hold_no_pin_code_or_hash(self):
		answers, codes = [], []

		def keep(result):
			answers.append(result)
			return result

		codes.append(self.begin_ok("set_pin"))
		keep(self.begin("set_pin"))
		codes.append(self.sent_code())
		keep(self.complete(self.another_code(codes[-1]), "set_pin", new_pin=PIN))
		keep(self.complete(codes[-1], "set_pin", new_pin="123456"))
		token = keep(self.complete(codes[-1], "set_pin", new_pin=PIN))
		self.assertTrue(token["ok"])
		keep(self.begin("sign_in", OTHER_PIN))
		keep(self.begin("sign_in", PIN))
		codes.append(self.sent_code())
		keep(self.complete(codes[-1], "sign_in", "helper"))
		frappe.set_user("Administrator")
		hr = helpers.make_user(f"hr@{helpers.TEST_DOMAIN}", roles=["HR Manager"]).name
		frappe.set_user(hr)
		keep(get_employee_pin_status(self.employee))
		hashes = [self.stored_pin()[0], employee_pin._keyed("pin", PIN)]
		keep(reset_employee_pin(self.employee))

		secrets_ = [PIN, OTHER_PIN, *codes, *hashes]
		logged = " ".join(self.logged)
		self.assertIn("Employee PIN set", logged)
		self.assertIn("Employee code sent", logged)
		tokens = {a.get("access_token") for a in answers} | {a.get("refresh_token") for a in answers}
		for secret in secrets_:
			for answer in answers:
				shown = " ".join(str(v) for k, v in answer.items() if k not in ("access_token", "refresh_token"))
				self.assertNotIn(secret, shown)
			self.assertNotIn(secret, logged)
		for token in tokens - {None}:
			self.assertNotIn(token, logged)
		self.assertEqual(self.new_error_logs(), [])
		self.assertEqual(frappe.db.count("SMS Log"), self.sms_logs)
		self.assertFalse(frappe.db.count("Version", {"ref_doctype": STATE_DOCTYPE}))

	def assertServerError(self, result, *secrets_):
		"""Raised, Frappe would write every local variable of the request to the Error Log. The
		answer rolls the request back, and with it everything this test built."""
		self.assertRefusedWith(result, "server_error")
		self.assertEqual(frappe.local.response.pop("http_status_code"), 500)
		self.assertFalse(frappe.db.exists("Employee", self.employee))
		logs = self.new_error_logs()
		self.assertEqual(len(logs), 1)
		title, error = logs[0]
		self.assertIn("Employee sign-in failed in", title)
		self.assertIn("RuntimeError", error)
		self.assertIn("employee_access.py", error)
		for secret in (*secrets_, "boom"):
			self.assertNotIn(secret, f"{title} {error}")

	def test_an_unexpected_error_in_begin_is_a_500_that_logs_no_pin(self):
		self.give_pin()
		with patch("trident_attendance.employee_pin.pin_matches", side_effect=RuntimeError(f"boom {PIN}")):
			result = self.begin("sign_in", PIN)
		self.assertServerError(result, PIN)

	def test_an_unexpected_error_in_complete_is_a_500_that_logs_no_code_or_pin(self):
		code = self.begin_ok("set_pin")
		with patch("trident_attendance.employee_pin.check_code", side_effect=RuntimeError(f"boom {code}")):
			result = self.complete(code, "set_pin", new_pin=PIN)
		self.assertServerError(result, code, PIN)

	def test_a_caller_that_is_not_the_hub_still_gets_the_403(self):
		frappe.set_user(self.user)
		self.assertRaises(HubOnlyError, self.begin, "set_pin")
		self.assertRaises(HubOnlyError, self.complete, "123456")
		self.assertNothingSent()


class TestResetEmployeePin(PinCase):
	def as_role(self, *roles, name="office"):
		frappe.set_user("Administrator")
		user = helpers.make_user(f"{name}-{len(roles)}-{roles[0].lower().replace(' ', '')}@{helpers.TEST_DOMAIN}", roles=roles)
		frappe.set_user(user.name)
		return user.name

	def lock_pin_and_sign_in(self):
		"""An employee with a PIN, a live token, a locked PIN and a set_pin code on its way."""
		frappe.set_user(HUB)
		frappe.db.delete(STATE_DOCTYPE, {"name": self.employee})
		self.give_pin()
		token = self.complete(self.begin_ok("sign_in"), "sign_in", "own")
		for _i in range(5):
			self.begin("sign_in", OTHER_PIN)
		self.assertRefused(self.begin("sign_in", PIN), "pin_locked")
		code = self.begin_ok("set_pin")
		return token, code

	def test_each_allowed_role_can_reset(self):
		for role in ("HR Manager", "HR User", "Attendance Admin", "System Manager"):
			token, code = self.lock_pin_and_sign_in()
			self.as_role(role)
			self.assertEqual(
				get_employee_pin_status(self.employee),
				{"ok": True, "employee": self.employee, "pin_set": True, "pin_locked": True},
			)
			result = reset_employee_pin(self.employee)
			self.assertEqual(result, {"ok": True, "employee": self.employee, "had_pin": True, "revoked": 1})
			self.assertEqual(
				get_employee_pin_status(self.employee),
				{"ok": True, "employee": self.employee, "pin_set": False, "pin_locked": False},
			)
			self.assertFalse(employee_pin.has_pin(self.employee))
			self.assertIsNone(accepted(token["access_token"]))
			self.assertFalse(refreshable(token["refresh_token"]))
			frappe.set_user(HUB)
			# The pending code went with the PIN, and the lock is off.
			self.assertRefused(self.complete(code, "set_pin", new_pin=OTHER_PIN), "no_code")
			self.assertRefused(self.begin("sign_in", PIN), "pin_not_set")
			self.assertFalse(self.state().pin_locked_until)
			frappe.db.set_value(STATE_DOCTYPE, self.employee, "codes_sent_at", None)
			self.assertTrue(self.complete(self.begin_ok("set_pin"), "set_pin", new_pin=OTHER_PIN)["ok"])
			self.assertTrue(self.begin("sign_in", OTHER_PIN)["ok"])

	def test_a_reset_does_not_hand_out_more_codes(self):
		with self.later(0):
			for _i in range(3):
				self.begin_ok("set_pin")
			self.as_role("HR Manager")
			reset_employee_pin(self.employee)
			frappe.set_user(HUB)
			self.assertRefused(self.begin("set_pin"), "too_many_codes")

	def test_everyone_else_is_refused(self):
		token, code = self.lock_pin_and_sign_in()
		others = [self.user, "Guest"]
		others.append(self.as_role("Attendance Marking"))
		others.append(self.as_role("Employee", "Accounts Manager", "Projects Manager"))
		for user in others:
			frappe.set_user(user)
			self.assertRaises(frappe.PermissionError, reset_employee_pin, self.employee)
			self.assertRaises(frappe.PermissionError, get_employee_pin_status, self.employee)
			# Nor do they learn whether an employee exists.
			self.assertRaises(frappe.PermissionError, reset_employee_pin, "TOK-NO-SUCH-EMPLOYEE")
		self.assertTrue(employee_pin.has_pin(self.employee))
		self.assertEqual(accepted(token["access_token"]), self.user)
		self.assertTrue(self.state().pin_locked_until)

	def test_being_the_hub_is_not_what_allows_it(self):
		"""A role decides. The hub's login holds Attendance Admin; without it, it is refused."""
		self.give_pin()
		frappe.set_user(HUB)
		self.assertTrue(reset_employee_pin(self.employee)["ok"])
		self.give_pin()
		frappe.set_user("Administrator")
		frappe.get_doc("User", HUB).remove_roles("Attendance Admin")
		self.assertEqual(frappe.db.get_single_value("Trident Attendance Settings", "hub_service_user"), HUB)
		frappe.set_user(HUB)
		self.assertRaises(frappe.PermissionError, reset_employee_pin, self.employee)
		self.assertRaises(frappe.PermissionError, get_employee_pin_status, self.employee)
		self.assertTrue(employee_pin.has_pin(self.employee))

	def test_hr_limited_to_other_employees_cannot_reset_this_one(self):
		self.give_pin()
		frappe.set_user("Administrator")
		other = helpers.make_employee("Tokother").name
		hr = helpers.make_user(f"hr-limited@{helpers.TEST_DOMAIN}", roles=["HR User"]).name
		frappe.get_doc(
			{"doctype": "User Permission", "user": hr, "allow": "Employee", "for_value": other}
		).insert(ignore_permissions=True)
		frappe.set_user(hr)
		self.assertRaises(frappe.PermissionError, reset_employee_pin, self.employee)
		self.assertRaises(frappe.PermissionError, get_employee_pin_status, self.employee)
		self.assertTrue(employee_pin.has_pin(self.employee))
		self.assertTrue(reset_employee_pin(other)["ok"])

	def test_unknown_employee_no_pin_and_one_who_left(self):
		self.as_role("HR User")
		self.assertRefusedWith(reset_employee_pin("TOK-NO-SUCH-EMPLOYEE"), "employee_not_found")
		self.assertRefusedWith(get_employee_pin_status(None), "employee_not_found")
		self.assertEqual(
			reset_employee_pin(self.employee), {"ok": True, "employee": self.employee, "had_pin": False, "revoked": 0}
		)
		self.give_pin()
		frappe.db.set_value("Employee", self.employee, {"status": "Left", "custom_app_access": 0, "user_id": None})
		self.assertEqual(
			reset_employee_pin(self.employee), {"ok": True, "employee": self.employee, "had_pin": True, "revoked": 0}
		)
		self.assertFalse(employee_pin.has_pin(self.employee))

	def test_looking_at_the_status_writes_nothing(self):
		self.as_role("HR User")
		self.assertEqual(
			get_employee_pin_status(self.employee),
			{"ok": True, "employee": self.employee, "pin_set": False, "pin_locked": False},
		)
		self.assertFalse(frappe.db.exists(STATE_DOCTYPE, self.employee))

	def test_whitelisted_post_only_and_not_for_guests(self):
		for method in (reset_employee_pin, get_employee_pin_status):
			self.assertIn(method, frappe.whitelisted)
			self.assertNotIn(method, frappe.guest_methods)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[reset_employee_pin], ["POST"])

	def test_the_employee_form_gets_its_script(self):
		from frappe.desk.form.meta import get_code_files_via_hooks

		paths = [p.replace("\\", "/") for p in get_code_files_via_hooks("doctype_js", "Employee")]
		ours = [p for p in paths if p.endswith("trident_attendance/public/js/employee.js")]
		self.assertEqual(len(ours), 1, paths)
		script = frappe.read_file(ours[0])
		self.assertIn("trident_attendance.employee_access.reset_employee_pin", script)
		self.assertIn("trident_attendance.employee_access.get_employee_pin_status", script)
		self.assertIn("custom_app_access", script)
		self.assertIn("frappe.confirm", script)


class TestEmployeeEvents(PinCase):
	def test_deleting_an_employee_deletes_the_pin_and_the_state(self):
		frappe.set_user("Administrator")
		gone = helpers.make_employee("Tokgone").name
		employee_pin.set_pin(gone, PIN)
		employee_pin.new_code(employee_pin.load_state(gone), "set_pin")
		self.assertTrue(frappe.db.exists(STATE_DOCTYPE, gone))
		frappe.delete_doc("Employee", gone, force=True)
		self.assertFalse(employee_pin.has_pin(gone))
		self.assertFalse(frappe.db.exists(STATE_DOCTYPE, gone))

	def test_a_renamed_employee_keeps_the_pin_and_the_lock(self):
		self.give_pin()
		for _i in range(5):
			self.begin("sign_in", OTHER_PIN)
		old, new = self.employee, "TOK-RENAMED-EMPLOYEE"
		# What frappe.rename_doc does for `__Auth`, then this app's after_rename.
		rename_password("Employee", old, new)
		employee_pin.after_employee_rename(None, "after_rename", old, new, False)
		self.assertTrue(employee_pin.pin_matches(new, PIN))
		self.assertFalse(frappe.db.exists(STATE_DOCTYPE, old))
		self.assertTrue(employee_pin.pin_lock_wait(employee_pin.load_state(new, for_update=False)))
		# Put back, so the rollback finds the rows it knows.
		rename_password("Employee", new, old)

	def test_a_merged_employee_drops_its_state(self):
		self.give_pin()
		self.begin_ok("set_pin")
		employee_pin.after_employee_rename(None, "after_rename", self.employee, "TOK-OTHER", True)
		self.assertFalse(frappe.db.exists(STATE_DOCTYPE, self.employee))
