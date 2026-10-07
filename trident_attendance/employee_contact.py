"""Where an employee's one-time code goes and how it is sent: SMS to the mobile number on the
Employee, else e-mail to the employee's own address.

The message holds a live code, so it is sent without leaving a copy on the site: no SMS Log,
no Email Queue row, and nothing here writes it to a log.
"""

import re

import frappe
from frappe import _
from frappe.utils import validate_email_address

CHANNEL_SMS = "sms"
CHANNEL_EMAIL = "email"

EMAIL_FIELDS = ("prefered_email", "personal_email", "company_email")
CONTACT_FIELDS = ("cell_number", *EMAIL_FIELDS)

KENYA = "254"
DOT = "•"


class SendError(Exception):
	"""The code did not go out. The text never holds the message or the gateway's URL."""


def normalise_mobile(raw) -> str | None:
	"""The number as digits starting with its country code, or None when it is not one.

	A Kenyan mobile written the local way (07.., 01.., or without the 0) gets 254. Anything
	else that reads as a full international number is left as it is.
	"""
	text = re.sub(r"[\s().-]", "", str(raw or ""))
	international = text.startswith(("+", "00"))
	digits = text[1:] if text.startswith("+") else text[2:] if text.startswith("00") else text
	if not re.fullmatch(r"[0-9]+", digits):
		return None
	if not international and re.fullmatch(r"0?[17][0-9]{8}", digits):
		return KENYA + digits[-9:]
	if re.fullmatch(r"2540[17][0-9]{8}", digits):
		# +254 0712 345 678: the 0 kept after the country code.
		return KENYA + digits[-9:]
	if 8 <= len(digits) <= 15 and not digits.startswith("0"):
		return digits
	return None


def mask_mobile(number) -> str:
	if number.startswith(KENYA) and len(number) == 12:
		return f"0{number[3]}{DOT * 2} {DOT * 3} {number[-3:]}"
	return f"+{number[:3]}{DOT * 2} {DOT * 3} {number[-3:]}"


def mask_email(address) -> str:
	local, _at, domain = address.partition("@")
	return f"{local[:1]}{DOT * 3}@{domain}"


def mask(channel, address) -> str:
	return mask_mobile(address) if channel == CHANNEL_SMS else mask_email(address)


def own_email(row) -> str | None:
	"""The employee's own address. Never the invented `<employee id>@staff.<domain>` of a
	provisioned user: nobody reads that mailbox."""
	for field in EMAIL_FIELDS:
		address = validate_email_address(str(row.get(field) or "").strip())
		if address and not address.rpartition("@")[2].lower().startswith("staff."):
			return address
	return None


def sms_ready() -> bool:
	return bool(frappe.db.get_single_value("SMS Settings", "sms_gateway_url"))


def choose(row) -> tuple[str | None, str | None]:
	"""(channel, address) for this employee, or (None, None) when there is nowhere to send."""
	number = normalise_mobile(row.get("cell_number"))
	email = own_email(row)
	# With a number, no address and no gateway, the SMS is tried and fails: that is a site to
	# set up (`send_failed`), not an employee without a contact.
	if number and (sms_ready() or not email):
		return CHANNEL_SMS, number
	if email:
		return CHANNEL_EMAIL, email
	return None, None


def send_code(channel, address, code, minutes):
	"""Sends the code or raises SendError."""
	text = _("Your employee app code is {0}. It expires in {1} minutes. Do not share it.").format(code, minutes)
	try:
		if channel == CHANNEL_SMS:
			_send_sms(address, text)
		else:
			_send_email(address, text)
	except SendError:
		raise
	except Exception as e:
		# Not the error's own text: a gateway called with GET has the message in its URL.
		status = getattr(getattr(e, "response", None), "status_code", None)
		raise SendError(f"{type(e).__name__}{f' (HTTP {status})' if status else ''}") from None


def _send_sms(number, text):
	"""One message through SMS Settings, as `send_via_gateway` sends it but without its SMS Log
	row, which keeps the message text."""
	from frappe.core.doctype.sms_settings import sms_settings

	settings = frappe.get_doc("SMS Settings")
	if not (settings.sms_gateway_url and settings.message_parameter and settings.receiver_parameter):
		raise SendError("SMS Settings is not filled in")
	headers = sms_settings.get_headers(settings)
	args = {settings.message_parameter: text}
	for parameter in settings.get("parameters"):
		if not parameter.header:
			args[parameter.parameter] = parameter.value
	args[settings.receiver_parameter] = number
	status = sms_settings.send_request(
		settings.sms_gateway_url,
		args,
		headers,
		settings.use_post,
		headers.get("Content-Type") == "application/json",
	)
	if not 200 <= status < 300:
		raise SendError(f"HTTP {status}")


def _send_email(address, text):
	queued = frappe.sendmail(recipients=[address], subject=_("Your employee app code"), message=text)
	if not queued:
		raise SendError("the e-mail was not queued")
	try:
		queued.send()
		sent = frappe.db.get_value("Email Queue", queued.name, "status") == "Sent"
	finally:
		# The row keeps the message, and one left unsent would go out later with a dead code.
		frappe.db.delete("Email Queue Recipient", {"parent": queued.name})
		frappe.db.delete("Email Queue", {"name": queued.name})
	if not sent:
		raise SendError("the e-mail was not sent")
