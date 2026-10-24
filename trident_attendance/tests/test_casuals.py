import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase

from trident_attendance import install
from trident_attendance.api import get_casuals
from trident_attendance.tests import helpers
from trident_attendance.trident_attendance.report.casual_attendance import casual_attendance as report

DT = "Casual Labourer"


class TestCasualApproval(FrappeTestCase):
	"""A Casual Registrar registers a casual with their pay; it waits as Pending Approval until a
	Casual Approver approves it, and only then can the phone find them. Attendance Marking alone
	no longer registers casuals."""

	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		install.ensure_casual_approval()
		self.registrar = helpers.make_user(f"registrar@{helpers.TEST_DOMAIN}", roles=("Casual Registrar",)).name
		self.approver = helpers.make_user(f"approver@{helpers.TEST_DOMAIN}", roles=("Casual Approver",)).name
		self.marker = helpers.make_user(f"marker@{helpers.TEST_DOMAIN}", roles=("Attendance Marking",)).name
		self.supplier = frappe.get_doc({"doctype": "Supplier", "supplier_name": "Tok Labour Ltd",
		                                "supplier_group": frappe.db.get_value("Supplier Group", {}, "name")}
		                               ).insert(ignore_permissions=True).name

	def register(self, id_number="98765432"):
		frappe.set_user(self.registrar)
		return frappe.get_doc({"doctype": DT, "full_name": "Peter Tok Test", "id_number": id_number,
		                       "subcontractor": self.supplier, "daily_rate": 900, "mpesa_number": "254712345678",
		                       "next_of_kin_name": "Mary Tok Test"}).insert()

	def test_registered_with_pay_waits_for_the_approver(self):
		doc = self.register()
		self.assertEqual(doc.status, "Pending Approval")
		self.assertEqual((doc.daily_rate, doc.mpesa_number, doc.subcontractor_name), (900, "254712345678", "Tok Labour Ltd"))
		frappe.set_user(self.marker)                                          # the phone asks as its supervisor
		self.assertEqual(get_casuals(id_number="98765432"), [], "the phone does not find a casual not yet approved")
		frappe.set_user(self.registrar)
		with self.assertRaises(frappe.ValidationError):
			apply_workflow(frappe.get_doc(DT, doc.name), "Approve")          # the registrar may not approve
		frappe.set_user(self.approver)
		self.assertEqual(frappe.get_doc(DT, doc.name).daily_rate, 900, "the approver sees the pay")
		apply_workflow(frappe.get_doc(DT, doc.name), "Approve")
		self.assertEqual(frappe.db.get_value(DT, doc.name, "status"), "Active")
		frappe.set_user("Administrator")
		self.assertEqual([c["name"] for c in get_casuals(id_number="98765432")], [doc.name])

	def test_rejected_then_sent_again_deactivated_and_back(self):
		doc = self.register("11122233")
		frappe.set_user(self.approver)
		apply_workflow(frappe.get_doc(DT, doc.name), "Reject")
		self.assertEqual(frappe.db.get_value(DT, doc.name, "status"), "Rejected")
		frappe.set_user(self.registrar)
		apply_workflow(frappe.get_doc(DT, doc.name), "Send Again")
		frappe.set_user(self.approver)
		apply_workflow(frappe.get_doc(DT, doc.name), "Approve")
		frappe.set_user(self.registrar)
		apply_workflow(frappe.get_doc(DT, doc.name), "Deactivate")
		self.assertEqual(frappe.db.get_value(DT, doc.name, "status"), "Left")
		apply_workflow(frappe.get_doc(DT, doc.name), "Reactivate")
		self.assertEqual(frappe.db.get_value(DT, doc.name, "status"), "Active")

	def test_once_approved_only_the_approver_changes_the_pay_and_the_photo(self):
		doc = self.register("44455566")
		frappe.set_user(self.approver)
		apply_workflow(frappe.get_doc(DT, doc.name), "Approve")
		frappe.set_user(self.registrar)
		mine = frappe.get_doc(DT, doc.name)
		mine.next_of_kin_name = "Jane Tok Test"
		mine.save()                                                           # other details: still theirs
		for field, value in (("daily_rate", 5000), ("mpesa_number", "254799999999"), ("photo", "/files/other.jpg")):
			mine = frappe.get_doc(DT, doc.name)
			mine.set(field, value)
			with self.assertRaises(frappe.PermissionError, msg=field):
				mine.save()
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc({"doctype": "File", "file_name": "swap.jpg", "content": helpers.JPEG,
			                "attached_to_doctype": DT, "attached_to_name": doc.name, "attached_to_field": "photo",
			                "is_private": 1}).insert()

	def test_an_approved_casuals_photo_file_is_not_deleted_or_replaced(self):
		doc = self.register("55566677")
		face = frappe.get_doc({"doctype": "File", "file_name": "tok-face.jpg", "content": helpers.JPEG,
		                       "attached_to_doctype": DT, "attached_to_name": doc.name, "attached_to_field": "photo",
		                       "is_private": 1}).insert()
		mine = frappe.get_doc(DT, doc.name)
		mine.photo = face.file_url
		mine.save()
		frappe.set_user(self.approver)
		apply_workflow(frappe.get_doc(DT, doc.name), "Approve")
		frappe.set_user(self.registrar)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("File", face.name).delete()
		moved = frappe.get_doc("File", face.name)
		moved.attached_to_doctype, moved.attached_to_name = "User", "Administrator"     # off the casual
		with self.assertRaises(frappe.PermissionError):
			moved.save()
		frappe.set_user("Administrator")
		self.assertTrue(frappe.db.exists("File", face.name))
		frappe.set_user(self.approver)
		theirs = frappe.get_doc(DT, doc.name)
		theirs.daily_rate = 1000
		theirs.save()
		self.assertEqual(frappe.db.get_value(DT, doc.name, ["daily_rate", "next_of_kin_name"]), (1000, "Jane Tok Test"))

	def test_attendance_marking_no_longer_registers_and_never_sees_pay(self):
		frappe.set_user(self.marker)
		self.assertFalse(frappe.has_permission(DT, "create"))
		self.assertTrue(frappe.has_permission("Casual Checkin", "read"))
		self.assertFalse(frappe.has_permission("Casual Checkin", "create"))
		meta = frappe.get_meta(DT)
		for f in ("daily_rate", "mpesa_number", "bank_name", "bank_account_no"):
			self.assertEqual(meta.get_field(f).permlevel, 1, f)

	def test_the_workflow_is_made_once(self):
		self.assertTrue(frappe.db.exists("Workflow", install.CASUAL_WORKFLOW))
		self.assertFalse(install.ensure_casual_approval(), "a second run changes nothing")


class TestCasualAttendance(FrappeTestCase):
	"""One row per casual per day: IN and OUT is Present (never a half day), one alone is flagged."""

	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		doc = frappe.get_doc({"doctype": DT, "full_name": "Mary Tok Test", "id_number": "11223344"})
		doc.flags.ignore_permissions = True
		doc.insert()
		self.casual = doc.name

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
