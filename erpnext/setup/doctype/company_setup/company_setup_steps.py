# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""The steps of Company Setup.

A step is a function that gets the Company Setup document and returns its title,
description, and whether it is done, plus an optional short summary of what was done, whether it
is started but not done, and the form tab where it is done. "Done" always comes from the company's
data, never from a click.

Every app, ERPNext included, lists its steps in hooks.py, in order. A step can go just before
another step by its name (the last part of its path)::

    company_setup_steps = [
        {"step": "india_compliance.onboarding.gst_settings", "before": "opening_balances"},
        "hrms.onboarding.add_employees",  # no place given: just before Go Live
    ]

Apps come in install order, so a step can name a step of an app installed before it. A step whose
place is not found goes just before Go Live, and Go Live is always last.

A step can take another step's place, which is then left out::

    {"step": "india_compliance.onboarding.gst_settings", "replaces": "taxes"}

A step that returns None is hidden: not listed and not counted.
"""

import frappe
from frappe import _
from frappe.query_builder import Case
from frappe.query_builder.functions import Sum
from frappe.utils import flt, fmt_money, formatdate, getdate

from erpnext.setup.doctype.company_setup.bank_accounts import (
	cheque_words,
	get_bank_match,
	get_bank_status,
	get_cheque_blocker,
	get_cheque_checks,
)
from erpnext.setup.doctype.company_setup.fixed_assets import get_asset_checks, get_assets_status
from erpnext.setup.doctype.company_setup.open_orders import (
	get_boms_status,
	get_order_checks,
	get_orders_status,
)
from erpnext.setup.doctype.company_setup.opening_invoices import (
	TABLES,
	count_text,
	get_cached,
	get_day_before,
	get_hand_posted,
	get_missing_default_account,
	get_missing_fiscal_year,
	get_posting_reds,
	get_sheet_match,
	has_fiscal_year,
	normalize_account_name,
	number,
)
from erpnext.setup.doctype.company_setup.opening_stock import (
	get_items_status,
	get_stock_checks,
	get_stock_match,
	get_stock_reds,
	get_stock_status,
	get_warehouses_status,
)
from erpnext.setup.doctype.company_setup.taxes import get_taxes_status

HOOK_NAME = "company_setup_steps"
# Go Live always comes last, whatever the hooks say
LAST_STEP = "erpnext.setup.doctype.company_setup.company_setup_steps.go_live"


def get_step_paths() -> list[str]:
	"""Every app's steps, in the order the user sees them: in install order, each just before the
	step its "before" names, else just before Go Live; a step that "replaces" another takes its
	place. Go Live is always last."""
	steps = []
	for entry in get_hooked_steps():
		place = entry.get("replaces") or entry.get("before")
		index = find_step(steps, place) if place else None
		if index is None:
			steps.append(entry["step"])
		elif entry.get("replaces"):
			steps[index] = entry["step"]
		else:
			steps.insert(index, entry["step"])
	return [*steps, LAST_STEP]


def get_hooked_steps() -> list[dict]:
	"""Steps from the hook of every installed app, in install order. Bad entries are skipped, and so is
	a place that is Go Live: nothing goes after it or takes its place."""
	entries = []
	for app in frappe.get_installed_apps():
		for entry in frappe.get_hooks(HOOK_NAME, app_name=app) or []:
			if isinstance(entry, str):
				entry = {"step": entry}
			if not isinstance(entry, dict) or not isinstance(entry.get("step"), str):
				frappe.logger("company_setup").warning(f"Skipping step {entry!r} from {app}")
				continue
			if is_last_step(entry["step"]):
				continue
			entries.append(
				{
					"step": entry["step"],
					**{
						key: entry[key]
						for key in ("before", "replaces")
						if isinstance(entry.get(key), str) and not is_last_step(entry[key])
					},
				}
			)
	return entries


def find_step(steps: list, name: str) -> int | None:
	"""Where a step is in the list, by its full path or its name (the last part of the path)."""
	for index, step in enumerate(steps):
		if name in (step, step.rsplit(".", 1)[-1]):
			return index
	return None


def is_last_step(name: str | None) -> bool:
	return bool(name) and name in (LAST_STEP, LAST_STEP.rsplit(".", 1)[-1])


def migration_date(onboarding) -> dict:
	day_before = get_day_before(onboarding)
	return {
		"title": _("Migration date"),
		"description": _(
			"The day you move to ERPNext. Your old balances come in as they stood the day before."
		),
		"summary": onboarding.migration_date
		and _("{0} · balances as on {1}").format(
			formatdate(onboarding.migration_date), formatdate(day_before)
		),
		"done": bool(onboarding.migration_date),
		"required": True,
	}


def chart_of_accounts(onboarding) -> dict:
	if onboarding.chart_of_accounts_source == "Existing":
		description = _("You kept the chart made during setup. Edit it any time.")
	elif onboarding.chart_of_accounts_source == "Imported":
		description = _("You imported your own chart. Edit it any time.")
	else:
		description = _("Keep the chart made during setup, or import your own.")
	done = bool(onboarding.chart_of_accounts_source)
	summary = None
	if done:
		count = frappe.db.count("Account", {"company": onboarding.company})
		# posting adds Temporary Opening to an imported chart: counted apart, so the number stays the sheet's
		added = onboarding.chart_of_accounts_source == "Imported" and frappe.db.count(
			"Account",
			{
				"company": onboarding.company,
				"account_type": "Temporary",
				"account_name": _("Temporary Opening"),
			},
		)
		summary = (
			_("{0} accounts · plus Temporary Opening").format(number(count - added))
			if added
			else _("{0} accounts").format(number(count))
		)
	return {
		"title": _("Chart of accounts"),
		"description": description,
		"summary": summary,
		"done": done,
		"tab": "accounts_tab",
		"required": True,
	}


def taxes(onboarding) -> dict:
	"""ERPNext's own tax templates step. A country's compliance app replaces it."""
	status = get_taxes_status(onboarding)
	if not status.saved:
		summary = _("Add your sales and purchase tax templates")
	elif status.templates:
		summary = count_text(status.templates, _("1 tax template"), _("{0} tax templates"))
	else:
		summary = _("No taxes charged")
	return {
		"title": _("Taxes"),
		"description": _("Your sales and purchase tax templates, each with its tax accounts and rates."),
		"summary": summary,
		"done": bool(status.saved),
		"tab": "accounts_tab",
		"required": True,
	}


def opening_balances(onboarding) -> dict:
	rows = onboarding.opening_balances
	posted = all_opening_balances_posted(onboarding)
	if posted:
		summary = (
			_("1 account posted") if len(rows) == 1 else _("{0} accounts posted").format(number(len(rows)))
		)
	elif rows:
		currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
		difference = flt(
			sum(flt(row.debit) - flt(row.credit) for row in rows if row.status != "Posted"), money_precision()
		)
		drafts = sum(1 for row in rows if row.status != "Posted")
		unpicked = sum(1 for row in rows if row.status != "Posted" and not row.account)
		covered = get_posted_cover(onboarding) if difference else 0
		left = flt(difference + covered, money_precision())
		if not difference:
			summary = _("{0} ready to post · balanced").format(number(drafts))
		elif covered and abs(left) < 1:
			summary = _("{0} ready to post · covered by what is posted").format(number(drafts))
		elif covered:
			summary = _("{0} ready to post · {1} not covered yet").format(
				number(drafts), fmt_money(abs(left), currency=currency)
			)
		else:
			summary = _("{0} ready to post · difference {1}").format(
				number(drafts), fmt_money(abs(difference), currency=currency)
			)
		if unpicked:
			summary = _("{0} in draft · {1} need an account").format(number(drafts), number(unpicked))
	else:
		summary = None
	return {
		"title": _("Opening balances"),
		"description": _("Bank, cash, loans, capital, tax and other balances from your old books."),
		"summary": summary,
		"done": posted,
		"started": bool(rows) and not posted,
		"tab": "accounts_tab",
		"required": True,
	}


def bank_accounts(onboarding) -> dict:
	status = get_bank_status(onboarding)
	match = get_bank_match(onboarding)
	posted = status.cheques or status.charges or status.deposits
	cheques_done = (
		status.has_uncleared_cheques == "Yes"
		and posted
		and not status.drafts
		and (not match or match["matched"] == match["total"])
	)
	done = (
		bool(status.accounts)
		and status.added == status.accounts
		and bool(status.default_bank)
		and (status.has_uncleared_cheques == "No" or cheques_done)
	)
	accounts = _("1 bank account") if status.accounts == 1 else _("{0} bank accounts").format(status.accounts)
	skipped = any(
		row.step_key.endswith(".bank_accounts") and row.status == "Skipped" for row in onboarding.steps
	)
	if not status.accounts and not status.unmarked and not onboarding.opening_balances:
		summary = before_trial_balance()
	elif not status.accounts and not status.unmarked:
		summary = _("No bank lines in your opening balances · add bank details if you have a bank account")
	elif not status.accounts:
		summary = count_text(
			status.unmarked,
			_("1 account under Bank Accounts or Bank OD is not marked as a bank account."),
			_("{0} accounts under Bank Accounts or Bank OD are not marked as bank accounts."),
		)
	elif onboarding.status == "Live":
		summary = _("{0} · reconcile from {1} in Bank Reconciliation").format(
			accounts, formatdate(get_day_before(onboarding))
		)
	elif skipped:
		summary = _("Skipped · add bank details before your first bank payment")
	elif not status.added:
		summary = (
			_("{0} in your trial balance · add their bank details")
			if onboarding.opening_balances
			else _("{0} · add their bank details")
		).format(accounts)
	elif status.added < status.accounts:
		summary = _("{0} of {1} bank accounts added.").format(status.added, status.accounts)
		if status.no_bank:
			first = frappe.get_cached_value("Account", status.no_bank[0], "account_name")
			summary = _("{0} of {1} bank accounts added · {2}: pick its bank to add it").format(
				status.added, status.accounts, first
			)
			if len(status.no_bank) > 1:
				summary += " · " + _("{0} more").format(len(status.no_bank) - 1)
	elif not status.default_bank:
		summary = _("{0} · pick a default bank").format(accounts)
	elif status.has_uncleared_cheques == "Yes" and status.drafts:
		summary = _("{0} · uncleared cheques in draft").format(accounts)
	elif posted:
		summary = _(
			"{0} · {1} posted. When the bank clears them, mark them in Bank Clearance, from {2} on."
		).format(
			accounts,
			count_cheques(status.cheques, status.deposits, status.charges),
			formatdate(get_day_before(onboarding)),
		)
	elif status.has_uncleared_cheques == "No":
		summary = _("{0} · No uncleared cheques").format(accounts)
	else:
		summary = accounts
	return {
		"title": _("Bank accounts"),
		"description": cheque_words(
			onboarding.company, _("Bank details and default bank, and the cheques the bank had not cleared.")
		),
		"summary": summary and cheque_words(onboarding.company, summary),
		"done": done,
		"started": bool(onboarding.bank_accounts or onboarding.uncleared_cheques) and not done,
		"warnings": status.statement_off if onboarding.status != "Live" else [],
		"blocker": status.drafts
		and not status.posting
		and onboarding.migration_date
		and get_blocker_line([get_cheque_blocker(onboarding, get_cheque_checks(onboarding))]),
		"has_lines": bool(status.accounts),
		"tab": "accounts_tab",
	}


def count_cheques(cheques: int, deposits: int, charges: int = 0) -> str:
	"""Like "11 cheques and 1 deposit", or "4 cheques, 1 card charge and 1 deposit"."""
	parts = []
	if cheques:
		parts.append(_("1 cheque") if cheques == 1 else _("{0} cheques").format(number(cheques)))
	if charges:
		parts.append(_("1 card charge") if charges == 1 else _("{0} card charges").format(number(charges)))
	if deposits:
		parts.append(_("1 deposit") if deposits == 1 else _("{0} deposits").format(number(deposits)))
	if len(parts) > 1:
		return _("{0} and {1}").format(", ".join(parts[:-1]), parts[-1])
	return parts[0]


def customers(onboarding) -> dict:
	count = frappe.db.count("Customer", {"disabled": 0})
	disabled = frappe.db.count("Customer", {"disabled": 1})
	return {
		"title": _("Customers"),
		"description": _("Import your customers with their addresses and tax IDs, as often as you like."),
		"summary": count_parties(count, disabled, _("1 customer"), _("{0} customers")),
		"done": bool(count),
		"tab": "selling_tab",
		"required": has_party_lines(onboarding, "Customer"),
	}


def count_parties(count: int, disabled: int, one: str, many: str) -> str | None:
	"""Like "24 customers (1 disabled)"."""
	if not count:
		return None
	text = one if count == 1 else many.format(number(count))
	return _("{0} ({1} disabled)").format(text, number(disabled)) if disabled else text


def count_entries(rows, is_sales: bool) -> str:
	"""Like "39 invoices + 1 credit or advance": the rows below zero."""
	credits = sum(1 for row in rows if flt(row.outstanding_amount) < 0)
	invoices = len(rows) - credits
	parts = []
	if invoices or not credits:
		if is_sales:
			parts.append(_("1 invoice") if invoices == 1 else _("{0} invoices").format(number(invoices)))
		else:
			parts.append(_("1 bill") if invoices == 1 else _("{0} bills").format(number(invoices)))
	if credits and is_sales:
		parts.append(
			_("1 credit or advance") if credits == 1 else _("{0} credits or advances").format(number(credits))
		)
	elif credits:
		parts.append(
			_("1 debit or advance") if credits == 1 else _("{0} debits or advances").format(number(credits))
		)
	return _("{0} + {1}").format(*parts) if len(parts) == 2 else parts[0]


def opening_sales_invoices(onboarding) -> dict:
	return opening_invoices(
		onboarding,
		"Sales Invoice",
		_("Opening sales invoices"),
		_("Customer invoices still unpaid on the day before migration."),
		"selling_tab",
	)


def suppliers(onboarding) -> dict:
	count = frappe.db.count("Supplier", {"disabled": 0})
	disabled = frappe.db.count("Supplier", {"disabled": 1})
	return {
		"title": _("Suppliers"),
		"description": _("Import your suppliers with their addresses and tax IDs, as often as you like."),
		"summary": count_parties(count, disabled, _("1 supplier"), _("{0} suppliers")),
		"done": bool(count),
		"tab": "buying_tab",
		"required": has_party_lines(onboarding, "Supplier"),
	}


def opening_purchase_invoices(onboarding) -> dict:
	return opening_invoices(
		onboarding,
		"Purchase Invoice",
		_("Unpaid supplier bills"),
		_("Supplier bills still unpaid on the day before migration."),
		"buying_tab",
	)


def opening_invoices(onboarding, doctype: str, title: str, description: str, tab: str) -> dict:
	totals = frappe.get_all(
		doctype,
		filters={"company": onboarding.company, "is_opening": "Yes", "docstatus": 1},
		fields=[{"COUNT": "*", "as": "count"}, {"SUM": "outstanding_amount", "as": "outstanding"}],
	)[0]
	table = "opening_sales_invoices" if doctype == "Sales Invoice" else "opening_purchase_invoices"
	# drafts an invoice made by hand looks to stand for: to review, not to post
	hand = get_hand_posted(onboarding, "Customer" if doctype == "Sales Invoice" else "Supplier")
	drafts = [row for row in onboarding.get(table) if row.status != "Created" and row not in hand]
	failed = sum(1 for row in drafts if row.status == "Failed")
	currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
	# counted like the windows: invoices + credits, the amount net of the credits
	is_sales = doctype == "Sales Invoice"
	posted = [row for row in onboarding.get(table) if row.status == "Created"]
	if drafts and totals.count:
		text = (
			_("{0} posted · {1} left to post: {2} failed, fix them and post again").format(
				count_entries(posted, is_sales), count_entries(drafts, is_sales), failed
			)
			if failed
			else _("{0} posted · {1} left to post").format(
				count_entries(posted, is_sales), count_entries(drafts, is_sales)
			)
		)
	elif failed:
		text = _("{0} ready to post · {1} failed, fix them and post again").format(
			count_entries(drafts, is_sales), failed
		)
	elif totals.count:
		undone = onboarding.get("undone_sales_invoices" if is_sales else "undone_purchase_invoices")
		text = (_("{0} posted · {1}") if undone else _("All {0} posted · {1}")).format(
			count_entries(posted, is_sales),
			fmt_money(sum(flt(row.outstanding_amount) for row in posted), currency=currency, precision=0),
		)
	elif drafts:
		to_review = len({row.party_name for row in drafts if not row.party and not row.create_party})
		amount = fmt_money(sum(flt(row.outstanding_amount) for row in drafts), currency=currency, precision=0)
		text = _("{0} ready to post · {1}").format(count_entries(drafts, is_sales), amount)
		if to_review:
			text += " · " + (
				_("1 name to review") if to_review == 1 else _("{0} names to review").format(to_review)
			)
	else:
		text = None
	if hand:
		text = " · ".join(
			filter(
				None,
				[
					text,
					_("1 row looks posted by hand · review")
					if len(hand) == 1
					else _("{0} rows look posted by hand · review").format(len(hand)),
				],
			)
		)
	# done once all is posted and adds up to what the trial balance says, when there is one
	match = get_sheet_match(onboarding, "Customer" if doctype == "Sales Invoice" else "Supplier")
	off = [
		line
		for line in (match and (match["lines"] or [{"sheet": match["sheet"], "entered": match["entered"]}]))
		or []
		if abs(flt(line["sheet"]) - flt(line["entered"])) >= 1
	]
	done = bool(totals.count) and not drafts and not hand and not off
	if totals.count and not drafts and not hand and off:
		text = " · ".join([text, get_off_text(off, currency)])
	return {
		"title": title,
		"description": description,
		"summary": text,
		"done": done,
		"started": bool(drafts or hand or totals.count) and not done,
		"blocker": bool(drafts) and onboarding.migration_date and get_invoice_blocker(onboarding, doctype),
		"tab": tab,
		"required": has_party_lines(onboarding, "Customer" if doctype == "Sales Invoice" else "Supplier"),
	}


def get_off_text(lines: list[dict], currency: str) -> str:
	"""Like "Debtors - Domestic is ₹5,30,000 more than the trial balance", or "2 ledgers don't match
	the trial balance"."""
	if len(lines) > 1:
		return _("{0} ledgers don't match the trial balance").format(len(lines))
	line = lines[0]
	off = flt(line["entered"]) - flt(line["sheet"])
	amount = fmt_money(abs(off), currency=currency, precision=0)
	if line.get("account"):
		text = (
			_("{0} is {1} more than the trial balance")
			if off > 0
			else _("{0} is {1} short of the trial balance")
		)
		return text.format(line["account"], amount)
	return (_("{0} more than the trial balance") if off > 0 else _("{0} short of the trial balance")).format(
		amount
	)


def get_invoice_blocker(onboarding, doctype: str) -> str | None:
	party_type = "Customer" if doctype == "Sales Invoice" else "Supplier"
	reds = get_posting_reds(
		onboarding.company, get_day_before(onboarding), voucher_types=(doctype, "Journal Entry")
	)
	missing = get_missing_default_account(onboarding, party_type)
	return get_blocker_line([missing, *(red["text"] for red in reds)])


def has_party_lines(onboarding, party_type: str) -> bool:
	"""The trial balance has what customers owe, or what suppliers are owed."""
	account_type = "Receivable" if party_type == "Customer" else "Payable"
	lines = (frappe.parse_json(onboarding.sheet_accounts) or {}).get(account_type) or {}
	return any(flt(amount, money_precision()) for amount in lines.values())


def items(onboarding) -> dict:
	"""Needed when there is stock to add."""
	status = get_items_status(onboarding)
	if status.missing:
		summary = _("{0} · {1} on your stock sheet are not in ERPNext").format(
			count_text(status.item_count, _("1 item"), _("{0} items")), number(status.missing)
		)
	elif status.item_count:
		summary = " · ".join(
			[
				count_text(status.stock_items, _("1 stock item"), _("{0} stock items"))
				+ " · "
				+ count_text(status.services, _("1 service"), _("{0} services"))
				if status.services
				else count_text(status.item_count, _("1 item"), _("{0} items")),
				count_text(status.item_groups, _("1 item group"), _("{0} item groups")),
				count_text(status.units, _("1 unit"), _("{0} units")),
			]
		)
	elif not onboarding.opening_balances:
		summary = before_trial_balance()
	elif not needs_stock(onboarding):
		summary = _("Not needed · no stock in your opening balances · add items if you keep stock")
	else:
		summary = None
	return {
		"title": _("Items"),
		"description": _("Import your item groups, units and items before your opening stock."),
		"summary": summary,
		"done": bool(status.item_count) and not status.missing,
		"tab": "stock_tab",
		"required": needs_stock(onboarding),
	}


def warehouses(onboarding) -> dict:
	"""Needed when there is stock to add. Done once the window is saved: the default warehouses are
	there to review, not to take as they are."""
	status = get_warehouses_status(onboarding)
	warehouse_count = count_text(status.warehouses, _("1 warehouse"), _("{0} warehouses"))
	if status.missing:
		summary = _("{0} · {1} on your stock sheet are not in ERPNext").format(
			warehouse_count, number(status.missing)
		)
	elif not status.saved:
		summary = _("{0} · review them").format(warehouse_count) if status.warehouses else None
	else:
		parts = [warehouse_count]
		if status.job_workers:
			parts.append(count_text(len(status.job_workers), _("1 job worker"), _("{0} job workers")))
		if status.work_in_progress:
			parts.append(
				count_text(status.work_in_progress, _("1 work in progress"), _("{0} work in progress"))
			)
		summary = " · ".join(parts) if status.warehouses else None
	return {
		"title": _("Warehouses"),
		"description": _("Your warehouses, those at job workers and for work in progress."),
		"summary": summary,
		"done": status.saved and not status.missing,
		"tab": "stock_tab",
		"required": needs_stock(onboarding),
	}


def opening_stock(onboarding) -> dict:
	"""Needed when the trial balance has a stock line, or stock rows are added."""
	status = get_stock_status(onboarding)
	currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
	off = fmt_money(abs(status.off), currency=currency, precision=0)
	uploads = count_text(status.posted_sources, _("1 upload"), _("{0} uploads"))
	if status.posting:
		summary = (
			_("Posting {0} in the background…").format(count_text(status.drafts, _("1 row"), _("{0} rows")))
			if status.drafts
			else _("Updating stock in the background…")
		)
	elif status.drafts and status.posted:
		summary = _("{0} posted · {1} in draft").format(
			count_text(status.posted, _("1 row"), _("{0} rows")), ", ".join(status.draft_sources) or _("rows")
		)
	elif status.drafts:
		summary = _("{0} in draft").format(count_text(status.drafts, _("1 row"), _("{0} rows")))
		if len(status.draft_sources) > 1:
			summary += ", " + count_text(len(status.draft_sources), _("1 upload"), _("{0} uploads"))
		if status.lines and abs(status.off) >= 1:
			summary += " · " + _("off by {0}").format(off)
	elif status.posted and status.lines and not status.matches and abs(status.off) < 1:
		summary = _("{0} posted, {1} · the total matches, but stock is on the wrong accounts").format(
			count_text(status.posted, _("1 row"), _("{0} rows")), uploads
		)
	elif status.posted and status.lines and not status.matches:
		summary = _("{0} posted, {1} · off by {2} · fix rows, or post the difference").format(
			count_text(status.posted, _("1 row"), _("{0} rows")), uploads, off
		)
	elif status.posted:
		summary = _("{0} in {1}").format(
			count_text(status.posted, _("1 row"), _("{0} rows")),
			count_text(status.warehouses, _("1 warehouse"), _("{0} warehouses")),
		)
		if onboarding.status != "Live" and status.posted_sources > 1:
			summary += ", " + uploads
		if status.lines:
			summary += " · " + _("matches your trial balance")
	elif status.lines:
		summary = _("{0} in stock in your trial balance · add it before your first stock entry").format(
			fmt_money(status.line_total, currency=currency, precision=0)
		)
	elif not onboarding.opening_balances:
		summary = before_trial_balance()
	else:
		summary = _("Not needed · no stock in your opening balances · add stock if you keep any")
	done = not status.drafts and bool(status.posted) and (status.matches or not status.lines)
	return {
		"title": _("Opening stock"),
		"description": _("The stock on the day before migration, by item and warehouse."),
		"summary": summary,
		"done": done,
		"started": bool(status.drafts or status.posted) and not done,
		"blocker": status.drafts
		and not status.posting
		and onboarding.migration_date
		and get_stock_blocker_line(onboarding),
		"tab": "stock_tab",
		"required": needs_stock(onboarding),
	}


def get_stock_blocker_line(onboarding) -> str | None:
	checks = get_cached(onboarding, "stock_checks", get_stock_checks)
	missing = checks["reds"]
	both = missing.missing_warehouses and missing.missing_items
	texts = []
	for red in get_stock_reds(checks):
		if red["key"] == "no_drafts" or (both and red["key"] == "missing_items"):
			continue
		if both and red["key"] == "missing_warehouses":
			texts.append(
				_("{0} and {1} are not in ERPNext.").format(
					count_text(len(missing.missing_warehouses), _("1 warehouse"), _("{0} warehouses")),
					count_text(len(missing.missing_items), _("1 item"), _("{0} items")),
				)
			)
		else:
			texts.append(red["text"])
	return get_blocker_line(texts)


def get_blocker_line(texts: list) -> str | None:
	"""The card's red line: the first reason the drafts can't be posted, its first sentence only
	(the window says how to fix it), and how many reasons more."""
	texts = [text for text in texts if text]
	if not texts:
		return None
	line = _("Can't post yet: {0}").format(texts[0].split(". ")[0].rstrip("."))
	if len(texts) > 1:
		line += " · " + _("{0} more").format(len(texts) - 1)
	return line


def get_posted_cover(onboarding) -> float:
	"""What the customer invoices, supplier bills and, with a stock line, the stock posted add up to:
	the opening balances' difference waits for it, as the Opening balances window says."""
	covered = sum(
		flt(row.outstanding_amount) for row in onboarding.opening_sales_invoices if row.status == "Created"
	) - sum(
		flt(row.outstanding_amount) for row in onboarding.opening_purchase_invoices if row.status == "Created"
	)
	if flt(onboarding.sheet_stock):
		stock = get_stock_match(onboarding) or {}
		covered += flt(stock.get("books")) + flt(stock.get("difference_posted"))
	return covered


def before_trial_balance() -> str:
	"""What a step the trial balance decides on says until the trial balance is in."""
	return _("Optional · we'll tell you once your trial balance is in")


def needs_stock(onboarding) -> bool:
	"""The trial balance has a stock line, or stock rows are added."""
	status = get_stock_status(onboarding)
	return bool(status.lines or status.drafts or status.posted)


def job_workers(onboarding) -> dict:
	"""Optional. Job workers are set up in Warehouses."""
	names = get_warehouses_status(onboarding).job_workers
	return {
		"title": _("Job workers"),
		"description": _("Suppliers who do job work, each with a warehouse for your stock with them."),
		"summary": _("{0} · {1}").format(
			count_text(len(names), _("1 job worker"), _("{0} job workers")), ", ".join(names)
		)
		if names
		else _("Add them in Warehouses"),
		"done": bool(names),
		"tab": "subcontracting_tab",
	}


def open_sales_orders(onboarding) -> dict:
	return open_orders(
		onboarding,
		"Sales Order",
		_("Open sales orders"),
		_("Enter only the qty not yet delivered. Do not add advances again."),
		_("{0} to deliver"),
		"selling_tab",
	)


def open_purchase_orders(onboarding) -> dict:
	return open_orders(
		onboarding,
		"Purchase Order",
		_("Open purchase orders"),
		_("Enter only the qty not yet received. Do not add advances again."),
		_("{0} to receive"),
		"buying_tab",
	)


def fixed_assets(onboarding) -> dict:
	"""Optional, and open after Go live: checked as on today."""
	status = get_assets_status(onboarding)
	currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
	skipped = any(
		row.step_key.endswith(".fixed_assets") and row.status == "Skipped" for row in onboarding.steps
	)
	assets = count_text(status.posted, _("1 asset"), _("{0} assets"))
	match = status.match
	if status.drafts:
		summary = _("{0} in draft").format(count_text(status.drafts, _("1 asset"), _("{0} assets")))
		if status.off:
			account, amount = next(iter(status.off.items()))
			summary += " · " + _("off by {0} from {1}").format(
				fmt_money(abs(amount), currency=currency, precision=0),
				frappe.get_cached_value("Account", account, "account_name"),
			)
	elif match and match["hand_after"]:
		summary = _("{0} · {1} booked by hand after they were added").format(
			assets, fmt_money(match["hand_after"], currency=currency, precision=0)
		)
	elif match and match["matches"]:
		summary = _("{0} · matches your asset lines as on today").format(assets)
	elif match:
		off = abs(flt(match["cost"] - match["books_cost"], 2)) or abs(
			flt(match["depreciation"] - match["books_depreciation"], 2)
		)
		summary = (
			_("{0} · off by {1} from your asset lines as on today").format(
				assets, fmt_money(off, currency=currency, precision=0)
			)
			if off
			else _("{0} · off from your asset lines as on today").format(assets)
		)
	elif skipped:
		summary = _("Skipped · add assets before your first depreciation run")
	elif status.line_total:
		summary = _(
			"{0} in your trial balance · add the asset list before your first depreciation run"
		).format(fmt_money(status.line_total, currency=currency, precision=0))
	elif not onboarding.opening_balances:
		summary = before_trial_balance()
	else:
		summary = _(
			"Optional · no fixed assets in your opening balances · add the asset list if you have any"
		)
	# a register off from the lines was pointed out before posting; it doesn't keep the step open
	done = bool(status.posted) and not status.drafts and not (match and match["hand_after"])
	return {
		"title": _("Fixed assets"),
		"description": _("The asset register behind your asset lines, so depreciation runs from here."),
		"summary": summary,
		"done": done,
		"started": bool(status.drafts or status.posted) and not done,
		"blocker": status.drafts
		and not status.posting
		and onboarding.migration_date
		and get_blocker_line(
			[
				red["text"]
				for red in get_asset_checks(onboarding, check_items=False)["reds"]
				if red["key"] != "empty"
			]
		),
		"tab": "assets_tab",
		# money in the trial balance waits for this step: Go live points it out even when skipped
		"has_lines": bool(status.line_total),
	}


def boms(onboarding) -> dict:
	"""Optional. BOMs come in through the standard Data Import."""
	status = get_cached(onboarding, "boms_status", lambda doc: get_boms_status(doc.company))
	summary = count_text(status.boms, _("1 BOM"), _("{0} BOMs")) if status.boms else None
	if status.without_bom:
		missing = count_text(
			len(status.without_bom), _("1 item you make has no BOM"), _("{0} items you make have no BOM")
		)
		summary = f"{summary} · {missing}" if summary else missing
	return {
		"title": _("BOMs"),
		"description": _(
			"Add your BOMs (bills of materials: what goes into each product you make) before your first work order."
		),
		"summary": summary,
		"done": bool(status.boms),
		"tab": "manufacturing_tab",
	}


def open_work_orders(onboarding) -> dict:
	return open_orders(
		onboarding,
		"Work Order",
		_("Open work orders"),
		_("Work orders still running on the day before migration, for the qty left."),
		_("{0} left"),
		"manufacturing_tab",
	)


def open_subcontracting_orders(onboarding) -> dict:
	return open_orders(
		onboarding,
		"Subcontracting Order",
		_("Open subcontracting orders"),
		_("Orders with goods still at the job worker. Do not send materials again."),
		None,
		"subcontracting_tab",
	)


def open_orders(
	onboarding, order_type: str, title: str, description: str, left: str | None, tab: str
) -> dict:
	"""An optional step: done once its orders are made and none is left in draft."""
	status = get_orders_status(onboarding, order_type)
	label = (
		count_text(status.orders, _("1 work order"), _("{0} work orders"))
		if order_type == "Work Order"
		else count_text(status.orders, _("1 order"), _("{0} orders"))
	)
	if status.drafts:
		summary = _("{0} in draft").format(count_text(status.drafts, _("1 row"), _("{0} rows")))
	elif not status.orders:
		summary = None
	elif left:
		qty = ", ".join(" ".join(filter(None, (number(qty), unit))) for qty, unit in status.quantities)
		summary = f"{label} · {left.format(qty)}"
	else:
		summary = _("{0} · goods at {1}").format(label, ", ".join(status.parties))
	return {
		"title": title,
		"description": description,
		"summary": summary,
		"done": bool(status.orders) and not status.drafts,
		"started": bool(status.drafts),
		"blocker": status.drafts
		and onboarding.migration_date
		and get_blocker_line([red["text"] for red in get_order_checks(onboarding, order_type)["reds"]]),
		"tab": tab,
	}


def check_books(onboarding) -> dict:
	temporary = get_temporary_opening(onboarding)
	posted = all_opening_balances_posted(onboarding) or has_opening_invoices(onboarding.company)
	waiting = get_waiting(onboarding)
	bank = get_bank_match(onboarding)
	banks_match = not bank or bank["matched"] == bank["total"]
	stock = get_stock_match(onboarding)
	stock_matches = not stock or not stock["line"] or stock["matches"]
	assets = get_assets_status(onboarding).match
	parts = get_temporary_parts(onboarding)
	if waiting:
		summary = _("{0} still to post").format(frappe.utils.comma_and(waiting, add_quotes=False))
	elif not banks_match:
		summary = _("{0} of {1} bank accounts match their lines").format(bank["matched"], bank["total"])
	elif over := [part for part in parts if part.get("over")]:
		summary = _("More than your trial balance: {0} · check them or the trial balance").format(
			get_parts_text(onboarding, over)
		)
	elif to_come := [part for part in parts if part.get("to_come")]:
		summary = _("Waiting for: {0}").format(get_parts_text(onboarding, to_come))
	elif not stock_matches and abs(stock["off"]) < 1:
		summary = _("Stock is on the wrong accounts")
	elif not stock_matches:
		currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
		summary = _("Stock is off by {0}").format(fmt_money(abs(stock["off"]), currency=currency))
	elif assets and assets["hand_after"]:
		summary = _("Depreciation was booked by hand after the assets were posted")
	elif posted and temporary:
		currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
		summary = _("{0} left in Temporary Opening").format(fmt_money(abs(temporary), currency=currency))
	elif posted:
		summary = _("Your books add up")
	else:
		summary = None
	return {
		"title": _("Check your books"),
		"description": _(
			"Compare with your old books. Done when everything is posted, each bank matches its line and Temporary Opening is zero."
		),
		"summary": summary,
		"done": not waiting and posted and not temporary and banks_match and stock_matches,
		"tab": "accounts_tab",
		"required": True,
	}


def go_live(onboarding) -> dict:
	summary = None
	if onboarding.status == "Live":
		temporary = get_temporary_opening(onboarding)
		currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
		summary = (
			_("Live · {0} still in Temporary Opening").format(fmt_money(abs(temporary), currency=currency))
			if temporary
			else _("Live")
		)
	return {
		"title": _("Go live"),
		"description": _("Start using ERPNext. We point out anything left, but nothing stops you."),
		"summary": summary,
		"done": onboarding.status == "Live",
		"required": True,
	}


def get_parts_text(onboarding, parts: list[dict]) -> str:
	"""Like "customer invoices ₹26,06,833 · stock ₹4,000"."""
	currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
	return " · ".join(
		f"{part['label']} {fmt_money(abs(part['amount']), currency=currency, precision=0)}" for part in parts
	)


def get_temporary_parts(onboarding) -> list[dict]:
	"""What Temporary Opening is still waiting for, with what each takes of it now: the draft
	opening balances, the stock not posted (or its difference, once every row is posted), and the
	invoices and bills short of the trial balance, or over it. Once all are posted it is zero."""
	parts = []
	drafts = flt(
		sum(
			flt(row.debit) - flt(row.credit) for row in onboarding.opening_balances if row.status != "Posted"
		),
		2,
	)
	if drafts:
		parts.append({"label": _("opening balances"), "amount": drafts})
	stock = get_stock_match(onboarding)
	if stock and stock["line"] and abs(stock["off"]) >= 1:
		all_posted = stock["books"] and not any(row.status != "Posted" for row in onboarding.opening_stock)
		over = not all_posted and stock["off"] < 0
		parts.append(
			{
				"label": _("stock difference") if all_posted else _("stock"),
				"amount": stock["off"],
				"to_come": not all_posted and not over,
				"over": over,
			}
		)
	for party_type, label, sign in (
		("Customer", _("customer invoices"), 1),
		("Supplier", _("supplier bills"), -1),
	):
		match = get_sheet_match(onboarding, party_type)
		if not match:
			continue
		posted = match["entered"] - sum(
			flt(row.outstanding_amount)
			for row in onboarding.get(TABLES[party_type])
			if row.status != "Created"
		)
		short = flt(match["sheet"] - posted, money_precision())
		if abs(short) >= 0.01:
			parts.append(
				{
					"label": label,
					"amount": flt(sign * short, money_precision()),
					"to_come": short > 0,
					"over": short < 0,
				}
			)
	return parts


def get_waiting(onboarding) -> list[str]:
	"""What is entered but not posted yet, as "141 balances", "625 invoices" and "40 bills"."""
	labels = {
		"opening_balances": (_("1 balance"), _("{0} balances")),
		"opening_sales_invoices": (_("1 invoice"), _("{0} invoices")),
		"opening_purchase_invoices": (_("1 bill"), _("{0} bills")),
	}
	waiting = []
	for table, (one, many) in labels.items():
		count = sum(1 for row in onboarding.get(table) if row.status not in ("Posted", "Created"))
		if count:
			waiting.append(one if count == 1 else many.format(number(count)))
	return waiting


DRAFT_LABELS = {
	"opening_sales_invoices": (_("1 opening invoice"), _("{0} opening invoices")),
	"opening_purchase_invoices": (_("1 opening bill"), _("{0} opening bills")),
	"opening_balances": (_("1 opening balance"), _("{0} opening balances")),
	"opening_stock": (_("1 stock line"), _("{0} stock lines")),
	"fixed_assets": (_("1 asset"), _("{0} assets")),
	"uncleared_cheques": (_("1 uncleared cheque"), _("{0} uncleared cheques")),
}


def get_dated_drafts(onboarding) -> dict[str, list[tuple]]:
	"""Each table's opening drafts, with the date each would post on (None when it has none yet):
	invoices on their own date where ERPNext has its year, the rest on the day before migration."""
	day_before = get_day_before(onboarding)
	years = {}

	def get_date(row):
		own = row.invoice_date and getdate(row.invoice_date)
		if own and own not in years:
			years[own] = has_fiscal_year(own, onboarding.company)
		return own if own and years[own] else day_before

	dated = {}
	for table in DRAFT_LABELS:
		if table in TABLES.values():
			dated[table] = [(row, get_date(row)) for row in onboarding.get(table) if row.status != "Created"]
		else:
			dated[table] = [(row, day_before) for row in onboarding.get(table) if row.status != "Posted"]
	return dated


def get_draft_rows_by(onboarding, date, dated: dict | None = None) -> dict[str, list]:
	"""The opening drafts, by table, that would be dated on or before date: once the year to date is
	closed they can never be posted. dated: get_dated_drafts, when already known."""
	date = getdate(date)
	if dated is None:
		dated = get_dated_drafts(onboarding)
	rows = {table: [row for row, on in drafts if on and on <= date] for table, drafts in dated.items()}
	return {table: found for table, found in rows.items() if found}


def get_drafts_by(onboarding, date, dated: dict | None = None) -> list[str]:
	return get_draft_lines(get_draft_rows_by(onboarding, date, dated))


def get_draft_lines(rows_by_table: dict[str, list]) -> list[str]:
	"""get_draft_rows_by as lines, like "625 opening invoices in Delhi sheet" and "2 assets"."""
	lines = []
	for table, rows in rows_by_table.items():
		one, many = DRAFT_LABELS[table]
		sheets = {}
		for row in rows:
			sheet = row.source if table in TABLES.values() else None
			sheets[sheet] = sheets.get(sheet, 0) + 1
		for sheet, count in sheets.items():
			text = count_text(count, one, many)
			lines.append(_("{0} in {1}").format(text, sheet) if sheet else text)
	return lines


def has_opening_invoices(company: str) -> bool:
	return any(
		frappe.db.exists(doctype, {"company": company, "is_opening": "Yes", "docstatus": 1})
		for doctype in ("Sales Invoice", "Purchase Invoice")
	)


def all_opening_balances_posted(onboarding) -> bool:
	rows = onboarding.opening_balances
	return bool(rows) and all(row.status == "Posted" for row in rows)


def get_temporary_opening(onboarding) -> float:
	"""get_temporary_opening_balance, once per save: onload and the steps all ask for it."""
	return get_cached(onboarding, "temporary_opening", lambda doc: get_temporary_opening_balance(doc.company))


def get_temporary_opening_balance(company: str) -> float:
	"""Debit minus credit on the company's Temporary accounts. Zero once the opening is complete."""
	accounts = frappe.get_all(
		"Account", filters={"company": company, "account_type": "Temporary", "is_group": 0}, pluck="name"
	)
	if not accounts:
		return 0.0

	gle = frappe.qb.DocType("GL Entry")
	balance = (
		frappe.qb.from_(gle)
		.select(Sum(gle.debit) - Sum(gle.credit))
		.where(gle.account.isin(accounts) & (gle.is_cancelled == 0))
	).run()[0][0]
	return flt(balance, frappe.get_precision("GL Entry", "debit"))


def money_precision() -> int:
	"""The decimals amounts are kept to, as the ledger keeps them: 3 for a currency like KWD."""
	return frappe.get_precision("GL Entry", "debit")


def get_unclosed_years(onboarding) -> list[dict]:
	"""The years ended before the migration date whose profit or loss is not closed into the balance
	sheet: ERPNext's Balance Sheet shows it as an unclosed year until a Period Closing Voucher does.
	Oldest first, with the years before them that have entries but no profit, like a year added for
	old invoices: ERPNext closes a year only after the one before it."""
	if not onboarding.migration_date:
		return []
	gle = frappe.qb.DocType("GL Entry")
	account = frappe.qb.DocType("Account")
	year = frappe.qb.DocType("Fiscal Year")
	rows = (
		frappe.qb.from_(gle)
		.join(account)
		.on(gle.account == account.name)
		.join(year)
		.on(gle.fiscal_year == year.name)
		.select(
			gle.fiscal_year,
			year.year_start_date,
			year.year_end_date,
			Sum(
				Case().when(account.root_type.isin(["Income", "Expense"]), gle.credit - gle.debit).else_(0)
			).as_("profit"),
		)
		.where(
			(gle.company == onboarding.company)
			& (gle.is_cancelled == 0)
			# only the entries before the migration date: the years after it are not ours to close
			& (gle.posting_date < onboarding.migration_date)
			& (year.year_end_date < onboarding.migration_date)
		)
		.groupby(gle.fiscal_year, year.year_start_date, year.year_end_date)
		.orderby(year.year_start_date)
	).run(as_dict=True)
	closed = set(
		frappe.get_all(
			"Period Closing Voucher",
			filters={"company": onboarding.company, "docstatus": 1},
			pluck="fiscal_year",
		)
	)
	rows = [row for row in rows if row.fiscal_year not in closed]
	last = max((index for index, row in enumerate(rows) if flt(row.profit, money_precision())), default=-1)
	return rows[: last + 1]


# the Company's default accounts Go live checks, and when each is needed
DEFAULT_ACCOUNTS = (
	("default_receivable_account", None),
	("default_payable_account", None),
	("default_income_account", None),
	# labelled Default Cost of Goods Sold Account: a company without stock sells nothing from it
	("default_expense_account", "items"),
	("round_off_account", None),
	("cost_center", None),
	("default_bank_account", "bank"),
	("default_inventory_account", "stock"),
	("stock_adjustment_account", "stock"),
	("stock_received_but_not_billed", "stock"),
)


def get_default_account_gaps(onboarding) -> list[dict]:
	"""The Company defaults ERPNext posts to that are missing, or wrong, like a credit card as the
	default bank, each with its label and why."""
	company = frappe.get_cached_doc("Company", onboarding.company)
	needs = {
		"bank": bool(get_bank_status(onboarding).accounts),
		"stock": bool(company.enable_perpetual_inventory) and needs_stock(onboarding),
		"items": needs_stock(onboarding),
	}
	gaps = []
	for fieldname, need in DEFAULT_ACCOUNTS:
		if need and not needs[need]:
			continue
		label = _(company.meta.get_label(fieldname))
		value = company.get(fieldname)
		if not value:
			gaps.append({"fieldname": fieldname, "label": label, "reason": _("not set")})
		elif fieldname == "default_bank_account" and (
			frappe.get_cached_value("Account", value, "root_type") != "Asset"
		):
			gaps.append(
				{
					"fieldname": fieldname,
					"label": label,
					"reason": _("{0} is not a bank you hold money in").format(value),
				}
			)
	return gaps


# the Company defaults for customers and suppliers, by the account type they take
PARTY_DEFAULTS = {"default_receivable_account": "Receivable", "default_payable_account": "Payable"}
# the names the main customers' and suppliers' ledger usually has
PARTY_LEDGER_NAMES = {
	"Receivable": ("debtors", "sundry debtors", "accounts receivable", "trade receivables"),
	"Payable": ("creditors", "sundry creditors", "accounts payable", "trade payables"),
}


def get_usual_party_ledger(company: str, account_type: str) -> str | None:
	"""The company's receivable (payable) ledger in its currency, of several the one with the usual
	name, like Sundry Debtors. None when none has it: the user picks then, never the biggest line."""
	currency = frappe.get_cached_value("Company", company, "default_currency")
	ledgers = frappe.get_all(
		"Account",
		filters={
			"company": company,
			"account_type": account_type,
			"is_group": 0,
			"disabled": 0,
			"account_currency": currency,
		},
		fields=["name", "account_name"],
		order_by="lft",
	)
	if len(ledgers) == 1:
		return ledgers[0].name
	return next(
		(
			ledger.name
			for name in PARTY_LEDGER_NAMES[account_type]
			for ledger in ledgers
			if normalize_account_name(ledger.account_name) == name
		),
		None,
	)


def get_missing_years(onboarding) -> list[str]:
	"""The years ERPNext lacks for the migration date and today: work from the migration date on
	goes in them. Go live adds them."""
	if not onboarding.migration_date:
		return []
	dates = {getdate(onboarding.migration_date), max(getdate(), getdate(onboarding.migration_date))}
	return sorted(
		{year.year for date in dates if (year := get_missing_fiscal_year(date, onboarding.company))}
	)
