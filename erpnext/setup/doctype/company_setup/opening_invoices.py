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
from contextlib import contextmanager
from datetime import date
from difflib import get_close_matches

import frappe
from dateutil import parser
from frappe import _
from frappe.core.doctype.data_import.exporter import Exporter
from frappe.model import table_fields
from frappe.utils import (
	add_days,
	add_years,
	cstr,
	flt,
	fmt_money,
	formatdate,
	get_user_date_format,
	getdate,
	strip_html,
)
from frappe.utils.xlsxutils import build_xlsx_response

from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
	get_checks_for_pl_and_bs_accounts,
	get_dimensions,
)
from erpnext.accounts.party import get_due_date, get_party_account
from erpnext.accounts.utils import get_fiscal_year
from erpnext.setup.doctype.company_setup.chart_sheet import (
	download_sheet,
	find_column,
	find_heading,
	get_amount,
	get_cell,
	get_dr_cr,
	get_signed_amount,
	is_total_row,
	read_sheet,
)
from erpnext.setup.setup_wizard.operations.install_fixtures import get_fy_details

TABLES = {"Customer": "opening_sales_invoices", "Supplier": "opening_purchase_invoices"}
# the posted rows undone one by one, kept to download and upload again
UNDONE_TABLES = {"Customer": "undone_sales_invoices", "Supplier": "undone_purchase_invoices"}
INVOICE_TYPES = {"Customer": "Sales", "Supplier": "Purchase"}
# what a row keeps when it is undone, or made again from an undone one
INVOICE_ROW_FIELDS = (
	"party_type",
	"party",
	"party_name",
	"invoice_number",
	"invoice_date",
	"due_date",
	"outstanding_amount",
	"source",
)
PARTY_NAME_FIELDS = {"Customer": "customer_name", "Supplier": "supplier_name"}
PARTY_ACCOUNT_FIELDS = {"Customer": "debit_to", "Supplier": "credit_to"}
INVOICE_ACCOUNT_TYPES = {"Customer": ("Receivable", "Asset"), "Supplier": ("Payable", "Liability")}
# the report title of every party ledger together, as get_similar_key gives it
PARTY_GROUP_TITLES = {"Customer": "sundry debtors", "Supplier": "sundry creditors"}
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
		"bill",
		"inv_no",
		"voucher_no",
		"ref_no",
		"num",
	),
	"invoice_date": ("invoice_date", "date", "bill_date", "voucher_date", "inv_date", "bill_dt", "inv_dt"),
	"due_date": ("due_date", "due_on", "due_dt"),
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
	# the receivable or payable account a bill is on, when the report says (an Excel tracker, Zoho)
	"ledger": ("ledger", "ledger_name", "account", "account_name", "a_c"),
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
# "30,000.00 Cr" or "30,000.00 Dr." after an amount
# only Latin accents: marks in other scripts, like Hindi vowel signs, change the name
ACCENTS = re.compile("[\u0300-\u036f]")
# how alike two names must be to look like one spelt two ways
LOOK_ALIKE = 0.85
# letters of initials like "I.C.I.C.I.", apart after punctuation is removed
INITIALS = re.compile(r"\b(\w) (?=\w\b)")
LEADING_MS = re.compile(r"^(m s|ms|messrs) ")
LEGAL_WORDS = re.compile(r"\b(private|pvt|limited|ltd|llp|inc)\b")
# above this many invoices are made in the background
BACKGROUND_FROM = 50
# how often, in rows, a background posting tells the form how far it is
PROGRESS_EVERY = 10
# how often, in rows, a background posting saves what it posted (each invoice is posted on its own)
COMMIT_EVERY = 100


def get_table(party_type: str) -> str:
	if party_type not in TABLES:
		frappe.throw(
			_("Opening invoices are for customers or suppliers, not {0}.").format(party_type),
			title=_("Not a customer or supplier"),
		)
	return TABLES[party_type]


def get_parties(party_type: str) -> frappe._dict:
	"""Every customer or supplier, disabled ones too: shown names by ID, IDs by the ways a name
	matches, and the disabled IDs."""
	name_field = PARTY_NAME_FIELDS[party_type]
	parties = frappe._dict(names={}, exact={}, similar={}, disabled=set())
	# only those the user may see: a name matched or suggested is one they can open
	for party in frappe.get_list(party_type, fields=["name", name_field, "disabled"], limit=0):
		parties.names[party.name] = party.get(name_field) or party.name
		for name in {party.name, party.get(name_field)}:
			if name:
				parties.exact.setdefault(get_exact_key(name), set()).add(party.name)
				parties.similar.setdefault(get_similar_key(name), set()).add(party.name)
		if party.disabled:
			parties.disabled.add(party.name)
	return parties


def get_exact_key(name: str) -> str:
	return " ".join(cstr(name).casefold().rstrip(" .").split())


def get_similar_key(name: str) -> str:
	"""A name without case, accents, punctuation, words like Pvt Ltd or a leading M/s, with "&" read
	as "and" and initials joined, so "M/s I.C.I.C.I. Bank" is "icici bank"."""
	text = ACCENTS.sub("", unicodedata.normalize("NFKD", cstr(name))).casefold().replace("&", " and ")
	key = " ".join(LEGAL_WORDS.sub(" ", re.sub(r"[\W_]+", " ", text)).split())
	return INITIALS.sub(r"\1", LEADING_MS.sub("", key))


def normalize_account_name(name: str) -> str:
	"""A name as people mean it: case, punctuation and "&" set aside, and a trailing "A/c" or
	"Account" dropped, so "Furniture & Fixtures A/c" is "furniture and fixtures"."""
	words = re.sub(r"[^a-z0-9 ]", " ", cstr(name).lower().replace("&", " and ")).split()
	if words[-2:] == ["a", "c"]:
		words = words[:-2]
	elif words[-1:] in (["ac"], ["account"]):
		words = words[:-1]
	return " ".join(words)


def get_singular_key(name: str) -> str:
	"""A name as normalize_account_name has it, each word in the singular: "Plants and Machineries" is
	"plant and machinery"."""
	return " ".join(stem(word) for word in normalize_account_name(name).split())


def stem(word: str) -> str:
	"""A word in the singular: "salaries" is "salary", "taxes" is "tax"."""
	if len(word) > 4 and word.endswith("ies"):
		return word[:-3] + "y"
	if len(word) > 4 and word.endswith(("ses", "xes", "ches", "shes")):
		return word[:-2]
	if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
		return word[:-1]
	return word


def find_party(parties: frappe._dict, name: str) -> str | None:
	"""The one customer or supplier a name means: the same name ignoring case and spaces, else also
	ignoring accents, punctuation and words like Pvt Ltd. None when none or more than one match."""
	for index, key in ((parties.exact, get_exact_key(name)), (parties.similar, get_similar_key(name))):
		matches = index.get(key) if key else None
		if matches:
			return next(iter(matches)) if len(matches) == 1 else None
	return None


def get_posted_names(onboarding, party_type: str) -> dict[str, str]:
	"""The customer or supplier each name on an earlier sheet went to, by its name key, so the next
	office's sheet, or the same one after an undo, links the same spelling without asking again."""
	undone = onboarding.get(UNDONE_TABLES[party_type])
	posted = [row for row in onboarding.get(get_table(party_type)) if row.status == "Created"]
	return {
		get_similar_key(row.party_name): row.party
		for row in [*undone, *posted]
		if row.party and row.party_name
	}


def match_party(parties: frappe._dict, posted: dict[str, str], name: str) -> str | None:
	"""find_party, else the customer or supplier an earlier sheet's same spelling went to."""
	party = find_party(parties, name)
	if not party:
		party = posted.get(get_similar_key(name))
	return party if party in parties.names else None


def get_suggestions(parties: frappe._dict, names: list[str]) -> dict[str, str]:
	"""For names that are not a customer or supplier yet, the ID of one that looks almost the same:
	a typo of it, or its short form, like "Mahindra Lifespace" for "Mahindra Lifespace Developers Ltd"."""
	known = [key for key, matches in parties.similar.items() if len(matches) == 1]
	suggestions = {}
	for name in set(names):
		key = get_similar_key(name)
		if not key or find_party(parties, name):
			continue
		match = get_close_matches(key, known, n=1, cutoff=LOOK_ALIKE) or get_short_form_of(key, known)
		if match:
			suggestions[name] = next(iter(parties.similar[match[0]]))
	return suggestions


def get_short_form_of(key: str, known: list[str]) -> list[str]:
	"""The known name this one is a short form of: each of its words starts a word of the known name,
	in order and from the first, like "afcons infra" for "afcons infrastructure". When several fit,
	the one with the fewest words besides."""
	words = key.split()
	if len(words) < 2:
		return []
	fits = []
	for other in known:
		other_words = other.split()
		if not other_words[0].startswith(words[0]):
			continue
		found = 0
		for word in other_words:
			if found < len(words) and word.startswith(words[found]):
				found += 1
		if found == len(words):
			fits.append((len(other_words), other))
	fits.sort()
	if fits and (len(fits) == 1 or fits[0][0] < fits[1][0]):
		return [fits[0][1]]
	return []


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
	"""The rows of a receivables or payables report, how many are advances, the columns that were
	not used, and the dates that could not be read (bad_dates). Reports from other software are read as exported: headings under title rows,
	totals and subtotals anywhere, an amount split into Dr and Cr columns (Tally), and bills grouped
	under a line naming their customer or supplier (QuickBooks, Busy)."""
	report, keys = read_sheet(file_url)
	rows = [row.cells for row in report]
	start = find_heading(keys, is_heading_row)
	sheet = get_sheet_columns(
		keys[start] if start is not None else [],
		keys[start + 1] if start is not None and start + 1 < len(keys) else [],
	)
	if sheet.columns["party_name"] is None:
		frappe.throw(
			_("The sheet needs a Name column and an Outstanding column."), title=_("Columns not found")
		)

	invoices, advances, party, bad_dates = [], 0, None, []
	body = rows[start + 1 + sheet.sub_heading :]
	day_first = {
		field: get_day_first(get_cell(row, sheet.columns[field]) for row in body)
		for field in ("invoice_date", "due_date")
	}
	for row in body:
		if is_total_row(row):
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
		dates = {}
		for field in ("invoice_date", "due_date"):
			cell = get_cell(row, sheet.columns[field])
			dates[field] = get_sheet_date(cell, day_first[field])
			if cstr(cell).strip() and not dates[field]:
				bad_dates.append(
					{
						"party_name": name,
						"invoice_number": invoice_number,
						"field": field,
						"text": cstr(cell).strip(),
					}
				)
		invoices.append(
			{
				"party_name": name,
				# Tally's mark for an amount set against no bill
				"invoice_number": None if invoice_number.lower() in ("", "on account") else invoice_number,
				**dates,
				"outstanding_amount": amount,
				"ledger": cstr(get_cell(row, sheet.columns["ledger"])).strip(),
			}
		)
	unused = [
		cstr(label).strip()
		for index, label in enumerate(rows[start])
		if index not in sheet.used and cstr(label).strip()
	]
	titles = [
		value for row in rows[: start or 0] for value in row if isinstance(value, str) and value.strip()
	]
	return frappe._dict(
		invoices=invoices, advances=advances, unused_columns=unused, titles=titles, bad_dates=bad_dates
	)


def is_heading_row(keys: list[str]) -> bool:
	"""A row of column headings with an amount among them, not a title like "Debtors outstanding"."""
	return sum(1 for key in keys if key) > 1 and find_invoice_column(keys, "outstanding_amount") is not None


def get_sheet_columns(headers: list[str], below: list[str]) -> frappe._dict:
	"""Which column holds what in a receivables or payables report, from its heading row and the
	row under it."""
	columns = {field: find_invoice_column(headers, field) for field in COLUMN_NAMES}
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


def find_invoice_column(headers: list[str], field: str) -> int | None:
	"""The column of a field: by one of its usual headings, else by the words in a heading, like
	"Outstanding (INR)" or "Their Bill No"."""
	column = find_column(headers, COLUMN_NAMES[field])
	if column is not None:
		return column
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
	usual = "dr" if party_type == "Customer" else "cr"
	side = get_dr_cr(side)
	if side:
		amount = get_amount(value)
		return amount if side == usual else -amount
	if get_dr_cr(value):
		# debit minus credit, as a trial balance reads it: a supplier's usual side is the credit
		signed = get_signed_amount(value)
		return signed if usual == "dr" else -signed
	return get_amount(value)


def get_sheet_date(value, day_first: bool | None = None) -> str | None:
	"""A date cell, or text in the yyyy-mm-dd form or with the day first or not: as day_first says, the
	way the whole column writes dates (get_day_first), else as the site writes them."""
	if not value:
		return None
	text = cstr(value).strip()
	if day_first is None:
		day_first = get_user_date_format().startswith("dd")
	day_first = day_first and not ISO_DATE.match(text)
	if not isinstance(value, str):
		return str(getdate(value)) if isinstance(value, date) else None
	# getdate shows its own error for text it can't read, like 31/02/2026: such a date is left empty
	try:
		return str(parser.parse(text, dayfirst=day_first).date())
	except (ValueError, OverflowError):
		return None


def get_day_first(values) -> bool | None:
	"""Whether a column's text dates put the day first, from a value that can only be read one way,
	like 25/03/2026 (day first) or 03/25/2026 (month first). None when every value fits both: the
	site's format decides then."""
	for value in values:
		parts = (
			re.match(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}\s*$", value)
			if isinstance(value, str)
			else None
		)
		if not parts:
			continue
		first, second = int(parts.group(1)), int(parts.group(2))
		if first > 12 >= second:
			return True
		if second > 12 >= first:
			return False
	return None


@frappe.whitelist()
def download_opening_invoices(company: str, party_type: str, undone_from: str | None = None):
	"""A sheet to fill with unpaid invoices, with the drafts already entered. With undone_from, the
	invoices undone from that source instead, with their account, to fix and upload again."""
	onboarding = frappe.get_doc("Company Setup", company)
	onboarding.check_permission("read")
	table = get_table(party_type)
	if undone_from is not None:
		rows = [[*SHEET_COLUMNS, "Ledger"]]
		for row in onboarding.get(UNDONE_TABLES[party_type]):
			if (row.source or "") == undone_from:
				rows.append(
					[
						row.party_name or row.party,
						row.invoice_number,
						row.invoice_date,
						row.due_date,
						row.outstanding_amount,
						row.account,
					]
				)
		download_sheet(
			f"{frappe.scrub(company)}_undone_{frappe.scrub(party_type)}_invoices",
			rows,
			_("Opening Invoices"),
		)
		return
	rows = [list(SHEET_COLUMNS)]
	for row in onboarding.get(table):
		if row.status != "Created":
			rows.append(
				[row.party_name, row.invoice_number, row.invoice_date, row.due_date, row.outstanding_amount]
			)

	download_sheet(
		f"{frappe.scrub(company)}_opening_{frappe.scrub(party_type)}_invoices",
		rows,
		_("Opening Invoices"),
	)


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


def get_sheet_match(onboarding, party_type: str) -> dict | None:
	"""What the trial balance says customers owe or suppliers are owed, by account when it says so,
	set against the invoices or bills entered so far, posted or not. None without a trial balance."""
	account_type = "Receivable" if party_type == "Customer" else "Payable"
	sheet = (frappe.parse_json(onboarding.sheet_accounts or "{}") or {}).get(account_type)
	if not sheet:
		return None
	# the sheet keeps the ledger's side: a supplier's balance is a credit
	sign = 1 if party_type == "Customer" else -1
	default = get_party_account(party_type, None, onboarding.company)
	# a draft an invoice made by hand stands for counts once, as that invoice
	hand = get_hand_posted(onboarding, party_type)
	hand_posted = {row.name for row in hand}
	rows = [row for row in onboarding.get(get_table(party_type)) if row.name not in hand_posted]
	accounts = get_row_accounts(onboarding, party_type, rows)
	waiting = get_waiting_parties(onboarding, party_type, rows)
	entered = {}
	# the drafts that can't post as they are: in another currency, by currency, or of a disabled party
	left_out = {"currencies": {}, "disabled": {"amount": 0, "count": 0}}
	for row in rows:
		account = accounts[row.name]
		line = entered.setdefault(
			account, {"amount": 0, "count": 0, "draft": 0, "waiting": 0, "disabled": {}, "left_out": 0}
		)
		amount = flt(row.outstanding_amount)
		line["amount"] += amount
		line["count"] += 1
		if row.status != "Created":
			line["draft"] += amount
			if (not row.party and not row.create_party) or row.party in waiting.other_currency:
				line["waiting"] += amount
			elif row.party in waiting.disabled:
				line["disabled"][row.party] = line["disabled"].get(row.party, 0) + amount
			if row.party in waiting.other_currency:
				bucket = left_out["currencies"].setdefault(
					waiting.other_currency[row.party], {"amount": 0, "count": 0}
				)
			elif row.party in waiting.disabled:
				bucket = left_out["disabled"]
			else:
				continue
			bucket["amount"] += amount
			bucket["count"] += 1
			line["left_out"] += amount
	outside = get_outside_invoices(onboarding, party_type)
	for invoice in outside:
		line = entered.setdefault(
			invoice.account,
			{"amount": 0, "count": 0, "draft": 0, "waiting": 0, "disabled": {}, "left_out": 0},
		)
		line["amount"] += flt(invoice.amount)
		line["count"] += 1
	# a line no account stands for, like Busy's Sundry Debtors beside its sub-groups, is the only other
	# ledger's, else the default ledger's when that has no line; else it can only match in total
	if "" in sheet and len(sheet) > 1:
		others = [a for a in get_invoice_accounts(onboarding.company, party_type) if a not in sheet]
		line = others[0] if len(others) == 1 else default if default not in sheet else None
		if line:
			sheet = {(line if account == "" else account): amount for account, amount in sheet.items()}
	by_account = "" not in sheet
	accounts = sorted(set(sheet) | set(entered)) if by_account else [None]
	return {
		"by_account": by_account,
		"sheet": sign * sum(flt(amount) for amount in sheet.values()),
		"entered": sum(line["amount"] for line in entered.values()),
		"lines": [
			{
				"account": account,
				"sheet": sign * flt(sheet[account]) if account in sheet else None,
				"entered": entered.get(account, {}).get("amount", 0),
				"count": entered.get(account, {}).get("count", 0),
				"draft": entered.get(account, {}).get("draft", 0),
				# drafts that stay drafts when posting: unconfirmed or in another currency, and the
				# disabled customers' or suppliers' unless they are posted anyway
				"waiting": entered.get(account, {}).get("waiting", 0),
				"disabled": entered.get(account, {}).get("disabled", {}),
				# of those, the ones in another currency or of a disabled party, as the card leaves out
				"left_out": entered.get(account, {}).get("left_out", 0),
			}
			for account in accounts
		]
		if by_account
		else [],
		"outside": {"count": len(outside), "amount": sum(flt(invoice.amount) for invoice in outside)},
		"hand_posted": {"count": len(hand), "amount": sum(flt(row.outstanding_amount) for row in hand)},
		"left_out": left_out,
	}


def get_waiting_parties(onboarding, party_type: str, rows) -> frappe._dict:
	"""The draft rows' customers or suppliers that keep them drafts when posting: disabled, or in
	another currency."""
	from erpnext.setup.doctype.company_setup.company_setup import get_other_currency_parties

	parties = {row.party for row in rows if row.status != "Created" and row.party}
	return frappe._dict(
		other_currency=get_other_currency_parties(onboarding.company, party_type, parties),
		disabled=set(
			frappe.get_all(
				party_type, filters={"name": ("in", list(parties) or [""]), "disabled": 1}, pluck="name"
			)
		),
	)


def get_hand_posted(onboarding, party_type: str) -> list:
	"""The draft rows an opening invoice made by hand stands for: same customer or supplier, and its
	number, or else its amount in the company's currency when only one invoice of theirs has it."""
	drafts = [row for row in onboarding.get(get_table(party_type)) if row.status != "Created" and row.party]
	if not drafts:
		return []
	parties = {row.party for row in drafts}
	outside = [doc for doc in get_outside_invoices(onboarding, party_type) if doc.party in parties]
	matched, used = [], set()
	for row in drafts:
		theirs = [doc for doc in outside if doc.party == row.party and doc.name not in used]
		number = cstr(row.invoice_number).strip()
		found = [doc for doc in theirs if number and number in (doc.name, doc.get("bill_no"))]
		if not found:
			found = [doc for doc in theirs if abs(flt(doc.amount) - flt(row.outstanding_amount)) < 1]
		if len(found) == 1:
			used.add(found[0].name)
			row.hand_invoice = found[0].name
			matched.append(row)
	return matched


def get_cached(onboarding, key: str, method):
	"""What method gives for the onboarding as last saved or loaded: the steps, onload and checks ask
	for it several times in one request."""
	stored = onboarding.flags.get(key)
	if not stored or stored[0] != onboarding.modified:
		stored = onboarding.flags[key] = (onboarding.modified, method(onboarding))
	return stored[1]


def get_outside_invoices(onboarding, party_type: str) -> list[frappe._dict]:
	"""The opening invoices made outside Company Setup, by hand or Data Import, with their party,
	account and amount in the company's currency, whatever currency they were billed in. Read once
	per save of the setup: the form asks several times on each open."""
	party = "customer" if party_type == "Customer" else "supplier"
	invoices = get_cached(
		onboarding,
		f"outside_invoices_{party_type}",
		lambda onboarding: frappe.get_all(
			"Sales Invoice" if party_type == "Customer" else "Purchase Invoice",
			filters={"company": onboarding.company, "is_opening": "Yes", "docstatus": 1},
			fields=[
				"name",
				f"{party} as party",
				f"{PARTY_ACCOUNT_FIELDS[party_type]} as account",
				"base_grand_total as amount",
				*(["bill_no"] if party_type == "Supplier" else []),
			],
		),
	)
	own = {row.invoice for row in onboarding.get(get_table(party_type)) if row.invoice}
	return [invoice for invoice in invoices if invoice.name not in own]


def get_ledger_overs(onboarding, party_type: str, disabled_parties: list[str] | None = None) -> list[dict]:
	"""The receivable or payable ledgers the drafts that post would put above their trial balance
	line while another ledger is short of its own: money going to the wrong ledger. Empty when the
	trial balance doesn't say by ledger, or none is short. disabled_parties: those posted anyway."""
	match = get_sheet_match(onboarding, party_type)
	if not match or not match["by_account"]:
		return []
	lines = []
	for line in match["lines"]:
		stays = line["waiting"] + sum(
			amount for party, amount in line["disabled"].items() if party not in (disabled_parties or [])
		)
		lines.append(line | {"entered": line["entered"] - stays, "draft": line["draft"] - stays})
	short = [line for line in lines if line["sheet"] is not None and line["sheet"] - line["entered"] >= 1]
	if not short:
		return []
	return [
		{
			"account": line["account"],
			"account_name": get_account_name(line["account"]),
			"sheet": flt(line["sheet"]),
			"entered": flt(line["entered"], 2),
			"over": flt(line["entered"] - flt(line["sheet"]), 2),
			"short": [
				{
					"account": other["account"],
					"account_name": get_account_name(other["account"]),
					"short": flt(other["sheet"] - other["entered"], 2),
				}
				for other in short
			],
		}
		for line in lines
		if abs(flt(line["draft"])) >= 0.005 and line["entered"] - flt(line["sheet"]) >= 1
	]


def get_ledger_over_message(over: dict, currency: str) -> str:
	short = over["short"][0]
	return _(
		"These rows would put {0} on {1}, {2} more than its trial balance line, while {3} is {4} short. Pick the ledger this sheet is for in Post to, or fix the rows."
	).format(
		fmt_money(over["entered"], currency=currency),
		over["account_name"],
		fmt_money(over["over"], currency=currency),
		short["account_name"],
		fmt_money(short["short"], currency=currency),
	)


def get_account_name(account: str) -> str:
	return frappe.get_cached_value("Account", account, "account_name") or account


def get_title_account(company: str, party_type: str, titles: list[str]) -> str | None:
	"""The receivable or payable ledger a report's title rows name, like Tally's "Ledger: Debtors -
	Retainers", when the company has several and one name is found whole: the longest, when only one
	is that long. Busy's and Tally's "Sundry Debtors" or "Sundry Creditors" is the group of every
	party ledger, so it names none of them."""
	accounts = get_invoice_accounts(company, party_type)
	if len(accounts) < 2 or not titles:
		return None
	group = f" {PARTY_GROUP_TITLES[party_type]} "
	keys = [f" {get_similar_key(title)} ".replace(group, " ") for title in titles]
	found = sorted(
		(
			(len(key), account.name)
			for account in frappe.get_all(
				"Account", filters={"name": ("in", accounts)}, fields=["name", "account_name"]
			)
			if (key := get_similar_key(account.account_name)) and any(f" {key} " in title for title in keys)
		),
		reverse=True,
	)
	if not found or (len(found) > 1 and found[0][0] == found[1][0]):
		return None
	return found[0][1]


def get_party_accounts(party_type: str, parties, company: str) -> tuple[dict, dict, dict]:
	"""get_party_account for many parties in a few queries: the account each one's invoices go to,
	the account saved on the party itself, and the one saved on the party or its group (neither is
	the company's default)."""
	parties = list(set(filter(None, parties)))
	if not parties:
		return {}, {}, {}
	group_field = "customer_group" if party_type == "Customer" else "supplier_group"
	saved = dict(
		frappe.get_all(
			"Party Account",
			filters={"parenttype": party_type, "parent": ("in", parties), "company": company},
			fields=["parent", "account"],
			as_list=True,
		)
	)
	groups = dict(
		frappe.get_all(
			party_type, filters={"name": ("in", parties)}, fields=["name", group_field], as_list=True
		)
	)
	group_accounts = dict(
		frappe.get_all(
			"Party Account",
			filters={
				"parenttype": f"{party_type} Group",
				"parent": ("in", list(set(filter(None, groups.values()))) or [""]),
				"company": company,
			},
			fields=["parent", "account"],
			as_list=True,
		)
	)
	kept = {
		party: account
		for party in parties
		if (account := saved.get(party) or group_accounts.get(groups.get(party)))
	}
	default = get_party_account(party_type, None, company)
	return {party: kept.get(party) or default for party in parties}, saved, kept


def get_new_party_accounts(party_type: str, company: str, rows) -> dict[str, str]:
	"""For rows whose customer or supplier is to be added, by name: the ledger saved on the group
	the new one gets."""
	field = PARTY_DEFAULTS[party_type][0]
	groups = {
		row.party_name: make_party(party_type, row.party_name, company, row.party_group).get(field)
		for row in rows
		if row.create_party and not row.party
	}
	group_accounts = dict(
		frappe.get_all(
			"Party Account",
			filters={
				"parenttype": f"{party_type} Group",
				"parent": ("in", list(set(filter(None, groups.values()))) or [""]),
				"company": company,
			},
			fields=["parent", "account"],
			as_list=True,
		)
	)
	return {name: group_accounts[group] for name, group in groups.items() if group in group_accounts}


def get_posted_accounts(party_type: str, rows) -> dict[str, str]:
	"""The receivable or payable account each posted row's invoice or advance is on, by its name."""
	posted = [row for row in rows if row.status == "Created" and row.invoice]
	invoices = [row.invoice for row in posted if flt(row.outstanding_amount) >= 0]
	advances = [row.invoice for row in posted if flt(row.outstanding_amount) < 0]
	accounts = {}
	if invoices:
		accounts |= dict(
			frappe.get_all(
				"Sales Invoice" if party_type == "Customer" else "Purchase Invoice",
				filters={"name": ("in", invoices)},
				fields=["name", PARTY_ACCOUNT_FIELDS[party_type]],
				as_list=True,
			)
		)
	if advances:
		accounts |= dict(
			frappe.get_all(
				"Journal Entry Account",
				filters={"parent": ("in", advances), "party_type": party_type},
				fields=["parent", "account"],
				as_list=True,
			)
		)
	return accounts


def get_account_clashes(onboarding, party_type: str) -> list[dict]:
	"""Customers or suppliers whose invoices about to be posted and whose earlier ones, or the account
	saved on them, are on different accounts, where something the user said picks one (a sheet's or a
	typed account, or the saved account) and the trial balance is short on that one and over on the
	other. "posted" when the earlier invoices are the odd ones, "draft" when these are, and "split"
	for an earlier sheet's invoices posted apart from the rest of that sheet (get_split_clashes)."""
	match = get_sheet_match(onboarding, party_type)
	if not match or not match["by_account"]:
		return []
	gap = {line["account"]: flt(line["sheet"]) - flt(line["entered"]) for line in match["lines"]}
	in_sheet = {line["account"] for line in match["lines"] if line["sheet"] is not None}
	stray_drafts = get_stray_drafts(
		onboarding.get(get_table(party_type)),
		get_row_accounts(onboarding, party_type, onboarding.get(get_table(party_type))),
	)

	def caused_by_posted(account, posted_rows, target):
		"""Whether the posted rows, not this upload's strays, put the account over, and moving them
		keeps the target within its line."""
		if account in in_sheet and stray_drafts.get(account, 0) >= -gap.get(account, 0) - 0.005:
			return False
		return sum(flt(row.outstanding_amount) for row in posted_rows) <= gap.get(target, 0) + 0.005

	def is_short(account):
		return gap.get(account, 0) > 0.005

	def is_over(account):
		return gap.get(account, 0) < -0.005 or account not in in_sheet

	rows = onboarding.get(get_table(party_type))
	posted_accounts = get_posted_accounts(party_type, rows)
	party_accounts, saved_accounts, _kept = get_party_accounts(
		party_type, [row.party for row in rows], onboarding.company
	)
	by_party = {}
	for row in rows:
		if row.party:
			by_party.setdefault(row.party, []).append(row)
	clashes = {}
	for party, party_rows in by_party.items():
		drafts = [row for row in party_rows if row.status != "Created"]
		if not drafts:
			continue
		saved = saved_accounts.get(party)
		posted = {}
		for row in party_rows:
			if row.status == "Created" and posted_accounts.get(row.invoice):
				posted.setdefault(posted_accounts[row.invoice], []).append(row)
		going = {}
		for row in drafts:
			going.setdefault(row.account or party_accounts[party], []).append(row)
		for account, these in going.items():
			said = any(row.account for row in these)
			for other in (set(posted) | {saved}) - {account, None}:
				if not (said or other == saved or any(row.account for row in posted.get(other, []))):
					continue
				if (
					is_short(account)
					and is_over(other)
					and posted.get(other)
					and caused_by_posted(other, posted[other], account)
				):
					kind, target, moving = "posted", account, posted[other]
				elif is_short(other) and is_over(account):
					kind, target, moving = "draft", other, these
				else:
					continue
				clash = clashes.setdefault(
					(kind, target, other if kind == "posted" else account),
					{
						"kind": kind,
						"account": target,
						"from_account": other if kind == "posted" else account,
						"short": gap[target],
						"said": False,
						"saved": False,
						"earlier": False,
						"parties": [],
					},
				)
				# for the wording: whether this sheet says its account, the saved account is the one
				# to use, and earlier invoices are on the other account
				clash["said"] = clash["said"] or said
				clash["saved"] = clash["saved"] or saved == target
				clash["earlier"] = clash["earlier"] or bool(posted.get(other))
				clash["parties"].append(
					{
						"party": party,
						"sources": sorted({row.source or "" for row in moving}),
						"count": len(moving),
						"amount": sum(flt(row.outstanding_amount) for row in moving),
					}
				)
	splits = get_split_clashes(onboarding, party_type, match)
	# an over ledger an earlier sheet's strays explain: those move, not this sheet's rows
	split_from = {clash["from_account"] for clash in splits}
	clashes = [
		clash
		for clash in clashes.values()
		if not (clash["kind"] == "draft" and clash["from_account"] in split_from)
	]
	for clash in clashes + splits:
		for key in ("account", "from_account"):
			clash[f"{key}_name"] = frappe.get_cached_value("Account", clash[key], "account_name")
	return clashes + splits


def get_split_clashes(onboarding, party_type: str, match: dict) -> list[dict]:
	"""Posted invoices on a ledger over its trial balance line, from a sheet whose other rows are on a
	ledger short of its line: that sheet's strays, like names confirmed in Review names that went to
	the default ledger. One clash per sheet ledger and stray ledger."""
	gap = {line["account"]: flt(line["sheet"]) - flt(line["entered"]) for line in match["lines"]}
	in_sheet = {line["account"] for line in match["lines"] if line["sheet"] is not None}
	rows = onboarding.get(get_table(party_type))
	accounts = get_row_accounts(onboarding, party_type, rows)
	by_source = {}
	for row in rows:
		by_source.setdefault(row.source or "", {}).setdefault(accounts[row.name], []).append(row)
	stray_drafts = get_stray_drafts(rows, accounts)
	clashes = {}
	for source, on in by_source.items():
		if not source or len(on) < 2:
			continue
		# the sheet's ledger is where most of its rows are
		home = max(on, key=lambda account: len(on[account]))
		if gap.get(home, 0) <= 0.005:
			continue
		for account, these in on.items():
			strays = [row for row in these if row.status == "Created"]
			if account == home or not strays or (account in in_sheet and gap[account] >= -0.005):
				continue
			# this upload's own strays there explain the gap: those are the rows to look at
			if account in in_sheet and stray_drafts.get(account, 0) >= -gap[account] - 0.005:
				continue
			# moving them must not put the sheet's ledger over its own line
			if sum(flt(row.outstanding_amount) for row in strays) > gap[home] + 0.005:
				continue
			clash = clashes.setdefault(
				(home, account),
				{
					"kind": "split",
					"account": home,
					"from_account": account,
					"short": gap[home],
					"parties": [],
				},
			)
			for party in sorted({row.party for row in strays}):
				mine = [row for row in strays if row.party == party]
				clash["parties"].append(
					{
						"party": party,
						"sources": [source],
						"count": len(mine),
						"amount": sum(flt(row.outstanding_amount) for row in mine),
						"invoices": [row.invoice for row in mine],
					}
				)
	return list(clashes.values())


def get_stray_drafts(rows, accounts: dict[str, str]) -> dict[str, float]:
	"""By account, the draft amount going there from sheets whose rows mostly go elsewhere, like a row
	moved by hand: the ones that can put a ledger over, rather than what was posted."""
	by_source = {}
	for row in rows:
		on = by_source.setdefault(row.source or "", {})
		on[accounts[row.name]] = on.get(accounts[row.name], 0) + 1
	homes = {source: max(on, key=on.get) for source, on in by_source.items()}
	stray = {}
	for row in rows:
		account = accounts[row.name]
		if row.status != "Created" and account != homes[row.source or ""]:
			stray[account] = stray.get(account, 0) + flt(row.outstanding_amount)
	return stray


def get_row_accounts(onboarding, party_type: str, rows) -> dict[str, str]:
	"""The account each row is on, or will go to, by row name, as get_sheet_match counts it."""
	default = get_party_account(party_type, None, onboarding.company)
	posted_accounts = get_posted_accounts(party_type, rows)
	party_accounts = get_party_accounts(party_type, [row.party for row in rows], onboarding.company)[0]
	return {
		row.name: posted_accounts.get(row.invoice) or row.account or party_accounts.get(row.party) or default
		for row in rows
	}


def fix_account_clashes(onboarding, party_type: str, fixes: list[dict]):
	"""Do what the user ticked on the clashes found now, and nothing else: put these drafts on the
	other account, or cancel the earlier invoices and add them back as drafts on this sheet's account."""
	from erpnext.setup.doctype.company_setup.posted_entries import check_undo

	wanted = {(fix.get("kind"), fix.get("account"), fix.get("from_account")) for fix in fixes}
	rows = onboarding.get(get_table(party_type))
	posted_accounts = get_posted_accounts(party_type, rows)
	party_accounts = get_party_accounts(party_type, [row.party for row in rows], onboarding.company)[0]
	for clash in get_account_clashes(onboarding, party_type):
		if (clash["kind"], clash["account"], clash["from_account"]) not in wanted:
			continue
		parties = {party["party"] for party in clash["parties"]}
		if clash["kind"] == "split":
			check_undo(onboarding)
			by_source = {}
			for party in clash["parties"]:
				by_source.setdefault(party["sources"][0], []).extend(party["invoices"])
			move_posted(onboarding, party_type, by_source, clash["account"])
			rows = onboarding.get(get_table(party_type))
			continue
		if clash["kind"] == "draft":
			for row in rows:
				if (
					row.party in parties
					and row.status != "Created"
					and (row.account or party_accounts[row.party]) == clash["from_account"]
				):
					row.update({"account": clash["account"], "account_by_hand": 1})
			continue
		check_undo(onboarding)
		by_source = {}
		for row in rows:
			if (
				row.party in parties
				and row.status == "Created"
				and posted_accounts.get(row.invoice) == clash["from_account"]
			):
				by_source.setdefault(row.source or "", []).append(row.invoice)
		move_posted(onboarding, party_type, by_source, clash["account"])
		rows = onboarding.get(get_table(party_type))
	onboarding.flags.posting = True
	onboarding.save()


def move_posted(onboarding, party_type: str, by_source: dict[str, list[str]], account: str):
	"""Undo these posted invoices and add them back as drafts on the account, to post again."""
	from erpnext.setup.doctype.company_setup.posted_entries import undo_invoices

	for source, invoices in by_source.items():
		undo_invoices(onboarding, party_type, source, invoices)
		for undone in onboarding.get(UNDONE_TABLES[party_type]):
			if undone.invoice in invoices:
				onboarding.append(
					get_table(party_type),
					{field: undone.get(field) for field in INVOICE_ROW_FIELDS}
					| {"account": account, "account_by_hand": 1, "status": "Draft"},
				)


def set_ledger_accounts(company: str, party_type: str, invoices: list[dict]) -> list[str]:
	"""Put each invoice on the account its sheet's ledger column names, when that is one of the
	company's receivable or payable accounts, by name with or without the company's abbreviation.
	Returns the ledger names that are not."""
	accounts = get_invoice_accounts(company, party_type)
	by_name = {account.lower(): account for account in accounts}
	by_name |= {
		name.lower(): account
		for account, name in frappe.get_all(
			"Account",
			filters={"name": ("in", accounts or [""])},
			fields=["name", "account_name"],
			as_list=True,
		)
	}
	unknown = set()
	for invoice in invoices:
		ledger = invoice.pop("ledger", None)
		if not ledger:
			continue
		if ledger.lower() in by_name:
			# the sheet names the ledger: as if picked on the row
			invoice.update({"account": by_name[ledger.lower()], "account_by_hand": 1})
		else:
			unknown.add(ledger)
	return sorted(unknown)


def get_invoice_accounts(company: str, party_type: str) -> list[str]:
	"""The accounts a customer's or supplier's opening invoices can go to, in the company's currency.
	An employee advance is a payable account too, but an asset, so it isn't one of them."""
	account_type, root_type = INVOICE_ACCOUNT_TYPES[party_type]
	return frappe.get_all(
		"Account",
		filters={
			"company": company,
			"account_type": account_type,
			"root_type": root_type,
			"is_group": 0,
			"disabled": 0,
			"account_currency": frappe.get_cached_value("Company", company, "default_currency"),
		},
		pluck="name",
		order_by="lft",
	)


def get_missing_default_account(onboarding, party_type: str) -> str | None:
	"""Why the draft rows can't post: the company has no default receivable or payable account and a
	row needs it, having no account of its own. A chart with such accounts only in another currency
	leaves the default empty."""
	if get_party_account(party_type, None, onboarding.company):
		return None
	with_account = set(
		frappe.get_all(
			"Party Account",
			filters={"company": onboarding.company, "parenttype": party_type},
			pluck="parent",
		)
	)
	if not any(
		not row.account and row.party not in with_account
		for row in onboarding.get(get_table(party_type))
		if row.status != "Created"
	):
		return None
	currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
	if party_type == "Customer":
		return _(
			"{0} has no default receivable account in {1}. Set Default Receivable Account on the company first."
		).format(onboarding.company, currency)
	return _(
		"{0} has no default payable account in {1}. Set Default Payable Account on the company first."
	).format(onboarding.company, currency)


def get_party_currency(party_type: str, party: str, company: str, account: str | None = None) -> str:
	"""The currency of the receivable or payable account a customer's or supplier's invoices go to,
	or else the one it is billed in, like a USD customer on the company's own receivable account."""
	account = account or get_party_account(party_type, party, company)
	currency = frappe.get_cached_value("Account", account, "account_currency")
	if currency != frappe.get_cached_value("Company", company, "default_currency"):
		return currency
	return frappe.db.get_value(party_type, party, "default_currency") or currency


def get_new_names(onboarding, party_type: str, parties: frappe._dict, confirmed: bool = False) -> list[str]:
	"""The names on the draft rows that are no customer or supplier yet; with confirmed, only those the
	user confirmed as new."""
	return sorted(
		{
			row.party_name
			for row in onboarding.get(get_table(party_type))
			if row.status != "Created"
			and row.party_name
			and row.party not in parties.names
			and not find_party(parties, row.party_name)
			and (row.create_party or not confirmed)
		}
	)


def make_party(party_type: str, name: str, company: str | None = None, group: str | None = None):
	"""A new, unsaved customer or supplier with this name and the defaults a new one gets. Where the
	settings have none, as after a setup wizard that skipped them, the company's country as the
	territory, and the only group there is; else none, as a group can't be picked."""
	party = frappe.new_doc(party_type)
	party.set(PARTY_NAME_FIELDS[party_type], name)
	# the group picked when the name was confirmed as new
	party.set(PARTY_DEFAULTS[party_type][0], group or None)
	settings = "Selling Settings" if party_type == "Customer" else "Buying Settings"
	for field in PARTY_DEFAULTS[party_type]:
		if not party.get(field):
			party.set(field, frappe.db.get_single_value(settings, field) or get_default_node(field, company))
	return party


def get_default_node(field: str, company: str | None) -> str | None:
	doctype = {
		"customer_group": "Customer Group",
		"territory": "Territory",
		"supplier_group": "Supplier Group",
	}[field]
	if field == "territory" and company:
		country = frappe.get_cached_value("Company", company, "country")
		if country and frappe.db.get_value("Territory", country, "is_group") == 0:
			return country
	leaves = frappe.get_all(doctype, filters={"is_group": 0}, pluck="name", limit=2)
	return leaves[0] if len(leaves) == 1 else None


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


def number(value: int) -> str:
	"""A count with the site's thousands separators, like 1,400."""
	return fmt_money(value, precision=0)


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
