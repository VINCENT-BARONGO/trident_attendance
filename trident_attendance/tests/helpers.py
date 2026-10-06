"""Records the employee-access tests build. Everything is made inside the test's transaction
and rolled back with it."""

import frappe

from trident_attendance.employee_access import APP_ACCESS_FIELD
from trident_attendance.utils import SETTINGS_DOCTYPE, employee_id_field

TEST_DOMAIN = "tok-test.example"


def _jpeg() -> bytes:
	"""A real image: Frappe opens every uploaded JPEG to strip its EXIF data."""
	import io

	from PIL import Image

	out = io.BytesIO()
	Image.new("RGB", (8, 8), (120, 90, 60)).save(out, format="JPEG")
	return out.getvalue()


JPEG = _jpeg()


def make_user(email, roles=()):
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"send_welcome_email": 0,
			"roles": [{"role": r} for r in roles],
		}
	)
	user.flags.no_welcome_mail = True
	return user.insert(ignore_permissions=True)


def make_employee(first_name, id_number=None, user=None, app_access=1, status="Active"):
	doc = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": first_name,
			"last_name": "Tok Test",
			"gender": frappe.db.get_value("Gender", {}, "name"),
			"date_of_birth": "1990-01-01",
			"date_of_joining": "2020-01-01",
			"company": frappe.db.get_value("Company", {}, "name"),
			"status": "Active",
			"user_id": user,
			employee_id_field(): id_number,
			APP_ACCESS_FIELD: app_access,
		}
	)
	doc.insert(ignore_permissions=True)
	if status != "Active":
		# Straight to the row: leaving properly would also disable the user, which is another test.
		frappe.db.set_value("Employee", doc.name, "status", status)
	return doc


def make_self_service_employee(first_name, id_number=None, **kwargs):
	"""An employee linked to a user holding only the self-service roles. The link comes first:
	ERPNext takes Employee Self Service off a user no Employee points at."""
	user = make_user(f"{first_name.lower()}@{TEST_DOMAIN}")
	employee = make_employee(first_name, id_number=id_number, user=user.name, **kwargs)
	user.reload()
	user.add_roles("Employee Self Service")
	return employee, user.name


def make_oauth_client(scopes="all openid"):
	return frappe.get_doc(
		{
			"doctype": "OAuth Client",
			"app_name": "tok-test-client",
			"scopes": scopes,
			"redirect_uris": "http://localhost/callback",
			"default_redirect_uri": "http://localhost/callback",
			"grant_type": "Authorization Code",
			"response_type": "Code",
			"skip_authorization": 1,
		}
	).insert(ignore_permissions=True)


def set_settings(**values):
	for field, value in values.items():
		frappe.db.set_single_value(SETTINGS_DOCTYPE, field, value)
	frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)


def attach_photo(employee):
	file = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"tok_test_{employee}.jpg",
			"attached_to_doctype": "Employee",
			"attached_to_name": employee,
			"attached_to_field": "image",
			"is_private": 1,
			"content": JPEG,
		}
	).insert(ignore_permissions=True)
	frappe.db.set_value("Employee", employee, "image", file.file_url)
	return file


def reset():
	"""Undo a test: back to Administrator, nothing kept, no test settings left in the cache."""
	frappe.set_user("Administrator")
	frappe.db.rollback()
	frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
