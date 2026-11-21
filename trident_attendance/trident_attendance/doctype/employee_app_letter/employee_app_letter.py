# The copy of an HR letter shown in the employee app (trident_attendance.letters). Rows are made
# by the app itself: from an email sent from the Employee record or an Official Letter, or from
# Employee > Send to app.

from frappe.model.document import Document


class EmployeeAppLetter(Document):
	pass
