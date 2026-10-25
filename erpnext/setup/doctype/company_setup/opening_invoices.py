# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Opening invoices: what customers owed and what was owed to suppliers when the company started.

Each unpaid invoice of the old books becomes an opening sales or purchase invoice, so later
payments can be matched against it and its due date keeps counting. They are made by the Opening
Invoice Creation Tool, only for rows whose customer or supplier exists; the rest stay as drafts.
A negative amount is an advance: money a customer paid, or we paid a supplier, before any bill.
It becomes an opening journal entry marked as an advance, to be set against a later invoice.
"""

from contextlib import contextmanager
from datetime import date

import frappe
from frappe import _
from frappe.core.doctype.data_import.exporter import Exporter
from frappe.model import table_fields
from frappe.utils import add_days, add_years, cstr, flt, formatdate, getdate, strip_html
from frappe.utils.xlsxutils import build_xlsx_response

from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
	get_checks_for_pl_and_bs_accounts,
	get_dimensions,
)
from erpnext.accounts.party import get_due_date, get_party_account
from erpnext.accounts.utils import get_fiscal_year
from erpnext.setup.doctype.company_setup.invoice_accounts import (
	fix_account_clashes,
	get_account_clashes,
	get_cached,
	get_default_node,
	get_hand_posted,
	get_invoice_accounts,
	get_ledger_over_message,
	get_ledger_overs,
	get_missing_default_account,
	get_new_names,
	get_new_party_accounts,
	get_party_accounts,
	get_party_currency,
	get_sheet_match,
	get_title_account,
	get_waiting_parties,
	make_party,
	set_ledger_accounts,
)
from erpnext.setup.doctype.company_setup.invoice_sheet import (
	BACKGROUND_FROM,
	COMMIT_EVERY,
	INVOICE_ROW_FIELDS,
	INVOICE_TYPES,
	PARTY_ACCOUNT_FIELDS,
	PROGRESS_EVERY,
	SHEET_LINKED_FIELDS,
	SHEET_PARTY_FIELDS,
	TABLES,
	UNDONE_TABLES,
	download_opening_invoices,
	find_party,
	get_day_first,
	get_exact_key,
	get_look_alikes,
	get_parties,
	get_posted_names,
	get_sheet_amount,
	get_sheet_date,
	get_similar_key,
	get_singular_key,
	get_suggestions,
	get_table,
	match_party,
	normalize_account_name,
	number,
	read_invoice_sheet,
)
from erpnext.setup.setup_wizard.operations.install_fixtures import get_fy_details


def post_invoices(
	onboarding,
	party_type: str,
	keep_old_dates: bool = False,
	progress: bool = False,
	disabled_parties: list[str] | None = None,
):
	"""Each draft row whose customer or supplier exists and is enabled becomes an opening invoice in
	its own savepoint, so a failed row is undone alone and noted on the row. A disabled or
	other-currency one stays a draft, but for the disabled ones named in disabled_parties: they are
	enabled for the posting and disabled again after it. The names confirmed as new are added first."""
	company = onboarding.company
	day_before = get_day_before(onboarding)
	ensure_fiscal_year(day_before, company)
	if keep_old_dates:
		# the years the old invoices fall in, added once and outside the rows' savepoints, so each one
		# is posted on its own date with its own due date
		for row in onboarding.get(get_table(party_type)):
			if (
				row.status != "Created"
				and row.invoice_date
				and getdate(row.invoice_date) <= getdate(day_before)
				and not has_fiscal_year(row.invoice_date, company)
			):
				ensure_fiscal_year(row.invoice_date, company)
	temporary = get_temporary_account(company, create=True)
	currency = frappe.get_cached_value("Company", company, "default_currency")
	parties = get_parties(party_type)
	not_added = add_parties(onboarding, party_type, parties)
	if not_added is not None:
		parties = get_parties(party_type)
	not_added = not_added or {}
	enabled = sorted(set(disabled_parties or []) & parties.disabled)
	with parties_enabled(party_type, enabled):
		parties.disabled -= set(enabled)

		tool = frappe.new_doc("Opening Invoice Creation Tool")
		tool.company = company
		tool.invoice_type = INVOICE_TYPES[party_type]
		dimensions = get_default_dimensions(company)
		hand_posted = {row.name for row in get_hand_posted(onboarding, party_type)}
		# whether ERPNext has the year of each invoice date, looked up once per date
		has_year = {}
		drafts = [
			row
			for row in onboarding.get(get_table(party_type))
			if row.status != "Created" and row.name not in hand_posted
		]
		for done, row in enumerate(drafts):
			# the open form shows how far a background posting is
			# a background posting keeps what it posted so far: a job stopped later loses only the rest
			if progress and done and not done % COMMIT_EVERY and not frappe.in_test:
				onboarding.flags.posting = True
				onboarding.save()
				frappe.db.commit()
			if progress and not done % PROGRESS_EVERY:
				frappe.publish_realtime(
					"company_setup_invoices_progress",
					{"company": company, "party_type": party_type, "done": done, "total": len(drafts)},
					doctype="Company Setup",
					docname=company,
				)
			row.party = row.party if row.party in parties.names else find_party(parties, row.party_name)
			if not row.party:
				error = (
					_("{0} could not be added: {1}").format(row.party_name, not_added[row.party_name])
					if row.party_name in not_added
					else _("{0} is not in ERPNext yet.").format(row.party_name)
				)
				row.update({"status": "Failed", "error": error})
				continue
			account = row.account or get_party_account(party_type, row.party, company)
			if (
				row.party in parties.disabled
				or get_party_currency(party_type, row.party, company, account) != currency
			):
				row.update({"status": "Draft", "error": None})
				continue
			# dated as in the old books when the books have that year, so ageing stays right
			if row.invoice_date not in has_year:
				has_year[row.invoice_date] = has_fiscal_year(row.invoice_date, company)
			posting_date = row.invoice_date if has_year[row.invoice_date] else day_before
			savepoint = f"opening_invoice_{row.idx}"
			frappe.db.savepoint(savepoint)
			try:
				if flt(row.outstanding_amount) < 0:
					voucher = post_advance(
						company, party_type, row.party, row, posting_date, temporary, dimensions
					)
					row.update({"status": "Created", "invoice": voucher, "error": None})
					continue
				tool.set(
					"invoices",
					[
						{
							"party": row.party,
							"party_name": row.party_name,
							"posting_date": posting_date,
							# without one on the sheet, from the party's payment terms, as on a new invoice
							"due_date": max(
								getdate(
									row.due_date
									or get_due_date(
										posting_date, party_type, row.party, company, row.invoice_date
									)
								),
								getdate(posting_date),
							),
							"outstanding_amount": row.outstanding_amount,
							"invoice_number": row.invoice_number,
							"supplier_invoice_date": row.invoice_date if party_type == "Supplier" else None,
							"temporary_opening_account": temporary,
							**dimensions,
						}
					],
				)
				invoice = tool.get_invoices()[0]
				invoice[PARTY_ACCOUNT_FIELDS[party_type]] = account
				# the amounts are in the company's currency, whatever the party usually uses
				invoice.currency = currency
				if party_type == "Supplier":
					# a supplier's invoice number is theirs, not a name for our purchase invoice
					invoice.bill_no = invoice.pop("invoice_number", None)
				doc = frappe.get_doc(invoice)
				doc.flags.ignore_mandatory = True
				doc.flags.dont_auto_add_taxes = True
				number = invoice.get("invoice_number")
				if number and frappe.db.get_value(doc.doctype, number, "company") not in (None, company):
					# another company's invoice on this site has the number: told apart by this one's abbreviation
					number = f"{number}-{frappe.get_cached_value('Company', company, 'abbr')}"
				if number and frappe.db.get_value(doc.doctype, number, "docstatus") == 2:
					# posted again beside a cancelled one kept: amends the last of them, named like
					# "INV-1-1" whatever the site's amend naming, so the number stays
					doc.amended_from = get_last_amendment(doc.doctype, number)
					doc.insert(set_name=get_amended_name(doc.doctype, number))
				else:
					doc.insert(set_name=number)
				doc.submit()
				row.update({"status": "Created", "invoice": doc.name, "error": None})
			except Exception as e:
				frappe.db.rollback(save_point=savepoint)
				frappe.clear_messages()
				row.update({"status": "Failed", "error": get_error_text(e)})

	# an undone invoice posted again leaves the undone ones
	posted = {
		(row.party, row.invoice_number or "")
		for row in onboarding.get(get_table(party_type))
		if row.status == "Created"
	}
	onboarding.set(
		UNDONE_TABLES[party_type],
		[
			row
			for row in onboarding.get(UNDONE_TABLES[party_type])
			if (row.party, row.invoice_number or "") not in posted
		],
	)
	onboarding.flags.posting = True
	onboarding.save()


def add_parties(onboarding, party_type: str, parties: frappe._dict) -> dict[str, str] | None:
	"""Add each customer or supplier confirmed as new that ERPNext doesn't have, each with a normal
	save so mandatory fields apply. Returns why each one that could not be added was not, or None when
	none was confirmed."""
	names = get_new_names(onboarding, party_type, parties, confirmed=True)
	groups = {
		row.party_name: row.party_group
		for row in onboarding.get(get_table(party_type))
		if row.create_party and row.party_group
	}
	if not names:
		return None
	not_added = {}
	for name in names:
		frappe.db.savepoint("opening_party")
		shown_before = len(frappe.local.message_log)
		try:
			make_party(party_type, name, onboarding.company, groups.get(name)).insert()
		except Exception as e:
			frappe.db.rollback(save_point="opening_party")
			# what the save showed, like which mandatory field is empty; the error only names fields
			shown = [strip_html(cstr(m.get("message"))) for m in frappe.local.message_log[shown_before:]]
			frappe.local.message_log = frappe.local.message_log[:shown_before]
			not_added[name] = "; ".join(filter(None, shown)) or get_error_text(e)
	return not_added


@frappe.whitelist()
def download_new_parties(company: str, party_type: str):
	"""The Data Import sheet for customers or suppliers, with a row for each new name on the draft
	opening invoices and the defaults a new one gets, to complete and import."""
	onboarding = frappe.get_doc("Company Setup", company)
	onboarding.check_permission("read")
	get_table(party_type)
	frappe.has_permission(party_type, "create", throw=True)
	parties = get_parties(party_type)

	fields = list(SHEET_PARTY_FIELDS[party_type])
	fields += [
		df.fieldname
		for df in frappe.get_meta(party_type).fields
		if df.reqd and df.fieldtype not in table_fields and df.fieldname not in fields
	]
	exporter = Exporter(
		party_type, export_fields={party_type: fields, **SHEET_LINKED_FIELDS}, file_type="Excel"
	)
	rows = exporter.get_csv_array()
	for name in get_new_names(onboarding, party_type, parties):
		party = make_party(party_type, name, onboarding.company)
		rows.append([None if df.is_child_table_field else party.get(df.fieldname) for df in exporter.fields])
	build_xlsx_response(rows, _("New customers") if party_type == "Customer" else _("New suppliers"))


def post_advance(
	company: str,
	party_type: str,
	party: str,
	row,
	posting_date,
	temporary: str,
	dimensions: dict | None = None,
) -> str:
	"""An opening journal entry for an advance, on the party's account and marked as one, so a
	later invoice can be set against it. Returns its name."""
	# a customer's advance is a credit on what they owe, a supplier's a debit on what we owe
	amount = flt(row.outstanding_amount) if party_type == "Customer" else -flt(row.outstanding_amount)
	je = frappe.new_doc("Journal Entry")
	je.update(
		{
			"voucher_type": "Opening Entry",
			"is_opening": "Yes",
			"company": company,
			"posting_date": posting_date,
			"cheque_no": row.invoice_number,
			"cheque_date": row.invoice_number and (row.invoice_date or posting_date),
			"user_remark": _("Opening advance from Company Setup"),
		}
	)
	if dimensions is None:
		dimensions = get_default_dimensions(company)
	append_line(
		je,
		row.account or get_party_account(party_type, party, company),
		amount,
		dimensions,
		party_type=party_type,
		party=party,
		is_advance="Yes",
	)
	append_line(je, temporary, -amount, dimensions)
	je.insert()
	je.submit()
	return je.name


def get_posting_dates(onboarding, party_type: str) -> dict:
	"""The dates the drafts go on, for the posting confirmation: their own invoice dates where ERPNext
	has the year, else the day before migration (with how many, and the years missing)."""
	day_before = get_day_before(onboarding)
	known, kept, no_year, undated, missing = {}, [], [], 0, set()
	for row in onboarding.get(get_table(party_type)):
		if row.status == "Created":
			continue
		date = row.invoice_date and getdate(row.invoice_date)
		if date and date not in known:
			known[date] = has_fiscal_year(date, onboarding.company)
		if date and known[date]:
			kept.append(date)
			continue
		if not date:
			undated += 1
			continue
		no_year.append(row)
		missing.add(get_missing_fiscal_year(date, onboarding.company).year)
	return {
		"kept": len(kept),
		"first": min(kept, default=None),
		"last": max(kept, default=None),
		"no_year": len(no_year),
		"no_year_amount": sum(flt(row.outstanding_amount) for row in no_year),
		# the oldest few by name; the confirmation counts the rest
		"no_year_rows": [
			{
				"party": row.party or row.party_name,
				"invoice_number": row.invoice_number,
				"invoice_date": row.invoice_date,
				"amount": flt(row.outstanding_amount),
			}
			for row in sorted(no_year, key=lambda row: getdate(row.invoice_date))
		],
		"undated": undated,
		"missing_years": sorted(missing),
		"day_before": day_before,
	}


def has_fiscal_year(date, company: str) -> bool:
	return bool(date) and bool(get_fiscal_year(date, company=company, boolean=True))


def get_missing_fiscal_year(date, company: str) -> frappe._dict | None:
	"""The financial year a date falls in, when ERPNext doesn't have it yet. Migrating on the first
	day of a year dates the opening entries on the last day of the year before, which a new site
	usually doesn't have."""
	if not date or has_fiscal_year(date, company):
		return None
	latest = frappe.get_all(
		"Fiscal Year", fields=["year_start_date"], order_by="year_start_date desc", limit=1
	)
	start = getdate(latest[0].year_start_date) if latest else getdate(f"{getdate(date).year}-01-01")
	while start > getdate(date):
		start = add_years(start, -1)
	while add_years(start, 1) <= getdate(date):
		start = add_years(start, 1)
	end = add_days(add_years(start, 1), -1)
	return frappe._dict(year=get_fy_details(start, end), year_start_date=start, year_end_date=end)


def get_fiscal_year_red(company: str, *dates) -> dict | None:
	"""The years these dates fall in that ERPNext doesn't have and the user can't add, as a red like
	get_posting_reds gives. None when every year is there or the user can add Fiscal Years."""
	if frappe.has_permission("Fiscal Year", "create"):
		return None
	missing = sorted({year.year for date in dates if (year := get_missing_fiscal_year(date, company))})
	if not missing:
		return None
	return {
		"text": _("Ask a System Manager to add the financial year {0} first.").format(", ".join(missing)),
		"fix": {"label": _("New Fiscal Year"), "doctype": "Fiscal Year", "name": "new"},
	}


def ensure_fiscal_year(date, company: str):
	"""Add the financial year the opening entries fall in, when ERPNext doesn't have it yet, and the
	years between it and the years ERPNext has, so none is missing in between."""
	# a failed posting rolls the year back, but not ERPNext's cached list of years
	frappe.cache.delete_key("fiscal_years")
	year = get_missing_fiscal_year(date, company)
	if not year:
		return
	if red := get_fiscal_year_red(company, date):
		frappe.throw(red["text"], title=_("Financial year missing"))
	years = frappe.get_all("Fiscal Year", fields=["year_start_date", "year_end_date"])
	later = min(
		(
			getdate(row.year_start_date)
			for row in years
			if getdate(row.year_start_date) > year.year_start_date
		),
		default=None,
	)
	earlier = max(
		(getdate(row.year_end_date) for row in years if getdate(row.year_end_date) < year.year_start_date),
		default=None,
	)
	add_fiscal_year(year, company)
	following = year
	while (
		later
		and (following := get_missing_fiscal_year(add_days(following.year_end_date, 1), company))
		and getdate(following.year_end_date) < later
	):
		add_fiscal_year(following, company)
	preceding = year
	while (
		earlier
		and (preceding := get_missing_fiscal_year(add_days(preceding.year_start_date, -1), company))
		and getdate(preceding.year_start_date) > earlier
	):
		add_fiscal_year(preceding, company)
	# so the form doesn't take a year undone by a failed posting for one that is there
	frappe.db.after_rollback.add(lambda: frappe.cache.delete_key("fiscal_years"))


def get_last_amendment(doctype: str, name: str) -> str:
	"""The last document of the amendment chain that starts at name."""
	while next_name := frappe.db.get_value(doctype, {"amended_from": name}, "name"):
		name = next_name
	return name


def get_amended_name(doctype: str, number: str) -> str:
	"""The first free name like "INV-1-1", "INV-1-2" for a number that is taken."""
	count = 1
	while frappe.db.exists(doctype, f"{number}-{count}"):
		count += 1
	return f"{number}-{count}"


def add_fiscal_year(year: frappe._dict, company: str):
	"""Add the year. On a site with other companies a year of that name may be theirs: this company's
	is then its own, named after it too."""
	if frappe.db.exists("Fiscal Year", year.year):
		abbr = frappe.get_cached_value("Company", company, "abbr")
		year = year | {"year": f"{year.year} ({abbr})", "companies": [{"company": company}]}
	frappe.get_doc({"doctype": "Fiscal Year", **year}).insert()
	frappe.cache.delete_key("fiscal_years")


def get_day_before(onboarding) -> date | None:
	"""The day before migration, which the opening entries are dated; None without a migration date."""
	return getdate(add_days(onboarding.migration_date, -1)) if onboarding.migration_date else None


def count_text(count: int, one: str, many: str, *args) -> str:
	"""Like "1 item" or "1,400 items": one or many, with the count and args filled in."""
	return (one if count == 1 else many).format(number(count), *args)


def get_error_text(error: Exception) -> str:
	"""An error's message as plain text, for a row or the open form to show."""
	return strip_html(cstr(error)) or error.__class__.__name__


def get_temporary_account(company: str, create: bool = False) -> str | None:
	"""The account that takes the opening difference. An imported chart may have none: posting adds one
	under Assets, as in the standard charts."""
	account = frappe.db.get_value(
		"Account", {"company": company, "account_type": "Temporary", "is_group": 0, "disabled": 0}
	)
	if account or not create:
		return account
	return add_account(company, _("Temporary Opening"), "Temporary", "Asset")


def add_account(
	company: str, account_name: str, account_type: str, root_type: str, parent: str | None = None
) -> str:
	"""A ledger of this type under the parent group, or else straight under the company's top-level
	account of the root type."""
	parent = parent or frappe.db.get_value(
		"Account",
		{"company": company, "root_type": root_type, "is_group": 1, "parent_account": ("is", "not set")},
	)
	return (
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"account_type": account_type,
				"company": company,
				"parent_account": parent,
			}
		)
		.insert()
		.name
	)


def get_posting_blocker(
	company: str,
	posting_date,
	undo: bool = False,
	profit_and_loss: bool = False,
	voucher_types: tuple[str, ...] = ("Journal Entry",),
	to_date=None,
	opening: bool = True,
) -> str | None:
	"""Why opening entries dated posting_date (to to_date) can't be posted (or undone), as a sentence
	to show in place of ERPNext's error. None when nothing stops them. See get_posting_reds."""
	reds = get_posting_reds(company, posting_date, undo, profit_and_loss, voucher_types, to_date, opening)
	return reds[0]["text"] if reds else None


def get_posting_fix(company: str, posting_date, **kwargs) -> dict | None:
	"""The fix of the first reason get_posting_blocker gives, as {label, doctype, name}."""
	reds = get_posting_reds(company, posting_date, **kwargs)
	return reds[0]["fix"] if reds else None


def get_posting_reds(
	company: str,
	posting_date,
	undo: bool = False,
	profit_and_loss: bool = False,
	voucher_types: tuple[str, ...] = ("Journal Entry",),
	to_date=None,
	opening: bool = True,
) -> list[dict]:
	"""Every reason opening entries dated posting_date (to to_date) can't be posted (or undone), each
	as {text, fix}: fix is the record to open to fix it, as {label, doctype, name}. With
	profit_and_loss, the entries have income or expense lines too; voucher_types are the documents
	posted. Without opening, they are ordinary entries, which a closed year stops only on or before
	its end."""
	reds = []
	closing = frappe.get_all(
		"Period Closing Voucher",
		filters={"company": company, "docstatus": 1},
		fields=["name", "period_end_date"],
		order_by="period_end_date desc",
		limit=1,
	)
	closing = closing[0] if closing else None
	fix = closing and {
		"label": _("Open {0}").format(closing.name),
		"doctype": "Period Closing Voucher",
		"name": closing.name,
	}
	if not opening:
		closed = closing and closing.period_end_date
		if posting_date and closed and getdate(posting_date) <= getdate(closed):
			text = (
				_(
					"The year is closed in ERPNext up to {0}, so entries dated {1} can't be undone. Cancel the closing entry first."
				)
				if undo
				else _(
					"The year is closed in ERPNext up to {0}, so entries dated {1} can't be posted. Cancel the closing entry first."
				)
			)
			reds.append({"text": text.format(formatdate(closed), formatdate(posting_date)), "fix": fix})
	elif closing:
		reds.append(
			{
				"text": _(
					"Last year is closed in ERPNext, so opening entries can't be undone. Cancel the closing entry first."
				)
				if undo
				else _(
					"Last year is closed in ERPNext, so opening entries can't be posted. Cancel the closing entry first."
				),
				"fix": fix,
			}
		)

	if not undo:
		if red := get_fiscal_year_red(company, posting_date, to_date):
			reds.append(red)
		for dimension in get_checks_for_pl_and_bs_accounts():
			if (
				dimension.company == company
				and not dimension.default_dimension
				and (dimension.mandatory_for_bs or (profit_and_loss and dimension.mandatory_for_pl))
			):
				reds.append(
					{
						"text": _(
							"Every entry needs a {0}, and {1} has no default {0}. Set one in Accounting Dimension {0} first."
						).format(dimension.label, company),
						"fix": {
							"label": _("Set the default {0}").format(dimension.label),
							"doctype": "Accounting Dimension",
							"name": frappe.db.get_value(
								"Accounting Dimension", {"fieldname": dimension.fieldname}
							),
							# set right here: the dimension's own form leads away from Company Setup
							"document_type": frappe.db.get_value(
								"Accounting Dimension", {"fieldname": dimension.fieldname}, "document_type"
							),
						},
					}
				)

	period = posting_date and get_closed_period(company, voucher_types, posting_date, to_date or posting_date)
	if period:
		text = (
			_("Accounting Period {0} is closed for {1}, so these entries can't be undone. Open it first.")
			if undo
			else _("Accounting Period {0} is closed for {1}. Open it first.")
		)
		reds.append(
			{
				"text": text.format(period.name, _(period.document_type)),
				"fix": {
					"label": _("Open {0}").format(period.name),
					"doctype": "Accounting Period",
					"name": period.name,
				},
			}
		)

	# as ERPNext checks it: the role may post into frozen dates, except Administrator
	frozen_till, role = frappe.db.get_value(
		"Company", company, ["accounts_frozen_till_date", "role_allowed_for_frozen_entries"]
	)
	if (
		posting_date
		and frozen_till
		and getdate(posting_date) <= getdate(frozen_till)
		and (role not in frappe.get_roles() or frappe.session.user == "Administrator")
	):
		text = (
			_(
				"Accounts are frozen up to {0}, so entries dated {1} can't be undone. Change Accounts Frozen Till Date on the company first."
			)
			if undo
			else _(
				"Accounts are frozen up to {0}, so entries dated {1} can't be posted. Change Accounts Frozen Till Date on the company first."
			)
		)
		reds.append(
			{
				"text": text.format(formatdate(frozen_till), formatdate(posting_date)),
				"fix": {"label": _("Open company"), "doctype": "Company", "name": company},
			}
		)
	return reds


def get_closed_period(company: str, voucher_types, from_date, to_date) -> frappe._dict | None:
	"""A closed Accounting Period of the company for one of these documents over the dates, as
	ERPNext checks it on post and on cancel. One whose exempted role the user has doesn't count."""
	return next(iter(get_closed_periods(company, voucher_types, from_date, to_date)), None)


def get_closed_periods(company: str, voucher_types, from_date=None, to_date=None) -> list[frappe._dict]:
	"""The company's Accounting Periods closed for one of these documents, over the dates when given.
	One whose exempted role the user has doesn't count."""
	period = frappe.qb.DocType("Accounting Period")
	closed = frappe.qb.DocType("Closed Document")
	query = (
		frappe.qb.from_(period)
		.inner_join(closed)
		.on(period.name == closed.parent)
		.select(period.name, period.end_date, period.exempted_role, closed.document_type)
		.where(
			(period.company == company)
			& (period.disabled == 0)
			& (closed.closed == 1)
			& closed.document_type.isin(list(voucher_types))
		)
	)
	if from_date:
		query = query.where((period.start_date <= to_date) & (period.end_date >= from_date))
	roles = frappe.get_roles()
	return [row for row in query.run(as_dict=True) if not (row.exempted_role and row.exempted_role in roles)]


def append_line(je, account: str, amount: float, dimensions: dict, cost_center: str | None = None, **fields):
	"""A journal entry line debiting the account by the amount, or crediting it when negative."""
	je.append(
		"accounts",
		{
			"account": account,
			"cost_center": cost_center,
			"debit_in_account_currency": max(amount, 0),
			"credit_in_account_currency": max(-amount, 0),
			**fields,
			**dimensions,
		},
	)


def append_lines(
	je, account: str, amount: float, other: str, dimensions: dict, cost_center: str | None = None
):
	"""The account's line, debited by the amount (credited when negative), and the other account's
	line the other way."""
	append_line(je, account, amount, dimensions, cost_center)
	append_line(je, other, -amount, dimensions, cost_center)


@contextmanager
def parties_enabled(party_type: str, parties: list[str]):
	"""These disabled customers or suppliers enabled for a posting or undo, and disabled again after."""
	for party in parties:
		frappe.get_doc(party_type, party).check_permission("write")
		frappe.db.set_value(party_type, party, "disabled", 0, update_modified=False)
	yield
	for party in parties:
		frappe.db.set_value(party_type, party, "disabled", 1, update_modified=False)


def get_default_dimensions(company: str) -> dict[str, str]:
	"""The company's default for each accounting dimension that has one, by fieldname."""
	return {
		fieldname: default for fieldname, default in get_dimensions()[1].get(company, {}).items() if default
	}
