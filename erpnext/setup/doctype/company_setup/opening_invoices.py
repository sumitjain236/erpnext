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
# the posted rows undone one by one, kept to download and upload again
UNDONE_TABLES = {"Customer": "undone_sales_invoices", "Supplier": "undone_purchase_invoices"}
INVOICE_TYPES = {"Customer": "Sales", "Supplier": "Purchase"}
PARTY_NAME_FIELDS = {"Customer": "customer_name", "Supplier": "supplier_name"}
PARTY_ACCOUNT_FIELDS = {"Customer": "debit_to", "Supplier": "credit_to"}
INVOICE_ACCOUNT_TYPES = {"Customer": ("Receivable", "Asset"), "Supplier": ("Payable", "Liability")}
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
	return " ".join(cstr(name).casefold().rstrip(" .").split())


def get_similar_key(name: str) -> str:
	"""A name without case, accents, punctuation, words like Pvt Ltd or a leading M/s, with "&" read
	as "and" and initials joined, so "M/s I.C.I.C.I. Bank" is "icici bank"."""
	text = ACCENTS.sub("", unicodedata.normalize("NFKD", cstr(name))).casefold().replace("&", " and ")
	key = " ".join(LEGAL_WORDS.sub(" ", re.sub(r"[\W_]+", " ", text)).split())
	return INITIALS.sub(r"\1", LEADING_MS.sub("", key))


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
	office's sheet links the same spelling without asking again."""
	return {
		get_similar_key(row.party_name): row.party
		for row in onboarding.get(get_table(party_type))
		if row.status == "Created" and row.party and row.party_name
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
				"ledger": cstr(get_cell(row, sheet.columns["ledger"])).strip(),
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
		frappe.response[
			"filename"
		] = f"{frappe.scrub(company)}_undone_{frappe.scrub(party_type)}_invoices.xlsx"
		frappe.response["filecontent"] = make_xlsx(rows, _("Opening Invoices")).getvalue()
		frappe.response["type"] = "binary"
		return
	rows = [list(SHEET_COLUMNS)]
	for row in onboarding.get(table):
		if row.status != "Created":
			rows.append(
				[row.party_name, row.invoice_number, row.invoice_date, row.due_date, row.outstanding_amount]
			)

	frappe.response["filename"] = f"{frappe.scrub(company)}_opening_{frappe.scrub(party_type)}_invoices.xlsx"
	frappe.response["filecontent"] = make_xlsx(rows, _("Opening Invoices")).getvalue()
	frappe.response["type"] = "binary"


def create_invoices(company: str, party_type: str, keep_old_dates: bool = False):
	"""Post every draft row as an opening invoice, one posting at a time."""
	# a second posting waits here, then finds the rows posted
	frappe.db.get_value("Company Setup", company, "name", for_update=True)
	post_invoices(frappe.get_doc("Company Setup", company), party_type, keep_old_dates)


def create_invoices_in_background(company: str, party_type: str, keep_old_dates: bool = False):
	"""create_invoices as a background job, telling the open form when done or why not."""
	event = {"company": company, "party_type": party_type}
	try:
		create_invoices(company, party_type, keep_old_dates)
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


def post_invoices(onboarding, party_type: str, keep_old_dates: bool = False):
	"""Each draft row whose customer or supplier exists and is enabled becomes an opening invoice in
	its own savepoint, so a failed row is undone alone and noted on the row. The names confirmed as new
	are added first."""
	company = onboarding.company
	day_before = add_days(onboarding.migration_date, -1)
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

	tool = frappe.new_doc("Opening Invoice Creation Tool")
	tool.company = company
	tool.invoice_type = INVOICE_TYPES[party_type]
	drafts = [row for row in onboarding.get(get_table(party_type)) if row.status != "Created"]
	for done, row in enumerate(drafts):
		# the open form shows how far the posting is
		if not done % PROGRESS_EVERY:
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
		if row.party in parties.disabled:
			row.update(
				{"status": "Failed", "error": _("{0} is disabled. Enable it first.").format(row.party)}
			)
			continue
		account = row.account or get_party_account(party_type, row.party, company)
		party_currency = frappe.get_cached_value("Account", account, "account_currency")
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
			if account != get_party_account(party_type, row.party, company):
				keep_party_account(company, party_type, row.party, account)
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
	default = frappe.get_cached_value(
		"Company",
		onboarding.company,
		"default_receivable_account" if party_type == "Customer" else "default_payable_account",
	)
	rows = onboarding.get(get_table(party_type))
	posted_accounts = get_posted_accounts(party_type, rows)
	party_accounts = get_party_accounts(party_type, [row.party for row in rows], onboarding.company)[0]
	entered = {}
	for row in rows:
		account = posted_accounts.get(row.invoice) or row.account or party_accounts.get(row.party) or default
		line = entered.setdefault(account, {"amount": 0, "count": 0, "draft": 0})
		line["amount"] += flt(row.outstanding_amount)
		line["count"] += 1
		if row.status != "Created":
			line["draft"] += flt(row.outstanding_amount)
	# a line no account stands for, like customers listed by name, can only be matched in total
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
			}
			for account in accounts
		]
		if by_account
		else [],
	}


def get_party_accounts(party_type: str, parties, company: str) -> tuple[dict, dict]:
	"""get_party_account for many parties in a few queries: the account each one's invoices go to,
	and the account saved on the party itself (not its group's or the company's default)."""
	parties = list(set(filter(None, parties)))
	if not parties:
		return {}, {}
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
	default = frappe.get_cached_value(
		"Company",
		company,
		"default_receivable_account" if party_type == "Customer" else "default_payable_account",
	)
	return {
		party: saved.get(party) or group_accounts.get(groups.get(party)) or default for party in parties
	}, saved


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
	other. "posted" when the earlier invoices are the odd ones, "draft" when these are."""
	match = get_sheet_match(onboarding, party_type)
	if not match or not match["by_account"]:
		return []
	gap = {line["account"]: flt(line["sheet"]) - flt(line["entered"]) for line in match["lines"]}
	in_sheet = {line["account"] for line in match["lines"] if line["sheet"] is not None}

	def is_short(account):
		return gap.get(account, 0) > 0.005

	def is_over(account):
		return gap.get(account, 0) < -0.005 or account not in in_sheet

	rows = onboarding.get(get_table(party_type))
	posted_accounts = get_posted_accounts(party_type, rows)
	party_accounts, saved_accounts = get_party_accounts(
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
				if is_short(account) and is_over(other) and posted.get(other):
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
	for clash in clashes.values():
		for key in ("account", "from_account"):
			clash[f"{key}_name"] = frappe.get_cached_value("Account", clash[key], "account_name")
	return list(clashes.values())


def fix_account_clashes(onboarding, party_type: str, fixes: list[dict]):
	"""Do what the user ticked on the clashes found now, and nothing else: put these drafts on the
	other account, or cancel the earlier invoices and add them back as drafts on this sheet's account."""
	from erpnext.setup.doctype.company_setup.posted_entries import check_undo, undo_invoices

	wanted = {(fix.get("kind"), fix.get("account"), fix.get("from_account")) for fix in fixes}
	rows = onboarding.get(get_table(party_type))
	posted_accounts = get_posted_accounts(party_type, rows)
	party_accounts = get_party_accounts(party_type, [row.party for row in rows], onboarding.company)[0]
	for clash in get_account_clashes(onboarding, party_type):
		if (clash["kind"], clash["account"], clash["from_account"]) not in wanted:
			continue
		parties = {party["party"] for party in clash["parties"]}
		if clash["kind"] == "draft":
			for row in rows:
				if (
					row.party in parties
					and row.status != "Created"
					and (row.account or party_accounts[row.party]) == clash["from_account"]
				):
					row.account = clash["account"]
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
		for source, invoices in by_source.items():
			undo_invoices(onboarding, party_type, source, invoices)
			for undone in onboarding.get(UNDONE_TABLES[party_type]):
				if undone.invoice in invoices:
					onboarding.append(
						get_table(party_type),
						{
							field: undone.get(field)
							for field in (
								"party_type",
								"party",
								"party_name",
								"invoice_number",
								"invoice_date",
								"due_date",
								"outstanding_amount",
								"source",
							)
						}
						| {"account": clash["account"], "status": "Draft"},
					)
			rows = onboarding.get(get_table(party_type))
	onboarding.flags.posting = True
	onboarding.save()


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
			invoice["account"] = by_name[ledger.lower()]
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


def keep_party_account(company: str, party_type: str, party: str, account: str):
	"""Save the account picked in the table on the customer or supplier, so later invoices go there."""
	doc = frappe.get_doc(party_type, party)
	row = next((row for row in doc.accounts if row.company == company), None) or doc.append(
		"accounts", {"company": company}
	)
	row.account = account
	doc.save()


def get_party_currency(party_type: str, party: str, company: str) -> str:
	"""The currency of the receivable or payable account a customer's or supplier's invoices go to."""
	account = get_party_account(party_type, party, company)
	return frappe.get_cached_value("Account", account, "account_currency")


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
		party = make_party(party_type, name, onboarding.company)
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
			"account": row.account or get_party_account(party_type, party, company),
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


def get_posting_dates(onboarding, party_type: str) -> dict:
	"""The dates the drafts go on, for the posting confirmation: their own invoice dates where ERPNext
	has the year, else the day before migration (with how many, and the years missing)."""
	day_before = getdate(add_days(onboarding.migration_date, -1))
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
