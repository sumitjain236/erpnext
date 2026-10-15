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
from frappe.utils.csvutils import read_csv_content
from frappe.utils.xlsxutils import (
	build_xlsx_response,
	make_xlsx,
	read_xls_file_from_attached_file,
	read_xlsx_file_from_attached_file,
)

from erpnext.accounts.doctype.chart_of_accounts_importer.chart_of_accounts_importer import get_file
from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_fiscal_year

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
	),
	"invoice_date": ("invoice_date", "date", "bill_date", "voucher_date"),
	"due_date": ("due_date", "due_on"),
	"outstanding_amount": (
		"outstanding",
		"outstanding_amount",
		"pending_amount",
		"pending",
		"balance_due",
		"balance",
		"amount",
	),
}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
# only Latin accents: marks in other scripts, like Hindi vowel signs, change the name
ACCENTS = re.compile("[\u0300-\u036f]")
LEGAL_WORDS = re.compile(r"\b(private|pvt|limited|ltd|llp|inc)\b")
# how far down a report's headings may be, under its title rows
HEADING_ROWS = 15
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
			match = get_close_matches(key, known, n=1, cutoff=0.85)
			if match:
				suggestions[name] = next(iter(parties.similar[match[0]]))
	return suggestions


def read_invoice_sheet(file_url: str, party_type: str) -> frappe._dict:
	"""The rows of a receivables or payables report, and how many are advances. The headings may
	sit under a few title rows, as in Tally's export."""
	file_doc, extension = get_file(file_url)
	content = file_doc.get_content()
	if extension == "csv":
		rows = read_csv_content(content)
	elif extension == "xlsx":
		rows = read_xlsx_file_from_attached_file(fcontent=content)
	else:
		rows = read_xls_file_from_attached_file(content)
	keys = [[re.sub(r"[^a-z0-9]+", "_", cstr(value).lower()).strip("_") for value in row] for row in rows]
	start = next(
		(
			i
			for i, row in enumerate(keys[:HEADING_ROWS])
			if set(row) & set(COLUMN_NAMES["party_name"])
			and set(row) & set(COLUMN_NAMES["outstanding_amount"])
		),
		None,
	)
	if start is None:
		frappe.throw(_("The sheet needs a Name column and an Outstanding column."))
	headers = keys[start]
	columns = {
		field: next((headers.index(name) for name in names if name in headers), None)
		for field, names in COLUMN_NAMES.items()
	}

	def cell(row, field):
		index = columns[field]
		return row[index] if index is not None and index < len(row) else None

	invoices, advances = [], 0
	for row in rows[start + 1 :]:
		name = cstr(cell(row, "party_name")).strip()
		amount = get_sheet_amount(cell(row, "outstanding_amount"), party_type)
		if not name or not amount:
			continue
		if amount < 0:
			advances += 1
		invoice_number = cstr(cell(row, "invoice_number")).strip()
		invoices.append(
			{
				"party_name": name,
				# Tally's mark for an amount set against no bill
				"invoice_number": None if invoice_number.lower() in ("", "on account") else invoice_number,
				"invoice_date": get_sheet_date(cell(row, "invoice_date")),
				"due_date": get_sheet_date(cell(row, "due_date")),
				"outstanding_amount": amount,
			}
		)
	return frappe._dict(invoices=invoices, advances=advances)


def get_sheet_amount(value, party_type: str) -> float:
	"""An amount cell; text like Tally's "30,000.00 Cr" is an advance when it is on the other side
	from what the customer owes us, or we owe the supplier."""
	if not isinstance(value, str):
		return flt(value)
	text = value.strip()
	side = text[-2:].lower()
	if side not in ("dr", "cr"):
		return flt(text)
	usual = "dr" if party_type == "Customer" else "cr"
	amount = flt(text[:-2])
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
			doc.insert(set_name=invoice.get("invoice_number"))
			doc.submit()
			row.update({"status": "Created", "invoice": doc.name, "error": None})
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			frappe.clear_messages()
			row.update({"status": "Failed", "error": strip_html(cstr(e)) or e.__class__.__name__})

	onboarding.flags.posting = True
	onboarding.save()


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


def ensure_fiscal_year(date, company: str):
	"""Add the financial year the opening entries fall in. Migrating on the first day of a year
	dates them on the last day of the year before, which a new site usually doesn't have."""
	if has_fiscal_year(date, company):
		return
	latest = frappe.get_all(
		"Fiscal Year", fields=["year_start_date"], order_by="year_start_date desc", limit=1
	)
	start = getdate(latest[0].year_start_date) if latest else getdate(f"{getdate(date).year}-01-01")
	while start > getdate(date):
		start = add_years(start, -1)
	while add_years(start, 1) <= getdate(date):
		start = add_years(start, 1)
	end = add_days(add_years(start, 1), -1)
	frappe.get_doc(
		{
			"doctype": "Fiscal Year",
			"year": str(start.year) if start.year == end.year else f"{start.year}-{end.year}",
			"year_start_date": start,
			"year_end_date": end,
		}
	).insert()


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
