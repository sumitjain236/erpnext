# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

"""What the Company Setup windows read. Unlike a doc method, these return only their data, not the
whole document with its thousands of rows."""

import frappe

from erpnext.setup.doctype.company_setup import opening_stock as stock
from erpnext.setup.doctype.company_setup import posted_entries


def get_setup(company: str):
	doc = frappe.get_doc("Company Setup", company)
	doc.check_permission("read")
	return doc


@frappe.whitelist()
def preview_opening_balances(company: str) -> dict:
	return get_setup(company).preview_opening_balances()


@frappe.whitelist()
def get_invoice_names(company: str, party_type: str) -> dict:
	return get_setup(company).get_invoice_names(party_type)


@frappe.whitelist()
def get_posted_entries(company: str, kind: str) -> dict:
	return get_setup(company).get_posted_entries(kind)


@frappe.whitelist()
def get_delete_setting(company: str) -> dict:
	get_setup(company)
	return posted_entries.get_delete_setting()


@frappe.whitelist()
def get_undo_split(
	company: str, kind: str, source: str | None = None, names: list | str | None = None
) -> dict:
	names = frappe.parse_json(names) if isinstance(names, str) else names
	return posted_entries.get_undo_split(get_setup(company), kind, source, names)


@frappe.whitelist()
def get_posting_dates(company: str, party_type: str) -> dict:
	return get_setup(company).get_posting_dates(party_type)


@frappe.whitelist()
def get_account_clashes(company: str, party_type: str) -> list[dict]:
	return get_setup(company).get_account_clashes(party_type)


@frappe.whitelist()
def get_bank_accounts(company: str) -> dict:
	return get_setup(company).get_bank_accounts()


@frappe.whitelist()
def get_uncleared_cheques(company: str) -> dict:
	return get_setup(company).get_uncleared_cheques()


@frappe.whitelist()
def get_warehouses(company: str) -> dict:
	return get_setup(company).get_warehouses()


@frappe.whitelist()
def get_opening_stock(company: str) -> dict:
	return get_setup(company).get_opening_stock()


@frappe.whitelist()
def check_stock_correction(company: str, rows: list | str) -> dict:
	return get_setup(company).check_stock_correction(rows)


@frappe.whitelist()
def get_stock_difference(company: str) -> dict:
	return get_setup(company).get_stock_difference()


@frappe.whitelist()
def get_fixed_assets(company: str) -> dict:
	return get_setup(company).get_fixed_assets()


@frappe.whitelist()
def get_taxes(company: str) -> dict:
	return get_setup(company).get_taxes()


@frappe.whitelist()
def get_open_orders(company: str, order_type: str) -> dict:
	return get_setup(company).get_open_orders(order_type)


@frappe.whitelist()
def get_made_orders(company: str, order_type: str) -> dict:
	return get_setup(company).get_made_orders(order_type)


@frappe.whitelist()
def get_boms(company: str) -> dict:
	return get_setup(company).get_boms()


@frappe.whitelist()
def get_stock_status(company: str, year_end: str | None = None) -> dict:
	"""For the page to poll while stock posts or a year's stock closes in the background, without
	loading the document: whether a stock job runs, and the status of the year's Stock Closing Entry."""
	frappe.has_permission("Company Setup", "read", company, throw=True)
	return {
		"posting": stock.is_posting_stock(company),
		"closing": stock.get_stock_closing(company, year_end).status if year_end else None,
	}
