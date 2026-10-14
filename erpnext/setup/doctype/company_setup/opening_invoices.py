# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Opening invoices: what customers owed and what was owed to suppliers when the company started.

Each unpaid invoice of the old books becomes an opening sales or purchase invoice, so later
payments can be matched against it and its due date keeps counting. They are made by the Opening
Invoice Creation Tool, which also adds a customer or supplier by name when there is none yet.
"""

import re
from difflib import get_close_matches

import frappe
from frappe import _
from frappe.utils import add_days, add_years, cstr, flt, get_user_date_format, getdate, strip_html
from frappe.utils.xlsxutils import make_xlsx

from erpnext.accounts.doctype.chart_of_accounts_importer.chart_of_accounts_importer import get_file
from erpnext.accounts.utils import get_fiscal_year
from erpnext.setup.doctype.company_setup.chart_sheet import read_rows

TABLES = {"Customer": "opening_sales_invoices", "Supplier": "opening_purchase_invoices"}
INVOICE_TYPES = {"Customer": "Sales", "Supplier": "Purchase"}
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
# above this many invoices are made in the background
BACKGROUND_FROM = 50


def get_table(party_type: str) -> str:
	if party_type not in TABLES:
		frappe.throw(_("Opening invoices are for customers or suppliers, not {0}.").format(party_type))
	return TABLES[party_type]


def get_party_names(party_type: str) -> dict[str, str]:
	"""Each customer or supplier by its name, as shown, and by its ID."""
	name_field = "customer_name" if party_type == "Customer" else "supplier_name"
	parties = {}
	for party in frappe.get_all(party_type, filters={"disabled": 0}, fields=["name", name_field]):
		parties[party.name] = party.name
		parties[party.get(name_field)] = party.name
	return parties


def find_party(parties: dict[str, str], name: str) -> str | None:
	"""The customer or supplier with this name, ignoring case and extra spaces."""
	name = " ".join(cstr(name).split())
	if name in parties:
		return parties[name]
	lowered = {key.lower(): value for key, value in parties.items() if key}
	return lowered.get(name.lower())


def get_suggestions(party_type: str, names: list[str]) -> dict[str, str]:
	"""For names that are not a customer or supplier yet, one that looks almost the same."""
	parties = get_party_names(party_type)
	known = list({key for key in parties if key})
	suggestions = {}
	for name in set(names):
		if name and not find_party(parties, name):
			match = get_close_matches(name, known, n=1, cutoff=0.85)
			if match:
				suggestions[name] = match[0]
	return suggestions


def read_invoice_sheet(file_url: str) -> frappe._dict:
	"""The rows of a receivables or payables report, and the ones left out with the reason."""
	file_doc, extension = get_file(file_url)
	rows = [
		{re.sub(r"[^a-z0-9]+", "_", cstr(key).lower()).strip("_"): value for key, value in row.items()}
		for row in read_rows(file_doc, extension, as_dict=True)
	]
	headers = set(rows[0]) if rows else set()
	columns = {
		field: next((name for name in names if name in headers), None)
		for field, names in COLUMN_NAMES.items()
	}
	if not columns["party_name"] or not columns["outstanding_amount"]:
		frappe.throw(_("The sheet needs a Name column and an Outstanding column."))

	invoices, advances = [], 0
	for row in rows:
		name = cstr(row.get(columns["party_name"])).strip()
		amount = flt(row.get(columns["outstanding_amount"]))
		if not name or not amount:
			continue
		if amount < 0:
			# money paid in advance is not an invoice
			advances += 1
			continue
		invoices.append(
			{
				"party_name": name,
				"invoice_number": cstr(row.get(columns["invoice_number"])).strip() or None,
				"invoice_date": get_sheet_date(row.get(columns["invoice_date"])),
				"due_date": get_sheet_date(row.get(columns["due_date"])),
				"outstanding_amount": amount,
			}
		)
	return frappe._dict(invoices=invoices, advances=advances)


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


def create_invoices(company: str, party_type: str):
	"""Post every draft row as an opening invoice, and tell the open form when done or why not."""
	event = {"company": company, "party_type": party_type}
	try:
		post_invoices(frappe.get_doc("Company Setup", company), party_type)
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


def post_invoices(onboarding, party_type: str):
	"""Each draft row becomes an opening invoice in its own savepoint, so a failed row, and the
	customer or supplier added for it, is undone alone and noted on the row."""
	company = onboarding.company
	day_before = add_days(onboarding.migration_date, -1)
	ensure_fiscal_year(day_before, company)
	temporary = get_temporary_account(company, create=True)
	currency = frappe.get_cached_value("Company", company, "default_currency")
	parties = get_party_names(party_type)

	tool = frappe.new_doc("Opening Invoice Creation Tool")
	tool.company = company
	tool.invoice_type = INVOICE_TYPES[party_type]
	tool.create_missing_party = 1
	for row in onboarding.get(get_table(party_type)):
		if row.status == "Created":
			continue
		# dated as in the old books when the books have that year, so ageing stays right
		posting_date = row.invoice_date if has_fiscal_year(row.invoice_date, company) else day_before
		savepoint = f"opening_invoice_{row.idx}"
		frappe.db.savepoint(savepoint)
		try:
			tool.set(
				"invoices",
				[
					{
						"party": find_party(parties, row.party_name),
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
			party = doc.get(frappe.scrub(party_type))
			# a later row with the same new name uses the party just added
			parties[row.party_name] = party
			row.update({"status": "Created", "invoice": doc.name, "error": None, "party": party})
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			frappe.clear_messages()
			row.update({"status": "Failed", "error": strip_html(cstr(e)) or e.__class__.__name__})

	onboarding.flags.posting = True
	onboarding.save()


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

	parent = frappe.db.get_value(
		"Account",
		{"company": company, "root_type": "Asset", "is_group": 1, "parent_account": ("is", "not set")},
	)
	return (
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": _("Temporary Opening"),
				"account_type": "Temporary",
				"company": company,
				"parent_account": parent,
			}
		)
		.insert()
		.name
	)
