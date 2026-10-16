# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""The sheets a company fills to bring its chart of accounts and opening balances over.

The chart sheet has the Chart of Accounts Importer's eight columns, then two optional balance
columns. The balances sheet has an account and its balance, or a trial balance's opening and
closing columns, of which the closing ones are used.
"""

import csv
import re
from io import BytesIO, StringIO

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt
from frappe.utils.csvutils import read_csv_content
from frappe.utils.xlsxutils import make_xlsx, read_xls_file_from_attached_file
from openpyxl import load_workbook

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
GROUP_COLUMNS = ("group", "under", "parent_group", "account_group")
NAME_COLUMNS = ("account_name", "account", "particulars", "ledger", "ledger_name", "name")
# how far down a report's headings may be, under its title rows
HEADING_ROWS = 15
# a report's own lines that add up the others
TOTAL_LINE = re.compile(r"^(grand\s+)?total(\s+for\s.*)?:?$", re.IGNORECASE)
# the groups other software keeps customers, suppliers and stock under (Tally, Busy, QuickBooks)
PARTY_GROUPS = {
	"Receivable": (
		"sundry debtors",
		"debtors",
		"accounts receivable",
		"accounts receivable a r",
		"trade receivables",
	),
	"Payable": (
		"sundry creditors",
		"creditors",
		"accounts payable",
		"accounts payable a p",
		"trade payables",
	),
	"Stock": ("stock in hand", "closing stock", "inventory", "inventory asset"),
}


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
	"""The lines of a balances sheet or trial balance, as exported from other software: under a few
	title rows, with group lines that total the lines indented under them, and a total at the end.
	Each line knows the groups it is under; group lines are marked, totals left out."""
	rows = read_report(file_url)
	keys = [[get_column_key(value) for value in row.cells] for row in rows]
	heading, amounts = next(
		(
			(i, get_amount_columns(keys[max(i - 3, 0) : i + 1]))
			for i in range(min(len(keys), HEADING_ROWS))
			if get_amount_columns(keys[max(i - 3, 0) : i + 1])
		),
		(None, None),
	)
	if heading is None:
		frappe.throw(
			_(
				"The sheet needs an Account Name column and Debit and Credit columns (or Closing Debit and Closing Credit)."
			)
		)
	debit_column, credit_column = amounts
	# the name heading may sit rows above the amounts, like Tally's "Particulars"
	name_column = next(
		(row.index(key) for row in reversed(keys[: heading + 1]) for key in NAME_COLUMNS if key in row), 0
	)
	number_column = keys[heading].index("account_number") if "account_number" in keys[heading] else None
	# a report may name each account's group in a column, as Busy does, instead of indenting
	group_column = next((keys[heading].index(key) for key in GROUP_COLUMNS if key in keys[heading]), None)

	lines = []
	for row in rows[heading + 1 :]:
		name = cstr(get_cell(row.cells, name_column)).strip()
		if not name or TOTAL_LINE.match(name):
			continue
		number = cstr(get_cell(row.cells, number_column)).strip()
		lines.append(
			frappe._dict(
				account_name=name,
				account_number=number,
				label=f"{number} - {name}" if number else name,
				debit=get_amount(get_cell(row.cells, debit_column)),
				credit=get_amount(get_cell(row.cells, credit_column)),
				indent=get_cell(row.indents, name_column) or 0,
				group=cstr(get_cell(row.cells, group_column)).strip(),
			)
		)

	balances, parents = [], []
	for line, after in zip(lines, [*lines[1:], None], strict=True):
		while parents and parents[-1].indent >= line.indent:
			parents.pop()
		line.groups = [parent.account_name for parent in parents] + ([line.group] if line.group else [])
		# a line with lines indented under it is a group, and its amount only totals theirs
		line.is_group = bool(after and after.indent > line.indent)
		if line.is_group:
			parents.append(line)
		if line.is_group or line.debit or line.credit:
			balances.append(line)
	return balances


def read_report(file_url: str) -> list[frappe._dict]:
	"""Every row of an uploaded csv or Excel sheet as it is, with how far each cell's text is
	indented: reports from accounting software indent each account under its group. A csv keeps no
	indent, as Frappe's reader trims its values."""
	file_doc, extension = get_file(file_url)
	content = file_doc.get_content()
	if extension == "xlsx":
		sheet = load_workbook(BytesIO(content), data_only=True).active
		return [
			frappe._dict(cells=[cell.value for cell in row], indents=[get_indent(cell) for cell in row])
			for row in sheet.iter_rows()
		]
	rows = read_csv_content(content) if extension == "csv" else read_xls_file_from_attached_file(content)
	return [frappe._dict(cells=row, indents=[get_indent(value) for value in row]) for row in rows]


def get_indent(cell) -> float:
	value = getattr(cell, "value", cell)
	if not isinstance(value, str):
		return 0
	indent = cell.alignment.indent if hasattr(cell, "alignment") else 0
	return indent + len(value) - len(value.lstrip())


def get_amount_columns(heading_rows: list[list[str]]) -> tuple[int, int] | None:
	"""The debit and credit columns of the last heading row, the closing ones first: they are the
	balances on the day. Any heading with debit or Dr in it counts, like Busy's "Dr. Amount (Rs.)",
	and a label like "Closing Balance" in a row above counts for the columns under it."""
	width = max(len(row) for row in heading_rows)
	above = [set() for _ in range(width)]
	for row in heading_rows:
		label = ""
		for index in range(width):
			# a merged label is in its first cell only, and covers the empty ones after it
			label = get_cell(row, index) or label
			above[index] |= set(label.split("_"))
	found = {}
	for index, key in enumerate(heading_rows[-1]):
		words = set(key.split("_"))
		debit, credit = bool(words & {"debit", "dr"}), bool(words & {"credit", "cr"})
		# a "Dr/Cr" column says which side, it holds no amount
		if debit != credit:
			rank = 0 if "closing" in above[index] else 2 if "opening" in above[index] else 1
			found.setdefault(("debit" if debit else "credit", rank), index)
	return next(
		(
			(found[("debit", rank)], found[("credit", rank)])
			for rank in (0, 1, 2)
			if ("debit", rank) in found and ("credit", rank) in found
		),
		None,
	)


def get_amount(value) -> float:
	"""A number cell, or the number in a text cell past a currency like "Rs." or "$"; brackets or a
	minus before it make it negative."""
	if not isinstance(value, str):
		return flt(value)
	number = re.search(r"\d[\d,]*(\.\d+)?", value)
	if not number:
		return 0
	amount = flt(number.group().replace(",", ""))
	return -amount if value.strip().startswith("(") or "-" in value[: number.start()] else amount


def get_column_key(value) -> str:
	return re.sub(r"[^a-z0-9]+", "_", cstr(value).lower()).strip("_")


def get_cell(cells: list, index: int | None):
	return cells[index] if index is not None and index < len(cells) else None


def get_party_group_type(names: list[str]) -> str | None:
	"""Receivable, Payable or Stock when one of these names is the group other software keeps
	customers, suppliers or stock under."""
	keys = {get_column_key(name).replace("_", " ") for name in names}
	return next((account_type for account_type, groups in PARTY_GROUPS.items() if keys & set(groups)), None)


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

	frappe.response["filename"] = (
		f"{frappe.scrub(company)}_chart_of_accounts.xlsx"
		if template == "Current Chart"
		else "sample_chart_of_accounts.xlsx"
	)
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
