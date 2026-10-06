"""Runs this checkout's tests on a bench whose installed copy of the app is another branch.

    cd <bench>/sites && ../env/bin/python <this checkout>/scripts/run_tests_from_src.py [--keep] [site]

The checkout goes first on sys.path, so `trident_attendance` is this code, not the bench's. The
site may lack what the tests need (the settings doctype as it is here, Employee > App Access);
both are put in place first and taken away again afterwards unless --keep is given. On a site
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
APP_ACCESS = "Employee-custom_app_access"
TESTS = ["trident_attendance.tests.test_employee_access", "trident_attendance.tests.test_provision"]


def prepare() -> dict:
	from frappe.modules.import_file import import_file_by_path

	state = {
		"had_settings": bool(frappe.db.exists("DocType", SETTINGS)),
		"had_field": bool(frappe.db.exists("Custom Field", APP_ACCESS)),
	}
	import_file_by_path(str(ROOT / SETTINGS_JSON), force=True)
	if not state["had_field"]:
		fixture = json.loads((ROOT / "trident_attendance/fixtures/custom_field.json").read_text(encoding="utf-8"))
		field = next(f for f in fixture if f["name"] == APP_ACCESS)
		frappe.get_doc(field).insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache()
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
	if not state["had_field"]:
		frappe.delete_doc("Custom Field", APP_ACCESS, force=True, ignore_missing=True)
		frappe.db.commit()
		if frappe.db.has_column("Employee", "custom_app_access"):
			frappe.db.sql_ddl("alter table `tabEmployee` drop column `custom_app_access`")
	frappe.db.commit()
	frappe.clear_cache()


def main():
	args = [a for a in sys.argv[1:] if a != "--keep"]
	keep = "--keep" in sys.argv
	frappe.init(site=args[0] if args else "attendance.localhost", sites_path=".")
	frappe.connect()
	import trident_attendance

	if not Path(trident_attendance.__file__).resolve().is_relative_to(ROOT):
		sys.exit(f"trident_attendance resolved to {trident_attendance.__file__}, not {ROOT}")
	print(f"Testing {trident_attendance.__file__} on {frappe.local.site}")

	frappe.flags.in_test = True
	state = prepare()
	try:
		suite = unittest.defaultTestLoader.loadTestsFromNames(TESTS)
		result = unittest.TextTestRunner(verbosity=2).run(suite)
	finally:
		if keep:
			frappe.db.rollback()
			frappe.clear_cache()
		else:
			restore(state)
		left = frappe.get_all("User", filters={"name": ["like", "%tok-test.example"]}, pluck="name")
		left += frappe.get_all("Employee", filters={"last_name": "Tok Test"}, pluck="name")
		print(f"Test records left on the site: {left or 'none'}")
		frappe.destroy()
	sys.exit(0 if result.wasSuccessful() and not left else 1)


if __name__ == "__main__":
	main()
