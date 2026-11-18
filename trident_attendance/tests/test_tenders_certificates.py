import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from trident_attendance import install
from trident_attendance.tests import helpers
from trident_attendance.trident_attendance.doctype.payment_certificate import payment_certificate as pc
from trident_attendance.trident_attendance.report.certificates_awaiting_invoice import certificates_awaiting_invoice as awaiting
from trident_attendance.trident_attendance.report.retention_due import retention_due
from trident_attendance.trident_attendance.report.tender_performance import tender_performance

TODAY = nowdate()


def company():
	return frappe.db.get_value("Company", {}, "name")


def make_project(name="Tok Towers"):
	return frappe.get_doc({"doctype": "Project", "project_name": name, "company": company()}).insert(ignore_permissions=True)


def make_tender(**fields):
	doc = {"doctype": "Tender", "tender_name": "Tok Mall plumbing", "client_name": "Tok Builders", "tender_value": 50_000_000,
	       "closing_date": TODAY, "company": company()}
	doc.update(fields)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def make_certificate(project, **fields):
	doc = {"doctype": "Payment Certificate", "project": project, "valuation_no": 1, "submitted_on": add_days(TODAY, -20),
	       "valuation_amount": 1_000_000, "company": company()}
	doc.update(fields)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def invoice_for(certificate, project, name="TOK-SINV-0001", posting_date=TODAY, **fields):
	"""An invoice as the hooks see it. It is never saved: posting one needs a chart of accounts, an item and
	a customer, none of which is what is tested here."""
	inv = frappe.new_doc("Sales Invoice")
	inv.update({"name": name, "project": project, "posting_date": posting_date, "is_return": 0, pc.INVOICE_FIELD: certificate})
	inv.update(fields)
	return inv


class TestTender(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")

	def test_a_tender_cannot_be_marked_submitted_without_the_day_it_went(self):
		with self.assertRaises(frappe.ValidationError):
			make_tender(status="Submitted")

	def test_the_submission_day_cannot_be_in_the_future(self):
		with self.assertRaises(frappe.ValidationError):
			make_tender(submitted_on=add_days(TODAY, 1))

	def test_on_time_is_worked_out_from_the_closing_date(self):
		on_time = make_tender(closing_date=TODAY, submitted_on=add_days(TODAY, -1))
		late = make_tender(tender_name="Late one", closing_date=add_days(TODAY, -3), submitted_on=add_days(TODAY, -1))
		self.assertEqual((on_time.status, on_time.submitted_on_time), ("Submitted", 1))
		self.assertEqual(late.submitted_on_time, 0)

	def test_value_won_defaults_to_the_tender_value_and_is_cleared_when_not_won(self):
		tender = make_tender(submitted_on=TODAY, status="Won")
		self.assertEqual(tender.value_won, 50_000_000)
		self.assertEqual(str(tender.result_date), TODAY)
		tender.status = "Lost"
		tender.reason = "Price"
		tender.save()
		self.assertEqual(tender.value_won, 0)

	def test_the_year_is_read_against_the_two_targets(self):
		make_tender(submitted_on=TODAY, status="Won", tender_value=400_000_000, value_won=350_000_000)
		make_tender(tender_name="Second", submitted_on=TODAY, status="Lost", tender_value=600_000_000, reason="Price")
		make_tender(tender_name="Third", closing_date=add_days(TODAY, -5), submitted_on=TODAY, tender_value=1_000_000_000)
		make_tender(tender_name="Still pricing", tender_value=9_000_000_000)        # not sent: not counted
		_cols, rows, _msg, _chart, summary = tender_performance.execute({"year": frappe.utils.getdate(TODAY).year})
		self.assertEqual(sum(r["submitted_value"] for r in rows), 2_000_000_000)
		self.assertEqual((sum(r["on_time"] for r in rows), sum(r["late"] for r in rows)), (2, 1))
		self.assertEqual(sum(r["won_value"] for r in rows), 350_000_000)
		by_label = {s["label"]: s["value"] for s in summary}
		self.assertEqual(by_label["Of the submitted target"], 66.7)                 # 2bn of 3bn
		self.assertEqual(by_label["Of the won target"], 50.0)                       # 350m of 700m
		self.assertEqual(by_label["Win rate (decided tenders)"], 50.0)


class TestPaymentCertificate(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		install.ensure_certificate_field()
		self.project = make_project().name

	def test_the_link_field_is_added_to_the_sales_invoice_once(self):
		self.assertTrue(frappe.get_meta("Sales Invoice").has_field(pc.INVOICE_FIELD))
		self.assertFalse(install.ensure_certificate_field())

	def test_a_valuation_starts_as_submitted_and_counts_days_to_the_certificate(self):
		cert = make_certificate(self.project)
		self.assertEqual((cert.status, cert.days_to_certify), ("Submitted", None))
		cert.certified_on = add_days(TODAY, -6)
		cert.certified_amount = 900_000
		cert.save()
		self.assertEqual((cert.status, cert.days_to_certify), ("Certified", 14))
		self.assertEqual(str(cert.certificate_received_on), TODAY)                  # recorded today: taken as received today

	def test_one_record_per_valuation_of_a_project(self):
		make_certificate(self.project)
		with self.assertRaises(frappe.ValidationError):
			make_certificate(self.project)
		make_certificate(self.project, valuation_no=2)

	def test_dates_are_kept_honest(self):
		with self.assertRaises(frappe.ValidationError):
			make_certificate(self.project, submitted_on=add_days(TODAY, 1))
		with self.assertRaises(frappe.ValidationError):                              # certified before it was sent
			make_certificate(self.project, valuation_no=2, certified_on=add_days(TODAY, -30), certified_amount=1)
		with self.assertRaises(frappe.ValidationError):                              # certified with no amount
			make_certificate(self.project, valuation_no=3, certified_on=add_days(TODAY, -2))
		with self.assertRaises(frappe.ValidationError):                              # more retention than was certified
			make_certificate(self.project, valuation_no=4, certified_on=add_days(TODAY, -2), certified_amount=100, retention_amount=200)

	def test_follow_ups_are_stamped_with_who_made_them(self):
		cert = make_certificate(self.project, follow_ups=[{"follow_up_date": TODAY, "note": "QS to sign on Friday"}])
		self.assertEqual(cert.follow_ups[0].followed_up_by, "Administrator")

	def test_submitting_the_invoice_stamps_the_certificate(self):
		cert = make_certificate(self.project, certified_on=add_days(TODAY, -9), certificate_received_on=add_days(TODAY, -7),
		                        certified_amount=900_000)
		inv = invoice_for(cert.name, self.project, posting_date=add_days(TODAY, -4))
		pc.validate_invoice(inv)
		pc.on_invoice_submit(inv)
		cert.reload()
		self.assertEqual((cert.status, cert.sales_invoice, cert.days_to_invoice), ("Invoiced", "TOK-SINV-0001", 3))
		with self.assertRaises(frappe.ValidationError):                              # a second invoice cannot take it
			pc.validate_invoice(invoice_for(cert.name, self.project, name="TOK-SINV-0002"))
		with self.assertRaises(frappe.ValidationError):                              # nor can the record be deleted
			cert.delete()
		pc.on_invoice_cancel(inv)
		cert.reload()
		self.assertEqual((cert.status, cert.sales_invoice, cert.invoiced_on), ("Certified", None, None))

	def test_an_invoice_cannot_name_another_projects_or_an_uncertified_certificate(self):
		waiting = make_certificate(self.project)
		with self.assertRaises(frappe.ValidationError):
			pc.validate_invoice(invoice_for(waiting.name, self.project))
		other = make_project("Tok Plaza").name
		certified = make_certificate(other, certified_on=add_days(TODAY, -2), certified_amount=500)
		with self.assertRaises(frappe.ValidationError):
			pc.validate_invoice(invoice_for(certified.name, self.project))
		pc.validate_invoice(invoice_for(None, self.project))                         # an ordinary invoice is left alone

	def test_the_work_list_shows_certified_valuations_waiting_for_an_invoice(self):
		make_certificate(self.project, certified_on=add_days(TODAY, -10), certificate_received_on=add_days(TODAY, -8),
		                 certified_amount=900_000)                                   # 8 days: late
		make_certificate(self.project, valuation_no=2, certified_on=add_days(TODAY, -3), certificate_received_on=add_days(TODAY, -2),
		                 certified_amount=400_000)                                   # 2 days: in time
		make_certificate(self.project, valuation_no=3)                                # still with the client
		_cols, rows = awaiting.execute({"project": self.project})
		self.assertEqual([(r["valuation_no"], r["days_waiting"], r["late"]) for r in rows], [(1, 8, 1), (2, 2, 0)])
		_cols, rows = awaiting.execute({"project": self.project, "include_not_certified": 1})
		self.assertEqual({r["valuation_no"] for r in rows}, {1, 2, 3})

	def test_retention_due_lists_what_can_be_claimed(self):
		certified = dict(certified_on=add_days(TODAY, -5), certified_amount=1_000_000, retention_amount=100_000)
		make_certificate(self.project, retention_release_date=add_days(TODAY, -10), **certified)             # due now
		make_certificate(self.project, valuation_no=2, retention_release_date=add_days(TODAY, 30), **certified)   # soon
		make_certificate(self.project, valuation_no=3, retention_release_date=add_days(TODAY, 300), **certified)  # far off
		make_certificate(self.project, valuation_no=4, **certified)                                          # no date
		_cols, rows = retention_due.execute({"project": self.project})
		self.assertEqual([(r["valuation_no"], r["days_overdue"]) for r in rows], [(1, 10), (2, 0)])
		_cols, rows = retention_due.execute({"project": self.project, "show_all": 1})
		self.assertEqual(len(rows), 4)
