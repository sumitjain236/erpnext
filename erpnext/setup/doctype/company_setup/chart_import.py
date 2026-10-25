# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt

from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import build_tree_from_json
from erpnext.accounts.doctype.chart_of_accounts_importer.chart_of_accounts_importer import replace_chart
from erpnext.setup.doctype.company_setup import opening_stock as stock
from erpnext.setup.doctype.company_setup.balances import sort_balances
from erpnext.setup.doctype.company_setup.chart_sheet import (
	NO_OPENING_BALANCE,
	get_file_label,
	read_chart_sheet,
)
from erpnext.setup.doctype.company_setup.company_setup_steps import PARTY_DEFAULTS, get_usual_party_ledger
from erpnext.setup.doctype.company_setup.opening_invoices import add_account


class ChartMixin:
	@frappe.whitelist()
	def use_existing_chart(self):
		"""Keep the chart of accounts made during setup."""
		self.start_change()
		self.chart_of_accounts_source = "Existing"
		self.save()

	@frappe.whitelist()
	def preview_chart(self, file_url: str, group_kinds: dict | str | None = None) -> dict:
		"""What importing this sheet would change, for the Import dialog. group_kinds: what each group of
		a Busy list that Busy doesn't ship holds, see chart_sheet.get_busy_list_rows."""
		self.check_permission("write")
		sheet = read_chart_sheet(file_url, frappe.parse_json(group_kinds) if group_kinds else None)
		# the sheet's own account types: its accounts don't exist yet
		types = {row[0]: row[5] for row in sheet.rows[1:] if row and row[0]}
		kept = [
			balance for balance in sheet.balances if types.get(balance.account_name) not in NO_OPENING_BALANCE
		]
		left_out = {}
		for balance in sheet.balances:
			account_type = types.get(balance.account_name)
			if account_type in NO_OPENING_BALANCE:
				left_out[account_type] = flt(left_out.get(account_type)) + balance.debit - balance.credit
		return {
			"accounts": build_tree_from_json("", chart_data=sheet.forest, from_coa_importer=True),
			"current_accounts": frappe.db.count("Account", {"company": self.company}),
			"balances": len(kept),
			"debit": sum(balance.debit for balance in kept),
			"credit": sum(balance.credit for balance in kept),
			"left_out": {account_type: amount for account_type, amount in left_out.items() if amount},
			"draft_balances": len(self.opening_balances),
			"added_accounts": get_missing_system_accounts(self.company, set(types.values()))
			+ (
				[_("Stock Adjustment")]
				if "Stock" in types.values() and "Stock Adjustment" not in types.values()
				else []
			),
			"renamed": sheet.renamed,
			"unknown_groups": sheet.unknown_groups,
		}

	@frappe.whitelist()
	def import_chart(self, file_url: str, group_kinds: dict | str | None = None) -> dict:
		"""Replace the chart with the sheet's. Its balances become draft opening balances, and the
		accounts ERPNext needs that the sheet lacks are added. group_kinds: as for preview_chart.
		The replace deletes the tax templates: each company_setup_after_chart_replace hook is then
		called with the company, so an app can make its own again."""
		self.check_permission("write")
		self.start_change()
		sheet = read_chart_sheet(file_url, frappe.parse_json(group_kinds) if group_kinds else None)
		old_names = {
			row.account: frappe.db.get_value("Account", row.account, "account_name")
			for row in self.opening_balances
			if row.account
		}

		default_names = get_default_account_names(self.company)
		party_accounts = get_party_account_names(self.company)
		replace_chart(sheet.rows, self.company, refill_defaults=True)
		kept = keep_default_accounts(self.company, default_names)
		keep_party_accounts(self.company, party_accounts)
		set_default_bank(self.company)
		# a default kept by name stays; one ERPNext picked for the new chart may be set again
		ours = tuple(fieldname for fieldname in PARTY_DEFAULTS if fieldname not in kept)
		set_default_party_accounts(self.company, ours)
		added = add_system_accounts(self.company)
		added += stock.set_stock_defaults(self.company)
		self.chart_of_accounts_source = "Imported"
		# the replace deleted the tax templates, with the accounts they post to
		self.tax_templates = []
		self.taxes_saved = 0
		for method in frappe.get_hooks("company_setup_after_chart_replace"):
			frappe.get_attr(method)(self.company)
		if sheet.balances:
			balances = sort_balances(self.company, sheet.balances)
			self.set_sheet_totals(balances.result["left_out"], balances.left_out_accounts)
			self.opening_balances = []
			for row in balances.rows:
				self.append("opening_balances", row | {"source": get_file_label(file_url)})
		else:
			# the old accounts are gone: use the new account with the same name, if there is one
			for row in self.opening_balances:
				name = old_names.get(row.account)
				row.account = get_ledger_by_name(self.company, name)
		self.save()
		return {"added_accounts": added}


# accounts ERPNext posts to by itself: account type, root type, the Company default it fills, and
# whether only automatic stock accounting needs it (every purchase invoice does then)
SYSTEM_ACCOUNTS = (
	("Stock Received But Not Billed", "Liability", "stock_received_but_not_billed", True),
	("Round Off", "Expense", "round_off_account", False),
)


def get_default_account_names(company: str) -> dict[str, str]:
	"""The company's default accounts by field, as account names that a new chart can have again."""
	company_doc = frappe.get_cached_doc("Company", company)
	return {
		df.fieldname: frappe.db.get_value("Account", company_doc.get(df.fieldname), "account_name")
		for df in company_doc.meta.get_link_fields()
		if df.options == "Account" and company_doc.get(df.fieldname)
	}


def keep_default_accounts(company: str, default_names: dict[str, str]):
	"""Point each default back at the new account of the same name, like "Debtors", instead of the
	first account of its type the new chart happens to have. Returns those set, by field."""
	currency = frappe.get_cached_value("Company", company, "default_currency")
	values = {}
	for fieldname, account_name in default_names.items():
		# a ledger in another currency of the same name is not the company's default
		account = get_ledger_by_name(company, account_name, currency)
		if account:
			values[fieldname] = account
	set_company_defaults(company, values)
	return values


def set_company_defaults(company: str, values: dict):
	"""Set some of the company's default accounts, as one who may change the company."""
	if not values:
		return
	frappe.has_permission("Company", "write", company, throw=True)
	frappe.db.set_value("Company", company, values)


def set_default_bank(company: str):
	"""The default bank is a bank the company holds money in: a bank ledger under assets, never a
	credit card or overdraft the new chart keeps as a bank account."""
	bank = frappe.db.get_value("Company", company, "default_bank_account")
	if bank and frappe.db.get_value("Account", bank, "root_type") == "Asset":
		return
	set_company_defaults(
		company,
		{
			"default_bank_account": frappe.db.get_value(
				"Account",
				{
					"company": company,
					"account_type": "Bank",
					"is_group": 0,
					"root_type": "Asset",
					"disabled": 0,
				},
				order_by="lft",
			)
		},
	)


def set_default_party_accounts(company: str, fields: tuple):
	"""The receivable (payable) defaults ERPNext just picked for a new chart (fields) become the
	ledger with the usual name, like Sundry Debtors, not the first the chart happens to have; with
	several and none of that name, empty for the user to pick. Not once an opening invoice (bill)
	is posted."""
	for fieldname in fields:
		account_type = PARTY_DEFAULTS[fieldname]
		doctype = "Sales Invoice" if account_type == "Receivable" else "Purchase Invoice"
		if not frappe.db.exists(doctype, {"company": company, "is_opening": "Yes", "docstatus": 1}):
			set_company_defaults(company, {fieldname: get_usual_party_ledger(company, account_type)})


def get_party_account_names(company: str) -> list[dict]:
	"""The company's customer, supplier and group accounts, as account names that a new chart can have again."""
	rows = frappe.get_all(
		"Party Account",
		filters={"company": company},
		fields=["parent", "parenttype", "parentfield", "idx", "account", "advance_account"],
	)
	for row in rows:
		for field in ("account", "advance_account"):
			row[field] = row[field] and frappe.db.get_value("Account", row[field], "account_name")
	return rows


def get_ledger_by_name(company: str, account_name: str | None, currency: str | None = None) -> str | None:
	"""The company's ledger of this name, in this currency when given."""
	if not account_name:
		return None
	filters = {"company": company, "account_name": account_name, "is_group": 0}
	if currency:
		filters["account_currency"] = currency
	return frappe.db.get_value("Account", filters)


def keep_party_accounts(company: str, rows: list[dict]):
	"""Add back the Party Account rows the chart replace deletes, pointing at the new accounts of the
	same name. A row whose account the new chart lacks is left out, so the company default applies."""

	for row in rows:
		account = get_ledger_by_name(company, row.account)
		advance_account = get_ledger_by_name(company, row.advance_account)
		if not (account or advance_account):
			continue
		frappe.get_doc(
			{
				"doctype": "Party Account",
				"parent": row.parent,
				"parenttype": row.parenttype,
				"parentfield": row.parentfield,
				"idx": row.idx,
				"company": company,
				"account": account,
				"advance_account": advance_account,
			}
		).db_insert()


def get_missing_system_accounts(company: str, account_types: set) -> list[str]:
	"""The accounts add_system_accounts would add for a chart with these account types."""
	perpetual = frappe.get_cached_value("Company", company, "enable_perpetual_inventory")
	return [
		_(account_type)
		for account_type, _root_type, _fieldname, for_stock in SYSTEM_ACCOUNTS
		if (perpetual or not for_stock) and account_type not in account_types
	]


def add_system_accounts(company: str) -> list[str]:
	"""Add the accounts ERPNext posts to by itself that a chart from other software has no place for,
	and make them the company's defaults. Returns the names added."""
	company_doc = frappe.get_cached_doc("Company", company)
	added = []
	for account_type, root_type, fieldname, for_stock in SYSTEM_ACCOUNTS:
		if for_stock and not company_doc.enable_perpetual_inventory:
			continue
		account = frappe.db.get_value(
			"Account", {"company": company, "account_type": account_type, "is_group": 0}
		)
		if not account:
			account = add_account(company, _(account_type), account_type, root_type)
			added.append(account)
		if not frappe.db.get_value("Company", company, fieldname):
			set_company_defaults(company, {fieldname: account})
	return added
