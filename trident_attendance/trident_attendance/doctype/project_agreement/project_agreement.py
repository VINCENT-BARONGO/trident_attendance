import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, getdate, nowdate

DOCTYPE = "Project Agreement"
SETTINGS = "Trident Attendance Settings"
CHECKS = ("tax_compliance_checked", "nca_registration_checked", "insurance_checked", "bond_checked")


class ProjectAgreement(Document):
	"""A contract or subcontract agreement of a project.

	Complete = signed, the signed copy attached, payment terms written in, the four statutory checks ticked.
	In time = it became complete on or before the day work started. The day it became complete is stamped by
	the system the first time it sees the record complete and is never moved back, so an agreement finished
	late cannot be made to look early by editing it afterwards."""

	def validate(self):
		if self.signed_on and getdate(self.signed_on) > getdate(nowdate()):
			frappe.throw(_("Signed On cannot be in the future."))
		self._check_one_main_contract()
		self.complete = int(
			bool(self.signed_on and self.agreement_document and (self.payment_terms or "").strip())
			and all(cint(self.get(f)) for f in CHECKS)
		)
		# The stamp is the system's, whatever the form or an API call sends: kept from the saved record while
		# the agreement stays complete, set to today when it becomes complete, cleared when it stops being so.
		before = self.get_doc_before_save()
		was = before.completed_on if (before and cint(before.complete)) else None
		self.completed_on = (was or nowdate()) if self.complete else None
		self.in_time = int(bool(self.complete and getdate(self.completed_on) <= getdate(self.work_start_date)))
		self.status = "Complete" if self.complete else ("Signed, Incomplete" if self.signed_on else "Not Signed")

	def _check_one_main_contract(self):
		if self.agreement_type != "Main Contract":
			return
		other = frappe.db.get_value(
			DOCTYPE, {"project": self.project, "agreement_type": "Main Contract", "name": ["!=", self.name or ""]}, "name"
		)
		if other:
			frappe.throw(_("Project {0} already has its main contract agreement: {1}.").format(self.project, other))


def on_project_insert(doc, method=None):
	"""A new project gets its (unsigned) main contract agreement, so that a project started without one shows
	up as missing. Only on a site that asks for it, and never in the way of saving the project."""
	try:
		if not frappe.db.exists("DocType", DOCTYPE) or not cint(frappe.db.get_single_value(SETTINGS, "track_project_agreements")):
			return
		if frappe.db.exists(DOCTYPE, {"project": doc.name, "agreement_type": "Main Contract"}):
			return
		frappe.get_doc(
			{
				"doctype": DOCTYPE,
				"project": doc.name,
				"agreement_type": "Main Contract",
				"company": doc.company,
				"party_name": doc.get("customer") or "",
				"work_start_date": doc.get("expected_start_date") or nowdate(),
				"auto_created": 1,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Project Agreement not made for a new project", message=frappe.get_traceback())
