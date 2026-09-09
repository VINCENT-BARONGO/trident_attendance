"""Employee Checkin doc events: stage app punches and annotate them with hold reasons.

Punches from the mobile app never enter hrms auto-attendance: `skip_auto_attendance` stays 1
for their whole life and this app links them to Attendance itself (see tasks.mark_day). The
review state lives in `custom_review_status`.

Nothing here raises for a rule. Rejecting a request parks the punch as FAILED on the handset,
which never retries it, so a doubtful punch is held with a reason instead.
"""

from trident_attendance.checkin_rules import (
	DAY_PREFIXES,
	FACE_PREFIX,
	evaluate_face,
	evaluate_instant,
	refresh_project_reasons,
	strip_prefixes,
)
from trident_attendance.utils import STATUS_PENDING, get_settings, in_scope, join_reasons, split_reasons


def before_insert(doc, method=None):
	settings = get_settings()
	if not in_scope(doc, settings):
		return
	doc.skip_auto_attendance = 1
	doc.custom_review_status = STATUS_PENDING
	doc.custom_hold_reasons = None
	doc.custom_reviewed_by = None
	doc.custom_reviewed_on = None


def validate(doc, method=None):
	"""Runs after hrms's own validate. Idempotent across the app's follow-up PUTs."""
	settings = get_settings()
	if not in_scope(doc, settings):
		return

	if doc.is_new():
		doc.custom_hold_reasons = join_reasons(evaluate_instant(doc, settings))
		return

	if doc.custom_review_status != STATUS_PENDING:
		return
	if doc.has_value_changed("time") or doc.has_value_changed("log_type"):
		# Day-level reasons (pairing, supervisor order) are stale now; the next evaluation rebuilds them.
		doc.custom_hold_reasons = join_reasons(strip_prefixes(split_reasons(doc.custom_hold_reasons), DAY_PREFIXES))
	if doc.has_value_changed("custom_site_project"):
		doc.custom_hold_reasons = refresh_project_reasons(doc, settings)
	if doc.has_value_changed("custom_face_match_result"):
		kept = strip_prefixes(split_reasons(doc.custom_hold_reasons), (FACE_PREFIX,))
		doc.custom_hold_reasons = join_reasons(kept + evaluate_face(doc) + list(doc.flags.trident_reasons or []))
