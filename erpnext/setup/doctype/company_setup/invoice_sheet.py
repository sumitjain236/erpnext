# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import re
import unicodedata
from datetime import date
from difflib import get_close_matches

import frappe
from dateutil import parser
from frappe import _
from frappe.utils import cstr, fmt_money, get_user_date_format, getdate

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


def number(value: int) -> str:
	"""A count with the site's thousands separators, like 1,400."""
	return fmt_money(value, precision=0)
