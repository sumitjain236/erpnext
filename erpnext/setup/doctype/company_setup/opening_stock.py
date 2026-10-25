# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Stock: the items and warehouses on the stock sheets, and the opening stock behind the trial
balance's stock lines.

Each upload posts one Opening Stock reconciliation per warehouse, dated the day before migration,
against Temporary Opening. Once later entries exist a warehouse can't be undone, so wrong rows are
corrected with a Stock Reconciliation on the same day, again against Temporary Opening. What is left
between the stock posted and the trial balance can go to an Opening Stock Difference ledger.
"""

import re

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext.accounts.utils import get_stock_accounts
from erpnext.setup.doctype.company_setup.bank_accounts import get_balances
from erpnext.setup.doctype.company_setup.opening_invoices import (
	add_account,
	append_lines,
	get_cached,
	get_day_before,
	get_default_dimensions,
	get_posting_blocker,
	get_similar_key,
	get_temporary_account,
)
from erpnext.setup.doctype.company_setup.stock_checks import (
	get_corrections,
	get_kept_lines,
	get_later_entries,
	get_moved_serials,
	get_stock_blocker,
	get_stock_checks,
	get_stock_closing,
	get_stock_reds,
	get_stock_reposts,
	make_serial_numbers,
	make_stock_closing,
)
from erpnext.setup.doctype.company_setup.stock_posting import (
	check_correction,
	check_stock_undo,
	correct_opening_stock,
	correct_opening_stock_in_background,
	get_posted_stock,
	get_rows_to_undo,
	has_difference_on,
	is_posting_stock,
	post_opening_stock,
	post_opening_stock_in_background,
	undo_opening_stock,
	undo_opening_stock_in_background,
	undo_stock_difference,
)
from erpnext.setup.doctype.company_setup.stock_sheet import (
	BIG_GAP,
	CORRECT_JOB_ID,
	DIFFERENCE_ACCOUNT,
	JOB_ID,
	JOB_WORKER,
	UNDO_JOB_ID,
	WORK_IN_PROGRESS,
	download_opening_stock,
	get_expiry_date,
	get_item_index,
	get_items_status,
	get_line_key,
	get_opening_datetime,
	get_other_drafts,
	get_serials,
	get_stock_lines,
	get_unit_factors,
	get_unit_key,
	get_uom_index,
	get_warehouse_index,
	get_warehouses_status,
	match_item,
	match_warehouse,
	prepare_rows,
	read_stock_sheet,
	save_opening_stock,
)
from erpnext.setup.doctype.company_setup.stock_warehouses import (
	clear_line_on_warehouses,
	get_movable_lines,
	get_warehouse_rows,
	get_warehouses_with_entries,
	save_warehouses,
	set_line_on_warehouses,
)
from erpnext.stock import get_warehouse_account_map

# Stock difference and Check your books


def get_stock_match(onboarding) -> dict | None:
	"""The stock in the books on the day before against the trial balance's stock lines, each line on
	its own, with the difference posted. None without stock lines or posted stock."""
	return get_cached(onboarding, "stock_match", read_stock_match)


def read_stock_match(onboarding) -> dict | None:
	lines = get_stock_lines(onboarding)
	posted = [row for row in onboarding.opening_stock if row.status == "Posted"]
	if not onboarding.migration_date or not (lines or posted):
		return None
	company = onboarding.company
	day_before = get_day_before(onboarding)
	accounts = get_stock_accounts(company)
	books, differences = {}, {}
	for account, amount in get_balances(company, accounts, day_before).items():
		key = get_line_key(account, lines)
		books[key] = flt(books.get(key)) + flt(amount)
	for account, amount in (frappe.parse_json(onboarding.stock_difference) or {}).items():
		key = get_line_key(account, lines)
		differences[key] = flt(differences.get(key)) + flt(amount)
	warehouse_lines = get_warehouse_account_map(company)
	by_warehouse = {}
	for row in posted:
		account = (warehouse_lines.get(row.warehouse) or frappe._dict()).account
		warehouse = by_warehouse.setdefault(
			row.warehouse,
			{
				"warehouse": row.warehouse,
				"rows": 0,
				"posted": 0,
				"account": account,
				"line": get_line_key(account, lines),
			},
		)
		warehouse["rows"] += 1
		warehouse["posted"] = flt(warehouse["posted"] + flt(row.amount), 2)
	rows = []
	for account in sorted(
		set(lines) | {key for key, book in books.items() if flt(book, 2)} | set(differences)
	):
		line = flt(lines.get(account))
		book = flt(books.get(account), 2)
		rows.append(
			{
				"account": account,
				"line": line,
				"books": book,
				"difference_posted": flt(differences.get(account)),
				"difference": flt(line - book - flt(differences.get(account)), 2),
				"nothing_posted": bool(line and not book),
			}
		)
	total_line = flt(sum(lines.values()), 2)
	total_books = flt(sum(books.values()), 2)
	total_difference = flt(sum(differences.values()), 2)
	return {
		"lines": rows,
		"warehouses": list(by_warehouse.values()),
		"line": total_line,
		"books": total_books,
		"difference_posted": total_difference,
		"off": flt(total_line - total_books - total_difference, 2),
		"lines_off": [row["account"] for row in rows if abs(row["difference"]) >= 1],
		"matches": all(abs(row["difference"]) < 1 for row in rows),
	}


def get_difference_lines(onboarding) -> list[dict]:
	"""The stock lines off by more than ₹1 that Post the difference would post, with the share."""
	match = get_stock_match(onboarding)
	if not match:
		return []
	return [
		{
			"account": row["account"],
			"books": row["books"],
			"line": row["line"],
			"difference": row["difference"],
			"percent": flt(abs(row["difference"]) * 100 / abs(row["line"]), 1) if row["line"] else None,
			"nothing_posted": row["nothing_posted"],
			"big": abs(row["difference"]) > BIG_GAP * abs(row["line"]),
		}
		for row in match["lines"]
		if abs(row["difference"]) >= 1
	]


def get_difference_blocker(onboarding) -> str | None:
	"""Why the stock difference can't be posted, or None."""
	if any(row.status != "Posted" for row in onboarding.opening_stock):
		return _("Post every stock row first.")
	lines = get_difference_lines(onboarding)
	if not lines:
		return _("Your stock matches your trial balance.")
	if abs(sum(line["difference"] for line in lines)) < 1:
		return _(
			"The total matches, but stock is on the wrong accounts. Undo the warehouses on the wrong account, set their stock account in Warehouses, and post them again."
		)
	empty = next((line for line in lines if line["nothing_posted"]), None)
	if empty:
		return _("{0} has {1} in your trial balance and no stock posted. Upload its rows.").format(
			frappe.get_cached_value("Account", empty["account"], "account_name")
			if empty["account"]
			else _("Stock"),
			frappe.format_value(empty["line"], {"fieldtype": "Currency"}),
		)
	return None


def get_empty_warehouses(onboarding) -> list[str]:
	"""Warehouses of the Warehouses window with no stock posted, on a line Post the difference would
	post: their stock may still be to upload."""
	lines = get_stock_lines(onboarding)
	off = {line["account"] for line in get_difference_lines(onboarding)}
	posted = {row.warehouse for row in onboarding.opening_stock if row.status == "Posted"}
	accounts = get_warehouse_account_map(onboarding.company)
	# the warehouses every company gets, when they never held stock, are not missing an upload
	standard = {_("Stores"), _("Work In Progress"), _("Finished Goods"), _("Goods In Transit")}
	held = get_warehouses_with_entries(onboarding.company)
	return [
		row.warehouse
		for row in onboarding.warehouses
		if row.warehouse
		and row.warehouse not in posted
		and not (row.warehouse_name in standard and row.warehouse not in held)
		and get_line_key((accounts.get(row.warehouse) or frappe._dict()).account, lines) in off
	]


def has_profit_and_loss_rows(onboarding) -> bool:
	accounts = [row.account for row in onboarding.opening_balances if row.account]
	return bool(
		accounts
		and frappe.db.exists(
			"Account", {"name": ("in", accounts), "root_type": ("in", ("Income", "Expense"))}
		)
	)


def get_difference_account(onboarding, create: bool = False) -> str | None:
	"""The Opening Stock Difference ledger: in P&L when the trial balance has P&L rows, else with the
	capital. Made when missing; refused when one of that name is on the other side."""
	company = onboarding.company
	profit_and_loss = has_profit_and_loss_rows(onboarding)
	account = frappe.db.get_value(
		"Account",
		{"company": company, "account_name": _(DIFFERENCE_ACCOUNT), "is_group": 0},
		["name", "report_type"],
		as_dict=True,
	)
	if account:
		if (account.report_type == "Profit and Loss") != profit_and_loss:
			frappe.throw(
				_("{0} is a {1} account. Rename it, or pick the other side.").format(
					frappe.bold(account.name), _(account.report_type)
				),
				title=_("Wrong side"),
			)
		return account.name
	if not create:
		return None
	if profit_and_loss:
		return add_account(company, _(DIFFERENCE_ACCOUNT), "", "Expense")
	return add_account(company, _(DIFFERENCE_ACCOUNT), "", "Equity", parent=get_capital_group(onboarding))


def get_capital_group(onboarding) -> str | None:
	"""Where the difference goes with the capital: the group of the trial balance's capital or reserves
	ledger, else the top Equity group, else the top Liability group (the India chart has no Equity root)."""
	company = onboarding.company
	accounts = [row.account for row in onboarding.opening_balances if row.account]
	for account in frappe.get_all(
		"Account",
		filters={"name": ("in", accounts or [""])},
		fields=["name", "account_name", "root_type", "parent_account"],
	):
		if account.root_type == "Equity" or re.search(
			r"\b(capital|reserves?)\b", get_similar_key(account.account_name)
		):
			if account.parent_account:
				return account.parent_account
	for root_type in ("Equity", "Liability"):
		group = frappe.db.get_value(
			"Account",
			{"company": company, "root_type": root_type, "is_group": 1, "parent_account": ("is", "not set")},
		)
		if group:
			return group
	return None


def post_stock_difference(onboarding, checked: bool = False) -> str:
	"""One entry on the day before between Opening Stock Difference and Temporary Opening for what the
	stock lines are off by. With the capital it is an opening entry; in P&L an ordinary one. A gap over
	BIG_GAP of its line posts only when checked: the user ticked that no warehouse is missing."""
	blocker = get_difference_blocker(onboarding)
	if blocker:
		frappe.throw(blocker, title=_("Can't post the difference"))
	lines = get_difference_lines(onboarding)
	big = next((line for line in lines if line["big"]), None)
	if big and not checked:
		frappe.throw(
			_(
				"{0} is a big gap on {1}. A warehouse may be missing: upload it, or tick that none is missing."
			).format(
				frappe.format_value(abs(big["difference"]), {"fieldtype": "Currency"}),
				frappe.get_cached_value("Account", big["account"], "account_name")
				if big["account"]
				else _("Stock"),
			),
			title=_("Big gap"),
		)
	day_before = get_day_before(onboarding)
	profit_and_loss = has_profit_and_loss_rows(onboarding)
	blocker = get_posting_blocker(onboarding.company, day_before, profit_and_loss=profit_and_loss)
	if blocker:
		frappe.throw(blocker, title=_("Can't post the difference"))
	account = get_difference_account(onboarding, create=True)
	temporary = get_temporary_account(onboarding.company, create=True)
	amount = flt(sum(line["difference"] for line in lines), 2)
	dimensions = get_default_dimensions(onboarding.company)
	cost_center = frappe.get_cached_value("Company", onboarding.company, "cost_center")
	je = frappe.new_doc("Journal Entry")
	je.update(
		{
			"voucher_type": "Journal Entry" if profit_and_loss else "Opening Entry",
			"is_opening": "No" if profit_and_loss else "Yes",
			"company": onboarding.company,
			"posting_date": day_before,
			"user_remark": _("Opening stock difference from Company Setup"),
		}
	)
	append_lines(je, account, amount, temporary, dimensions, cost_center)
	je.insert()
	je.submit()
	onboarding.stock_difference = {line["account"]: line["difference"] for line in lines}
	onboarding.stock_difference_entry = je.name
	return je.name


def set_stock_defaults(company: str) -> list[str]:
	"""A default inventory account that is our own stock, not a job worker or work in progress line,
	and a stock adjustment account, which stock entries need. Returns the accounts added."""
	values, added = {}, []
	inventory = frappe.db.get_value("Company", company, "default_inventory_account")
	if (
		not inventory
		or JOB_WORKER.search(get_similar_key(inventory))
		or WORK_IN_PROGRESS.search(get_similar_key(inventory))
	):
		values["default_inventory_account"] = next(
			(
				account.name
				for account in frappe.get_all(
					"Account",
					filters={"company": company, "account_type": "Stock", "is_group": 0, "disabled": 0},
					fields=["name", "account_name"],
					order_by="lft",
				)
				if not JOB_WORKER.search(get_similar_key(account.account_name))
				and not WORK_IN_PROGRESS.search(get_similar_key(account.account_name))
			),
			inventory,
		)
	has_stock = values.get("default_inventory_account") or inventory
	if has_stock and not frappe.db.get_value("Company", company, "stock_adjustment_account"):
		values["stock_adjustment_account"] = frappe.db.get_value(
			"Account",
			{"company": company, "account_type": "Stock Adjustment", "is_group": 0, "disabled": 0},
			order_by="lft",
		)
		if not values["stock_adjustment_account"]:
			values["stock_adjustment_account"] = add_account(
				company, _("Stock Adjustment"), "Stock Adjustment", "Expense"
			)
			added.append(values["stock_adjustment_account"])
	if values:
		frappe.has_permission("Company", "write", doc=company, throw=True)
		frappe.db.set_value("Company", company, values)
	return added


def is_serial_batch_off() -> bool:
	return not cint(frappe.db.get_single_value("Stock Settings", "enable_serial_and_batch_no_for_item"))


def turn_on_serial_and_batch():
	settings = frappe.get_doc("Stock Settings")
	settings.enable_serial_and_batch_no_for_item = 1
	settings.save()


def turn_on_perpetual_inventory(company: str):
	doc = frappe.get_doc("Company", company)
	doc.enable_perpetual_inventory = 1
	doc.save()
	frappe.local.enable_perpetual_inventory = {}
	set_stock_defaults(company)


# Status for the steps


def get_stock_status(onboarding) -> frappe._dict:
	return get_cached(onboarding, "stock_status", read_stock_status)


def read_stock_status(onboarding) -> frappe._dict:
	drafts = [row for row in onboarding.opening_stock if row.status != "Posted"]
	posted = [row for row in onboarding.opening_stock if row.status == "Posted"]
	lines = get_stock_lines(onboarding)
	match = get_stock_match(onboarding)
	return frappe._dict(
		lines=lines,
		line_total=flt(sum(lines.values()), 2),
		drafts=len(drafts),
		draft_sources=sorted({row.source or "" for row in drafts}),
		posted=len(posted),
		posted_sources=len({row.source or "" for row in posted}),
		warehouses=len({row.warehouse for row in posted}),
		off=flt(sum(lines.values()) - sum(flt(row.amount) for row in onboarding.opening_stock), 2)
		- flt(match["difference_posted"] if match else 0),
		matches=bool(match and match["matches"]),
		posting=is_posting_stock(onboarding.company),
	)
