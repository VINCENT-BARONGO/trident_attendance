import json
from datetime import timedelta
from unittest.mock import patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, now_datetime, nowdate

from trident_attendance import push, push_hooks
from trident_attendance.announcements import ANNOUNCED_AT_FIELD, ANNOUNCEMENT_FIELD
from trident_attendance.push import (
	DEVICE_DOCTYPE,
	register_app_device,
	send_test_push,
	unregister_app_device,
)
from trident_attendance.push_hooks import NOTIFIED_STATUS_FIELD
from trident_attendance.tests import helpers
from trident_attendance.tests.test_announcements import make_newsletter
from trident_attendance.utils import SETTINGS_DOCTYPE

TOKEN_A = "tok-test-device-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN_B = "tok-test-device-token-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TOKEN_C = "tok-test-device-token-cccccccccccccccccccccccccccccccccccccccc"
PRIVATE_KEY_MARKER = "TOK-TEST-PRIVATE-KEY-MATERIAL"
ACCOUNT = {
	"type": "service_account",
	"project_id": "tok-test-project",
	"private_key_id": "abc123",
	"private_key": f"-----BEGIN PRIVATE KEY-----\n{PRIVATE_KEY_MARKER}\n-----END PRIVATE KEY-----\n",
	"client_email": "push@tok-test-project.iam.gserviceaccount.com",
	"token_uri": "https://oauth2.googleapis.com/token",
}
SEND_URL = "https://fcm.googleapis.com/v1/projects/tok-test-project/messages:send"
# The real HTTP call, kept before the tests replace it.
ORIGINAL_POST = push._post


class FakeResponse:
	def __init__(self, status_code, payload=None):
		self.status_code = status_code
		self.payload = payload

	def json(self):
		if self.payload is None:
			raise ValueError("no json")
		return self.payload


def fcm_error(status_code, status, error_code=None):
	details = [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": error_code}] if error_code else []
	return FakeResponse(status_code, {"error": {"code": status_code, "message": "x", "status": status, "details": details}})


def devices(user=None, **filters):
	if user:
		filters["user"] = user
	return frappe.get_all(DEVICE_DOCTYPE, filters=filters, fields=["name", "user", "token", "platform", "app_version", "enabled", "last_used", "last_error"], order_by="creation")


class PushCase(FrappeTestCase):
	"""Push on, a fake service account in the settings, the token fetch and the HTTP call to
	Firebase replaced. Two employees with self-service users."""

	PUSH_ENABLED = 1

	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		helpers.set_settings(push_enabled=self.PUSH_ENABLED, firebase_service_account=json.dumps(ACCOUNT))
		self.employee_a, self.user_a = helpers.make_self_service_employee("Tokone")
		self.employee_b, self.user_b = helpers.make_self_service_employee("Toktwo")
		self.employee_a, self.employee_b = self.employee_a.name, self.employee_b.name
		self.posted = []
		self.responses = []
		# Firebase's answer for one device token; devices are not sent to in a fixed order.
		self.responses_for = {}
		self.fetched = 0
		self.logged = []
		self.errors_logged = []
		self.start(patch("trident_attendance.push._post", self.fake_post))
		self.start(patch("trident_attendance.push._fetch_access_token", self.fake_fetch))
		self.start(patch("frappe.logger", lambda *a, **kw: self))
		self.start(patch("frappe.log_error", self.fake_log_error))
		push.forget_access_token(ACCOUNT)
		self.addCleanup(push.forget_access_token, ACCOUNT)

	def start(self, patcher):
		patcher.start()
		self.addCleanup(patcher.stop)

	def info(self, message, *args, **kwargs):
		self.logged.append(str(message))

	warning = error = exception = debug = info

	def fake_log_error(self, title=None, message=None, **kwargs):
		self.errors_logged.append(f"{title}\n{message}")

	def fake_post(self, url, bearer, payload):
		self.posted.append(frappe._dict(url=url, bearer=bearer, payload=payload))
		token = payload["message"]["token"]
		if token in self.responses_for:
			response = self.responses_for.pop(token)
		else:
			response = self.responses.pop(0) if self.responses else FakeResponse(200, {"name": "projects/x/messages/1"})
		if isinstance(response, Exception):
			raise response
		return response

	def fake_fetch(self, account):
		self.fetched += 1
		return f"bearer-{self.fetched}", 3600

	def register(self, user, token, **kwargs):
		frappe.set_user(user)
		result = register_app_device(token, **kwargs)
		frappe.set_user("Administrator")
		return result

	def assertRefused(self, result, reason):
		self.assertEqual(result.get("ok"), False, result)
		self.assertEqual(result.get("reason"), reason, result)

	def enqueued(self):
		"""Runs `notify_*` with the queue captured; answers the jobs' kwargs."""
		return EnqueueCapture()


class EnqueueCapture:
	def __enter__(self):
		self.jobs = []
		self.patcher = patch("frappe.enqueue", self.enqueue)
		self.patcher.start()
		return self

	def __exit__(self, *exc):
		self.patcher.stop()

	def enqueue(self, method, **kwargs):
		self.jobs.append(frappe._dict(method=method, **kwargs))

	def run(self):
		"""Runs the queued jobs, once each."""
		jobs, self.jobs = self.jobs, []
		for job in jobs:
			push._send_to_devices(job.devices, job.title, job.body, job.url, job.kind)


class TestDevices(PushCase):
	def test_register_inserts(self):
		self.assertEqual(self.register(self.user_a, TOKEN_A), {"ok": True})
		rows = devices(token=TOKEN_A)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual((row.user, row.platform, row.enabled, row.app_version), (self.user_a, "android", 1, None))
		self.assertTrue(row.last_used)
		self.assertIsNone(row.last_error)

	def test_register_updates_the_same_token(self):
		self.register(self.user_a, TOKEN_A)
		frappe.db.set_value(DEVICE_DOCTYPE, {"token": TOKEN_A}, {"enabled": 0, "last_error": "UNREGISTERED"})
		self.assertEqual(self.register(self.user_a, f"  {TOKEN_A}  ", platform="ios", app_version="3.14.0 (50)"), {"ok": True})
		rows = devices(token=TOKEN_A)
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].platform, rows[0].app_version, rows[0].enabled, rows[0].last_error), ("ios", "3.14.0 (50)", 1, None))

	def test_a_token_moves_to_the_caller(self):
		self.register(self.user_a, TOKEN_A)
		self.register(self.user_b, TOKEN_A)
		rows = devices(token=TOKEN_A)
		self.assertEqual([r.user for r in rows], [self.user_b])
		self.assertEqual(devices(user=self.user_a), [])

	def test_a_user_may_hold_several_devices(self):
		self.register(self.user_a, TOKEN_A)
		self.register(self.user_a, TOKEN_B, platform="ios")
		self.assertEqual(sorted(r.token for r in devices(user=self.user_a)), sorted([TOKEN_A, TOKEN_B]))

	def test_unregister_only_removes_the_callers_row(self):
		self.register(self.user_a, TOKEN_A)
		frappe.set_user(self.user_b)
		self.assertEqual(unregister_app_device(TOKEN_A), {"ok": True})
		frappe.set_user("Administrator")
		self.assertEqual(len(devices(token=TOKEN_A)), 1)
		frappe.set_user(self.user_a)
		self.assertEqual(unregister_app_device(TOKEN_A), {"ok": True})
		self.assertEqual(unregister_app_device(TOKEN_A), {"ok": True})
		frappe.set_user("Administrator")
		self.assertEqual(devices(token=TOKEN_A), [])

	def test_bad_token_length(self):
		frappe.set_user(self.user_a)
		for token in ("", None, "x" * 19, "x" * 513, "   " + "x" * 10):
			self.assertRefused(register_app_device(token), "token_invalid")
			self.assertRefused(unregister_app_device(token), "token_invalid")
		self.assertEqual(register_app_device("x" * 20), {"ok": True})
		self.assertEqual(register_app_device("y" * 512), {"ok": True})
		self.assertRefused(register_app_device(TOKEN_A, platform="windows"), "invalid_platform")
		frappe.set_user("Administrator")
		self.assertEqual(len(devices(user=self.user_a)), 2)

	def test_guest_is_refused(self):
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, register_app_device, TOKEN_A)
		self.assertRaises(frappe.PermissionError, unregister_app_device, TOKEN_A)
		self.assertRaises(frappe.PermissionError, send_test_push)
		frappe.set_user("Administrator")
		self.assertEqual(devices(token=TOKEN_A), [])

	def test_methods_are_post_only_and_not_for_guests(self):
		for method in (register_app_device, unregister_app_device, send_test_push):
			self.assertIn(method, frappe.whitelisted)
			self.assertNotIn(method, frappe.guest_methods)
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"])

	def test_only_a_system_manager_reads_the_rows(self):
		self.register(self.user_a, TOKEN_A)
		frappe.set_user(self.user_a)
		self.assertFalse(frappe.has_permission(DEVICE_DOCTYPE, "read"))
		self.assertFalse(frappe.has_permission(DEVICE_DOCTYPE, "write"))
		frappe.set_user("Administrator")
		self.assertEqual([p.role for p in frappe.get_meta(DEVICE_DOCTYPE).permissions], ["System Manager"])

	def test_the_token_is_not_logged(self):
		self.register(self.user_a, TOKEN_A)
		frappe.set_user(self.user_a)
		unregister_app_device(TOKEN_A)
		self.assertTrue(self.logged)
		for line in self.logged:
			self.assertNotIn(TOKEN_A, line)
			self.assertNotIn(TOKEN_A[:20], line)


class TestSender(PushCase):
	def test_message_shape(self):
		self.assertEqual(
			push.message("device-token", "New announcement", "Office closed Friday", "/ess/announcements/NL-0007", "announcement"),
			{
				"message": {
					"token": "device-token",
					"notification": {"title": "New announcement", "body": "Office closed Friday"},
					"data": {"url": "/ess/announcements/NL-0007", "kind": "announcement"},
					"android": {"priority": "HIGH", "notification": {"channel_id": "trident_employee"}},
				}
			},
		)

	def test_the_http_call(self):
		"""The request itself: Firebase's v1 URL, the bearer, JSON, a 10 s timeout."""
		with patch("trident_attendance.push.requests.post", return_value=FakeResponse(200, {})) as post:
			response = ORIGINAL_POST(SEND_URL, "bearer-x", {"message": {}})
		self.assertEqual(response.status_code, 200)
		post.assert_called_once()
		args, kwargs = post.call_args
		self.assertEqual(args[0], SEND_URL)
		self.assertEqual(kwargs["json"], {"message": {}})
		self.assertEqual(kwargs["timeout"], 10)
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer bearer-x")

	def test_one_request_per_enabled_device(self):
		self.register(self.user_a, TOKEN_A)
		self.register(self.user_a, TOKEN_B)
		self.register(self.user_b, TOKEN_C)
		frappe.db.set_value(DEVICE_DOCTYPE, {"token": TOKEN_C}, "enabled", 0)
		with self.enqueued() as queue:
			self.assertEqual(push.notify_all("T", "B", "/ess/", "test"), 2)
			self.assertEqual(len(queue.jobs), 1)
			job = queue.jobs[0]
			self.assertEqual(job.method, "trident_attendance.push._send_to_devices")
			self.assertEqual((job.queue, job.enqueue_after_commit, job.title, job.body, job.url, job.kind), ("short", True, "T", "B", "/ess/", "test"))
			self.assertEqual(sorted(job.devices), sorted(r.name for r in devices(enabled=1)))
			queue.run()
		self.assertEqual(sorted(p.payload["message"]["token"] for p in self.posted), sorted([TOKEN_A, TOKEN_B]))
		for p in self.posted:
			self.assertEqual(p.url, SEND_URL)
			self.assertEqual(p.bearer, "bearer-1")
			self.assertEqual(p.payload["message"]["notification"], {"title": "T", "body": "B"})
			self.assertEqual(p.payload["message"]["data"], {"url": "/ess/", "kind": "test"})
		self.assertEqual(self.fetched, 1)
		self.assertIn("Push test: 2 device(s), 2 sent, 0 failed, 0 disabled", self.logged)

	def test_notify_user_picks_that_users_devices(self):
		self.register(self.user_a, TOKEN_A)
		self.register(self.user_b, TOKEN_B)
		with self.enqueued() as queue:
			self.assertEqual(push.notify_user(self.user_a, "T", "B", "/ess/leave", "leave"), 1)
			self.assertEqual(push.notify_user(f"nobody@{helpers.TEST_DOMAIN}", "T", "B", "/ess/leave", "leave"), 0)
			self.assertEqual(push.notify_user(None, "T", "B", "/ess/leave", "leave"), 0)
			self.assertEqual(push.notify_user("Guest", "T", "B", "/ess/leave", "leave"), 0)
			self.assertEqual(len(queue.jobs), 1)
			queue.run()
		self.assertEqual([p.payload["message"]["token"] for p in self.posted], [TOKEN_A])

	def test_unregistered_disables_the_device(self):
		self.register(self.user_a, TOKEN_A)
		self.register(self.user_a, TOKEN_B)
		self.responses_for = {TOKEN_A: fcm_error(404, "NOT_FOUND", "UNREGISTERED")}
		with self.enqueued() as queue:
			push.notify_user(self.user_a, "T", "B", "/ess/", "test")
			queue.run()
		gone, fine = (devices(token=t)[0] for t in (TOKEN_A, TOKEN_B))
		self.assertEqual(gone.enabled, 0)
		self.assertIn("UNREGISTERED", gone.last_error)
		self.assertEqual((fine.enabled, fine.last_error), (1, None))
		self.assertIn("Push test: 2 device(s), 1 sent, 1 failed, 1 disabled", self.logged)
		# Firebase without the detail: the status alone says the same.
		self.responses = [fcm_error(404, "NOT_FOUND")]
		with self.enqueued() as queue:
			push.notify_user(self.user_a, "T", "B", "/ess/", "test")
			queue.run()
		self.assertEqual(devices(token=TOKEN_B)[0].enabled, 0)
		self.assertIn("NOT_FOUND", devices(token=TOKEN_B)[0].last_error)

	def test_other_errors_keep_the_device(self):
		self.register(self.user_a, TOKEN_A)
		for response, expected in (
			(fcm_error(400, "INVALID_ARGUMENT", "INVALID_ARGUMENT"), "INVALID_ARGUMENT"),
			(fcm_error(503, "UNAVAILABLE"), "UNAVAILABLE"),
			(FakeResponse(500), "HTTP 500"),
			(requests.ConnectTimeout("https://fcm.googleapis.com/secret"), "ConnectTimeout"),
		):
			self.responses = [response]
			with self.enqueued() as queue:
				push.notify_user(self.user_a, "T", "B", "/ess/", "test")
				queue.run()
			row = devices(token=TOKEN_A)[0]
			self.assertEqual(row.enabled, 1, expected)
			self.assertTrue(row.last_error.endswith(expected), (row.last_error, expected))
		self.assertIn("Push test: 1 device(s), 0 sent, 1 failed, 0 disabled", self.logged)

	def test_a_disabled_device_is_skipped_at_send_time(self):
		self.register(self.user_a, TOKEN_A)
		with self.enqueued() as queue:
			push.notify_user(self.user_a, "T", "B", "/ess/", "test")
			frappe.db.set_value(DEVICE_DOCTYPE, {"token": TOKEN_A}, "enabled", 0)
			queue.run()
		self.assertEqual(self.posted, [])
		self.assertEqual(self.fetched, 0)

	def test_the_bearer_is_cached_until_near_expiry(self):
		self.assertEqual(push.access_token(ACCOUNT), "bearer-1")
		self.assertEqual(push.access_token(ACCOUNT), "bearer-1")
		self.assertEqual(self.fetched, 1)
		push.forget_access_token(ACCOUNT)
		self.assertEqual(push.access_token(ACCOUNT), "bearer-2")
		# Another key for the project does not inherit this key's bearer.
		other = {**ACCOUNT, "private_key_id": "def456"}
		self.addCleanup(push.forget_access_token, other)
		self.assertEqual(push.access_token(other), "bearer-3")
		self.assertEqual(push.access_token(ACCOUNT), "bearer-2")
		# A short-lived one is not kept at all.
		push.forget_access_token(ACCOUNT)
		with patch("trident_attendance.push._fetch_access_token", return_value=("short", push.ACCESS_TOKEN_MARGIN - 1)):
			self.assertEqual(push.access_token(ACCOUNT), "short")
			self.assertEqual(push.access_token(ACCOUNT), "short")
		self.assertEqual(push.access_token(ACCOUNT), "bearer-4")
		with patch("frappe.utils.redis_wrapper.RedisWrapper.set_value") as set_value:
			push.forget_access_token(ACCOUNT)
			push.access_token(ACCOUNT)
			self.assertEqual(set_value.call_args.kwargs["expires_in_sec"], 3600 - push.ACCESS_TOKEN_MARGIN)

	def test_a_refused_bearer_is_dropped(self):
		self.register(self.user_a, TOKEN_A)
		self.responses = [fcm_error(401, "UNAUTHENTICATED")]
		with self.enqueued() as queue:
			push.notify_user(self.user_a, "T", "B", "/ess/", "test")
			queue.run()
			push.notify_user(self.user_a, "T", "B", "/ess/", "test")
			queue.run()
		self.assertEqual([p.bearer for p in self.posted], ["bearer-1", "bearer-2"])
		self.assertEqual(devices(token=TOKEN_A)[0].enabled, 1)

	def test_push_off_sends_nothing(self):
		self.register(self.user_a, TOKEN_A)
		helpers.set_settings(push_enabled=0)
		with self.enqueued() as queue:
			self.assertEqual(push.notify_user(self.user_a, "T", "B", "/ess/", "test"), 0)
			self.assertEqual(push.notify_all("T", "B", "/ess/", "test"), 0)
			self.assertEqual(queue.jobs, [])
		self.assertEqual(self.posted, [])
		self.assertEqual(len(devices(token=TOKEN_A)), 1)

	def test_no_service_account_fails_the_job_quietly(self):
		self.register(self.user_a, TOKEN_A)
		helpers.set_settings(firebase_service_account=None)
		with self.enqueued() as queue:
			push.notify_user(self.user_a, "T", "B", "/ess/", "test")
			queue.run()
		self.assertEqual(self.posted, [])
		self.assertIn("Push test not sent: no Firebase service account", self.logged)
		self.assertEqual(devices(token=TOKEN_A)[0].enabled, 1)

	def test_an_unknown_kind_is_a_programming_error(self):
		self.assertRaises(ValueError, push.notify_all, "T", "B", "/ess/", "newsletter")

	def test_nothing_sensitive_reaches_the_logs(self):
		self.register(self.user_a, TOKEN_A)
		body = "Salary paid: KES 123,456"
		with self.enqueued() as queue:
			push.notify_user(self.user_a, "Payslip ready", body, "/ess/payslips/x", "payslip")
			queue.run()
			# A failure inside the job: logged as its type and place, never with the key.
			with patch("trident_attendance.push._send_one", side_effect=RuntimeError(f"boom {PRIVATE_KEY_MARKER} {TOKEN_A}")):
				push.notify_user(self.user_a, "Payslip ready", body, "/ess/payslips/x", "payslip")
				queue.run()
		self.assertEqual(len(self.errors_logged), 1)
		self.assertIn("trident_attendance: push payslip failed\nRuntimeError", self.errors_logged[0])
		for text in self.logged + self.errors_logged:
			self.assertNotIn(TOKEN_A, text)
			self.assertNotIn(PRIVATE_KEY_MARKER, text)
			self.assertNotIn("123,456", text)
			self.assertNotIn("bearer-", text)

	def test_a_broken_job_never_raises(self):
		self.register(self.user_a, TOKEN_A)
		with patch("trident_attendance.push._fetch_access_token", side_effect=OSError("no network")):
			push._send_to_devices([devices()[0].name], "T", "B", "/ess/", "test")
		self.assertEqual(len(self.errors_logged), 1)
		self.assertIn("OSError", self.errors_logged[0])


class TestAccessTokenFetch(FrappeTestCase):
	def test_google_auth_signs_the_jwt_and_reads_the_bearer(self):
		"""The real exchange down to the transport, which is replaced: no request leaves."""
		from cryptography.hazmat.primitives import serialization
		from cryptography.hazmat.primitives.asymmetric import rsa

		pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
			serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
		)
		account = {**ACCOUNT, "private_key": pem.decode()}
		calls = []

		class FakeTransportResponse:
			status = 200
			headers = {}
			data = json.dumps({"access_token": "ya29.tok-test", "expires_in": 3599, "token_type": "Bearer"}).encode()

		def fake_request(method=None, url=None, headers=None, body=None, **kwargs):
			calls.append(frappe._dict(method=method, url=url, body=body))
			return FakeTransportResponse()

		with patch("google.auth.transport.requests.Request", return_value=fake_request):
			bearer, expires_in = push._fetch_access_token(account)
		self.assertEqual(bearer, "ya29.tok-test")
		self.assertTrue(3500 <= expires_in <= 3600, expires_in)
		self.assertEqual(len(calls), 1)
		self.assertEqual((calls[0].method, calls[0].url), ("POST", ACCOUNT["token_uri"]))
		body = calls[0].body.decode() if isinstance(calls[0].body, bytes) else str(calls[0].body)
		self.assertIn("urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer", body)
		self.assertIn("assertion=", body)
		self.assertNotIn(PRIVATE_KEY_MARKER, body)


class TestServiceAccount(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		self.had_conf = "fcm_service_account" in frappe.conf
		self.addCleanup(lambda: frappe.conf.pop("fcm_service_account", None) if not self.had_conf else None)

	def test_from_the_settings(self):
		helpers.set_settings(firebase_service_account=json.dumps(ACCOUNT))
		self.assertEqual(push.service_account(), ACCOUNT)

	def test_from_the_settings_through_the_form(self):
		"""Saved on the form the field is encrypted and the row shows stars; it still reads back."""
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.firebase_service_account = json.dumps(ACCOUNT)
		settings.save()
		self.assertTrue(set(frappe.db.get_single_value(SETTINGS_DOCTYPE, "firebase_service_account")) <= {"*"})
		frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
		self.assertEqual(push.service_account(), ACCOUNT)

	def test_site_config_stands_in(self):
		helpers.set_settings(firebase_service_account=None)
		frappe.conf["fcm_service_account"] = json.dumps(ACCOUNT)
		self.assertEqual(push.service_account(), ACCOUNT)
		frappe.conf["fcm_service_account"] = dict(ACCOUNT)
		self.assertEqual(push.service_account(), ACCOUNT)
		helpers.set_settings(firebase_service_account=json.dumps({**ACCOUNT, "project_id": "from-settings"}))
		self.assertEqual(push.service_account()["project_id"], "from-settings")

	def test_nothing_usable(self):
		helpers.set_settings(firebase_service_account=None)
		frappe.conf.pop("fcm_service_account", None)
		self.assertIsNone(push.service_account())
		for bad in ("not json", "[1, 2]", json.dumps({"project_id": "x"}), json.dumps({**ACCOUNT, "private_key": ""})):
			helpers.set_settings(firebase_service_account=bad)
			self.assertIsNone(push.service_account(), bad)

	def test_only_a_system_manager_changes_the_key(self):
		hr = helpers.make_user(f"hr@{helpers.TEST_DOMAIN}", roles=["HR Manager", "Attendance Admin"]).name
		frappe.set_user(hr)
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.firebase_service_account = json.dumps(ACCOUNT)
		self.assertRaises(frappe.PermissionError, settings.save)
		# The switch is theirs.
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.push_enabled = 1
		settings.save()
		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, "push_enabled"), 1)
		frappe.set_user("Administrator")
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.firebase_service_account = json.dumps(ACCOUNT)
		settings.save()
		frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
		self.assertEqual(push.service_account(), ACCOUNT)
		# Saving the page without touching the key keeps it, for anyone.
		frappe.set_user(hr)
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.photo_retention_days = 31
		settings.save()
		frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
		self.assertEqual(push.service_account(), ACCOUNT)


class TestSendTestPush(PushCase):
	def test_to_the_callers_devices(self):
		self.register(self.user_a, TOKEN_A)
		self.register(self.user_b, TOKEN_B)
		frappe.set_user(self.user_a)
		with self.enqueued() as queue:
			self.assertEqual(send_test_push(), {"ok": True, "devices": 1})
			job = queue.jobs[0]
			self.assertEqual((job.title, job.url, job.kind), ("Test notification", "/ess/", "test"))
			self.assertTrue(job.body)
			queue.run()
		self.assertEqual([p.payload["message"]["token"] for p in self.posted], [TOKEN_A])
		self.assertEqual(self.posted[0].payload["message"]["data"], {"url": "/ess/", "kind": "test"})

	def test_without_a_device(self):
		frappe.set_user(self.user_a)
		with self.enqueued() as queue:
			self.assertEqual(send_test_push(), {"ok": True, "devices": 0})
			self.assertEqual(queue.jobs, [])

	def test_refusals(self):
		self.register(self.user_a, TOKEN_A)
		helpers.set_settings(push_enabled=0)
		frappe.set_user(self.user_a)
		self.assertRefused(send_test_push(), "push_disabled")
		frappe.set_user("Administrator")
		helpers.set_settings(push_enabled=1, firebase_service_account=None)
		frappe.set_user(self.user_a)
		self.assertRefused(send_test_push(), "no_service_account")
		self.assertEqual(self.posted, [])

	def test_an_unexpected_error_is_a_500_without_the_key(self):
		frappe.set_user(self.user_a)
		with patch("trident_attendance.push.service_account", side_effect=RuntimeError(f"boom {PRIVATE_KEY_MARKER}")):
			self.assertRefused(send_test_push(), "server_error")
		self.assertEqual(frappe.local.response.http_status_code, 500)
		self.assertEqual(len(self.errors_logged), 1)
		self.assertIn("RuntimeError", self.errors_logged[0])
		self.assertNotIn(PRIVATE_KEY_MARKER, self.errors_logged[0])


class TriggerCase(PushCase):
	"""Devices for both employees; the queue captured so each trigger's jobs can be read."""

	def setUp(self):
		super().setUp()
		self.register(self.user_a, TOKEN_A)
		self.register(self.user_b, TOKEN_B)
		self.queue = EnqueueCapture()
		self.queue.__enter__()
		self.addCleanup(self.queue.__exit__, None, None, None)
		self.mute = frappe.flags.mute_emails
		frappe.flags.mute_emails = True
		self.addCleanup(setattr, frappe.flags, "mute_emails", self.mute)

	def jobs(self):
		jobs, self.queue.jobs = self.queue.jobs, []
		return jobs


class TestNewsletterTrigger(TriggerCase):
	def test_ticked_on_insert(self):
		doc = make_newsletter("Tok Test ticked")
		jobs = self.jobs()
		self.assertEqual(len(jobs), 1)
		job = jobs[0]
		self.assertEqual((job.title, job.body, job.url, job.kind), ("New announcement", "Tok Test ticked", f"/ess/announcements/{doc.name}", "announcement"))
		self.assertEqual(sorted(job.devices), sorted(r.name for r in devices()))
		self.assertTrue(frappe.db.get_value("Newsletter", doc.name, ANNOUNCED_AT_FIELD))

	def test_ticked_later(self):
		doc = make_newsletter("Tok Test later", app=0)
		self.assertEqual(self.jobs(), [])
		self.assertFalse(frappe.db.get_value("Newsletter", doc.name, ANNOUNCED_AT_FIELD))
		doc.set(ANNOUNCEMENT_FIELD, 1)
		doc.save()
		self.assertEqual(len(self.jobs()), 1)
		self.assertTrue(frappe.db.get_value("Newsletter", doc.name, ANNOUNCED_AT_FIELD))

	def test_never_twice(self):
		doc = make_newsletter("Tok Test once")
		self.assertEqual(len(self.jobs()), 1)
		doc.reload()
		doc.subject = "Tok Test once, edited"
		doc.save()
		self.assertEqual(self.jobs(), [])
		doc.set(ANNOUNCEMENT_FIELD, 0)
		doc.save()
		doc.set(ANNOUNCEMENT_FIELD, 1)
		doc.save()
		self.assertEqual(self.jobs(), [])

	def test_an_unrelated_newsletter_sends_nothing(self):
		make_newsletter("Tok Test plain", app=0)
		self.assertEqual(self.jobs(), [])

	def test_a_push_failure_does_not_block_the_save(self):
		with patch("trident_attendance.push.notify_all", side_effect=RuntimeError("queue down")):
			doc = make_newsletter("Tok Test despite")
		self.assertTrue(frappe.db.exists("Newsletter", doc.name))
		self.assertEqual(len(self.errors_logged), 1)
		self.assertIn("trident_attendance: push on Newsletter failed\nRuntimeError", self.errors_logged[0])

	def test_the_hooks_are_wired(self):
		events = frappe.get_hooks("doc_events")
		self.assertIn("trident_attendance.push_hooks.newsletter_on_update", events["Newsletter"]["on_update"])
		for event in ("on_update", "on_submit", "on_update_after_submit"):
			self.assertIn("trident_attendance.push_hooks.leave_application_status", events["Leave Application"][event])
		self.assertIn("trident_attendance.push_hooks.salary_slip_on_submit", events["Salary Slip"]["on_submit"])


class TestLeaveTrigger(TriggerCase):
	def setUp(self):
		super().setUp()
		self.leave_type = frappe.get_doc(
			{"doctype": "Leave Type", "leave_type_name": "Tok Test Leave", "is_lwp": 1, "allow_negative": 1}
		).insert(ignore_permissions=True).name
		year = getdate(nowdate()).year
		self.holidays = frappe.get_doc(
			{"doctype": "Holiday List", "holiday_list_name": "Tok Test Holidays", "from_date": f"{year}-01-01", "to_date": f"{year + 1}-12-31"}
		).insert(ignore_permissions=True).name
		for employee in (self.employee_a, self.employee_b):
			frappe.db.set_value("Employee", employee, "holiday_list", self.holidays)
		self.employee_c = helpers.make_employee("Tokthree", holiday_list=self.holidays).name
		frappe.db.set_value("Employee", self.employee_c, "user_id", None)
		self.from_date, self.to_date = add_days(nowdate(), 30), add_days(nowdate(), 31)

	def leave(self, employee, status="Open"):
		doc = frappe.get_doc(
			{
				"doctype": "Leave Application",
				"naming_series": "HR-LAP-.YYYY.-",
				"employee": employee,
				"company": frappe.db.get_value("Employee", employee, "company"),
				"leave_type": self.leave_type,
				"from_date": self.from_date,
				"to_date": self.to_date,
				"posting_date": nowdate(),
				"status": status,
				"leave_approver": "Administrator",
				"description": "Tok Test",
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc

	def assertLeaveJob(self, jobs, title, user):
		self.assertEqual(len(jobs), 1, jobs)
		job = jobs[0]
		self.assertEqual((job.title, job.url, job.kind), (title, "/ess/leave", "leave"))
		self.assertIn(self.leave_type, job.body)
		self.assertIn(" to ", job.body)
		self.assertEqual(job.devices, [r.name for r in devices(user=user)])

	def test_approved_on_save_then_submitted(self):
		doc = self.leave(self.employee_a)
		self.assertEqual(self.jobs(), [])
		doc.status = "Approved"
		doc.save()
		self.assertLeaveJob(self.jobs(), "Leave approved", self.user_a)
		self.assertEqual(frappe.db.get_value("Leave Application", doc.name, NOTIFIED_STATUS_FIELD), "Approved")
		doc.description = "Tok Test, edited"
		doc.save()
		self.assertEqual(self.jobs(), [])
		doc.submit()
		self.assertEqual(self.jobs(), [])

	def test_approved_at_submit(self):
		doc = self.leave(self.employee_a)
		doc.status = "Approved"
		doc.submit()
		self.assertLeaveJob(self.jobs(), "Leave approved", self.user_a)
		self.assertEqual(frappe.db.get_value("Leave Application", doc.name, NOTIFIED_STATUS_FIELD), "Approved")

	def test_rejected_after_submit(self):
		doc = self.leave(self.employee_a)
		doc.status = "Approved"
		doc.submit()
		self.jobs()
		doc.reload()
		# Status is not "allow on submit" out of the box; a site that decides after submitting
		# does it through a workflow, which saves the way this flag does.
		doc.status = "Rejected"
		doc.flags.ignore_validate_update_after_submit = True
		doc.save()
		self.assertLeaveJob(self.jobs(), "Leave rejected", self.user_a)
		self.assertEqual(frappe.db.get_value("Leave Application", doc.name, NOTIFIED_STATUS_FIELD), "Rejected")
		doc.reload()
		doc.save()
		self.assertEqual(self.jobs(), [])

	def test_rejected_on_save(self):
		doc = self.leave(self.employee_b)
		doc.status = "Rejected"
		doc.save()
		self.assertLeaveJob(self.jobs(), "Leave rejected", self.user_b)

	def test_open_and_cancelled_send_nothing(self):
		doc = self.leave(self.employee_a)
		doc.description = "Tok Test, still open"
		doc.save()
		doc.status = "Cancelled"
		doc.save()
		self.assertEqual(self.jobs(), [])
		self.assertFalse(frappe.db.get_value("Leave Application", doc.name, NOTIFIED_STATUS_FIELD))

	def test_an_employee_without_a_user_gets_nothing(self):
		doc = self.leave(self.employee_c)
		doc.status = "Approved"
		doc.save()
		self.assertEqual(self.jobs(), [])
		self.assertEqual(frappe.db.get_value("Leave Application", doc.name, NOTIFIED_STATUS_FIELD), "Approved")

	def test_a_push_failure_does_not_block_the_approval(self):
		doc = self.leave(self.employee_a)
		doc.status = "Approved"
		with patch("trident_attendance.push.notify_user", side_effect=RuntimeError("queue down")):
			doc.submit()
		self.assertEqual(frappe.db.get_value("Leave Application", doc.name, ["docstatus", "status"]), (1, "Approved"))
		# Submitting a leave also runs the attendance hooks, which log errors of their own here.
		ours =[e for e in self.errors_logged if e.startswith("trident_attendance: push on Leave Application failed")]
		self.assertEqual(len(ours), 1)
		self.assertIn("RuntimeError", ours[0])


class TestSalarySlipTrigger(TriggerCase):
	"""A real Salary Slip needs a payroll set-up; the hook is handed the document a submit
	would hand it."""

	def slip(self, employee, name="Sal Slip/HR-EMP-00001/00001"):
		doc = frappe.get_doc({"doctype": "Salary Slip", "employee": employee, "start_date": "2026-09-01", "end_date": "2026-09-30"})
		doc.name = name
		return doc

	def test_payslip_ready(self):
		push_hooks.salary_slip_on_submit(self.slip(self.employee_a), "on_submit")
		jobs = self.jobs()
		self.assertEqual(len(jobs), 1)
		job = jobs[0]
		self.assertEqual((job.title, job.body, job.kind), ("Payslip ready", "September 2026", "payslip"))
		self.assertEqual(job.url, "/ess/payslips/Sal%20Slip%2FHR-EMP-00001%2F00001")
		self.assertEqual(job.devices, [r.name for r in devices(user=self.user_a)])

	def test_an_employee_without_a_user_gets_nothing(self):
		employee = helpers.make_employee("Tokthree").name
		frappe.db.set_value("Employee", employee, "user_id", None)
		push_hooks.salary_slip_on_submit(self.slip(employee), "on_submit")
		self.assertEqual(self.jobs(), [])

	def test_a_push_failure_is_logged_not_raised(self):
		with patch("trident_attendance.push.notify_user", side_effect=RuntimeError("queue down")):
			push_hooks.salary_slip_on_submit(self.slip(self.employee_a), "on_submit")
		self.assertEqual(len(self.errors_logged), 1)
		self.assertIn("trident_attendance: push on Salary Slip failed\nRuntimeError", self.errors_logged[0])


class TestWaitingForSomeone(TriggerCase):
	"""The ERP's own notifications, workflow steps and new leave requests reach the phone of
	the person they wait for."""

	def bell(self, **fields):
		doc = frappe._dict(doctype="Notification Log", type="Assignment", for_user=self.user_a, from_user="Administrator", subject="<b>Admin</b> assigned a task &amp; a form to you", **fields)
		push_hooks.notification_log_after_insert(doc, "after_insert")
		return self.jobs()

	def test_a_bell_notification_goes_to_its_user(self):
		jobs = self.bell()
		self.assertEqual(len(jobs), 1)
		job = jobs[0]
		self.assertEqual((job.title, job.body, job.url, job.kind), ("Assigned to you", "Admin assigned a task & a form to you", "/ess/", "erp"))
		self.assertEqual(job.devices, [r.name for r in devices(user=self.user_a)])

	def test_each_kind_has_its_title(self):
		for kind, title in (("Mention", "You were mentioned"), ("Share", "Shared with you"), ("Alert", "ERP notification"), ("", "ERP notification")):
			doc = frappe._dict(doctype="Notification Log", type=kind, for_user=self.user_b, from_user="Administrator", subject="Something")
			push_hooks.notification_log_after_insert(doc, "after_insert")
			jobs = self.jobs()
			self.assertEqual([j.title for j in jobs], [title], kind)

	def test_energy_points_and_own_actions_send_nothing(self):
		doc = frappe._dict(doctype="Notification Log", type="Energy Point", for_user=self.user_a, from_user="Administrator", subject="You gained 5 points")
		push_hooks.notification_log_after_insert(doc, "after_insert")
		self.assertEqual(self.jobs(), [])
		doc = frappe._dict(doctype="Notification Log", type="Assignment", for_user=self.user_a, from_user=self.user_a, subject="You assigned yourself")
		push_hooks.notification_log_after_insert(doc, "after_insert")
		self.assertEqual(self.jobs(), [])

	def test_a_long_subject_is_cut(self):
		doc = frappe._dict(doctype="Notification Log", type="Alert", for_user=self.user_a, from_user=None, subject="word " * 100)
		push_hooks.notification_log_after_insert(doc, "after_insert")
		body = self.jobs()[0].body
		self.assertLessEqual(len(body), push_hooks.BODY_LENGTH)
		self.assertNotIn("<", body)

	def test_a_real_notification_log_fires_the_hook(self):
		frappe.get_doc({"doctype": "Notification Log", "type": "Alert", "for_user": self.user_a, "from_user": "Administrator", "subject": "Tok Test alert", "document_type": "Employee", "document_name": self.employee_a}).insert(ignore_permissions=True)
		jobs = self.jobs()
		self.assertEqual([(j.title, j.body, j.kind) for j in jobs], [("ERP notification", "Tok Test alert", "erp")])

	def test_a_new_leave_request_goes_to_the_approver(self):
		doc = frappe._dict(doctype="Leave Application", status="Open", leave_approver=self.user_b, employee=self.employee_a, employee_name="Tokone Test", from_date=nowdate(), to_date=nowdate(), leave_type="Annual Leave")
		push_hooks.leave_application_after_insert(doc, "after_insert")
		jobs = self.jobs()
		self.assertEqual(len(jobs), 1)
		job = jobs[0]
		self.assertEqual((job.title, job.url, job.kind), ("Leave request", "/ess/team", "leave"))
		self.assertTrue(job.body.startswith("Tokone Test: "))
		self.assertIn("Annual Leave", job.body)
		self.assertEqual(job.devices, [r.name for r in devices(user=self.user_b)])

	def test_no_leave_request_push_without_an_approver_or_to_oneself(self):
		base = dict(doctype="Leave Application", status="Open", employee=self.employee_a, employee_name="Tokone Test", from_date=nowdate(), to_date=nowdate(), leave_type="Annual Leave")
		push_hooks.leave_application_after_insert(frappe._dict(leave_approver=None, **base), "after_insert")
		push_hooks.leave_application_after_insert(frappe._dict(leave_approver=self.user_a, **base), "after_insert")
		push_hooks.leave_application_after_insert(frappe._dict(leave_approver=self.user_b, **{**base, "status": "Approved"}), "after_insert")
		self.assertEqual(self.jobs(), [])

	def test_a_workflow_step_goes_to_those_who_may_act_and_have_the_app(self):
		role = "Employee Self Service"
		doc = frappe._dict(doctype="Workflow Action", status="Open", user=None, permitted_roles=[frappe._dict(role=role)], reference_doctype="Purchase Order", reference_name="PO-0001", workflow_state="Pending Approval")
		push_hooks.workflow_action_after_insert(doc, "after_insert")
		jobs = self.jobs()
		self.assertEqual(sorted(d for j in jobs for d in j.devices), sorted(r.name for r in devices()))
		self.assertEqual({(j.title, j.body, j.kind) for j in jobs}, {("Waiting for your approval", "Purchase Order PO-0001: Pending Approval", "erp")})

	def test_a_closed_workflow_step_or_an_unheld_role_sends_nothing(self):
		doc = frappe._dict(doctype="Workflow Action", status="Completed", user=None, permitted_roles=[frappe._dict(role="Employee Self Service")], reference_doctype="Purchase Order", reference_name="PO-0001", workflow_state="Approved")
		push_hooks.workflow_action_after_insert(doc, "after_insert")
		doc = frappe._dict(doctype="Workflow Action", status="Open", user=None, permitted_roles=[frappe._dict(role="Tok Test Role Nobody Holds")], reference_doctype="Purchase Order", reference_name="PO-0001", workflow_state="Pending")
		push_hooks.workflow_action_after_insert(doc, "after_insert")
		self.assertEqual(self.jobs(), [])

	def test_a_failure_never_blocks_the_erp(self):
		with patch("trident_attendance.push.notify_user", side_effect=RuntimeError("queue down")):
			self.bell()
		self.assertEqual(len(self.errors_logged), 1)
		self.assertIn("trident_attendance: push on Notification Log failed", self.errors_logged[0])
