import frappe
from frappe import _
from frappe.model.document import Document


class AppDevice(Document):
	"""One row per phone the employee app registered for push notifications. Written by
	`trident_attendance.push` (register / unregister, and the sender's answers); only a
	System Manager can see the rows."""

	def validate(self):
		self.token = (self.token or "").strip()
		if not 20 <= len(self.token) <= 512:
			frappe.throw(_("The device token must be 20 to 512 characters long."))
		if self.platform not in ("android", "ios"):
			frappe.throw(_("Platform must be android or ios."))
