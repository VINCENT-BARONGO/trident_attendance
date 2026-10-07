from frappe.model.document import Document


class EmployeeAppSignIn(Document):
	"""One row per employee: the pending one-time code (a keyed hash), the wrong-PIN lock and
	when codes were sent. Written only by `trident_attendance.employee_pin`; no role can read it."""
