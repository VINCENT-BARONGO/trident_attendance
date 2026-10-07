from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.oauth import OAuthWebRequestValidator
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from trident_attendance import employee_access
from trident_attendance.employee_access import (
	HubOnlyError,
	begin_employee_sign_in,
	complete_employee_sign_in,
	get_employee_for_verification,
	get_verification_photo,
	issue_employee_token,
	revoke_employee_tokens,
)
from trident_attendance.tests import helpers
from trident_attendance.utils import SETTINGS_DOCTYPE

HUB = f"hub@{helpers.TEST_DOMAIN}"
ID_NUMBER = "TOK90000001"


def accepted(access_token) -> str | None:
	"""The user Frappe's own bearer check accepts this token as, or None."""
	request = frappe._dict()
	if OAuthWebRequestValidator().validate_bearer_token(access_token, ["openid", "all"], request):
		return request.user
	return None


def refreshable(refresh_token) -> bool:
	"""Whether Frappe's refresh grant would still take this refresh token."""
	return bool(frappe.db.exists("OAuth Bearer Token", {"refresh_token": refresh_token, "status": "Active"}))


class EmployeeAccessCase(FrappeTestCase):
	# The face-era methods answer `face_sign_in_off` unless the setting is ticked.
	FACE_SIGN_IN = 0

	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		helpers.make_user(HUB, roles=["Attendance Admin"])
		self.client = helpers.make_oauth_client().name
		helpers.set_settings(
			hub_service_user=HUB,
			employee_token_client=self.client,
			helper_token_minutes=10,
			allow_face_sign_in=self.FACE_SIGN_IN,
		)
		self.employee, self.user = helpers.make_self_service_employee("Tokone", id_number=ID_NUMBER)
		self.employee = self.employee.name
		frappe.set_user(HUB)

	def assertRefused(self, result, reason):
		self.assertEqual(result.get("ok"), False, result)
		self.assertEqual(result.get("reason"), reason, result)
		self.assertNotIn("access_token", result)

	def active_tokens(self, user=None):
		return frappe.db.count("OAuth Bearer Token", {"user": user or self.user, "status": "Active"})


class TestHubOnly(EmployeeAccessCase):
	def calls(self):
		return (
			lambda: get_employee_for_verification(ID_NUMBER),
			lambda: get_verification_photo(self.employee),
			lambda: issue_employee_token(self.employee, "own"),
			lambda: issue_employee_token(self.employee, "helper"),
			lambda: revoke_employee_tokens(self.employee),
			lambda: begin_employee_sign_in(ID_NUMBER, "set_pin"),
			lambda: begin_employee_sign_in(ID_NUMBER, "sign_in", "135790"),
			lambda: complete_employee_sign_in(self.employee, "set_pin", "123456", "own", "135790"),
			lambda: complete_employee_sign_in(self.employee, "sign_in", "123456", "helper"),
		)

	def assertAllRefuse(self):
		for call in self.calls():
			self.assertRaises(HubOnlyError, call)
		self.assertEqual(self.active_tokens(), 0)

	def test_guest_is_refused(self):
		frappe.set_user("Guest")
		self.assertAllRefuse()

	def test_another_reviewer_is_refused(self):
		"""Holding the hub's role is not being the hub."""
		frappe.set_user("Administrator")
		helpers.make_user(f"reviewer@{helpers.TEST_DOMAIN}", roles=["Attendance Admin", "System Manager"])
		frappe.set_user(f"reviewer@{helpers.TEST_DOMAIN}")
		self.assertAllRefuse()

	def test_administrator_is_refused(self):
		frappe.set_user("Administrator")
		self.assertAllRefuse()
		helpers.set_settings(hub_service_user="Administrator")
		self.assertAllRefuse()

	def test_the_employee_is_refused(self):
		frappe.set_user(self.user)
		self.assertAllRefuse()

	def test_nobody_is_the_hub_when_the_setting_is_empty(self):
		helpers.set_settings(hub_service_user=None)
		self.assertAllRefuse()

	def test_hub_user_without_the_reviewer_role_is_refused(self):
		frappe.set_user("Administrator")
		frappe.get_doc("User", HUB).remove_roles("Attendance Admin")
		frappe.set_user(HUB)
		self.assertAllRefuse()

	def test_refusal_is_a_permission_error(self):
		self.assertTrue(issubclass(HubOnlyError, frappe.PermissionError))
		self.assertEqual(HubOnlyError.http_status_code, 403)

	def test_no_method_is_open_to_guests(self):
		for method in (
			get_employee_for_verification,
			get_verification_photo,
			issue_employee_token,
			revoke_employee_tokens,
			begin_employee_sign_in,
			complete_employee_sign_in,
		):
			self.assertIn(method, frappe.whitelisted)
			self.assertNotIn(method, frappe.guest_methods)

	def test_the_methods_that_change_things_are_post_only(self):
		for method in (
			issue_employee_token,
			revoke_employee_tokens,
			begin_employee_sign_in,
			complete_employee_sign_in,
		):
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"])

	def test_only_a_system_manager_names_the_hub(self):
		frappe.set_user("Administrator")
		hr = helpers.make_user(f"hr@{helpers.TEST_DOMAIN}", roles=["HR Manager", "Attendance Admin"]).name
		for field, value in (("hub_service_user", hr), ("employee_token_client", None), ("allow_face_sign_in", 1)):
			frappe.set_user(hr)
			settings = frappe.get_doc(SETTINGS_DOCTYPE)
			settings.set(field, value)
			self.assertRaises(frappe.PermissionError, settings.save)
		# The rest of the page is still theirs to save.
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.photo_retention_days = 31
		settings.save()
		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, "hub_service_user"), HUB)
		self.assertFalse(frappe.db.get_single_value(SETTINGS_DOCTYPE, "allow_face_sign_in"))
		frappe.set_user("Administrator")
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.hub_service_user = hr
		settings.save()
		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, "hub_service_user"), hr)

	def test_helper_minutes_setting_is_kept_in_range(self):
		frappe.set_user("Administrator")
		for minutes in (61, -1):
			settings = frappe.get_doc(SETTINGS_DOCTYPE)
			settings.helper_token_minutes = minutes
			self.assertRaises(frappe.ValidationError, settings.save)
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.helper_token_minutes = 0
		settings.save()
		self.assertEqual(settings.helper_token_minutes, 10)


class TestGetEmployeeForVerification(EmployeeAccessCase):
	FACE_SIGN_IN = 1

	def with_photo(self):
		frappe.set_user("Administrator")
		file = helpers.attach_photo(self.employee)
		self.addCleanup(frappe.delete_doc, "File", file.name, force=True, ignore_permissions=True)
		frappe.set_user(HUB)

	def test_ready_employee(self):
		self.with_photo()
		result = get_employee_for_verification(f"  {ID_NUMBER} ")
		self.assertEqual(
			result,
			{
				"ok": True,
				"employee": self.employee,
				"employee_name": "Tokone Tok Test",
				"has_photo": True,
				"has_user": True,
				"user_enabled": True,
				"app_access": True,
				"can_verify": True,
				"reason": None,
			},
		)

	def test_photo_bytes(self):
		self.with_photo()
		get_verification_photo(self.employee)
		self.assertEqual(frappe.local.response.filecontent, helpers.JPEG)
		self.assertEqual(frappe.local.response.type, "download")
		# The read runs as Administrator; the caller must be put back.
		self.assertEqual(frappe.session.user, HUB)

	def test_photo_missing_or_not_active(self):
		self.assertRaises(frappe.DoesNotExistError, get_verification_photo, self.employee)
		self.assertRaises(frappe.DoesNotExistError, get_verification_photo, "TOK-NO-SUCH-EMPLOYEE")
		self.with_photo()
		frappe.db.set_value("Employee", self.employee, "status", "Left")
		self.assertRaises(frappe.DoesNotExistError, get_verification_photo, self.employee)

	def test_no_photo(self):
		result = get_employee_for_verification(ID_NUMBER)
		self.assertTrue(result["ok"])
		self.assertEqual((result["has_photo"], result["can_verify"], result["reason"]), (False, False, "no_photo"))

	def test_ambiguous_id_number_refuses(self):
		frappe.set_user("Administrator")
		helpers.make_employee("Toktwin", id_number=ID_NUMBER)
		frappe.set_user(HUB)
		result = get_employee_for_verification(ID_NUMBER)
		self.assertRefused(result, "ambiguous")
		self.assertNotIn("employee", result)

	def test_a_left_namesake_is_not_ambiguous(self):
		frappe.set_user("Administrator")
		helpers.make_employee("Toktwin", id_number=ID_NUMBER, status="Left")
		frappe.set_user(HUB)
		self.assertEqual(get_employee_for_verification(ID_NUMBER)["employee"], self.employee)

	def test_unknown_and_empty_id_number(self):
		self.assertRefused(get_employee_for_verification("TOK00000000"), "not_found")
		self.assertRefused(get_employee_for_verification("  "), "id_number_required")
		self.assertRefused(get_employee_for_verification(None), "id_number_required")

	def test_left_employee(self):
		frappe.db.set_value("Employee", self.employee, "status", "Left")
		self.assertRefused(get_employee_for_verification(ID_NUMBER), "employee_inactive")

	def test_reasons(self):
		frappe.db.set_value("Employee", self.employee, "image", "/private/files/tok_test_none.jpg")
		cases = (
			("app_access_off", lambda: frappe.db.set_value("Employee", self.employee, "custom_app_access", 0)),
			("user_disabled", lambda: frappe.db.set_value("User", self.user, "enabled", 0)),
			("user_has_other_roles", lambda: frappe.get_doc("User", self.user).add_roles("Attendance Marking")),
			("no_user", lambda: frappe.db.set_value("Employee", self.employee, "user_id", None)),
		)
		self.assertIsNone(get_employee_for_verification(ID_NUMBER)["reason"])
		for reason, change in cases:
			frappe.db.savepoint("tok_case")
			frappe.set_user("Administrator")
			change()
			frappe.set_user(HUB)
			result = get_employee_for_verification(ID_NUMBER)
			self.assertEqual((result["ok"], result["can_verify"], result["reason"]), (True, False, reason))
			frappe.db.rollback(save_point="tok_case")
			frappe.clear_cache(user=self.user)


class TestIssueEmployeeToken(EmployeeAccessCase):
	FACE_SIGN_IN = 1

	def test_own_token(self):
		before = now_datetime()
		result = issue_employee_token(self.employee, "own", device="test-phone")
		self.assertTrue(result["ok"])
		self.assertEqual(result["user"], self.user)
		self.assertEqual(result["employee"], self.employee)
		self.assertEqual(result["expires_in"], 3600)
		self.assertTrue(result["refresh_token"])
		self.assertNotEqual(result["refresh_token"], result["access_token"])
		self.assertEqual(accepted(result["access_token"]), self.user)
		self.assertTrue(refreshable(result["refresh_token"]))
		token = frappe.get_doc("OAuth Bearer Token", result["access_token"])
		self.assertEqual(token.client, self.client)
		self.assertEqual(token.scopes, "openid all")
		self.assertGreaterEqual(token.expiration_time, before + timedelta(seconds=3590))
		self.assertLessEqual(token.expiration_time, now_datetime() + timedelta(seconds=3600))

	def test_tokens_are_long_and_never_repeat(self):
		tokens = {employee_access._new_token() for _i in range(200)}
		self.assertEqual(len(tokens), 200)
		self.assertTrue(all(len(t) >= 40 and t.isalnum() for t in tokens))

	def test_helper_token_is_short_and_cannot_be_renewed(self):
		result = issue_employee_token(self.employee, "helper")
		self.assertTrue(result["ok"])
		self.assertIsNone(result["refresh_token"])
		self.assertEqual(result["expires_in"], 600)
		self.assertEqual(accepted(result["access_token"]), self.user)
		token = frappe.get_doc("OAuth Bearer Token", result["access_token"])
		self.assertFalse(token.refresh_token)
		self.assertLessEqual(token.expiration_time, now_datetime() + timedelta(seconds=600))
		self.assertFalse(frappe.db.count("OAuth Bearer Token", {"user": self.user, "refresh_token": ["is", "set"]}))

	def test_helper_token_runs_out(self):
		result = issue_employee_token(self.employee, "helper")
		with patch("frappe.oauth.now_datetime", return_value=now_datetime() + timedelta(seconds=599)):
			self.assertEqual(accepted(result["access_token"]), self.user)
		with patch("frappe.oauth.now_datetime", return_value=now_datetime() + timedelta(seconds=601)):
			self.assertIsNone(accepted(result["access_token"]))

	def test_helper_minutes_come_from_the_setting(self):
		for minutes, seconds in ((3, 180), (None, 600), (0, 600), (-5, 60), (100000, 3600)):
			helpers.set_settings(helper_token_minutes=minutes)
			self.assertEqual(issue_employee_token(self.employee, "helper")["expires_in"], seconds)

	def test_helper_does_not_sign_the_own_phone_out(self):
		own = issue_employee_token(self.employee, "own")
		helper = issue_employee_token(self.employee, "helper")
		self.assertEqual(accepted(own["access_token"]), self.user)
		self.assertEqual(accepted(helper["access_token"]), self.user)
		self.assertTrue(refreshable(own["refresh_token"]))
		self.assertEqual(self.active_tokens(), 2)

	def test_reissuing_own_revokes_the_previous(self):
		first = issue_employee_token(self.employee, "own")
		helper = issue_employee_token(self.employee, "helper")
		second = issue_employee_token(self.employee, "own")
		self.assertIsNone(accepted(first["access_token"]))
		self.assertFalse(refreshable(first["refresh_token"]))
		self.assertIsNone(accepted(helper["access_token"]))
		self.assertEqual(accepted(second["access_token"]), self.user)
		self.assertTrue(refreshable(second["refresh_token"]))
		self.assertEqual(self.active_tokens(), 1)

	def test_own_leaves_other_users_alone(self):
		frappe.set_user("Administrator")
		other, other_user = helpers.make_self_service_employee("Tokother", id_number="TOK90000002")
		frappe.set_user(HUB)
		theirs = issue_employee_token(other.name, "own")
		issue_employee_token(self.employee, "own")
		issue_employee_token(self.employee, "own")
		self.assertEqual(accepted(theirs["access_token"]), other_user)
		self.assertEqual(self.active_tokens(other_user), 1)

	def test_invalid_kind(self):
		for kind in ("", None, "OWN", "admin"):
			self.assertRefused(issue_employee_token(self.employee, kind), "invalid_kind")
		self.assertEqual(self.active_tokens(), 0)

	def test_unknown_employee(self):
		self.assertRefused(issue_employee_token("TOK-NO-SUCH-EMPLOYEE", "own"), "employee_not_found")
		self.assertRefused(issue_employee_token(None, "own"), "employee_not_found")

	def test_inactive_employee(self):
		for status in ("Left", "Inactive", "Suspended"):
			frappe.db.set_value("Employee", self.employee, "status", status)
			for kind in ("own", "helper"):
				self.assertRefused(issue_employee_token(self.employee, kind), "employee_inactive")
		self.assertEqual(self.active_tokens(), 0)

	def test_app_access_off(self):
		frappe.db.set_value("Employee", self.employee, "custom_app_access", 0)
		for kind in ("own", "helper"):
			self.assertRefused(issue_employee_token(self.employee, kind), "app_access_off")
		self.assertEqual(self.active_tokens(), 0)

	def test_no_linked_user(self):
		frappe.db.set_value("Employee", self.employee, "user_id", None)
		self.assertRefused(issue_employee_token(self.employee, "own"), "no_user")
		frappe.db.set_value("Employee", self.employee, "user_id", f"gone@{helpers.TEST_DOMAIN}")
		self.assertRefused(issue_employee_token(self.employee, "helper"), "no_user")

	def test_disabled_user(self):
		frappe.db.set_value("User", self.user, "enabled", 0)
		for kind in ("own", "helper"):
			self.assertRefused(issue_employee_token(self.employee, kind), "user_disabled")
		self.assertEqual(self.active_tokens(), 0)

	def test_user_with_more_than_self_service_is_refused(self):
		for role in ("Attendance Marking", "Attendance Admin", "HR Manager", "HR User", "System Manager"):
			frappe.db.savepoint("tok_role")
			frappe.set_user("Administrator")
			frappe.get_doc("User", self.user).add_roles(role)
			frappe.set_user(HUB)
			for kind in ("own", "helper"):
				self.assertRefused(issue_employee_token(self.employee, kind), "user_has_other_roles")
			frappe.db.rollback(save_point="tok_role")
			frappe.clear_cache(user=self.user)
		self.assertEqual(self.active_tokens(), 0)
		self.assertTrue(issue_employee_token(self.employee, "helper")["ok"])

	def test_a_refused_own_request_revokes_nothing(self):
		own = issue_employee_token(self.employee, "own")
		frappe.set_user("Administrator")
		frappe.get_doc("User", self.user).add_roles("Attendance Marking")
		frappe.set_user(HUB)
		self.assertRefused(issue_employee_token(self.employee, "own"), "user_has_other_roles")
		self.assertEqual(accepted(own["access_token"]), self.user)

	def test_the_hub_and_administrator_are_never_issued_a_token(self):
		frappe.db.set_value("Employee", self.employee, "user_id", HUB)
		self.assertRefused(issue_employee_token(self.employee, "own"), "user_has_other_roles")
		frappe.db.set_value("Employee", self.employee, "user_id", "Administrator")
		self.assertRefused(issue_employee_token(self.employee, "own"), "user_has_other_roles")

	def test_oauth_client_not_set(self):
		helpers.set_settings(employee_token_client=None)
		for kind in ("own", "helper"):
			self.assertRefused(issue_employee_token(self.employee, kind), "oauth_client_not_set")

	def test_oauth_client_missing(self):
		helpers.set_settings(employee_token_client="tok-no-such-client")
		for kind in ("own", "helper"):
			self.assertRefused(issue_employee_token(self.employee, kind), "oauth_client_missing")
		self.assertEqual(self.active_tokens(), 0)

	def test_a_refusal_for_the_client_revokes_nothing(self):
		own = issue_employee_token(self.employee, "own")
		helpers.set_settings(employee_token_client=None)
		self.assertRefused(issue_employee_token(self.employee, "own"), "oauth_client_not_set")
		self.assertEqual(accepted(own["access_token"]), self.user)

	def test_scopes_are_limited_to_the_client(self):
		frappe.db.set_value("OAuth Client", self.client, "scopes", "openid")
		result = issue_employee_token(self.employee, "helper")
		self.assertEqual(frappe.db.get_value("OAuth Bearer Token", result["access_token"], "scopes"), "openid")
		frappe.db.set_value("OAuth Client", self.client, "scopes", "profile")
		self.assertRefused(issue_employee_token(self.employee, "helper"), "oauth_client_no_scopes")


class TestRevokeEmployeeTokens(EmployeeAccessCase):
	FACE_SIGN_IN = 1

	def web_app_token(self):
		"""A token the web app obtained for the same user through the ERP's sign-in step."""
		return frappe.get_doc(
			{
				"doctype": "OAuth Bearer Token",
				"client": self.client,
				"user": self.user,
				"scopes": "openid all",
				"access_token": employee_access._new_token(),
				"refresh_token": employee_access._new_token(),
				"expires_in": 3600,
			}
		).insert(ignore_permissions=True)

	def test_revokes_every_token_of_the_user(self):
		own = issue_employee_token(self.employee, "own")
		helper = issue_employee_token(self.employee, "helper")
		web = self.web_app_token()
		code = frappe.get_doc(
			{
				"doctype": "OAuth Authorization Code",
				"client": self.client,
				"user": self.user,
				"scopes": "openid all",
				"authorization_code": employee_access._new_token(),
				"redirect_uri_bound_to_authorization_code": "http://localhost/callback",
				"expiration_time": now_datetime() + timedelta(minutes=10),
			}
		).insert(ignore_permissions=True)
		self.assertEqual(accepted(web.access_token), self.user)

		result = revoke_employee_tokens(self.employee)
		self.assertEqual(result, {"ok": True, "employee": self.employee, "user": self.user, "revoked": 3})
		for access_token in (own["access_token"], helper["access_token"], web.access_token):
			self.assertIsNone(accepted(access_token))
		self.assertFalse(refreshable(own["refresh_token"]))
		self.assertFalse(refreshable(web.refresh_token))
		self.assertEqual(self.active_tokens(), 0)
		# A code not yet exchanged would have become one more token.
		self.assertEqual(frappe.db.get_value("OAuth Authorization Code", code.name, "validity"), "Invalid")
		self.assertEqual(revoke_employee_tokens(self.employee)["revoked"], 0)

	def test_other_users_keep_their_tokens(self):
		frappe.set_user("Administrator")
		other, other_user = helpers.make_self_service_employee("Tokother", id_number="TOK90000002")
		frappe.set_user(HUB)
		theirs = issue_employee_token(other.name, "own")
		issue_employee_token(self.employee, "own")
		self.assertEqual(revoke_employee_tokens(self.employee)["revoked"], 1)
		self.assertEqual(accepted(theirs["access_token"]), other_user)

	def test_works_for_an_employee_who_can_no_longer_be_issued_one(self):
		issue_employee_token(self.employee, "own")
		frappe.db.set_value("Employee", self.employee, {"status": "Left", "custom_app_access": 0})
		self.assertEqual(revoke_employee_tokens(self.employee)["revoked"], 1)

	def test_unknown_employee_and_no_user(self):
		self.assertRefused(revoke_employee_tokens("TOK-NO-SUCH-EMPLOYEE"), "employee_not_found")
		frappe.db.set_value("Employee", self.employee, "user_id", None)
		self.assertEqual(
			revoke_employee_tokens(self.employee),
			{"ok": True, "employee": self.employee, "user": None, "revoked": 0},
		)


class TestFaceSignInSwitch(EmployeeAccessCase):
	def face_calls(self):
		return (
			lambda: get_employee_for_verification(ID_NUMBER),
			lambda: get_verification_photo(self.employee),
			lambda: issue_employee_token(self.employee, "own"),
			lambda: issue_employee_token(self.employee, "helper"),
		)

	def test_off_by_default_and_every_face_method_refuses(self):
		self.assertEqual(frappe.get_meta(SETTINGS_DOCTYPE).get_field("allow_face_sign_in").default, "0")
		for call in self.face_calls():
			result = call()
			self.assertRefused(result, "face_sign_in_off")
			self.assertEqual(set(result), {"ok", "reason", "message"})
		self.assertEqual(self.active_tokens(), 0)
		# The photo method answers the same way, not with an image.
		self.assertNotEqual(frappe.local.response.get("type"), "download")

	def test_the_hub_gate_comes_first(self):
		frappe.set_user(self.user)
		for call in self.face_calls():
			self.assertRaises(HubOnlyError, call)

	def test_on_the_face_methods_work_again(self):
		helpers.set_settings(allow_face_sign_in=1)
		self.assertTrue(get_employee_for_verification(ID_NUMBER)["ok"])
		self.assertRaises(frappe.DoesNotExistError, get_verification_photo, self.employee)
		self.assertTrue(issue_employee_token(self.employee, "helper")["ok"])
		helpers.set_settings(allow_face_sign_in=0)
		self.assertRefused(issue_employee_token(self.employee, "helper"), "face_sign_in_off")

	def test_revoking_does_not_depend_on_the_switch(self):
		self.assertEqual(revoke_employee_tokens(self.employee)["revoked"], 0)
