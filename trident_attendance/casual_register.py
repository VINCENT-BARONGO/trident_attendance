"""The weekly casual attendance & pay register, as a PDF, inside the ERP desk.

The same sheet My HR downloads: for a Mon-Sat week, every casual on a site, grouped by site, with a
day-by-day P (present) / A (absent) grid and the pay (daily rate x days present), then site and grand
totals. A button on the Casual Attendance report calls download_weekly_register for the chosen week.

Scope matches the web-app register. HR and admins (System Manager, HR Manager, HR User, Attendance
Admin) get every site. Anyone else (a project coordinator: Casual Registrar / Casual Approver, or
Attendance Marking) gets only the sites they are assigned to (Project.allowed_users) plus any casual
they clocked in themselves (Casual Checkin.supervisor_user). The pull ignores row permissions and the
scope is enforced here in code, so a coordinator who cannot read a site's punches as themselves still
gets exactly their own sites — a coordinator on every project therefore sees everyone.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from html import escape
from typing import Any

import frappe
from frappe import _
from frappe.utils import getdate
from frappe.utils.pdf import get_pdf

CHECKIN = "Casual Checkin"
LABOURER = "Casual Labourer"
PROJECT = "Project"
PROJECT_USER = "Project Allowed User"  # Project.allowed_users child table: the sites a user is assigned to
WEEK_DAYS = 6  # Monday .. Saturday
DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

FULL_ROLES = {"System Manager", "HR Manager", "HR User", "Attendance Admin"}
# Everyone allowed to pull the register at all; the roles above see the whole company, the rest are scoped.
ALLOWED_ROLES = FULL_ROLES | {"Casual Registrar", "Casual Approver", "Attendance Marking"}


def monday_of(d: date) -> date:
	"""The Monday that starts the week containing `d`."""
	return d - timedelta(days=d.weekday())


def parse_week(value: Any, today: date) -> date:
	"""The Monday for `value` (any date in the wanted week), or this week's Monday. A value in the
	future is pulled back to the current week."""
	start = monday_of(today)
	if value:
		try:
			start = monday_of(getdate(value))
		except Exception:  # noqa: BLE001 - a bad date just means "this week"
			pass
	return min(start, monday_of(today))


def _money(value: Any) -> Decimal:
	try:
		return Decimal(str(value or 0))
	except (ValueError, ArithmeticError):
		return Decimal("0")


def _money_str(value: Decimal) -> str:
	return f"{value:,.0f}"


def _phone(value: Any) -> str:
	"""Kenyan number for the sheet: a stored 2547… becomes 07…; anything else is left as-is."""
	v = str(value or "").strip()
	if v.startswith("254") and len(v) >= 12:
		return "0" + v[3:]
	return v


def _punch_date(value: Any) -> date | None:
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	try:
		return datetime.fromisoformat(str(value).replace("T", " ")[:19]).date()
	except (ValueError, TypeError):
		return None


def _has_full(user: str) -> bool:
	return bool(FULL_ROLES & set(frappe.get_roles(user)))


def _allowed_projects(user: str) -> list[str]:
	"""The sites `user` is assigned to: Projects whose allowed_users (Project Allowed User) include
	them. Empty when the query cannot run — the register then falls back to their own punches only."""
	try:
		return frappe.get_all(
			PROJECT, filters=[[PROJECT_USER, "user", "=", user]], pluck="name", limit_page_length=0
		)
	except Exception:  # noqa: BLE001
		return []


def _punches(user: str, start: date, sat_end: date, *, full: bool) -> list[dict]:
	"""Every punch in [start, sat_end) for the user's scope — any log_type, so a single clock-in
	(even a lone OUT) marks the casual present that day. Row permissions are ignored and the scope is
	the window AND (a site the user is assigned to OR a casual they clocked in), unless `full`."""
	filters = [["time", ">=", f"{start} 00:00:00"], ["time", "<", f"{sat_end} 00:00:00"]]
	or_filters = None
	if not full:
		or_filters = [["supervisor_user", "=", user]]
		projects = _allowed_projects(user)
		if projects:
			or_filters.append(["project", "in", projects])
	return frappe.get_all(
		CHECKIN,
		filters=filters,
		or_filters=or_filters,
		fields=["casual_labourer", "full_name", "project", "time"],
		order_by="time asc",
		limit_page_length=0,
	)


def _labourers(names: list[str]) -> dict[str, dict]:
	"""full_name, id_number, daily_rate and phone for each casual, by name."""
	if not names:
		return {}
	rows = frappe.get_all(
		LABOURER,
		filters=[["name", "in", names]],
		fields=["name", "full_name", "id_number", "daily_rate", "phone"],
		limit_page_length=0,
	)
	return {r["name"]: r for r in rows}


def _project_names(names: list[str]) -> dict[str, str]:
	if not names:
		return {}
	rows = frappe.get_all(
		PROJECT, filters=[["name", "in", names]], fields=["name", "project_name"], limit_page_length=0
	)
	return {r["name"]: (r.get("project_name") or r["name"]) for r in rows}


def build(user: str, start: date, *, full: bool) -> dict:
	"""The register for the Mon-Sat week starting `start`. Shape mirrors the web-app register:
	{week_start, week_end, days:[{label,date}], sites:[{project, project_name, casuals:[{full_name,
	id_number, phone, present:[bool x6], days, rate, amount}], days, amount}], total_days,
	total_amount, casual_count, full}. Money is Decimal."""
	days = [start + timedelta(days=i) for i in range(WEEK_DAYS)]
	sat_end = start + timedelta(days=WEEK_DAYS)
	rows = _punches(user, start, sat_end, full=full)

	present: dict[tuple[str, str], set[date]] = {}
	for r in rows:
		project = (r.get("project") or "").strip() or "—"
		casual = r.get("casual_labourer")
		d = _punch_date(r.get("time"))
		if not casual or d is None or not (start <= d < sat_end):
			continue
		present.setdefault((project, casual), set()).add(d)

	casual_names = sorted({c for _, c in present})
	labourers = _labourers(casual_names)
	project_ids = sorted({p for p, _ in present})
	project_names = _project_names([p for p in project_ids if p != "—"])
	name_on_punch = {
		r["casual_labourer"]: (r.get("full_name") or "").strip() for r in rows if r.get("casual_labourer")
	}

	sites: list[dict] = []
	total_days = 0
	total_amount = Decimal("0")
	for project in project_ids:
		casuals: list[dict] = []
		site_days = 0
		site_amount = Decimal("0")
		for casual in sorted(
			(c for (p, c) in present if p == project),
			key=lambda c: (labourers.get(c, {}).get("full_name") or name_on_punch.get(c) or c).lower(),
		):
			dates = present[(project, casual)]
			flags = [d in dates for d in days]
			worked = sum(flags)
			lr = labourers.get(casual, {})
			rate = _money(lr.get("daily_rate"))
			amount = rate * worked
			casuals.append({
				"name": casual,
				"full_name": lr.get("full_name") or name_on_punch.get(casual) or casual,
				"id_number": lr.get("id_number") or "",
				"phone": lr.get("phone") or "",
				"present": flags,
				"days": worked,
				"rate": rate,
				"amount": amount,
			})
			site_days += worked
			site_amount += amount
		sites.append({
			"project": project,
			"project_name": project_names.get(project, project),
			"casuals": casuals,
			"days": site_days,
			"amount": site_amount,
		})
		total_days += site_days
		total_amount += site_amount

	return {
		"week_start": start,
		"week_end": start + timedelta(days=WEEK_DAYS - 1),  # Saturday, for display
		"days": [{"label": DAY_LABELS[i], "date": days[i]} for i in range(WEEK_DAYS)],
		"sites": sites,
		"total_days": total_days,
		"total_amount": total_amount,
		"casual_count": len(casual_names),
		"full": full,
	}


def _day(d: date) -> str:
	"""'Mon 9 Nov' without a leading zero (strftime %-d is not portable)."""
	return f"{d:%a} {d.day} {d:%b}"


def render_html(data: dict) -> str:
	"""The register as an HTML page wkhtmltopdf turns into the landscape sheet."""
	head = (
		f"{_day(data['week_start'])} – {_day(data['week_end'])} {data['week_end']:%Y}"
	)
	day_heads = "".join(f'<th class="d">{escape(d["label"])}</th>' for d in data["days"])

	blocks: list[str] = []
	for site in data["sites"]:
		label = (
			f"{site['project_name']} ({site['project']})" if site["project"] != "—" else "No site"
		)
		body: list[str] = []
		for c in site["casuals"]:
			bits = [c["full_name"]]
			if c.get("id_number"):
				bits.append(str(c["id_number"]))
			if c.get("phone"):
				bits.append(_phone(c["phone"]))
			who = escape("  ·  ".join(bits))
			cells = "".join(
				f'<td class="d{" p" if p else ""}">{"P" if p else "A"}</td>' for p in c["present"]
			)
			body.append(
				f'<tr><td class="who">{who}</td>{cells}'
				f'<td class="n">{c["days"]}</td>'
				f'<td class="n">{_money_str(c["rate"])}</td>'
				f'<td class="n">{_money_str(c["amount"])}</td></tr>'
			)
		blocks.append(
			f'<tr class="site"><td colspan="{WEEK_DAYS + 4}">{escape(label)}</td></tr>'
			f'<tr class="head"><th class="who">Casual</th>{day_heads}'
			f'<th class="n">Days</th><th class="n">Rate</th><th class="n">Amount</th></tr>'
			+ "".join(body)
			+ f'<tr class="total"><td class="who">{escape(label)} — total</td>'
			f'<td colspan="{WEEK_DAYS}"></td>'
			f'<td class="n">{site["days"]}</td><td></td>'
			f'<td class="n">{_money_str(site["amount"])}</td></tr>'
		)

	scope = (
		"Every site in the company."
		if data.get("full")
		else "Casuals on the sites you are assigned to, and any you clocked in this week."
	)
	grand = (
		f'<tr class="grand"><td class="who">GRAND TOTAL</td>'
		f'<td colspan="{WEEK_DAYS}"></td>'
		f'<td class="n">{data["total_days"]} days</td><td></td>'
		f'<td class="n">KSh {_money_str(data["total_amount"])}</td></tr>'
	)
	foot = (
		"P = present (clocked in that day), A = absent. Pay = daily rate × days present. " + scope
	)
	return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
	* {{ font-family: Helvetica, Arial, sans-serif; }}
	body {{ margin: 0; color: #111; }}
	.hdr {{ display: flex; justify-content: space-between; align-items: baseline;
	        border-bottom: 1.2px solid #111; padding-bottom: 4px; margin-bottom: 8px; }}
	.hdr h1 {{ font-size: 15px; margin: 0; }}
	.hdr .wk {{ font-size: 11px; }}
	table {{ width: 100%; border-collapse: collapse; }}
	th, td {{ font-size: 10px; padding: 2px 4px; }}
	th.who, td.who {{ text-align: left; }}
	th.d, td.d, th.n, td.n {{ text-align: center; }}
	th.n, td.n {{ text-align: right; }}
	td.d {{ text-align: center; }}
	td.d.p {{ font-weight: bold; }}
	tr.site td {{ font-size: 11px; font-weight: bold; padding-top: 10px; }}
	tr.head th {{ font-size: 9px; border-bottom: 0.5px solid #666; }}
	tr.total td {{ font-weight: bold; border-top: 0.5px solid #999; }}
	tr.grand td {{ font-size: 12px; font-weight: bold; border-top: 1.2px solid #111; padding-top: 6px; }}
	.foot {{ font-size: 8px; color: #333; margin-top: 10px; }}
	</style></head><body>
	<div class="hdr"><h1>Casual attendance &amp; pay</h1><div class="wk">{escape(head)}</div></div>
	<table>{"".join(blocks)}{grand}</table>
	<div class="foot">{escape(foot)}</div>
	</body></html>"""


def file_name(data: dict) -> str:
	return f"casual-attendance-{data['week_start']:%Y-%m-%d}.pdf"


@frappe.whitelist()
def download_weekly_register(week: str | None = None):
	"""Send the weekly register PDF for the Mon-Sat week of `week` (any date in it; default this
	week) as a download, scoped to the caller."""
	user = frappe.session.user
	if not (ALLOWED_ROLES & set(frappe.get_roles(user))):
		frappe.throw(_("You are not allowed to download the casual register."), frappe.PermissionError)
	start = parse_week(week, getdate())
	data = build(user, start, full=_has_full(user))
	pdf = get_pdf(
		render_html(data),
		options={
			"orientation": "Landscape",
			"page-size": "A4",
			"margin-top": "10mm",
			"margin-bottom": "10mm",
			"margin-left": "8mm",
			"margin-right": "8mm",
		},
	)
	frappe.local.response.filename = file_name(data)
	frappe.local.response.filecontent = pdf
	frappe.local.response.type = "download"
