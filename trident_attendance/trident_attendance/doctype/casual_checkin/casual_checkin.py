import frappe
from frappe import _
from frappe.model.document import Document


class CasualCheckin(Document):
	"""A casual labourer's punch, created by the attendance hub through the standard resource API
	(the hub's casual_push job). Idempotent on client_uid, which the hub looks up first."""

	def validate(self):
		status = frappe.db.get_value("Casual Labourer", self.casual_labourer, "status")
		if status and status != "Active":
			# Kept, not refused: the punch happened. The flag is for whoever pays casuals.
			frappe.msgprint(
				_("{0} is marked {1}, but was clocked on site.").format(self.casual_labourer, status),
				alert=True,
			)
