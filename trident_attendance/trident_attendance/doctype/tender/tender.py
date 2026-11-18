import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowdate

SENT = ("Submitted", "Won", "Lost")


class Tender(Document):
	"""One tender in the tender register. The Contract Procurement Manager's tender KRAs (value submitted in
	the year, submitted by the closing date, value won) are read from these records, so the dates are checked
	here: a tender cannot be marked sent without the day it went, and that day cannot be in the future."""

	def validate(self):
		if not self.prepared_by:
			self.prepared_by = self.owner or frappe.session.user
		if not self.company:
			self.company = frappe.defaults.get_user_default("Company") or frappe.db.get_value("Company", {}, "name")
		self._check_submission()
		self._check_result()

	def _check_submission(self):
		if self.status in SENT and not self.submitted_on:
			frappe.throw(_("Enter the day the tender was submitted before marking it {0}.").format(_(self.status)))
		if self.submitted_on and getdate(self.submitted_on) > getdate(nowdate()):
			frappe.throw(_("Submitted On cannot be in the future."))
		if self.submitted_on and self.status == "Preparing":
			self.status = "Submitted"
		self.submitted_on_time = int(
			bool(self.submitted_on and self.closing_date and getdate(self.submitted_on) <= getdate(self.closing_date))
		)

	def _check_result(self):
		if self.status == "Won":
			if not self.value_won:
				self.value_won = self.tender_value
		else:
			self.value_won = 0
		if self.status in ("Won", "Lost") and not self.result_date:
			self.result_date = nowdate()
		if self.status in ("Lost", "Not Submitted") and not (self.reason or "").strip():
			frappe.msgprint(_("Add the reason: it is what the next tender is priced from."), alert=True)
