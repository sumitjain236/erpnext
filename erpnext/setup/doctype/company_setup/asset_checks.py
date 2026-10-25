# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.permissions import get_doctype_roles
from frappe.utils import add_days, cint, flt, getdate, nowdate

from erpnext.accounts.doctype.account.account import get_account_autoname
from erpnext.setup.doctype.company_setup.asset_plan import (
	get_hand_checks,
	get_plan,
	get_wdv_gap,
	prepare_assets,
)
from erpnext.setup.doctype.company_setup.asset_sheet import (
	LOCATION,
	NET_VALUE_ACCOUNT,
	get_drafts,
	get_untyped_accounts,
	get_wrong_type_accounts,
)
from erpnext.setup.doctype.company_setup.bank_accounts import get_balances
from erpnext.setup.doctype.company_setup.opening_invoices import (
	get_closed_periods,
	get_error_text,
	get_fiscal_year_red,
	get_posting_blocker,
	get_posting_fix,
)

# the Item field of a goods or services code, as India Compliance adds; asked per category when there
HSN_FIELD = "gst_hsn_code"


# Checks


def get_net_value_categories(onboarding, prepared: list, plan: frappe._dict) -> dict[str, float]:
	"""Asset accounts kept at net value, with what of these rows ends on their own accumulated
	depreciation line: their depreciation and what was booked by hand. An account with posted rows
	is at net value when those moved; else when its line on the day before is the rows' cost less
	their depreciation, not their cost."""
	accounts = {asset.row.asset_account for asset in prepared if asset.row.asset_account}
	lines = get_line_balances(onboarding, list(accounts), plan.day_before)
	moves = {}
	for account in accounts:
		rows = [asset.row for asset in prepared if asset.row.asset_account == account]
		posted = [
			row for row in onboarding.fixed_assets if row.status == "Posted" and row.asset_account == account
		]
		cost = sum(flt(row.gross) for row in rows)
		depreciation = sum(flt(row.opening_depreciation) for row in rows)
		if (
			any(flt(row.moved) for row in posted)
			if posted
			else depreciation and abs(flt(lines.get(account)) - (cost - depreciation)) < 1
		):
			moves[account] = flt(depreciation + sum(flt(row.hand_booked) for row in rows), 2)
	return moves


def get_closed_to(company: str):
	"""The end of the last year closed: entries on or before it can't be posted any more."""
	return frappe.db.get_value(
		"Period Closing Voucher", {"company": company, "docstatus": 1}, [{"MAX": "period_end_date"}]
	)


def get_closed_until(company: str) -> frappe._dict:
	"""The latest of: the end of the company's last closed year, of its last Accounting Period closed
	for journal entries (unless the user has its exempted role), and the accounts frozen date (unless
	the user may post into it, as ERPNext checks). With the period's name, or frozen, when that one
	is the latest."""
	roles = frappe.get_roles()
	year_end = get_closed_to(company)
	ends = [frappe._dict(date=getdate(year_end))] if year_end else []
	for row in get_closed_periods(company, ("Journal Entry",)):
		ends.append(frappe._dict(date=getdate(row.end_date), period=row.name))
	frozen_till, role = frappe.db.get_value(
		"Company", company, ["accounts_frozen_till_date", "role_allowed_for_frozen_entries"]
	)
	if frozen_till and (role not in roles or frappe.session.user == "Administrator"):
		ends.append(frappe._dict(date=getdate(frozen_till), frozen=True))
	latest = max(ends, key=lambda end: end.date, default=frappe._dict(date=None))
	return frappe._dict(date=latest.date, period=latest.period, frozen=bool(latest.frozen))


def get_asset_checks(onboarding, check_items: bool = True) -> dict:
	"""What the Fixed assets window shows: the sheet, the books today, the reds that stop posting,
	the yellows and greys to show, and what the confirm says. check_items: validate the new
	categories' items (bad_hsn), which runs Item.validate with every app's hooks."""
	plan = get_plan(onboarding)
	drafts = get_drafts(onboarding)
	prepared = prepare_assets(onboarding, drafts, plan)
	categories = [
		row for row in onboarding.asset_categories if row.asset_account in {r.asset_account for r in drafts}
	]
	hand = get_hand_checks(onboarding, prepared, plan)
	closed = get_closed_until(onboarding.company)
	net_value = get_net_value_categories(onboarding, prepared, plan)
	net_value_date = get_net_value_date(onboarding, plan.hand)
	new_items = [row for row in categories if not get_category_item(row.asset_category)]
	hsn_label = get_hsn_label()
	unanswered = plan.mid_year and not onboarding.depreciation_booked
	year_red = get_fiscal_year_red(onboarding.company, onboarding.migration_date)
	untyped = get_untyped_accounts(onboarding, drafts)
	reds = frappe._dict(
		posting=year_red["text"]
		if year_red
		else get_posting_blocker(onboarding.company, net_value_date, opening=False)
		if any(net_value.values()) or plan.hand.amount
		else None,
		permission=get_permission_blocker(categories),
		no_account=[row.idx for row in drafts if not row.asset_account],
		untyped=sorted(untyped),
		wrong_type=get_wrong_type_accounts(drafts, categories, net_value),
		categories=[
			asset.row.idx
			for asset in prepared
			if not asset.method
			or (asset.method == "Straight Line" and not asset.life)
			or (asset.method == "Written Down Value" and not asset.rate)
		],
		rate=[
			asset.row.idx
			for asset in prepared
			if asset.method == "Written Down Value" and asset.rate and not 0 < asset.rate < 100
		],
		hsn_code=[row.asset_category for row in new_items if hsn_label and not row.hsn_code],
		bad_hsn={
			row.asset_category: error
			for row in new_items
			if check_items
			and hsn_label
			and row.hsn_code
			and (error := get_asset_item_error(row.asset_category, row.hsn_code))
		},
		accounts=[
			row.asset_category
			for row in categories
			if not row.depreciation_account
			or (not row.accumulated_account and row.asset_account not in net_value)
		],
		no_date=[row.idx for row in drafts if not row.purchase_date],
		after_migration=[
			row.idx
			for row in drafts
			if row.purchase_date and getdate(row.purchase_date) >= getdate(onboarding.migration_date)
		],
		over_cost=[
			asset.row.idx
			for asset in prepared
			if asset.depreciation > flt(asset.row.gross) - flt(asset.row.salvage) + 0.01
		],
		# ERPNext refuses depreciation till date with no period booked
		# what was booked this year is known only from the answer
		none_booked=[]
		if unanswered
		else [
			asset.row.idx for asset in prepared if asset.purchase and asset.depreciation and not asset.booked
		],
		life_over=[] if unanswered else [asset.row.idx for asset in prepared if asset.life_over],
		hand=hand.off,
		overcounted=hand.overcounted,
		answer=unanswered,
		booked_this_year=get_booked_this_year(onboarding, categories)
		if plan.mid_year and onboarding.depreciation_booked == "Only at year end"
		else 0,
		closed=closed if closed.date and closed.date >= plan.start else None,
	)
	ending = [
		asset
		for asset in prepared
		if asset.total and not asset.fully_depreciated and not asset.life_over and asset.periods_left <= 1
	]
	yellows = frappe._dict(
		no_depreciation=[
			asset.row.idx
			for asset in prepared
			if not asset.depreciation
			and asset.purchase
			and asset.purchase < plan.year.start
			and (asset.life or asset.rate)
		],
		ending=[asset.row.idx for asset in ending],
		ending_amount=flt(sum(asset.value - flt(asset.row.salvage) for asset in ending), 2),
		year_end_with_hand=bool(
			plan.mid_year and plan.hand.amount and onboarding.depreciation_booked == "Only at year end"
		),
		defaults=[
			label
			for field, label in (
				("capital_work_in_progress_account", _("Capital Work in Progress")),
				("disposal_account", _("Gain/Loss on Asset Disposal")),
			)
			if not frappe.get_cached_value("Company", onboarding.company, field)
		],
	)
	greys = frappe._dict(
		life_from_rate=sum(
			1 for asset in prepared if asset.method == "Written Down Value" and not asset.life and asset.rate
		),
		existing_categories=get_changed_categories(onboarding.company, categories),
	)
	asset_accounts = sorted({row.asset_account for row in onboarding.fixed_assets if row.asset_account})
	accumulated = sorted(
		{row.accumulated_account for row in onboarding.asset_categories if row.accumulated_account}
	)
	# a migration date still to come has the opening balances dated then
	books = get_line_balances(
		onboarding, asset_accounts + accumulated, max(getdate(nowdate()), plan.day_before)
	)
	return {
		"day_before": plan.day_before,
		"sheet": {
			"assets": len(drafts),
			"cost": flt(sum(flt(row.gross) for row in drafts), 2),
			"depreciation": flt(sum(flt(row.opening_depreciation) for row in drafts), 2),
			"categories": len(categories),
		},
		"books": {
			"fixed_assets": flt(sum(flt(books.get(account)) for account in asset_accounts), 2),
			"depreciation": -flt(sum(flt(books.get(account)) for account in accumulated), 2),
		},
		"hand": {
			"amount": hand.amount,
			"schedule": hand.schedule,
			"counted": hand.counted,
			"until": plan.booked_until if plan.hand.amount else None,
		},
		"hand_uncounted": hand.uncounted,
		"mid_year": plan.mid_year,
		"year_start": plan.year.start,
		"monthly": plan.monthly,
		"year_missing": plan.year.missing,
		"reds": get_asset_reds(reds, len(drafts), hand, hsn_label),
		"posting_fix": year_red["fix"]
		if year_red
		else reds.posting and get_posting_fix(onboarding.company, net_value_date, opening=False),
		"yellows": yellows,
		"greys": greys,
		"net_value": net_value,
		"net_value_accounts": {
			row.asset_account: get_net_value_account(onboarding.company, row)
			or get_account_autoname(None, NET_VALUE_ACCOUNT.format(row.asset_category), onboarding.company)
			for row in categories
			if row.asset_account in net_value
		},
		"net_value_date": net_value_date,
		"hsn_label": hsn_label,
		"wdv_gap": get_wdv_gap(plan, prepared),
		"fully_depreciated": sum(1 for asset in prepared if asset.fully_depreciated),
		"first_depreciation": {
			"date": plan.start,
			"amount": flt(sum(asset.first for asset in prepared if not asset.fully_depreciated), 2),
		},
		"auto_depreciation": cint(
			frappe.db.get_single_value("Accounts Settings", "book_asset_depreciation_entry_automatically")
		),
		"untyped_types": untyped,
		"off": get_category_off(onboarding, drafts, net_value, plan),
		"depreciation_off": get_depreciation_off(onboarding, categories, net_value, plan),
	}


def get_permission_blocker(categories: list) -> str | None:
	"""Why this user can't make what the post makes, naming a role that can."""
	needs = [("Asset", "create"), ("Asset", "submit")]
	for category in categories:
		exists = frappe.db.exists("Asset Category", category.asset_category)
		needs.append(("Asset Category", "write" if exists else "create"))
		if not exists or not get_category_item(category.asset_category):
			needs.append(("Item", "create"))
	if not frappe.db.exists("Location", LOCATION):
		needs.append(("Location", "create"))
	for doctype, ptype in dict.fromkeys(needs):
		if not frappe.has_permission(doctype, ptype):
			roles = [role for role in get_doctype_roles(doctype, ptype) if role != "System Manager"]
			return _(
				"Posting makes {0} records, which needs the {1} role. Ask an Administrator to add it in User."
			).format(_(doctype), _(roles[0] if roles else "System Manager"))
	return None


def get_changed_categories(company: str, categories: list) -> list[str]:
	"""Asset Categories made before whose accounts for this company the post sets to the step's."""
	changed = []
	for category in categories:
		row = frappe.db.get_value(
			"Asset Category Account",
			{"parent": category.asset_category, "parenttype": "Asset Category", "company_name": company},
			["fixed_asset_account", "accumulated_depreciation_account", "depreciation_expense_account"],
			as_dict=True,
		)
		if row and (
			row.fixed_asset_account != category.asset_account
			or row.depreciation_expense_account != category.depreciation_account
			or (
				category.accumulated_account
				and row.accumulated_depreciation_account != category.accumulated_account
			)
		):
			changed.append(category.asset_category)
	return changed


def get_line_balances(onboarding, accounts: list[str], date) -> dict[str, float]:
	"""The accounts' balances on the date, with their trial balance lines not posted yet: until the
	balances are posted, the books are as they will be."""
	balances = get_balances(onboarding.company, accounts, date)
	for row in onboarding.opening_balances:
		if row.status != "Posted" and row.account in accounts:
			balances[row.account] = flt(balances.get(row.account)) + flt(row.debit) - flt(row.credit)
	return balances


def get_category_off(onboarding, rows: list, net_value: dict, plan: frappe._dict) -> dict[str, float]:
	"""By asset account, how far the rows' cost is from its line on the day before, when not at net value.
	Nothing while a row has no account: its cost is on one of the lines."""
	if any(not row.asset_account for row in rows):
		return {}
	accounts = sorted({row.asset_account for row in rows} - set(net_value))
	lines = get_line_balances(onboarding, accounts, plan.day_before)
	off = {}
	for account in accounts:
		cost = sum(flt(row.gross) for row in onboarding.fixed_assets if row.asset_account == account)
		if abs(flt(lines.get(account)) - cost) >= 1:
			off[account] = flt(flt(lines.get(account)) - cost, 2)
	return off


def get_depreciation_off(
	onboarding, categories: list, net_value: dict, plan: frappe._dict
) -> dict[str, float]:
	"""By accumulated depreciation line of these categories, how far the depreciation of all rows on it
	is from the line on the day before. Net-value categories are checked by their cost."""
	lines = {}
	for category in onboarding.asset_categories:
		if category.accumulated_account and category.asset_account not in net_value:
			lines.setdefault(category.accumulated_account, set()).add(category.asset_account)
	lines = {
		line: accounts
		for line, accounts in lines.items()
		if any(category.accumulated_account == line for category in categories)
	}
	balances = get_line_balances(onboarding, list(lines), plan.day_before)
	off = {}
	for line, accounts in lines.items():
		depreciation = sum(
			flt(row.opening_depreciation) for row in onboarding.fixed_assets if row.asset_account in accounts
		)
		difference = flt(-flt(balances.get(line)) - depreciation, 2)
		if abs(difference) >= 1:
			off[line] = difference
	return off


def get_booked_this_year(onboarding, categories: list) -> float:
	"""Depreciation in the trial balance for this year before the migration date: the old books
	booked it during the year."""
	accounts = {row.depreciation_account for row in categories if row.depreciation_account}
	return flt(
		sum(
			flt(row.debit) - flt(row.credit)
			for row in onboarding.opening_balances
			if row.account in accounts or row.account_type == "Depreciation"
		),
		2,
	)


def get_asset_reds(
	reds: frappe._dict, assets: int, hand: frappe._dict, hsn_label: str | None = None
) -> list[dict]:
	"""What stops posting, in the order to fix it: key, the sentence to show, and the rows, accounts
	or categories it is about."""
	texts = (
		("empty", not assets, _("Upload a sheet or add rows to post.")),
		("posting", reds.posting, reds.posting),
		("permission", reds.permission, reds.permission),
		("answer", reds.answer, _("Answer the question to post.")),
		(
			"booked_this_year",
			reds.booked_this_year > 0,
			_(
				"Your trial balance has {0} of depreciation this year, so your old books booked it during the year. Answer Every month."
			).format(frappe.format(reds.booked_this_year, {"fieldtype": "Currency"})),
		),
		(
			"no_account",
			reds.no_account,
			(
				_("Pick the asset account of 1 row."),
				_("Pick the asset account of {0} rows."),
			),
		),
		(
			"untyped",
			reds.untyped,
			(
				_("Mark the type of 1 account first."),
				_("Mark the type of {0} accounts first."),
			),
		),
		(
			"wrong_type",
			reds.wrong_type,
			(
				_("1 account picked is of another type. Pick another account."),
				_("{0} accounts picked are of another type. Pick other accounts."),
			),
		),
		(
			"categories",
			reds.categories,
			(
				_("Depreciation is not set for 1 row. Set it up by category."),
				_("Depreciation is not set for {0} rows. Set it up by category."),
			),
		),
		(
			"rate",
			reds.rate,
			(
				_("The rate of 1 row is not a percent between 0 and 100."),
				_("The rate of {0} rows is not a percent between 0 and 100."),
			),
		),
		(
			"hsn_code",
			reds.hsn_code,
			_("Give the {0} code of 1 category.").format(hsn_label)
			if len(reds.hsn_code or []) == 1
			else _("Give the {0} codes of {1} categories.").format(hsn_label, len(reds.hsn_code or [])),
		),
		(
			"bad_hsn",
			reds.bad_hsn,
			"; ".join(f"{category}: {error}" for category, error in (reds.bad_hsn or {}).items()),
		),
		(
			"accounts",
			reds.accounts,
			(
				_("Pick the depreciation accounts of 1 category."),
				_("Pick the depreciation accounts of {0} categories."),
			),
		),
		(
			"no_date",
			reds.no_date,
			(
				_("Purchase date is missing for 1 asset."),
				_("Purchase date is missing for {0} assets."),
			),
		),
		(
			"after_migration",
			reds.after_migration,
			(
				_("Purchase date is after the migration date for 1 asset."),
				_("Purchase date is after the migration date for {0} assets."),
			),
		),
		(
			"over_cost",
			reds.over_cost,
			(
				_("Depreciation is more than cost for 1 asset."),
				_("Depreciation is more than cost for {0} assets."),
			),
		),
		(
			"none_booked",
			reds.none_booked,
			(
				_("1 asset bought this year has depreciation, but your old books booked none this year."),
				_("{0} assets bought this year have depreciation, but your old books booked none this year."),
			),
		),
		(
			"life_over",
			reds.life_over,
			(
				_("Life is over for 1 asset but it still has value. Raise the life or the depreciation."),
				_(
					"Life is over for {0} assets but they still have value. Raise the life or the depreciation."
				),
			),
		),
		(
			"overcounted",
			reds.overcounted,
			(
				_(
					"1 posted asset counts more depreciation booked by hand than your books have now. Undo it and post it again."
				),
				_(
					"{0} posted assets count more depreciation booked by hand than your books have now. Undo them and post them again."
				),
			),
		),
		(
			"hand",
			reds.hand,
			_(
				"Booked by hand {0}, the schedule gives {1}. Spread or download the hand-booked depreciation to post."
			).format(
				frappe.format(hand.amount, {"fieldtype": "Currency"}),
				frappe.format(hand.schedule, {"fieldtype": "Currency"}),
			),
		),
		(
			"closed",
			reds.closed,
			_(
				"Accounting Period {0} is closed for Journal Entry, but no depreciation was booked in it for these assets. Book it by hand first, or open the period."
			).format(reds.closed.period)
			if reds.closed and reds.closed.period
			else _(
				"Accounts are frozen up to {0}, but no depreciation was booked till then for these assets. Book it by hand first, or change Accounts Frozen Till Date on the company."
			).format(frappe.format(reds.closed.date, {"fieldtype": "Date"}))
			if reds.closed and reds.closed.frozen
			else _(
				"The year is closed, but no depreciation was booked in it for these assets. Book it by hand first, or cancel the closing voucher."
			),
		),
	)
	return [
		{
			"key": key,
			"text": (text[0] if len(value) == 1 else text[1].format(len(value)))
			if isinstance(text, tuple)
			else text,
			"rows": value if isinstance(value, list) else [],
		}
		for key, value, text in texts
		if value
	]


def get_asset_blocker(checks: dict) -> str | None:
	"""Why the drafts can't be posted, or None: the first red."""
	return checks["reds"][0]["text"] if checks["reds"] else None


def get_net_value_date(onboarding, hand: frappe._dict):
	"""The migration date, or the day of the last hand entry it moves from, or the day after a later
	closed year."""
	closed = get_closed_until(onboarding.company).date
	date = max(getdate(onboarding.migration_date), getdate(hand.last or onboarding.migration_date))
	return max(date, add_days(closed, 1)) if closed else date


def get_net_value_account(company: str, category) -> str | None:
	"""The ledger a net-value category's depreciation moves to, when made before: found by name or on
	the category's ERPNext accounts."""
	return frappe.db.get_value(
		"Account",
		{
			"company": company,
			"account_name": NET_VALUE_ACCOUNT.format(category.asset_category),
			"is_group": 0,
		},
	) or frappe.db.get_value(
		"Asset Category Account",
		{
			"parent": category.asset_category,
			"parenttype": "Asset Category",
			"company_name": company,
			"fixed_asset_account": category.asset_account,
		},
		"accumulated_depreciation_account",
	)


def get_category_item(category: str) -> str | None:
	return frappe.db.get_value(
		"Item", {"is_fixed_asset": 1, "asset_category": category, "disabled": 0}, order_by="creation"
	)


def get_asset_item(category: str, hsn_code: str | None = None) -> str:
	"""The category's first fixed asset item, else a new one named "<category> - Asset"."""
	return get_category_item(category) or new_asset_item(category, hsn_code).insert().name


def get_asset_item_error(category: str, hsn_code: str) -> str | None:
	"""Why the category's new item can't be made with this code, by the rules of the app that adds
	the code, like its length or its master: checked as the item is saved, without saving it."""
	doc = new_asset_item(category, hsn_code)
	muted = frappe.flags.mute_messages
	frappe.flags.mute_messages = True
	try:
		doc.run_method("validate")
	except frappe.ValidationError as e:
		return get_error_text(e)
	finally:
		frappe.flags.mute_messages = muted
	master = frappe.get_meta("Item").get_field(HSN_FIELD).options
	if master and not frappe.db.exists(master, hsn_code):
		return _("Could not find {0}: {1}").format(_(master), hsn_code)
	return None


def new_asset_item(category: str, hsn_code: str | None = None):
	doc = frappe.new_doc("Item")
	doc.update(
		{
			"item_code": f"{category} - Asset",
			"item_group": get_asset_item_group() or doc.item_group or "All Item Groups",
			"is_stock_item": 0,
			"is_fixed_asset": 1,
			"asset_category": category,
		}
	)
	if get_hsn_label():
		doc.set(HSN_FIELD, hsn_code)
	return doc


def get_hsn_label() -> str | None:
	"""The label of the Item's goods or services code, when an app adds one."""
	meta = frappe.get_meta("Item")
	return _(meta.get_label(HSN_FIELD)) if meta.has_field(HSN_FIELD) else None


def get_asset_item_group() -> str | None:
	"""A group named for fixed asset items, when there is one."""
	return frappe.db.get_value("Item Group", {"is_group": 0, "item_group_name": ("like", "%asset%")})
