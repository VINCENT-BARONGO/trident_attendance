"""Project Variation: one change to a project's scope, from the client's instruction to the valuation it is
claimed in. Trident's valuations are the project's Delivery Notes (its certificates), so a variation is claimed
on a Delivery Note of the same project."""

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from trident_attendance.tests import helpers
from trident_attendance.trident_attendance.doctype.project_variation import project_variation as pv

TODAY = nowdate()


def company():
	return frappe.db.get_value("Company", {}, "name")


def make_project(name="Tok Towers"):
	return frappe.get_doc({"doctype": "Project", "project_name": name, "company": company()}).insert(ignore_permissions=True)


def make_variation(project, **fields):
	doc = {"doctype": "Project Variation", "project": project, "variation_no": 1, "description": "Extra floor drains",
	       "instructed_on": add_days(TODAY, -10), "company": company()}
	doc.update(fields)
	d = frappe.get_doc(doc)
	d.flags.ignore_links = True                     # the valuations below are not real Delivery Notes
	return d.insert(ignore_permissions=True)


def valuations(by_name):
	"""frappe.db.get_value with Delivery Notes answered from ``by_name`` {name: project}; anything else as usual."""
	real = frappe.db.get_value

	def get_value(doctype, *args, **kw):
		if doctype == "Delivery Note":
			return by_name.get(args[0] if args else kw.get("filters"))
		return real(doctype, *args, **kw)
	return mock.patch.object(pv.frappe.db, "get_value", side_effect=get_value)


class TestProjectVariation(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		self.project = make_project().name

	def test_a_variation_moves_from_raised_to_priced_to_approved_to_claimed(self):
		variation = make_variation(self.project)
		self.assertEqual((variation.status, variation.days_to_price), ("Raised", None))
		variation.update({"priced_on": add_days(TODAY, -6), "amount_priced": 250_000})
		variation.save()
		self.assertEqual((variation.status, variation.days_to_price, variation.priced_by), ("Priced", 4, "Administrator"))
		variation.update({"approved_on": add_days(TODAY, -1), "amount_approved": 200_000})
		variation.save()
		self.assertEqual(variation.status, "Approved")
		with valuations({"MAT-DN-0001": self.project}):
			variation.valuation = "MAT-DN-0001"
			variation.flags.ignore_links = True
			variation.save()
		self.assertEqual(variation.status, "Invoiced")

	def test_dates_and_amounts_are_kept_honest(self):
		with self.assertRaises(frappe.ValidationError):                              # instructed in the future
			make_variation(self.project, instructed_on=add_days(TODAY, 1))
		with self.assertRaises(frappe.ValidationError):                              # priced before it was instructed
			make_variation(self.project, priced_on=add_days(TODAY, -20), amount_priced=1)
		with self.assertRaises(frappe.ValidationError):                              # priced with no amount
			make_variation(self.project, priced_on=add_days(TODAY, -2))
		with self.assertRaises(frappe.ValidationError):                              # approved before it was priced
			make_variation(self.project, approved_on=add_days(TODAY, -1), amount_approved=1)
		make_variation(self.project)
		with self.assertRaises(frappe.ValidationError):                              # the same variation twice
			make_variation(self.project)
		other = make_project("Tok Plaza").name
		with valuations({"MAT-DN-0002": other}), self.assertRaises(frappe.ValidationError):
			make_variation(self.project, variation_no=2, valuation="MAT-DN-0002")    # another project's valuation
