"""The weekly casual register PDF (trident_attendance.casual_register), the download the Casual
Attendance report offers. Scope must match the web-app register: HR/admins see every site; a
coordinator sees only the sites they are assigned to (Project.allowed_users) plus any casual they
clocked in (supervisor_user). Then the P/A grid, days present and pay, and the role gate."""

from datetime import date
from decimal import Decimal

import frappe
from frappe.tests.utils import FrappeTestCase

from trident_attendance import casual_register as reg
from trident_attendance.tests import helpers

DT = "Casual Labourer"
MON = date(2026, 11, 9)  # Monday; week runs Mon 9 .. Sat 14 (Sun 15 must be excluded)


def _day(d, h=7):
	return f"2026-11-{d:02d} {h:02d}:30:00"


class TestCasualRegister(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		# Adding users to a Project makes ERPNext try to email them a welcome; the test site has no
		# outgoing Email Account, so mute email for the test (restored after).
		muted = frappe.flags.mute_emails
		frappe.flags.mute_emails = True
		self.addCleanup(lambda: setattr(frappe.flags, "mute_emails", muted))
		frappe.set_user("Administrator")
		self.company = frappe.db.get_value("Company", {}, "name")
		self.coord = helpers.make_user(f"coord@{helpers.TEST_DOMAIN}", roles=("Casual Registrar",)).name
		self.hr = helpers.make_user(f"hr@{helpers.TEST_DOMAIN}", roles=("HR Manager",)).name
		self.other = helpers.make_user(f"other@{helpers.TEST_DOMAIN}", roles=("Casual Registrar",)).name
		self.nobody = helpers.make_user(f"nobody@{helpers.TEST_DOMAIN}").name  # no casual role
		self.pa = self._project("Assigned Site", [self.coord])
		self.pb = self._project("Other Site", [self.other])
		# C1 on PA (coord's site), clocked by `other` -> coord still sees them (assigned-site rule).
		self.c1 = self._casual("Peter Test", "81110001", 800)
		# C2 on PB (not coord's site) but clocked by coord themselves -> coord sees them (own punch).
		self.c2 = self._casual("Mary Test", "81110002", 900)
		# C3 on PB clocked by `other` -> coord must NOT see them; HR (full) must.
		self.c3 = self._casual("Juma Test", "81110003", 700)
		self._punch(self.c1, self.pa, "IN", _day(9), self.other)
		self._punch(self.c1, self.pa, "IN", _day(10), self.other)
		self._punch(self.c2, self.pb, "IN", _day(9), self.coord)
		self._punch(self.c3, self.pb, "IN", _day(9), self.other)
		self._punch(self.c1, self.pa, "IN", _day(15), self.other)  # Sunday -> excluded

	def _project(self, name, allowed):
		# Assign via the standard Project > Users table, which exists in every bench. The live site
		# also has a custom "Project Allowed User" table; utils.assigned_projects reads whichever is
		# present, so the register scopes the same way here as in production.
		# welcome_email_sent=1 on each row makes ERPNext's Project.send_welcome_email skip them (it only
		# emails users whose flag is 0); with email muted too, inserting the Project sends nothing.
		doc = frappe.get_doc({"doctype": "Project", "project_name": name, "company": self.company,
		                      "users": [{"user": u, "welcome_email_sent": 1} for u in allowed]})
		return doc.insert(ignore_permissions=True).name

	def _casual(self, full_name, id_number, rate):
		doc = frappe.get_doc({"doctype": DT, "full_name": full_name, "id_number": id_number,
		                      "daily_rate": rate, "phone": "254700111222"})
		doc.flags.ignore_permissions = True
		return doc.insert().name

	def _punch(self, casual, project, log_type, time, supervisor_user):
		frappe.get_doc({"doctype": "Casual Checkin", "casual_labourer": casual, "project": project,
		                "log_type": log_type, "time": time, "supervisor_user": supervisor_user,
		                "client_uid": frappe.generate_hash(length=12)}).insert(ignore_permissions=True)

	# ---- scope --------------------------------------------------------------
	def _casual_names(self, data):
		return {c["name"] for s in data["sites"] for c in s["casuals"]}

	def test_coordinator_sees_assigned_site_and_own_punches_only(self):
		data = reg.build(self.coord, MON, full=False)
		names = self._casual_names(data)
		self.assertIn(self.c1, names, "a casual on the coordinator's assigned site shows, whoever clocked them")
		self.assertIn(self.c2, names, "a casual the coordinator clocked themselves shows, even off their sites")
		self.assertNotIn(self.c3, names, "a casual on another site, clocked by someone else, must not leak in")

	def test_hr_sees_every_site(self):
		data = reg.build(self.hr, MON, full=True)
		names = self._casual_names(data)
		self.assertEqual({self.c1, self.c2, self.c3}, names, "the full register covers every site")

	def test_grid_days_and_pay(self):
		data = reg.build(self.coord, MON, full=False)
		sites = {s["project"]: s for s in data["sites"]}
		peter = next(c for c in sites[self.pa]["casuals"] if c["name"] == self.c1)
		self.assertEqual(peter["present"], [True, True, False, False, False, False])  # Mon/Tue, not Sun
		self.assertEqual(peter["days"], 2)
		# ERPNext stores daily_rate as a float, so the Decimal may carry a trailing .0; compare by value.
		self.assertEqual(peter["rate"], Decimal("800"))        # rate x days present
		self.assertEqual(peter["amount"], Decimal("1600"))
		self.assertEqual(peter["phone"], "254700111222")

	def test_week_parsing_defaults_and_clamps(self):
		today = date(2026, 11, 11)
		self.assertEqual(reg.parse_week(None, today), MON)
		self.assertEqual(reg.parse_week("2026-11-14", today), MON)   # any day in the week -> its Monday
		self.assertEqual(reg.parse_week("2027-01-01", today), MON)   # the future is pulled back

	# ---- rendering ----------------------------------------------------------
	def test_html_has_names_grid_and_totals(self):
		html = reg.render_html(reg.build(self.hr, MON, full=True))
		self.assertIn("Casual attendance", html)
		self.assertIn("GRAND TOTAL", html)
		self.assertIn("KSh", html)
		self.assertIn("Peter Test", html)
		self.assertIn("0700111222", html)          # 2547... shown as 07...
		self.assertIn(">P<", html)                 # a present mark is drawn
		self.assertIn("Every site in the company.", html)   # full-scope footer

	# ---- role gate ----------------------------------------------------------
	def test_a_user_without_a_casual_role_may_not_download(self):
		frappe.set_user(self.nobody)
		with self.assertRaises(frappe.PermissionError):
			reg.download_weekly_register(week="2026-11-14")
