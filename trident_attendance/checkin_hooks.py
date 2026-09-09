"""Server-side staging gate for Employee Checkin.

Every punch is held for review on arrival, whatever created it: the Android app, the
ZKTeco sync tool, a manual Desk entry or a data import.

Enforcing it here rather than in each client is the whole point. A client-side flag
means staging holds only while every client remembers to set it -- and a sync tool
that gets updated, reconfigured or replaced would silently start feeding unreviewed
punches straight into Attendance, with nothing to notice it had happened.
"""

import frappe

# hrms' get_employee_checkins() filters on skip_auto_attendance = 0, so a held punch
# is invisible to attendance processing until a reviewer releases it.
HOLD_NEW_CHECKINS = True


def hold_for_review(doc, method=None):
	"""before_insert on Employee Checkin: stage the punch unless already staged."""
	if not HOLD_NEW_CHECKINS:
		return

	if doc.skip_auto_attendance:
		return

	# Releasing happens on existing rows, so this only ever touches genuinely new punches.
	doc.skip_auto_attendance = 1
	frappe.logger("trident_attendance").debug(
		f"Held new check-in for review: employee={doc.employee} time={doc.time} device={doc.device_id}"
	)
