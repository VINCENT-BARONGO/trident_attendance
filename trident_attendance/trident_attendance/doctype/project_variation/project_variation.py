import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import date_diff, getdate, nowdate

DOCTYPE = "Project Variation"


class ProjectVariation(Document):
	"""One change to a project's scope, from the client's instruction to the valuation it is claimed in.

	Two things are measured from it: how many days it took to price the variation and send it to the client,
	and how much of what was priced the client approved. Dates cannot be in the future and must follow one
	another, so a variation cannot be shown as priced before it was instructed."""

	def validate(self):
		if not self.priced_by and self.priced_on:
			self.priced_by = frappe.session.user
		self._check_unique()
		self._check_dates()
		self._check_amounts()
		self.days_to_price = date_diff(self.priced_on, self.instructed_on) if self.priced_on else None
		self.status = self._status()

	def _check_unique(self):
		other = frappe.db.get_value(
			DOCTYPE, {"project": self.project, "variation_no": self.variation_no, "name": ["!=", self.name or ""]}, "name"
		)
		if other:
			frappe.throw(_("Variation {0} of project {1} is already recorded as {2}.").format(self.variation_no, self.project, other))

	def _check_dates(self):
		today = getdate(nowdate())
		for field in ("instructed_on", "priced_on", "approved_on"):
			if self.get(field) and getdate(self.get(field)) > today:
				frappe.throw(_("{0} cannot be in the future.").format(_(self.meta.get_label(field))))
		if self.priced_on and getdate(self.priced_on) < getdate(self.instructed_on):
			frappe.throw(_("A variation cannot be priced before the instruction was received."))
		if self.approved_on and not self.priced_on:
			frappe.throw(_("Enter when the variation was priced before recording the client's approval."))
		if self.approved_on and getdate(self.approved_on) < getdate(self.priced_on):
			frappe.throw(_("Approved On cannot be before the variation was priced."))

	def _check_amounts(self):
		if self.priced_on and not self.amount_priced:
			frappe.throw(_("Enter the amount the variation was priced at."))
		if self.approved_on and self.amount_approved is None:
			frappe.throw(_("Enter the amount the client approved."))
		if self.payment_certificate:
			project = frappe.db.get_value("Payment Certificate", self.payment_certificate, "project")
			if project and project != self.project:
				frappe.throw(_("Valuation {0} belongs to project {1}.").format(self.payment_certificate, project))

	def _status(self):
		if self.rejected:
			return "Rejected"
		if self.payment_certificate:
			return "Invoiced"
		if self.approved_on:
			return "Approved"
		if self.priced_on:
			return "Priced"
		return "Raised"
