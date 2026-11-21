from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from trident_attendance import letters
from trident_attendance.letters import (
	DOCTYPE,
	employee_letters,
	employees_with_emails,
	get_my_letter,
	get_my_letter_file,
	letters_from_communication,
	list_my_letters,
	send_to_app,
)
from trident_attendance.tests import helpers


def make_file(name="warning.pdf", content=b"%PDF-1.4 tok test letter", attached_to=None):
	doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": name,
			"content": content,
			"is_private": 1,
			**(
				{"attached_to_doctype": attached_to[0], "attached_to_name": attached_to[1]}
				if attached_to
				else {}
			),
		}
	)
	doc.flags.ignore_permissions = True
	return doc.insert()


def make_email(
	reference_doctype,
	reference_name,
	subject="Tok Test warning letter",
	recipients="x@tok-test.example",
	sent="Sent",
):
	"""A Communication as the Email button leaves it (not sent: the tests never mail)."""
	doc = frappe.get_doc(
		{
			"doctype": "Communication",
			"communication_type": "Communication",
			"communication_medium": "Email",
			"sent_or_received": sent,
			"subject": subject,
			"content": "<p>Dear colleague, this is a warning.</p><script>alert(1)</script>",
			"recipients": recipients,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		}
	)
	doc.flags.ignore_permissions = True
	with patch("frappe.enqueue") as queued:
		doc.insert()
	return doc, queued


class LettersCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(helpers.reset)
		frappe.set_user("Administrator")
		helpers.set_settings(push_enabled=0)
		emp, self.user = helpers.make_self_service_employee("Tokletter")
		self.employee = emp.name
		other, self.other_user = helpers.make_self_service_employee("Tokcolleague")
		self.other = other.name
		self.hr = helpers.make_user(f"tokhr@{helpers.TEST_DOMAIN}", roles=["HR User"]).name
		self.pushes = []
		patcher = patch(
			"trident_attendance.push.notify_user",
			lambda user, title, body, url, kind: self.pushes.append((user, title, body, url, kind)) or 2,
		)
		patcher.start()
		self.addCleanup(patcher.stop)

	def letters_of(self, employee):
		return frappe.get_all(
			DOCTYPE,
			filters={"employee": employee},
			fields=["name", "subject", "source", "communication", "phones_notified", "message"],
		)


class TestFromEmail(LettersCase):
	def test_an_email_from_the_employee_record_becomes_a_letter(self):
		comm, queued = make_email("Employee", self.employee)
		self.assertEqual(queued.call_args.kwargs["communication"], comm.name)
		self.assertTrue(queued.call_args.kwargs["enqueue_after_commit"])
		make_file(attached_to=("Communication", comm.name))

		letters_from_communication(comm.name)

		rows = self.letters_of(self.employee)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(
			(row.subject, row.source, row.communication, row.phones_notified),
			("Tok Test warning letter", "Email", comm.name, 2),
		)
		self.assertNotIn("<script", row.message)
		files = frappe.get_all(
			"File",
			filters={"attached_to_doctype": DOCTYPE, "attached_to_name": row.name},
			fields=["file_name", "is_private"],
		)
		self.assertEqual([(f.file_name, f.is_private) for f in files], [("warning.pdf", 1)])
		self.assertEqual(
			self.pushes,
			[
				(
					self.user,
					"New letter from HR",
					"Tok Test warning letter",
					f"/ess/letters/{row.name}",
					"letter",
				)
			],
		)

	def test_the_same_email_twice_is_one_letter(self):
		comm, _ = make_email("Employee", self.employee)
		letters_from_communication(comm.name)
		letters_from_communication(comm.name)
		self.assertEqual(len(self.letters_of(self.employee)), 1)

	def test_other_mail_never_reaches_the_app(self):
		customer = frappe.db.get_value("Customer", {}, "name")
		if customer:
			comm, queued = make_email("Customer", customer)
			queued.assert_not_called()
			letters_from_communication(comm.name)
		received, queued = make_email("Employee", self.employee, sent="Received")
		queued.assert_not_called()
		letters_from_communication(received.name)
		self.assertEqual(self.letters_of(self.employee), [])

	def test_official_letter_recipients_are_matched_by_email(self):
		frappe.db.set_value("Employee", self.employee, "personal_email", "tokletter.home@tok-test.example")
		found = employees_with_emails(
			letters._addresses("Tok <TOKLETTER.home@tok-test.example>, nobody@tok-test.example")
		)
		self.assertEqual(found, [self.employee])
		self.assertEqual(employees_with_emails([]), [])


class TestSendToApp(LettersCase):
	def test_hr_sends_to_the_app_only(self):
		upload = make_file("contract.pdf")
		frappe.set_user(self.hr)
		result = send_to_app(
			self.employee, "Tok Test notice", "<p>Read this.</p>", [upload.file_url], also_email=0
		)
		self.assertTrue(result["ok"])
		frappe.set_user("Administrator")
		letter = frappe.get_doc(DOCTYPE, result["letter"])
		self.assertEqual(
			(letter.source, letter.sent_by, letter.employee), ("Sent to app", self.hr, self.employee)
		)
		self.assertEqual(
			frappe.db.count("File", {"attached_to_doctype": DOCTYPE, "attached_to_name": letter.name}), 1
		)

	def test_only_hr_may_send(self):
		frappe.set_user(self.user)
		with self.assertRaises(frappe.PermissionError):
			send_to_app(self.other, "Tok Test", "<p>x</p>")
		with self.assertRaises(frappe.PermissionError):
			employee_letters(self.other)

	def test_refusals(self):
		frappe.set_user(self.hr)
		self.assertEqual(send_to_app(self.employee, "  ", "<p>x</p>")["reason"], "no_subject")
		self.assertEqual(send_to_app(self.employee, "Tok", "")["reason"], "no_message")
		self.assertEqual(send_to_app("NOPE-0000", "Tok", "x")["reason"], "no_employee")
		# A tok-test user with no other address: nowhere to email it.
		self.assertEqual(send_to_app(self.employee, "Tok", "<p>x</p>", also_email=1)["reason"], "no_email")

	def test_also_email_goes_through_the_email(self):
		frappe.db.set_value("Employee", self.employee, "prefered_email", "tokletter.pref@tok-test.example")
		frappe.set_user(self.hr)
		with patch("frappe.core.doctype.communication.email.make", return_value={"name": "COMM-TOK"}) as made:
			result = send_to_app(self.employee, "Tok Test both", "<p>x</p>", also_email=1)
		self.assertEqual(
			result, {"ok": True, "emailed": "tokletter.pref@tok-test.example", "communication": "COMM-TOK"}
		)
		self.assertEqual(made.call_args.kwargs["doctype"], "Employee")
		self.assertEqual(made.call_args.kwargs["name"], self.employee)
		self.assertEqual(self.letters_of(self.employee), [])  # made from that email, not twice

	def test_the_form_sees_whether_it_was_opened(self):
		letter = letters.make_letter(self.employee, "Tok Test opened", "<p>x</p>", "Sent to app")
		frappe.set_user(self.hr)
		self.assertIsNone(employee_letters(self.employee)["letters"][0]["opened_at"])
		frappe.set_user(self.user)
		get_my_letter(letter)
		frappe.set_user(self.hr)
		shown = employee_letters(self.employee)
		self.assertEqual(shown["letters"][0]["name"], letter)
		self.assertIsNotNone(shown["letters"][0]["opened_at"])
		self.assertEqual(shown["letters"][0]["times_opened"], 1)


class TestMyLetters(LettersCase):
	def test_the_person_reads_only_their_own(self):
		mine = letters.make_letter(self.employee, "Tok Test mine", "<p>Mine</p>", "Sent to app")
		theirs = letters.make_letter(self.other, "Tok Test theirs", "<p>Theirs</p>", "Sent to app")
		frappe.set_user(self.user)
		self.assertFalse(frappe.has_permission(DOCTYPE, "read"))
		listed = list_my_letters()
		self.assertEqual([i["name"] for i in listed["items"]], [mine])
		self.assertEqual(listed["unread"], 1)
		self.assertEqual(get_my_letter(theirs)["reason"], "not_found")
		self.assertEqual(get_my_letter("LTR-NOPE")["reason"], "not_found")

	def test_opening_is_recorded(self):
		name = letters.make_letter(self.employee, "Tok Test open", "<p>Hello</p>", "Sent to app")
		frappe.set_user(self.user)
		got = get_my_letter(name)
		self.assertTrue(got["ok"])
		self.assertIn("Hello", got["html"])
		get_my_letter(name)
		self.assertEqual(list_my_letters()["unread"], 0)
		self.assertTrue(list_my_letters()["items"][0]["opened"])
		frappe.set_user("Administrator")
		row = frappe.db.get_value(
			DOCTYPE, name, ["opened_at", "last_opened_at", "times_opened"], as_dict=True
		)
		self.assertEqual(row.times_opened, 2)
		self.assertLessEqual(row.opened_at, row.last_opened_at)

	def test_files_are_served_to_the_owner_only(self):
		upload = make_file("letter.pdf")
		name = letters.make_letter(
			self.employee,
			"Tok Test file",
			"",
			"Sent to app",
			files=[{"file_url": upload.file_url, "file_name": "letter.pdf"}],
		)
		frappe.set_user(self.user)
		got = get_my_letter(name)
		self.assertEqual([f["file_name"] for f in got["files"]], ["letter.pdf"])
		get_my_letter_file(name, got["files"][0]["id"])
		self.assertEqual(frappe.local.response.filecontent, b"%PDF-1.4 tok test letter")
		self.assertEqual(frappe.session.user, self.user)
		frappe.set_user(self.other_user)
		with self.assertRaises(frappe.DoesNotExistError):
			get_my_letter_file(name, got["files"][0]["id"])
		frappe.set_user(self.user)
		with self.assertRaises(frappe.DoesNotExistError):
			get_my_letter_file(name, upload.name)  # a file not on the letter

	def test_no_employee_record_lists_nothing(self):
		lonely = helpers.make_user(f"toklonely@{helpers.TEST_DOMAIN}").name
		frappe.set_user(lonely)
		self.assertEqual(list_my_letters(), {"ok": True, "items": [], "unread": 0})
