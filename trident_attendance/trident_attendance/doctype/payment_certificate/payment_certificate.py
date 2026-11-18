import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import date_diff, getdate, nowdate

DOCTYPE = "Payment Certificate"
INVOICE_FIELD = "custom_payment_certificate"


class PaymentCertificate(Document):
	"""One valuation sent to a client on a project, followed until the client certifies it and it is invoiced.

	Three KRAs are read from these records, so the record keeps the dates honest:
	  - the coordinators': valuation submitted by the set date and followed up to approval;
	  - the Credit Controller's: invoice raised within days of the certificate reaching the office;
	  - the Credit Controller's: retention recorded and claimed when it falls due.
	The invoice side is never typed here: it is filled in when a Sales Invoice naming this certificate is
	submitted (see `on_invoice_submit`), so the person scored on invoicing cannot back-date it."""

	def validate(self):
		if not self.submitted_by:
			self.submitted_by = self.owner or frappe.session.user
		self._check_unique()
		self._check_dates()
		self._check_amounts()
		self._stamp_follow_ups()
		self._set_retention_claim()
		self._set_days_and_status()

	def _check_unique(self):
		other = frappe.db.get_value(
			DOCTYPE, {"project": self.project, "valuation_no": self.valuation_no, "name": ["!=", self.name or ""]}, "name"
		)
		if other:
			frappe.throw(_("Valuation {0} of project {1} is already recorded as {2}.").format(self.valuation_no, self.project, other))

	def _check_dates(self):
		today = getdate(nowdate())
		for field in ("submitted_on", "certified_on", "certificate_received_on"):
			if self.get(field) and getdate(self.get(field)) > today:
				frappe.throw(_("{0} cannot be in the future.").format(_(self.meta.get_label(field))))
		if self.certified_on and getdate(self.certified_on) < getdate(self.submitted_on):
			frappe.throw(_("Certified On cannot be before the valuation was submitted."))
		if self.certified_on and not self.certificate_received_on:
			# The day it is recorded is the day the office is taken to have had it.
			self.certificate_received_on = nowdate()
		if self.certificate_received_on and self.certified_on and getdate(self.certificate_received_on) < getdate(self.certified_on):
			frappe.throw(_("The certificate cannot have been received before its own date."))

	def _check_amounts(self):
		if self.certified_on and not self.certified_amount:
			frappe.throw(_("Enter the amount the client certified."))
		if (self.retention_amount or 0) > (self.certified_amount or self.valuation_amount or 0):
			frappe.throw(_("Retention held cannot be more than the amount certified."))

	def _stamp_follow_ups(self):
		for row in self.follow_ups or []:
			if not row.followed_up_by:
				row.followed_up_by = frappe.session.user

	def _set_retention_claim(self):
		if self.retention_invoice:
			posted = frappe.db.get_value("Sales Invoice", {"name": self.retention_invoice, "docstatus": 1}, "posting_date")
			if not posted:
				frappe.throw(_("The retention invoice must be a submitted Sales Invoice."))
			self.retention_claimed_on = posted
		else:
			self.retention_claimed_on = None

	def _set_days_and_status(self):
		self.days_to_certify = date_diff(self.certified_on, self.submitted_on) if self.certified_on else None
		start = self.certificate_received_on or self.certified_on
		self.days_to_invoice = date_diff(self.invoiced_on, start) if (self.invoiced_on and start) else None
		if self.rejected:
			self.status = "Rejected"
		elif self.sales_invoice:
			self.status = "Invoiced"
		elif self.certified_on:
			self.status = "Certified"
		else:
			self.status = "Submitted"

	def on_trash(self):
		if self.sales_invoice:
			frappe.throw(_("Invoice {0} was raised for this certificate; it cannot be deleted.").format(self.sales_invoice))


# ---------------------------------------------------------------------------
# Sales Invoice events (wired in hooks.py)
# ---------------------------------------------------------------------------


def _certificate_of(invoice):
	"""The certificate an invoice names, or None on a site without the field or the doctype."""
	if not invoice.meta.has_field(INVOICE_FIELD) or not frappe.db.exists("DocType", DOCTYPE):
		return None
	return invoice.get(INVOICE_FIELD) or None


def validate_invoice(doc, method=None):
	"""An invoice can only name a certificate of its own project, one that the client has certified and
	that no other invoice has taken."""
	name = _certificate_of(doc)
	if not name or doc.is_return:
		return
	cert = frappe.db.get_value(DOCTYPE, name, ["project", "certified_on", "sales_invoice", "status"], as_dict=True)
	if not cert:
		return
	if doc.project and cert.project != doc.project:
		frappe.throw(_("Payment Certificate {0} belongs to project {1}, not {2}.").format(name, cert.project, doc.project))
	if not cert.certified_on:
		frappe.throw(_("Payment Certificate {0} has not been certified yet: enter the certificate date on it first.").format(name))
	if cert.sales_invoice and cert.sales_invoice != doc.name and cert.sales_invoice != doc.amended_from:
		frappe.throw(_("Payment Certificate {0} was already invoiced on {1}.").format(name, cert.sales_invoice))


def on_invoice_submit(doc, method=None):
	name = _certificate_of(doc)
	if not name or doc.is_return:
		return
	cert = frappe.get_doc(DOCTYPE, name)
	cert.sales_invoice = doc.name
	cert.invoiced_on = doc.posting_date
	cert.flags.ignore_permissions = True
	cert.save()


def on_invoice_cancel(doc, method=None):
	name = _certificate_of(doc)
	if not name:
		return
	if frappe.db.get_value(DOCTYPE, name, "sales_invoice") != doc.name:
		return
	cert = frappe.get_doc(DOCTYPE, name)
	cert.sales_invoice = None
	cert.invoiced_on = None
	cert.flags.ignore_permissions = True
	cert.save()
