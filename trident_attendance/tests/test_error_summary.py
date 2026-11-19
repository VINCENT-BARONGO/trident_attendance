"""IT Help's error watch reads ERPNext's Error Log through trident_attendance.error_summary: grouped by
kind, newest message kept, only since the time asked, and only for System Manager or the Agent role."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from trident_attendance import error_summary as es
from trident_attendance.tests import helpers

TAG = "trident-test-error-summary"


class TestErrorSummary(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		# Cleanups run last-first: the role (if this test made it) goes after the test users are removed.
		if not frappe.db.exists("Role", "Agent"):  # Helpdesk's role; the test site may not have Helpdesk
			frappe.get_doc({"doctype": "Role", "role_name": "Agent"}).insert(ignore_permissions=True)
			self.addCleanup(lambda: frappe.delete_doc("Role", "Agent", force=True, ignore_permissions=True))
		self.addCleanup(helpers.reset)
		self.addCleanup(lambda: frappe.db.delete("Error Log", {"method": ["like", f"{TAG}%"]}))
		self.agent = helpers.make_user(f"itlogin@{helpers.TEST_DOMAIN}", roles=("Agent",)).name
		self.nobody = helpers.make_user(f"nobody@{helpers.TEST_DOMAIN}").name

	def _log(self, method, msg, minutes_ago=0):
		d = frappe.get_doc({"doctype": "Error Log", "method": method,
		                    "error": f"Traceback (most recent call last):\n  File x\n{msg}\n"}).insert(ignore_permissions=True)
		if minutes_ago:
			when = add_to_date(now_datetime(), minutes=-minutes_ago)
			frappe.db.set_value("Error Log", d.name, "creation", when, update_modified=False)
		return d

	def _mine(self, out):
		return {k["title"]: k for k in out["kinds"] if k["title"].startswith(TAG)}

	def test_grouped_by_kind_with_the_latest_message(self):
		self._log(f"{TAG} pay run", "ValueError: old one", minutes_ago=30)
		self._log(f"{TAG} pay run", "ValueError: newest one", minutes_ago=5)
		self._log(f"{TAG} sync", "KeyError: 'x'", minutes_ago=10)
		self._log(f"{TAG} long ago", "KeyError: 'y'", minutes_ago=180)
		frappe.set_user(self.agent)
		k = self._mine(es.error_summary())
		self.assertEqual(set(k), {f"{TAG} pay run", f"{TAG} sync"})            # the 3-hour-old one is outside the hour
		self.assertEqual(k[f"{TAG} pay run"]["count"], 2)
		self.assertEqual(k[f"{TAG} pay run"]["message"], "ValueError: newest one")
		self.assertNotIn("Traceback", str(k))                                  # only the last line goes out
		since = str(add_to_date(now_datetime(), minutes=-240))
		self.assertIn(f"{TAG} long ago", self._mine(es.error_summary(since=since)))

	def test_only_system_managers_and_agents(self):
		frappe.set_user(self.nobody)
		with self.assertRaises(frappe.PermissionError):
			es.error_summary()
