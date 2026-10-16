# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Opening invoices: what customers owed and what was owed to suppliers when the company started.

Each unpaid invoice of the old books becomes an opening sales or purchase invoice, so later
payments can be matched against it and its due date keeps counting. They are made by the Opening
Invoice Creation Tool, only for rows whose customer or supplier exists; the rest stay as drafts.
A negative amount is an advance: money a customer paid, or we paid a supplier, before any bill.
It becomes an opening journal entry marked as an advance, to be set against a later invoice.
"""

import re
import unicodedata
from difflib import get_close_matches

import frappe
from frappe import _
from frappe.core.doctype.data_import.exporter import Exporter
from frappe.model import table_fields
from frappe.utils import add_days, add_years, cstr, flt, get_user_date_format, getdate, strip_html
from frappe.utils.xlsxutils import build_xlsx_response, make_xlsx

from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_fiscal_year
from erpnext.setup.doctype.company_setup.chart_sheet import (
	HEADING_ROWS,
	TOTAL_LINE,
	get_amount,
	get_cell,
	get_column_key,
	read_report,
)

TABLES = {"Customer": "opening_sales_invoices", "Supplier": "opening_purchase_invoices"}
INVOICE_TYPES = {"Customer": "Sales", "Supplier": "Purchase"}
PARTY_NAME_FIELDS = {"Customer": "customer_name", "Supplier": "supplier_name"}
# filled from Selling or Buying Settings when a new one is added
PARTY_DEFAULTS = {"Customer": ("customer_group", "territory"), "Supplier": ("supplier_group",)}
# the columns of the sheet for new customers or suppliers, besides their other mandatory fields
SHEET_PARTY_FIELDS = {
	"Customer": ("customer_name", "customer_type", "customer_group", "territory", "tax_id"),
	"Supplier": ("supplier_name", "supplier_type", "supplier_group", "tax_id"),
}
SHEET_LINKED_FIELDS = {
	"contacts": ["first_name", "email_id", "mobile_no"],
	"addresses": ["address_line1", "city", "state", "pincode", "country"],
}
SHEET_COLUMNS = ("Name", "Invoice No", "Invoice Date", "Due Date", "Outstanding")
# the column names a receivables or payables report may use (Tally, Busy, Zoho, QuickBooks),
# the usual one first; compared without punctuation, so "Bill No." is bill_no
COLUMN_NAMES = {
	"party_name": (
		"name",
		"customer",
		"supplier",
		"party",
		"party_name",
		"party_s_name",
		"customer_name",
		"supplier_name",
		"vendor_name",
		"particulars",
	),
	"invoice_number": (
		"invoice_no",
		"invoice_number",
		"invoice",
		"bill_no",
		"bill_number",
		"voucher_no",
		"ref_no",
		"num",
	),
	"invoice_date": ("invoice_date", "date", "bill_date", "voucher_date"),
	"due_date": ("due_date", "due_on"),
	"outstanding_amount": (
		"outstanding",
		"outstanding_amount",
		"pending_amount",
		"pending_amt",
		"pending",
		"open_balance",
		"balance_due",
		"balance",
		"amount",
	),
	# which side an amount is on, when a report puts it in a column of its own (Busy)
	"side": ("dr_cr", "cr_dr"),
}
# the words that mark a column when none of its usual headings is there, the likeliest first
COLUMN_WORDS = {
	"party_name": ({"customer"}, {"supplier"}, {"vendor"}, {"party"}),
	"invoice_number": ({"bill", "no"}, {"invoice", "no"}, {"bill", "number"}, {"invoice", "number"}),
	"outstanding_amount": ({"outstanding"}, {"pending"}, {"balance"}, {"amount"}),
}
# words that rule a column out, like a bill's full amount or one in a foreign currency
COLUMN_WORDS_EXCLUDED = {
	"party_name": {"bill", "group", "ledger"},
	"invoice_number": {"date", "amount", "amt"},
	"outstanding_amount": {"bill", "fc", "foreign", "rate"},
}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
# only Latin accents: marks in other scripts, like Hindi vowel signs, change the name
ACCENTS = re.compile("[\u0300-\u036f]")
# how alike two names must be to look like one spelt two ways
LOOK_ALIKE = 0.85
LEGAL_WORDS = re.compile(r"\b(private|pvt|limited|ltd|llp|inc)\b")
# above this many invoices are made in the background
BACKGROUND_FROM = 50


def get_table(party_type: str) -> str:
	if party_type not in TABLES:
		frappe.throw(_("Opening invoices are for customers or suppliers, not {0}.").format(party_type))
	return TABLES[party_type]


def get_parties(party_type: str) -> frappe._dict:
	"""Every customer or supplier, disabled ones too: shown names by ID, IDs by the ways a name
	matches, and the disabled IDs."""
	name_field = PARTY_NAME_FIELDS[party_type]
	parties = frappe._dict(names={}, exact={}, similar={}, disabled=set())
	for party in frappe.get_all(party_type, fields=["name", name_field, "disabled"]):
		parties.names[party.name] = party.get(name_field) or party.name
		for name in {party.name, party.get(name_field)}:
			if name:
				parties.exact.setdefault(get_exact_key(name), set()).add(party.name)
				parties.similar.setdefault(get_similar_key(name), set()).add(party.name)
		if party.disabled:
			parties.disabled.add(party.name)
	return parties


def get_exact_key(name: str) -> str:
	return " ".join(cstr(name).casefold().split())


def get_similar_key(name: str) -> str:
	"""A name without case, accents, punctuation or words like Pvt Ltd."""
	text = ACCENTS.sub("", unicodedata.normalize("NFKD", cstr(name)))
	text = re.sub(r"[\W_]+", " ", text.casefold())
	return " ".join(LEGAL_WORDS.sub(" ", text).split())


def find_party(parties: frappe._dict, name: str) -> str | None:
	"""The one customer or supplier a name means: the same name ignoring case and spaces, else also
	ignoring accents, punctuation and words like Pvt Ltd. None when none or more than one match."""
	for index, key in ((parties.exact, get_exact_key(name)), (parties.similar, get_similar_key(name))):
		matches = index.get(key) if key else None
		if matches:
			return next(iter(matches)) if len(matches) == 1 else None
	return None


def get_suggestions(parties: frappe._dict, names: list[str]) -> dict[str, str]:
	"""For names that are not a customer or supplier yet, the ID of one that looks almost the same,
	like a typo of it."""
	known = [key for key, matches in parties.similar.items() if len(matches) == 1]
	suggestions = {}
	for name in set(names):
		key = get_similar_key(name)
		if key and not find_party(parties, name):
			match = get_close_matches(key, known, n=1, cutoff=LOOK_ALIKE)
			if match:
				suggestions[name] = next(iter(parties.similar[match[0]]))
	return suggestions


def get_look_alikes(names: list[str]) -> list[list[str]]:
	"""Pairs of new names that look like one customer or supplier spelt two ways. A typo keeps the
	first letter, so names are only compared with those starting the same, which keeps a sheet of
	thousands of new names quick."""
	by_letter = {}
	for name in names:
		key = get_similar_key(name)
		if key:
			by_letter.setdefault(key[0], []).append((key, name))
	pairs = []
	for group in by_letter.values():
		for i, (key, name) in enumerate(group):
			others = group[i + 1 :]
			matches = set(
				get_close_matches(key, [other for other, _ in others], n=len(others) or 1, cutoff=LOOK_ALIKE)
			)
			pairs += [[name, other_name] for other, other_name in others if other in matches]
	return pairs


def read_invoice_sheet(file_url: str, party_type: str) -> frappe._dict:
	"""The rows of a receivables or payables report, how many are advances, and the columns that
	were not used. Reports from other software are read as exported: headings under title rows,
	totals and subtotals anywhere, an amount split into Dr and Cr columns (Tally), and bills grouped
	under a line naming their customer or supplier (QuickBooks, Busy)."""
	rows = [row.cells for row in read_report(file_url)]
	keys = [[get_column_key(value) for value in row] for row in rows]
	start = next((i for i, row in enumerate(keys[:HEADING_ROWS]) if is_heading_row(row)), None)
	sheet = get_sheet_columns(
		keys[start] if start is not None else [],
		keys[start + 1] if start is not None and start + 1 < len(keys) else [],
	)
	if sheet.columns["party_name"] is None:
		frappe.throw(_("The sheet needs a Name column and an Outstanding column."))

	invoices, advances, party = [], 0, None
	for row in rows[start + 1 + sheet.sub_heading :]:
		if any(TOTAL_LINE.match(value.strip()) for value in row if isinstance(value, str)):
			party = None
			continue
		name = cstr(get_cell(row, sheet.columns["party_name"])).strip()
		amount = get_row_amount(row, sheet, party_type)
		if not amount:
			# in a grouped report, a line with just a name heads the bills under it
			party = name if sheet.grouped and name else party
			continue
		invoice_number = cstr(get_cell(row, sheet.columns["invoice_number"])).strip()
		if sheet.party_and_bill:
			name, invoice_number = party, name
		elif sheet.grouped and not name:
			name = party
		if not name:
			continue
		if amount < 0:
			advances += 1
		invoices.append(
			{
				"party_name": name,
				# Tally's mark for an amount set against no bill
				"invoice_number": None if invoice_number.lower() in ("", "on account") else invoice_number,
				"invoice_date": get_sheet_date(get_cell(row, sheet.columns["invoice_date"])),
				"due_date": get_sheet_date(get_cell(row, sheet.columns["due_date"])),
				"outstanding_amount": amount,
			}
		)
	unused = [
		cstr(label).strip()
		for index, label in enumerate(rows[start])
		if index not in sheet.used and cstr(label).strip()
	]
	return frappe._dict(invoices=invoices, advances=advances, unused_columns=unused)


def is_heading_row(keys: list[str]) -> bool:
	"""A row of column headings with an amount among them, not a title like "Debtors outstanding"."""
	return sum(1 for key in keys if key) > 1 and find_column(keys, "outstanding_amount") is not None


def get_sheet_columns(headers: list[str], below: list[str]) -> frappe._dict:
	"""Which column holds what in a receivables or payables report, from its heading row and the
	row under it."""
	columns = {field: find_column(headers, field) for field in COLUMN_NAMES}
	sheet = frappe._dict(columns=columns, grouped=False, party_and_bill=False, credit=None, sub_heading=0)
	# Busy's bill-wise report: "Party / Bill No.", a party's line, then its bills
	mixed = next((i for i, key in enumerate(headers) if {"party", "bill"} <= set(key.split("_"))), None)
	if columns["party_name"] is None and mixed is not None:
		columns["party_name"], sheet.party_and_bill = mixed, True
	# QuickBooks: the customer's name in a first column with no heading, over its bills
	elif columns["party_name"] is None and headers and not headers[0]:
		columns["party_name"] = 0
	sheet.grouped = sheet.party_and_bill or (columns["party_name"] == 0 and headers and not headers[0])
	# Tally: "Pending Amount" over a Dr and a Cr column, named on the row under the heading
	if columns["outstanding_amount"] is not None and "dr" in below and "cr" in below:
		columns["outstanding_amount"], sheet.credit, sheet.sub_heading = (
			below.index("dr"),
			below.index("cr"),
			1,
		)
	sheet.used = {index for index in [*columns.values(), sheet.credit] if index is not None}
	return sheet


def find_column(headers: list[str], field: str) -> int | None:
	"""The column of a field: by one of its usual headings, else by the words in a heading, like
	"Outstanding (INR)" or "Their Bill No"."""
	for name in COLUMN_NAMES[field]:
		if name in headers:
			return headers.index(name)
	for words in COLUMN_WORDS.get(field, ()):
		for index, key in enumerate(headers):
			found = set(key.split("_"))
			if words <= found and not found & COLUMN_WORDS_EXCLUDED.get(field, set()):
				return index
	return None


def get_row_amount(row: list, sheet: frappe._dict, party_type: str) -> float:
	if sheet.credit is not None:
		debit = get_cell(row, sheet.columns["outstanding_amount"])
		if get_amount(debit):
			return get_sheet_amount(debit, party_type, "dr")
		return get_sheet_amount(get_cell(row, sheet.credit), party_type, "cr")
	return get_sheet_amount(
		get_cell(row, sheet.columns["outstanding_amount"]), party_type, get_cell(row, sheet.columns["side"])
	)


def get_sheet_amount(value, party_type: str, side=None) -> float:
	"""An amount cell; text like Tally's "30,000.00 Cr", or a Dr/Cr column beside it, is an advance
	when it is on the other side from what the customer owes us, or we owe the supplier."""
	text = cstr(value).strip()
	side = cstr(side).strip().lower() or text[-2:].lower()
	if side in ("dr", "cr") and text[-2:].lower() == side:
		text = text[:-2]
	amount = get_amount(text if isinstance(value, str) else value)
	if side not in ("dr", "cr"):
		return amount
	usual = "dr" if party_type == "Customer" else "cr"
	return amount if side == usual else -amount


def get_sheet_date(value) -> str | None:
	"""A date cell, or text in the yyyy-mm-dd form or in the order the site writes dates."""
	if not value:
		return None
	text = cstr(value).strip()
	day_first = not ISO_DATE.match(text) and get_user_date_format().startswith("dd")
	try:
		return str(getdate(text if isinstance(value, str) else value, parse_day_first=day_first))
	except Exception:
		return None


@frappe.whitelist()
def download_opening_invoices(company: str, party_type: str):
	"""A sheet to fill with unpaid invoices, with the drafts already entered."""
	onboarding = frappe.get_doc("Company Setup", company)
	onboarding.check_permission("read")
	rows = [list(SHEET_COLUMNS)]
	for row in onboarding.get(get_table(party_type)):
		if row.status != "Created":
			rows.append(
				[row.party_name, row.invoice_number, row.invoice_date, row.due_date, row.outstanding_amount]
			)

	frappe.response["filename"] = f"{frappe.scrub(company)}_opening_{frappe.scrub(party_type)}_invoices.xlsx"
	frappe.response["filecontent"] = make_xlsx(rows, _("Opening Invoices")).getvalue()
	frappe.response["type"] = "binary"


def create_invoices(company: str, party_type: str, add_missing: bool = False):
	"""Post every draft row as an opening invoice, one posting at a time."""
	# a second posting waits here, then finds the rows posted
	frappe.db.get_value("Company Setup", company, "name", for_update=True)
	post_invoices(frappe.get_doc("Company Setup", company), party_type, add_missing)


def create_invoices_in_background(company: str, party_type: str, add_missing: bool = False):
	"""create_invoices as a background job, telling the open form when done or why not."""
	event = {"company": company, "party_type": party_type}
	try:
		create_invoices(company, party_type, add_missing)
	except Exception as e:
		frappe.db.rollback()
		# shown by the form, which may be waiting for a background job
		frappe.publish_realtime(
			"company_setup_invoices",
			{**event, "error": strip_html(cstr(e)) or e.__class__.__name__},
			doctype="Company Setup",
			docname=company,
		)
		raise
	frappe.publish_realtime(
		"company_setup_invoices", event, doctype="Company Setup", docname=company, after_commit=True
	)


def post_invoices(onboarding, party_type: str, add_missing: bool = False):
	"""Each draft row whose customer or supplier exists and is enabled becomes an opening invoice in
	its own savepoint, so a failed row is undone alone and noted on the row."""
	company = onboarding.company
	day_before = add_days(onboarding.migration_date, -1)
	ensure_fiscal_year(day_before, company)
	temporary = get_temporary_account(company, create=True)
	currency = frappe.get_cached_value("Company", company, "default_currency")
	parties = get_parties(party_type)
	not_added = {}
	if add_missing:
		not_added = add_parties(onboarding, party_type, parties)
		parties = get_parties(party_type)

	tool = frappe.new_doc("Opening Invoice Creation Tool")
	tool.company = company
	tool.invoice_type = INVOICE_TYPES[party_type]
	for row in onboarding.get(get_table(party_type)):
		if row.status == "Created":
			continue
		row.party = row.party if row.party in parties.names else find_party(parties, row.party_name)
		if not row.party:
			error = (
				_("{0} could not be added: {1}").format(row.party_name, not_added[row.party_name])
				if row.party_name in not_added
				else _("{0} is not in ERPNext yet.").format(row.party_name)
			)
			row.update({"status": "Failed", "error": error})
			continue
		if row.party in parties.disabled:
			row.update(
				{"status": "Failed", "error": _("{0} is disabled. Enable it first.").format(row.party)}
			)
			continue
		party_currency = get_party_currency(party_type, row.party, company)
		if party_currency != currency:
			row.update(
				{
					"status": "Failed",
					"error": _(
						"{0}'s account is in {1}. Opening invoices in another currency can't be posted here yet."
					).format(row.party, party_currency),
				}
			)
			continue
		# dated as in the old books when the books have that year, so ageing stays right
		posting_date = row.invoice_date if has_fiscal_year(row.invoice_date, company) else day_before
		savepoint = f"opening_invoice_{row.idx}"
		frappe.db.savepoint(savepoint)
		try:
			if flt(row.outstanding_amount) < 0:
				voucher = post_advance(company, party_type, row.party, row, posting_date, temporary)
				row.update({"status": "Created", "invoice": voucher, "error": None})
				continue
			tool.set(
				"invoices",
				[
					{
						"party": row.party,
						"party_name": row.party_name,
						"posting_date": posting_date,
						"due_date": max(getdate(row.due_date or posting_date), getdate(posting_date)),
						"outstanding_amount": row.outstanding_amount,
						"invoice_number": row.invoice_number,
						"supplier_invoice_date": row.invoice_date if party_type == "Supplier" else None,
						"temporary_opening_account": temporary,
					}
				],
			)
			invoice = tool.get_invoices()[0]
			# the amounts are in the company's currency, whatever the party usually uses
			invoice.currency = currency
			if party_type == "Supplier":
				# a supplier's invoice number is theirs, not a name for our purchase invoice
				invoice.bill_no = invoice.pop("invoice_number", None)
			doc = frappe.get_doc(invoice)
			doc.flags.ignore_mandatory = True
			doc.flags.dont_auto_add_taxes = True
			number = invoice.get("invoice_number")
			if number and frappe.db.get_value(doc.doctype, number, "docstatus") == 2:
				# posted again after a cancel: ERPNext's amended name, like "INV-1-1"
				doc.amended_from = number
				doc.insert()
			else:
				doc.insert(set_name=number)
			doc.submit()
			row.update({"status": "Created", "invoice": doc.name, "error": None})
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			frappe.clear_messages()
			row.update({"status": "Failed", "error": strip_html(cstr(e)) or e.__class__.__name__})

	onboarding.flags.posting = True
	onboarding.save()


def get_party_currency(party_type: str, party: str, company: str) -> str:
	"""The currency of the receivable or payable account a customer's or supplier's invoices go to."""
	account = get_party_account(party_type, party, company)
	return frappe.get_cached_value("Account", account, "account_currency")


def get_new_names(onboarding, party_type: str, parties: frappe._dict) -> list[str]:
	"""The names on the draft rows that are no customer or supplier yet."""
	return sorted(
		{
			row.party_name
			for row in onboarding.get(get_table(party_type))
			if row.status != "Created"
			and row.party_name
			and row.party not in parties.names
			and not find_party(parties, row.party_name)
		}
	)


def make_party(party_type: str, name: str):
	"""A new, unsaved customer or supplier with this name and the defaults a new one gets."""
	party = frappe.new_doc(party_type)
	party.set(PARTY_NAME_FIELDS[party_type], name)
	settings = "Selling Settings" if party_type == "Customer" else "Buying Settings"
	for field in PARTY_DEFAULTS[party_type]:
		if not party.get(field):
			party.set(field, frappe.db.get_single_value(settings, field) or None)
	return party


def add_parties(onboarding, party_type: str, parties: frappe._dict) -> dict[str, str]:
	"""Add each customer or supplier the draft rows name that ERPNext doesn't have, each with a normal
	save so mandatory fields apply. Returns why each one that could not be added was not."""
	not_added = {}
	for name in get_new_names(onboarding, party_type, parties):
		frappe.db.savepoint("opening_party")
		shown_before = len(frappe.local.message_log)
		try:
			make_party(party_type, name).insert()
		except Exception as e:
			frappe.db.rollback(save_point="opening_party")
			# what the save showed, like which mandatory field is empty; the error only names fields
			shown = [strip_html(cstr(m.get("message"))) for m in frappe.local.message_log[shown_before:]]
			frappe.local.message_log = frappe.local.message_log[:shown_before]
			not_added[name] = "; ".join(filter(None, shown)) or strip_html(cstr(e)) or e.__class__.__name__
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
		party = make_party(party_type, name)
		rows.append([None if df.is_child_table_field else party.get(df.fieldname) for df in exporter.fields])
	build_xlsx_response(rows, _("New customers") if party_type == "Customer" else _("New suppliers"))


def post_advance(company: str, party_type: str, party: str, row, posting_date, temporary: str) -> str:
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
	je.append(
		"accounts",
		{
			"account": get_party_account(party_type, party, company),
			"party_type": party_type,
			"party": party,
			"is_advance": "Yes",
			"debit_in_account_currency": max(amount, 0),
			"credit_in_account_currency": max(-amount, 0),
		},
	)
	je.append(
		"accounts",
		{
			"account": temporary,
			"debit_in_account_currency": max(-amount, 0),
			"credit_in_account_currency": max(amount, 0),
		},
	)
	je.insert()
	je.submit()
	return je.name


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
	return frappe._dict(
		year=str(start.year) if start.year == end.year else f"{start.year}-{end.year}",
		year_start_date=start,
		year_end_date=end,
	)


def ensure_fiscal_year(date, company: str):
	"""Add the financial year the opening entries fall in, when ERPNext doesn't have it yet."""
	# a failed posting rolls the year back, but not ERPNext's cached list of years
	frappe.cache.delete_key("fiscal_years")
	year = get_missing_fiscal_year(date, company)
	if year:
		frappe.get_doc({"doctype": "Fiscal Year", **year}).insert()
		# so the form doesn't take a year undone by a failed posting for one that is there
		frappe.db.after_rollback.add(lambda: frappe.cache.delete_key("fiscal_years"))


def get_temporary_account(company: str, create: bool = False) -> str | None:
	"""The account that takes the opening difference. An imported chart may have none: posting adds one
	under Assets, as in the standard charts."""
	account = frappe.db.get_value(
		"Account", {"company": company, "account_type": "Temporary", "is_group": 0, "disabled": 0}
	)
	if account or not create:
		return account
	return add_account(company, _("Temporary Opening"), "Temporary", "Asset")


def add_account(company: str, account_name: str, account_type: str, root_type: str) -> str:
	"""A ledger of this type straight under the company's top-level account of the root type."""
	parent = frappe.db.get_value(
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
