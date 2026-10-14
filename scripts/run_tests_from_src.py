"""Runs this checkout's tests on a bench whose installed copy of the app is another branch.

    cd <bench>/sites && ../env/bin/python <this checkout>/scripts/run_tests_from_src.py [--keep] [site] [test ...]

The checkout goes first on sys.path, so `trident_attendance` is this code, not the bench's. The
site may lack what the tests need (the settings doctype as it is here, the Employee App Sign In,
App Device, Shift Signal and Location Request doctypes, this checkout's custom fields); they are
put in place first and taken away again afterwards unless --keep is given. A `test` is a dotted
name under `trident_attendance.tests.` (a module, a class or one test); without any, every module in TESTS runs. On a site
where this branch is installed, `bench --site <site> run-tests --app trident_attendance` does.
"""

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import frappe

SETTINGS = "Trident Attendance Settings"
SETTINGS_JSON = "trident_attendance/trident_attendance/doctype/trident_attendance_settings/trident_attendance_settings.json"
# This checkout's own doctypes: imported from these files, dropped afterwards when the site
# did not have them.
DOCTYPES = {
	"Employee App Sign In": "trident_attendance/trident_attendance/doctype/employee_app_sign_in/employee_app_sign_in.json",
	"App Device": "trident_attendance/trident_attendance/doctype/app_device/app_device.json",
	"Shift Signal": "trident_attendance/trident_attendance/doctype/shift_signal/shift_signal.json",
	"Location Request": "trident_attendance/trident_attendance/doctype/location_request/location_request.json",
}
# Custom fields the tests need, from the fixture; dropped afterwards when the site lacked them.
FIELDS = [
	"Employee-custom_app_access",
	"Newsletter-custom_app_announcement",
	"Newsletter-custom_app_announced_at",
	"Leave Application-custom_app_notified_status",
]
TESTS = [
	"trident_attendance.tests.test_employee_access",
	"trident_attendance.tests.test_employee_pin",
	"trident_attendance.tests.test_provision",
	"trident_attendance.tests.test_announcements",
	"trident_attendance.tests.test_push",
	"trident_attendance.tests.test_own_records",
	"trident_attendance.tests.test_shift",
	"trident_attendance.tests.test_locate",
]


def pin_rows() -> int:
	"""App PINs and rows of this checkout's doctypes on the site. The tests must leave as many
	as they found."""
	count = frappe.db.count("__Auth", {"doctype": "Employee", "fieldname": "app_pin"})
	for doctype in DOCTYPES:
		if frappe.db.table_exists(doctype):
			count += frappe.db.count(doctype)
	return count


def prepare() -> dict:
	from frappe.modules.import_file import import_file_by_path

	state = {
		"had_settings": bool(frappe.db.exists("DocType", SETTINGS)),
		"had_doctypes": {dt: bool(frappe.db.exists("DocType", dt)) for dt in DOCTYPES},
		"had_fields": {name: bool(frappe.db.exists("Custom Field", name)) for name in FIELDS},
		"pin_rows": pin_rows(),
	}
	import_file_by_path(str(ROOT / SETTINGS_JSON), force=True)
	for path in DOCTYPES.values():
		import_file_by_path(str(ROOT / path), force=True)
	fixture = json.loads((ROOT / "trident_attendance/fixtures/custom_field.json").read_text(encoding="utf-8"))
	for name, had in state["had_fields"].items():
		if not had:
			field = next(f for f in fixture if f["name"] == name)
			frappe.get_doc(field).insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache()
	# The imports above ran with the hooks of the bench's copy. This checkout's must be read.
	if hasattr(frappe.local, "doc_events_hooks"):
		del frappe.local.doc_events_hooks
	return state


def restore(state):
	from frappe.modules.import_file import import_file_by_path

	frappe.set_user("Administrator")
	frappe.db.rollback()
	installed = Path(frappe.utils.get_bench_path()) / "apps" / "trident_attendance" / SETTINGS_JSON
	if state["had_settings"] and installed.exists():
		import_file_by_path(str(installed), force=True)
	elif not state["had_settings"]:
		frappe.flags.in_migrate = True  # a standard doctype is otherwise only deletable in developer mode
		frappe.delete_doc("DocType", SETTINGS, force=True, ignore_missing=True)
		frappe.flags.in_migrate = False
		frappe.db.delete("Singles", {"doctype": SETTINGS})
	for doctype, had in state["had_doctypes"].items():
		if had:
			continue
		frappe.flags.in_migrate = True
		frappe.delete_doc("DocType", doctype, force=True, ignore_missing=True)
		frappe.flags.in_migrate = False
		frappe.db.commit()
		frappe.db.sql_ddl(f"drop table if exists `tab{doctype}`")
	for name, had in state["had_fields"].items():
		if had:
			continue
		doctype, fieldname = name.split("-", 1)
		frappe.delete_doc("Custom Field", name, force=True, ignore_missing=True)
		frappe.db.commit()
		if frappe.db.has_column(doctype, fieldname):
			frappe.db.sql_ddl(f"alter table `tab{doctype}` drop column `{fieldname}`")
	frappe.db.commit()
	frappe.clear_cache()


def main():
	args = [a for a in sys.argv[1:] if a != "--keep"]
	keep = "--keep" in sys.argv
	tests = [a for a in args if a.startswith("trident_attendance.tests.")] or TESTS
	args = [a for a in args if a not in tests]
	frappe.init(site=args[0] if args else "attendance.localhost", sites_path=".")
	frappe.connect()
	import trident_attendance

	if not Path(trident_attendance.__file__).resolve().is_relative_to(ROOT):
		sys.exit(f"trident_attendance resolved to {trident_attendance.__file__}, not {ROOT}")
	print(f"Testing {trident_attendance.__file__} on {frappe.local.site}")

	frappe.flags.in_test = True
	state = prepare()
	try:
		suite = unittest.defaultTestLoader.loadTestsFromNames(tests)
		result = unittest.TextTestRunner(verbosity=2).run(suite)
	finally:
		if keep:
			frappe.db.rollback()
			frappe.clear_cache()
		else:
			restore(state)
		left = frappe.get_all("User", filters={"name": ["like", "%tok-test.example"]}, pluck="name")
		left += frappe.get_all("Employee", filters={"last_name": "Tok Test"}, pluck="name")
		left += frappe.get_all("Newsletter", filters={"subject": ["like", "Tok Test%"]}, pluck="name")
		if pin_rows() != state["pin_rows"]:
			left.append(f"{pin_rows() - state['pin_rows']} app PIN row(s) or row(s) of {', '.join(DOCTYPES)}")
		print(f"Test records left on the site: {left or 'none'}")
		frappe.destroy()
	sys.exit(0 if result.wasSuccessful() and not left else 1)


if __name__ == "__main__":
	main()
