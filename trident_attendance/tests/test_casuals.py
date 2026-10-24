import frappe
from frappe.tests.utils import FrappeTestCase

from trident_attendance.tests import helpers
from trident_attendance.trident_attendance.report.casual_attendance import casual_attendance as report

DT = "Casual Labourer"


class TestCasualRegistration(FrappeTestCase):
	"""Whoever marks attendance registers casuals (the web app's Casual labourers page), but never
	sees or changes their pay."""

	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		self.marker = helpers.make_user(f"marker@{helpers.TEST_DOMAIN}", roles=("Attendance Marking",)).name
		self.supplier = frappe.get_doc({"doctype": "Supplier", "supplier_name": "Tok Labour Ltd",
		                                "supplier_group": frappe.db.get_value("Supplier Group", {}, "name")}
		                               ).insert(ignore_permissions=True).name

	def test_attendance_marking_registers_and_deactivates_but_no_pay(self):
		frappe.set_user(self.marker)
		doc = frappe.get_doc({"doctype": DT, "full_name": "Peter Tok Test", "id_number": "98765432",
		                      "status": "Active", "subcontractor": self.supplier}).insert()
		self.assertEqual(doc.subcontractor_name, "Tok Labour Ltd")
		doc.status = "Left"
		doc.save()
		self.assertEqual(frappe.db.get_value(DT, doc.name, "status"), "Left")
		self.assertFalse(frappe.has_permission(DT, "delete", doc))
		meta = frappe.get_meta(DT)
		for f in ("daily_rate", "mpesa_number", "bank_name", "bank_account_no"):
			self.assertEqual(meta.get_field(f).permlevel, 1, f)
		self.assertNotIn(1, meta.get_permlevel_access("read", user=self.marker))

	def test_attendance_marking_reads_casual_checkins_only(self):
		frappe.set_user(self.marker)
		self.assertTrue(frappe.has_permission("Casual Checkin", "read"))
		self.assertFalse(frappe.has_permission("Casual Checkin", "create"))


class TestCasualAttendance(FrappeTestCase):
	"""One row per casual per day: IN and OUT is Present (never a half day), one alone is flagged."""

	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		self.casual = frappe.get_doc({"doctype": DT, "full_name": "Mary Tok Test", "id_number": "11223344",
		                              "status": "Active"}).insert(ignore_permissions=True).name

	def punch(self, log_type, time):
		frappe.get_doc({"doctype": "Casual Checkin", "casual_labourer": self.casual, "log_type": log_type,
		                "time": time, "client_uid": frappe.generate_hash(length=12)}).insert(ignore_permissions=True)

	def test_days(self):
		self.punch("IN", "2026-10-20 07:30:00")
		self.punch("OUT", "2026-10-20 17:00:00")
		self.punch("IN", "2026-10-21 07:45:00")
		self.punch("OUT", "2026-10-22 17:10:00")
		_, rows = report.execute({"from_date": "2026-10-20", "to_date": "2026-10-22", "casual_labourer": self.casual})
		got = [(str(r["date"]), r["status"], r["hours"]) for r in rows]
		self.assertEqual(got, [("2026-10-20", "Present", 9.5), ("2026-10-21", "Missing check-out", None),
		                       ("2026-10-22", "Missing check-in", None)])
		_, only = report.execute({"from_date": "2026-10-20", "to_date": "2026-10-22", "casual_labourer": self.casual,
		                          "status": "Present"})
		self.assertEqual(len(only), 1)
