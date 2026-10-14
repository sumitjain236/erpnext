# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""The sheets a company fills to bring its chart of accounts and opening balances over.

The chart sheet has the Chart of Accounts Importer's eight columns, then two optional balance
columns. The balances sheet has an account and its balance, or a trial balance's opening and
closing columns, of which the closing ones are used.
"""

import csv
from io import StringIO

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt
from frappe.utils.xlsxutils import make_xlsx

from erpnext.accounts.doctype.chart_of_accounts_importer.chart_of_accounts_importer import (
	build_forest,
	generate_data_from_csv,
	generate_data_from_excel,
	get_file,
	get_root_types,
	get_template,
	validate_columns,
)

IMPORTER_COLUMNS = 8
# the importer's columns a chart sheet must have
CHART_HEADERS = ("Account Name", "Parent Account", "Account Number", "Is Group", "Account Type", "Root Type")
BALANCE_COLUMNS = ("Opening Debit", "Opening Credit")
# the charts made at setup have no Equity root, so the importer's rule for all five root types is too strict
REQUIRED_ROOT_TYPES = ("Asset", "Liability", "Income", "Expense")
# customers and suppliers come in as opening invoices, stock as a stock entry, and the temporary
# account takes the difference, so these accounts get no opening balance of their own
NO_OPENING_BALANCE = ("Receivable", "Payable", "Stock", "Temporary")
BALANCE_SHEET_COLUMNS = ("Account Number", "Account Name", "Debit", "Credit")
# the amount columns a sheet may have, the closing ones first: they are the balances on the day
AMOUNT_COLUMNS = (
	("closing_debit", "closing_credit"),
	("debit", "credit"),
	("opening_debit", "opening_credit"),
)


def read_chart_sheet(file_url: str) -> frappe._dict:
	"""The importer's rows of the sheet, its accounts as a tree, and its opening balances."""
	file_doc, extension = get_file(file_url)
	validate_chart_headers(read_rows(file_doc, extension, as_dict=True))
	rows = [row[:IMPORTER_COLUMNS] for row in read_rows(file_doc, extension)]
	validate_columns(rows)
	validate_roots(rows)
	return frappe._dict(
		rows=rows,
		forest=build_forest(rows),
		balances=get_balances(read_rows(file_doc, extension, as_dict=True)),
	)


def validate_chart_headers(rows: list[dict]):
	"""A trial balance or another sheet is not a chart: say so before the importer trips over it."""
	headers = set(rows[0]) if rows else set()
	missing = [label for label in CHART_HEADERS if frappe.scrub(label) not in headers]
	if missing:
		frappe.throw(
			_(
				"This sheet isn't a chart of accounts: it has no {0} column. Download the sample or current chart, fill it in and upload that. Balances from a trial balance go in Opening balances instead."
			).format(", ".join(_(label) for label in missing)),
			title=_("Not a chart of accounts"),
		)


def validate_roots(rows: list[list]):
	# the importer gives a top-level account itself as its parent
	roots = [row for row in rows if row[0] and row[1] == row[0]]
	for row in roots:
		if row[6] not in get_root_types():
			frappe.throw(
				_(
					"{0} is a top-level account, so its Root Type must be Asset, Liability, Equity, Income or Expense."
				).format(frappe.bold(row[0]))
			)

	missing = set(REQUIRED_ROOT_TYPES) - {row[6] for row in roots}
	if missing:
		frappe.throw(_("Add a top-level account for: {0}").format(", ".join(sorted(missing))))


def read_rows(file_doc, extension: str, as_dict: bool = False) -> list:
	if extension == "csv":
		return generate_data_from_csv(file_doc, as_dict=as_dict)
	return generate_data_from_excel(file_doc, extension, as_dict=as_dict)


def read_balance_sheet(file_url: str) -> list[frappe._dict]:
	"""The accounts of a balances sheet or trial balance that have a balance."""
	file_doc, extension = get_file(file_url)
	rows = read_rows(file_doc, extension, as_dict=True)
	headers = rows[0].keys() if rows else ()
	amounts = next((pair for pair in AMOUNT_COLUMNS if set(pair) <= set(headers)), None)
	name_column = next((key for key in ("account_name", "account", "particulars") if key in headers), None)
	if not amounts or not name_column:
		frappe.throw(
			_(
				"The sheet needs an Account Name column and Debit and Credit columns (or Closing Debit and Closing Credit)."
			)
		)

	balances = []
	for row in rows:
		debit, credit = flt(row.get(amounts[0])), flt(row.get(amounts[1]))
		number, name = cstr(row.get("account_number")).strip(), cstr(row.get(name_column)).strip()
		if name and (debit or credit):
			label = f"{number} - {name}" if number else name
			balances.append(
				frappe._dict(
					account_name=name, account_number=number, label=label, debit=debit, credit=credit
				)
			)
	return balances


@frappe.whitelist()
def download_opening_balances(company: str):
	"""The accounts that can take an opening balance, with the draft balances filled in."""
	onboarding = frappe.get_doc("Company Setup", company)
	onboarding.check_permission("read")
	drafts = get_draft_balances(onboarding)
	accounts = frappe.get_list(
		"Account",
		filters={
			"company": company,
			"is_group": 0,
			"disabled": 0,
			"account_type": ("not in", NO_OPENING_BALANCE),
		},
		fields=["name", "account_number", "account_name"],
		order_by="lft",
	)
	rows = [list(BALANCE_SHEET_COLUMNS)]
	for account in accounts:
		debit, credit = drafts.get(account.name, (0, 0))
		rows.append([account.account_number, account.account_name, debit or None, credit or None])

	frappe.response["filename"] = f"{frappe.scrub(company)}_opening_balances.xlsx"
	frappe.response["filecontent"] = make_xlsx(rows, "Opening Balances").getvalue()
	frappe.response["type"] = "binary"


def get_draft_balances(onboarding) -> dict[str, tuple[float, float]]:
	"""The draft debit and credit of each account."""
	balances = {}
	for row in onboarding.opening_balances:
		if row.account and row.status != "Posted":
			debit, credit = balances.get(row.account, (0, 0))
			balances[row.account] = (debit + flt(row.debit), credit + flt(row.credit))
	return balances


def get_balances(rows: list[dict]) -> list[frappe._dict]:
	"""The rows with an opening debit or credit. Only accounts without sub-accounts can have one."""
	parents = {
		(cstr(row.get("parent_account_number")).strip(), cstr(row.get("parent_account")).strip())
		for row in rows
	}
	balances = []
	for row in rows:
		debit, credit = flt(row.get("opening_debit")), flt(row.get("opening_credit"))
		if not debit and not credit:
			continue

		number, name = cstr(row.get("account_number")).strip(), cstr(row.get("account_name")).strip()
		label = f"{number} - {name}" if number else name
		if cint(row.get("is_group")) or (number, name) in parents:
			frappe.throw(
				_(
					"{0} has sub-accounts, so it can't have an opening balance. Put it on one of its sub-accounts."
				).format(frappe.bold(label)),
				title=_("Balance on a group account"),
			)
		balances.append(
			frappe._dict(account_name=name, account_number=number, label=label, debit=debit, credit=credit)
		)
	return balances


@frappe.whitelist()
def download_chart_template(company: str, template: str):
	"""An Excel sheet to fill: a sample chart, or the company's chart with its draft opening balances."""
	onboarding = frappe.get_doc("Company Setup", company)
	onboarding.check_permission("read")

	rows = get_sample_rows(company)
	if template == "Current Chart":
		rows = [rows[0], *get_current_chart_rows(onboarding)]

	frappe.response["filename"] = f"{frappe.scrub(company)}_chart_of_accounts.xlsx"
	frappe.response["filecontent"] = make_xlsx(rows, "Chart of Accounts").getvalue()
	frappe.response["type"] = "binary"


def get_sample_rows(company: str) -> list[list]:
	"""The importer's sample chart, with the balance columns added."""
	rows = list(csv.reader(StringIO(get_template("Sample Template", company).getvalue())))
	return [rows[0] + list(BALANCE_COLUMNS), *rows[1:]]


def get_current_chart_rows(onboarding) -> list[list]:
	"""The company's accounts in the sheet's columns, with their draft opening balances."""
	accounts = frappe.get_list(
		"Account",
		filters={"company": onboarding.company},
		fields=[
			"name",
			"account_name",
			"account_number",
			"parent_account",
			"is_group",
			"account_type",
			"root_type",
			"account_currency",
		],
		order_by="lft",
	)
	by_name = {account.name: account for account in accounts}
	balances = get_draft_balances(onboarding)

	rows = []
	for account in accounts:
		parent = by_name.get(account.parent_account) or frappe._dict()
		debit, credit = balances.get(account.name, (0, 0))
		rows.append(
			[
				account.account_name,
				parent.account_name,
				account.account_number,
				parent.account_number,
				account.is_group,
				account.account_type,
				account.root_type,
				account.account_currency,
				debit or None,
				credit or None,
			]
		)
	return rows
