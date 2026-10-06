from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from trident_attendance.employee_access import SELF_SERVICE_ROLES, issue_employee_token
from trident_attendance.provision import provision_ess_users
from trident_attendance.tests import helpers

DOMAIN = helpers.TEST_DOMAIN


class TestProvisionEssUsers(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		self.one = helpers.make_employee("Provone", app_access=0).name
		self.two = helpers.make_employee("Provtwo", app_access=0).name
		self.names = [self.one, self.two]

	def address(self, employee):
		return f"{employee.lower()}@staff.{DOMAIN}"

	def outcomes(self, result):
		return {r["employee"]: r["outcome"] for r in result["employees"]}

	def stored_roles(self, user):
		"""Read from the table, not from a doc that was just saved: in the trial the role looked
		added and was gone from the database."""
		return set(frappe.get_all("Has Role", filters={"parent": user, "parenttype": "User"}, pluck="role"))

	def test_dry_run_is_the_default_and_changes_nothing(self):
		users = frappe.db.count("User")
		result = provision_ess_users(DOMAIN, employees=self.names)
		self.assertTrue(result["dry_run"])
		self.assertEqual((result["would_create"], result["created"]), (2, 0))
		self.assertEqual(self.outcomes(result), {self.one: "would create", self.two: "would create"})
		self.assertEqual(
			[r["user"] for r in result["employees"]], [self.address(self.one), self.address(self.two)]
		)
		self.assertEqual(frappe.db.count("User"), users)
		for name in self.names:
			self.assertFalse(frappe.db.exists("User", self.address(name)))
			self.assertIsNone(frappe.db.get_value("Employee", name, "user_id"))

	def test_dry_run_over_every_employee_changes_nothing(self):
		users = frappe.db.count("User")
		linked = frappe.db.count("Employee", {"user_id": ["is", "set"]})
		result = provision_ess_users(DOMAIN, dry_run="1")
		self.assertGreaterEqual(result["would_create"], 2)
		self.assertEqual(frappe.db.count("User"), users)
		self.assertEqual(frappe.db.count("Employee", {"user_id": ["is", "set"]}), linked)

	def test_real_run_creates_links_and_gives_the_role(self):
		with patch("frappe.sendmail") as sendmail:
			result = provision_ess_users(DOMAIN, dry_run=0, employees=self.names)
		sendmail.assert_not_called()
		self.assertFalse(result["dry_run"])
		self.assertEqual((result["created"], result["failed"], result["skipped"]), (2, 0, 0))
		for name in self.names:
			email = self.address(name)
			self.assertEqual(frappe.db.get_value("User", email, "enabled"), 1)
			self.assertEqual(frappe.db.get_value("Employee", name, "user_id"), email)
			self.assertEqual(self.stored_roles(email), {"Employee", "Employee Self Service"})
			self.assertLessEqual(set(frappe.get_roles(email)), SELF_SERVICE_ROLES)
			self.assertTrue(
				frappe.db.exists("User Permission", {"user": email, "allow": "Employee", "for_value": name})
			)
			self.assertTrue(frappe.db.get_value("Employee", name, "create_user_permission"))
			# App access stays HR's decision unless the run was asked to give it.
			self.assertFalse(frappe.db.get_value("Employee", name, "custom_app_access"))

	def test_password_is_set_and_unknown(self):
		from frappe.utils.password import check_password

		provision_ess_users(DOMAIN, dry_run=0, employees=[self.one])
		email = self.address(self.one)
		self.assertTrue(frappe.db.exists("__Auth", {"doctype": "User", "name": email, "fieldname": "password"}))
		for guess in (email, self.one, "password"):
			self.assertRaises(frappe.AuthenticationError, check_password, email, guess)

	def test_running_again_creates_nothing(self):
		provision_ess_users(DOMAIN, dry_run=0, employees=self.names)
		users = frappe.db.count("User")
		modified = frappe.db.get_value("User", self.address(self.one), "modified")
		for dry_run in (0, 1):
			again = provision_ess_users(DOMAIN, dry_run=dry_run, employees=self.names)
			self.assertEqual((again["created"], again["would_create"], again["skipped"]), (0, 0, 2))
			self.assertEqual(
				self.outcomes(again),
				{self.one: "skipped: already has a user", self.two: "skipped: already has a user"},
			)
		self.assertEqual(frappe.db.count("User"), users)
		self.assertEqual(frappe.db.get_value("User", self.address(self.one), "modified"), modified)
		self.assertEqual(self.stored_roles(self.address(self.one)), {"Employee", "Employee Self Service"})

	def test_enable_app_access_and_a_token(self):
		"""A provisioned user is one the hub can be issued a token for."""
		provision_ess_users(DOMAIN, dry_run=0, employees=[self.one], enable_app_access=1)
		self.assertEqual(frappe.db.get_value("Employee", self.one, "custom_app_access"), 1)
		self.assertFalse(frappe.db.get_value("Employee", self.two, "custom_app_access"))
		hub = helpers.make_user(f"hub@{DOMAIN}", roles=["Attendance Admin"]).name
		helpers.set_settings(hub_service_user=hub, employee_token_client=helpers.make_oauth_client().name)
		frappe.set_user(hub)
		result = issue_employee_token(self.one, "own")
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["user"], self.address(self.one))

	def test_an_existing_user_is_never_touched(self):
		supervisor = helpers.make_user(f"supervisor@{DOMAIN}", roles=["Attendance Marking"])
		linked = helpers.make_employee("Provsup", user=supervisor.name, app_access=0).name
		before = frappe.db.get_value("User", supervisor.name, "modified")
		roles = self.stored_roles(supervisor.name)
		result = provision_ess_users(DOMAIN, dry_run=0, employees=[linked, self.one], enable_app_access=1)
		self.assertEqual(self.outcomes(result), {linked: "skipped: already has a user", self.one: "created"})
		self.assertEqual(frappe.db.get_value("Employee", linked, "user_id"), supervisor.name)
		self.assertEqual(frappe.db.get_value("User", supervisor.name, "modified"), before)
		self.assertEqual(self.stored_roles(supervisor.name), roles)
		self.assertFalse(frappe.db.get_value("Employee", linked, "custom_app_access"))
		self.assertFalse(frappe.db.exists("User", self.address(linked)))

	def test_an_unlinked_user_with_the_address_is_left_alone(self):
		stray = helpers.make_user(self.address(self.one), roles=["Attendance Marking"])
		result = provision_ess_users(DOMAIN, dry_run=0, employees=self.names)
		self.assertEqual(
			self.outcomes(result),
			{self.one: "skipped: user exists but is not linked to the employee", self.two: "created"},
		)
		self.assertIsNone(frappe.db.get_value("Employee", self.one, "user_id"))
		self.assertEqual(self.stored_roles(stray.name), {"Attendance Marking"})

	def test_inactive_and_unknown_employees_are_skipped(self):
		frappe.db.set_value("Employee", self.two, "status", "Left")
		result = provision_ess_users(DOMAIN, dry_run=0, employees=f"{self.one}, {self.two}, TOK-NO-SUCH")
		self.assertEqual(
			self.outcomes(result),
			{
				self.one: "created",
				self.two: "skipped: no such Active employee",
				"TOK-NO-SUCH": "skipped: no such Active employee",
			},
		)
		self.assertFalse(frappe.db.exists("User", self.address(self.two)))

	def test_company_filter(self):
		result = provision_ess_users(DOMAIN, employees=self.names, company="Tok No Such Company")
		self.assertEqual((result["would_create"], result["skipped"]), (0, 2))

	def test_a_failure_undoes_that_employee_only(self):
		real_save = frappe.model.document.Document.save

		def save(doc, *args, **kwargs):
			if doc.doctype == "Employee" and doc.name == self.one:
				frappe.throw("Tok test failure")
			return real_save(doc, *args, **kwargs)

		with patch("frappe.model.document.Document.save", save):
			result = provision_ess_users(DOMAIN, dry_run=0, employees=self.names)
		outcomes = self.outcomes(result)
		self.assertTrue(outcomes[self.one].startswith("failed: Tok test failure"), outcomes)
		self.assertEqual(outcomes[self.two], "created")
		self.assertEqual((result["created"], result["failed"]), (1, 1))
		self.assertFalse(frappe.db.exists("User", self.address(self.one)))
		self.assertIsNone(frappe.db.get_value("Employee", self.one, "user_id"))
		# Nothing is left half done, so the next run finishes the job.
		again = provision_ess_users(DOMAIN, dry_run=0, employees=self.names)
		self.assertEqual(self.outcomes(again), {self.one: "created", self.two: "skipped: already has a user"})

	def test_the_role_is_stripped_without_the_link(self):
		"""Why the link comes first: ERPNext removes the role from a user no Employee points at."""
		user = helpers.make_user(f"unlinked@{DOMAIN}")
		user.add_roles("Employee Self Service")
		self.assertNotIn("Employee Self Service", self.stored_roles(user.name))

	def test_domain_must_be_a_domain(self):
		for domain in ("", None, "no dots", "a@b.c", "-bad.example", "staff"):
			self.assertRaises(frappe.ValidationError, provision_ess_users, domain, employees=self.names)
		result = provision_ess_users(f" @{DOMAIN.upper()} ", employees=[self.one])
		self.assertEqual(result["employees"][0]["user"], self.address(self.one))
