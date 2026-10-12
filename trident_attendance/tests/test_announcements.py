from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_url, now_datetime

from trident_attendance import announcements
from trident_attendance.announcements import (
	ANNOUNCED_AT_FIELD,
	ANNOUNCEMENT_FIELD,
	get_announcement,
	list_announcements,
	summary,
)
from trident_attendance.tests import helpers

SUBJECT = "Tok Test announcement"


def make_newsletter(subject=SUBJECT, content_type="Rich Text", message="<p>Hello</p>", app=1, **fields):
	"""A Newsletter as HR writes one, ticked for the app unless `app` is 0. Inserted as
	Administrator; the test's transaction takes it away."""
	content = {"Rich Text": "message", "Markdown": "message_md", "HTML": "message_html"}[content_type]
	doc = frappe.get_doc(
		{
			"doctype": "Newsletter",
			"subject": subject,
			"sender_name": "HR",
			"sender_email": f"hr@{helpers.TEST_DOMAIN}",
			"content_type": content_type,
			content: message,
			ANNOUNCEMENT_FIELD: app,
			**fields,
		}
	)
	doc.flags.ignore_permissions = True
	# Newsletter wants an audience (an Email Group); the tests' newsletters are never sent.
	doc.flags.ignore_mandatory = True
	doc.insert()
	return doc


class AnnouncementsCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		helpers.set_settings(push_enabled=0)
		self.employee, self.user = helpers.make_self_service_employee("Tokone")
		self.employee = self.employee.name

	def as_employee(self):
		frappe.set_user(self.user)


class TestListAnnouncements(AnnouncementsCase):
	def test_only_ticked_newsletters_are_listed(self):
		shown = make_newsletter("Tok Test shown")
		make_newsletter("Tok Test hidden", app=0)
		self.as_employee()
		result = list_announcements()
		self.assertTrue(result["ok"])
		names = [item["name"] for item in result["items"]]
		self.assertIn(shown.name, names)
		self.assertNotIn("Tok Test hidden", [item["subject"] for item in result["items"]])
		self.assertEqual(
			set(result["items"][names.index(shown.name)]), {"name", "subject", "shown_at", "summary"}
		)

	def test_the_box_is_the_permission(self):
		"""An employee's user has no permission on Newsletter and still reads the announcement."""
		doc = make_newsletter()
		self.as_employee()
		self.assertFalse(frappe.has_permission("Newsletter", "read"))
		self.assertIn(doc.name, [item["name"] for item in list_announcements()["items"]])
		self.assertTrue(get_announcement(doc.name)["ok"])

	def test_newest_first(self):
		older, newer, newest = (make_newsletter(f"Tok Test {n}") for n in ("older", "newer", "newest"))
		base = now_datetime()
		for doc, ago in ((older, 30), (newer, 20), (newest, 10)):
			frappe.db.set_value("Newsletter", doc.name, ANNOUNCED_AT_FIELD, base - timedelta(minutes=ago))
		self.as_employee()
		names = [item["name"] for item in list_announcements()["items"]]
		self.assertEqual(
			[n for n in names if n in (older.name, newer.name, newest.name)], [newest.name, newer.name, older.name]
		)

	def test_shown_at_is_when_the_box_was_ticked(self):
		doc = make_newsletter()
		announced_at = frappe.db.get_value("Newsletter", doc.name, ANNOUNCED_AT_FIELD)
		self.assertTrue(announced_at)
		self.as_employee()
		item = next(i for i in list_announcements()["items"] if i["name"] == doc.name)
		self.assertEqual(item["shown_at"], announced_at)
		self.assertEqual(get_announcement(doc.name)["shown_at"], announced_at)
		# Ticked some other way, without the time: the newsletter's modified time stands in.
		frappe.db.set_value("Newsletter", doc.name, ANNOUNCED_AT_FIELD, None)
		self.assertEqual(get_announcement(doc.name)["shown_at"], frappe.db.get_value("Newsletter", doc.name, "modified"))

	def test_limit_and_offset(self):
		docs = [make_newsletter(f"Tok Test {n}") for n in range(3)]
		base = now_datetime()
		for n, doc in enumerate(docs):
			frappe.db.set_value("Newsletter", doc.name, ANNOUNCED_AT_FIELD, base + timedelta(minutes=n))
		self.as_employee()
		first_two = [item["name"] for item in list_announcements(limit=2)["items"]]
		self.assertEqual(first_two, [docs[2].name, docs[1].name])
		self.assertEqual([item["name"] for item in list_announcements(limit=2, offset=2)["items"]][:1], [docs[0].name])
		self.assertEqual([item["name"] for item in list_announcements(limit="1", offset="1")["items"]], [docs[1].name])

	def test_limit_is_capped(self):
		self.as_employee()
		def last_query(get_all):
			return [c for c in get_all.call_args_list if c.args[:1] == ("Newsletter",)][-1].kwargs

		with patch("frappe.get_all", wraps=frappe.get_all) as get_all:
			list_announcements(limit=5000)
			self.assertEqual(last_query(get_all)["limit_page_length"], announcements.MAX_LIMIT)
			list_announcements(limit=0, offset=-5)
			self.assertEqual(last_query(get_all)["limit_page_length"], announcements.DEFAULT_LIMIT)
			self.assertEqual(last_query(get_all)["limit_start"], 0)

	def test_summary_is_plain_text(self):
		long_text = " ".join(f"word{n}" for n in range(60))
		doc = make_newsletter(message=f"<h1>Title &amp; more</h1><p>{long_text}</p>")
		self.as_employee()
		item = next(i for i in list_announcements()["items"] if i["name"] == doc.name)
		self.assertTrue(item["summary"].startswith("Title & more word0 word1"))
		self.assertNotIn("<", item["summary"])
		self.assertLessEqual(len(item["summary"]), announcements.SUMMARY_LENGTH + 1)
		self.assertTrue(item["summary"].endswith("…"))

	def test_summary_rules(self):
		self.assertEqual(summary("<p>Short &lt;3</p>"), "Short <3")
		self.assertEqual(summary("<p>  spaced\n\n out  </p>"), "spaced out")
		self.assertEqual(summary("a" * 200), "a" * 160 + "…")
		cut = summary("word " * 100)
		self.assertTrue(cut.endswith("word…"), cut)
		self.assertLessEqual(len(cut), 161)


class TestGetAnnouncement(AnnouncementsCase):
	def test_rich_text_is_kept_as_it_is(self):
		doc = make_newsletter(message="<p>Hello <b>there</b></p>")
		self.as_employee()
		result = get_announcement(doc.name)
		self.assertEqual(result["ok"], True)
		self.assertEqual(result["name"], doc.name)
		self.assertEqual(result["subject"], SUBJECT)
		self.assertEqual(result["html"], "<p>Hello <b>there</b></p>")
		self.assertEqual(set(result), {"ok", "name", "subject", "shown_at", "html"})

	def test_markdown_is_rendered(self):
		doc = make_newsletter(content_type="Markdown", message="# Heading\n\nSome **bold** text.")
		self.as_employee()
		html = get_announcement(doc.name)["html"]
		self.assertIn("<h1", html)
		self.assertIn("<strong>bold</strong>", html)
		self.assertNotIn("**", html)

	def test_html_is_kept_as_it_is(self):
		doc = make_newsletter(content_type="HTML", message='<div class="x"><p>Raw <em>html</em></p></div>')
		self.as_employee()
		self.assertEqual(get_announcement(doc.name)["html"], '<div class="x"><p>Raw <em>html</em></p></div>')

	def test_scripts_and_handlers_are_stripped(self):
		for content_type, message in (
			("HTML", '<p>Hi</p><script>alert(1)</script><img src="x" onerror="alert(2)"><a href="javascript:alert(3)">x</a>'),
			("Rich Text", '<p onclick="alert(4)">Hi</p><script src="https://evil.example/x.js"></script>'),
			("Markdown", 'Hi\n\n<script>alert(5)</script>\n\n<img src="x" onerror="alert(6)">'),
		):
			doc = make_newsletter(f"Tok Test {content_type}", content_type=content_type, message=message)
			self.as_employee()
			html = get_announcement(doc.name)["html"]
			frappe.set_user("Administrator")
			self.assertNotIn("<script", html, content_type)
			self.assertNotIn("onerror", html, content_type)
			self.assertNotIn("onclick", html, content_type)
			self.assertNotIn("javascript:", html, content_type)
			self.assertIn("Hi", html, content_type)

	def test_file_links_are_made_absolute(self):
		doc = make_newsletter(
			content_type="HTML",
			message=(
				'<a href="/files/memo.pdf">memo</a> <img src=\'/private/files/photo.jpg\'> '
				'<a href="https://example.com/files/x">keep</a> <a href="/app/newsletter">desk</a>'
			),
		)
		self.as_employee()
		html = get_announcement(doc.name)["html"]
		base = get_url().rstrip("/")
		self.assertIn(f'href="{base}/files/memo.pdf"', html)
		self.assertIn(f'src="{base}/private/files/photo.jpg"', html)
		self.assertIn('href="https://example.com/files/x"', html)
		self.assertIn('href="/app/newsletter"', html)

	def test_not_found(self):
		hidden = make_newsletter("Tok Test hidden", app=0)
		self.as_employee()
		for name in (hidden.name, "no-such-newsletter", "", None):
			result = get_announcement(name)
			self.assertEqual(result, {"ok": False, "reason": "not_found", "message": result["message"]}, name)
			self.assertNotIn("html", result)


class TestAccess(AnnouncementsCase):
	def test_guest_is_refused(self):
		doc = make_newsletter()
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, list_announcements)
		self.assertRaises(frappe.PermissionError, get_announcement, doc.name)

	def test_methods_are_whitelisted_but_not_for_guests(self):
		for method in (list_announcements, get_announcement):
			self.assertIn(method, frappe.whitelisted)
			self.assertNotIn(method, frappe.guest_methods)

	def test_any_signed_in_user_reads(self):
		doc = make_newsletter()
		frappe.set_user("Administrator")
		other = helpers.make_user(f"other@{helpers.TEST_DOMAIN}").name
		frappe.set_user(other)
		self.assertIn(doc.name, [item["name"] for item in list_announcements()["items"]])
