# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import re
from collections import Counter

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate

from erpnext.setup.doctype.company_setup.chart_sheet import (
	download_sheet,
	find_column,
	find_heading,
	get_amount,
	get_cell,
	get_column_key,
	is_total_row,
	read_sheet,
)
from erpnext.setup.doctype.company_setup.opening_invoices import (
	get_day_first,
	get_exact_key,
	get_sheet_date,
	get_similar_key,
	get_singular_key,
	normalize_account_name,
)

JOB_ID = "company_setup_assets::{0}"
UNDO_JOB_ID = "company_setup_undo::{0}::assets"
METHODS = ("Straight Line", "Written Down Value")
FIXED_ASSET_GROUP = re.compile(r"\bfixed assets?\b")
# what an asset list may call each column, the likeliest first
ASSET_COLUMNS = {
	"asset_name": ("asset_name", "asset", "particulars", "description", "name"),
	"asset_account_name": ("asset_account", "account", "ledger", "asset_group", "group", "category", "block"),
	"purchase_date": ("purchase_date", "date_of_purchase", "put_to_use", "available_for_use_date", "date"),
	"gross": (
		"cost",
		"gross_block",
		"gross",
		"original_cost",
		"purchase_cost",
		"asset_cost",
		"purchase_amount",
		"amount",
	),
	"opening_depreciation": (
		"depreciation_till_date",
		"depreciation_till",
		"accumulated_depreciation",
		"depreciation",
		"dep_till_date",
		"dep_till",
		"acc_dep",
	),
	"hand_booked": ("depreciation_since", "booked_by_hand_since"),
	"qty": ("qty", "quantity"),
	"salvage": ("salvage_value", "salvage", "residual_value"),
	"method": ("method", "depreciation_method"),
	"life": ("life", "useful_life", "life_years", "life_in_years"),
	"rate": ("rate", "rate_of_depreciation", "dep_rate", "depreciation_rate"),
}
# what a heading may end with that is not its name: a date, "as on", a unit or currency
HEADING_ENDS = re.compile(
	r"(_\d+)+$|_(as_on|as_at|upto|up_to)$|_(rs|inr|usd|in_rs|in_inr|yrs|years|in_years|pct)$"
)
SHEET_COLUMNS = (
	"Asset Name",
	"Asset Account",
	"Purchase Date",
	"Cost",
	"Depreciation till Date",
	"Qty",
	"Salvage Value",
	"Method",
	"Life",
	"Rate",
)
SHEET_FIELDS = (
	"asset_name",
	"asset_account_name",
	"purchase_date",
	"gross",
	"opening_depreciation",
	"qty",
	"salvage",
	"method",
	"life",
	"rate",
)
# a written down value asset with no life runs till its value is this share of its cost
WDV_SALVAGE = 0.05
# not translated, so a user in another language finds the same one
LOCATION = "Head Office"
NET_VALUE_ACCOUNT = "Accumulated Depreciation - {0}"
# words every accumulated depreciation line has, left out when matching one to a category
LINE_WORDS = {"acc", "accumulated", "depreciation", "dep", "provision", "for", "on", "of", "and"}


# what ERPNext's Asset Category of a category with posted assets holds
POSTED_CATEGORY_FIELDS = ("asset_category", "accumulated_account", "depreciation_account")
# the type each account of a category needs; a ledger named so is surely that type
CATEGORY_ACCOUNTS = {
	"accumulated_account": "Accumulated Depreciation",
	"depreciation_account": "Depreciation",
}


def get_drafts(onboarding) -> list:
	return [row for row in onboarding.fixed_assets if row.status != "Posted"]


# Sheets


def read_asset_sheet(file_url: str) -> list[dict]:
	"""The rows of an asset list or a CA's schedule, totals left out."""
	rows, keys = read_sheet(file_url, get_heading_key)
	start = find_heading(
		keys,
		lambda row: find_column(row, ASSET_COLUMNS["asset_name"]) is not None
		and find_column(row, ASSET_COLUMNS["gross"]) is not None,
	)
	if start is None:
		frappe.throw(
			_("The sheet needs an Asset Name column and a Cost column."), title=_("Columns not found")
		)
	columns = {field: find_column(keys[start], names) for field, names in ASSET_COLUMNS.items()}
	body = [row.cells for row in rows[start + 1 :]]
	day_first = get_day_first(get_cell(row, columns["purchase_date"]) for row in body)
	assets = []
	for row in body:
		name = cstr(get_cell(row, columns["asset_name"])).strip()
		if not name or is_total_row(row, columns["asset_name"]):
			continue
		method = cstr(get_cell(row, columns["method"])).strip().casefold()
		assets.append(
			{
				"asset_name": name,
				"asset_account_name": cstr(get_cell(row, columns["asset_account_name"])).strip() or None,
				"purchase_date": get_sheet_date(get_cell(row, columns["purchase_date"]), day_first),
				"gross": get_amount(get_cell(row, columns["gross"])),
				"opening_depreciation": get_amount(get_cell(row, columns["opening_depreciation"])),
				"hand_booked": get_amount(get_cell(row, columns["hand_booked"])),
				"qty": cint(get_amount(get_cell(row, columns["qty"]))) or 1,
				"salvage": get_amount(get_cell(row, columns["salvage"])),
				"method": "Written Down Value"
				if method in ("wdv", "written down value", "reducing balance")
				else "Straight Line"
				if method in ("slm", "sl", "straight line")
				else None,
				"life": cint(get_amount(get_cell(row, columns["life"]))),
				"rate": get_amount(get_cell(row, columns["rate"])),
			}
		)
	return assets


def get_heading_key(value) -> str:
	"""A heading's key without what it may end with: "Depreciation since 01-10-2026" is
	depreciation_since, "Cost (Rs)" is cost."""
	key = get_column_key(value)
	while (shorter := HEADING_ENDS.sub("", key)) != key:
		key = shorter
	return key


def get_fixed_asset_accounts(company: str) -> list[str]:
	"""Ledgers of type Fixed Asset, and untyped ledgers under a fixed assets group: Tally keeps its
	assets untyped. Accumulated depreciation lines are not assets."""
	accounts = frappe.get_all(
		"Account",
		filters={"company": company, "root_type": "Asset", "disabled": 0},
		fields=["name", "account_name", "account_type", "is_group", "lft", "rgt"],
		order_by="lft",
	)
	groups = [
		account
		for account in accounts
		if account.is_group
		and (
			account.account_type == "Fixed Asset"
			or FIXED_ASSET_GROUP.search(get_similar_key(account.account_name))
		)
	]
	return [
		account.name
		for account in accounts
		if not account.is_group
		and (
			account.account_type == "Fixed Asset"
			or (
				not account.account_type
				and "depreciation" not in get_similar_key(account.account_name)
				and any(group.lft < account.lft and account.rgt < group.rgt for group in groups)
			)
		)
	]


def get_account_index(company: str) -> list[dict[str, str | None]]:
	"""The company's fixed asset accounts by name and short name, by name as people mean it, and by
	that in the singular; a closer match is tried first. A key two accounts share leads to none."""
	index = [{}, {}, {}]
	accounts = get_fixed_asset_accounts(company)
	for account in frappe.get_all(
		"Account", filters={"name": ("in", accounts or [""])}, fields=["name", "account_name"]
	):
		for keys, key in (
			(index[0], get_exact_key(account.name)),
			(index[0], get_exact_key(account.account_name)),
			(index[1], normalize_account_name(account.account_name)),
			(index[2], get_singular_key(account.account_name)),
		):
			keys[key] = account.name if keys.get(key, account.name) == account.name else None
	return index


def find_asset_account(index: list[dict], name: str) -> str | None:
	"""The account a sheet names; QuickBooks writes "Computer Equipment:Original Cost" for the
	"Original Cost (Computer Equipment)" a chart import makes of it. None when two accounts fit."""
	*parents, leaf = (part.strip() for part in cstr(name).split(":"))
	names = [name, f"{leaf} ({parents[-1]})", leaf] if parents and all(parents) and leaf else [name]
	for keys, get_key in zip(index, (get_exact_key, normalize_account_name, get_singular_key), strict=True):
		for name in names:
			if get_key(name) in keys:
				return keys[get_key(name)]
	return None


def save_fixed_assets(onboarding, rows: list[dict], source: str | None = None):
	"""Replace the draft assets with these rows. Posted ones stay. Each row's account is matched to
	one of the company's fixed asset accounts, and gets a category of the same name."""
	index = get_account_index(onboarding.company)
	# a ledger picked by hand need not be typed or under a fixed assets group
	picked = set(
		frappe.get_all(
			"Account",
			filters={
				"company": onboarding.company,
				"name": ("in", [row.get("asset_account") for row in rows] or [""]),
				"root_type": "Asset",
				"is_group": 0,
			},
			pluck="name",
		)
	)
	onboarding.set("fixed_assets", [row for row in onboarding.fixed_assets if row.status == "Posted"])
	for row in rows:
		row = frappe._dict(row)
		if not cstr(row.asset_name).strip():
			continue
		account = row.asset_account if row.asset_account in picked else None
		account = account or find_asset_account(index, row.asset_account_name or row.asset_account)
		onboarding.append(
			"fixed_assets",
			{field: row.get(field) for field in SHEET_FIELDS}
			| {
				"asset_name": " ".join(cstr(row.asset_name).split()),
				"asset_account": account,
				"qty": cint(row.qty) or 1,
				"hand_booked": flt(row.hand_booked),
				"source": row.source or source,
				"status": "Draft",
			},
		)
	set_categories(onboarding)


def sort_upload(onboarding, rows: list[dict], source: str) -> frappe._dict:
	"""What a new upload does: the drafts it keeps, how many it replaces (those of the same sheet, and
	those of an asset name on it under any file name), and its rows less those posted already."""
	posted = {get_asset_key(row) for row in onboarding.fixed_assets if row.status == "Posted"}
	new = [row for row in rows if get_asset_key(row) not in posted]
	names = {get_exact_key(row["asset_name"]) for row in new}
	kept, replaced, picks = [], 0, {}
	for row in get_drafts(onboarding):
		if (row.source or "") == source or get_exact_key(row.asset_name) in names:
			replaced += 1
			picks[(get_exact_key(row.asset_name), get_exact_key(row.asset_account_name))] = row.asset_account
		else:
			kept.append(row.as_dict())
	# the account picked for a replaced row stays when the sheet still names the same one
	for row in new:
		key = (get_exact_key(row["asset_name"]), get_exact_key(row.get("asset_account_name")))
		row["asset_account"] = row.get("asset_account") or picks.get(key)
	return frappe._dict(kept=kept, replaced=replaced, rows=new, posted=len(rows) - len(new))


def get_asset_key(row) -> tuple:
	date = row.get("purchase_date")
	return (get_exact_key(row.get("asset_name")), getdate(date) if date else None, flt(row.get("gross"), 2))


def set_categories(onboarding):
	"""One category per asset account on the rows, named after the account. Method is the one most
	rows have, and life and rate those its rows agree on, until set by hand; the accounts from the
	chart when sure."""
	saved = {row.asset_account: row for row in onboarding.asset_categories}
	accounts = sorted({row.asset_account for row in onboarding.fixed_assets if row.asset_account})
	onboarding.asset_categories = []
	for account in accounts:
		row = saved.get(account)
		rows = [asset for asset in onboarding.fixed_assets if asset.asset_account == account]
		counts = Counter(asset.method for asset in rows if asset.method).most_common(2)
		method = counts[0][0] if counts and (len(counts) == 1 or counts[0][1] > counts[1][1]) else None
		rows = [asset for asset in rows if not asset.method or asset.method == method]
		lives = {cint(asset.life) for asset in rows if asset.life}
		rates = {flt(asset.rate) for asset in rows if asset.rate}
		name = frappe.get_cached_value("Account", account, "account_name")
		set_up = row and row.method
		onboarding.append(
			"asset_categories",
			{
				"asset_account": account,
				"asset_category": row.asset_category if row else name,
				"method": row.method if set_up else method,
				"life": row.life if set_up else (next(iter(lives)) if len(lives) == 1 else 0),
				"rate": row.rate if set_up else (next(iter(rates)) if len(rates) == 1 else 0),
				"accumulated_account": (row and row.accumulated_account)
				or guess_accumulated_account(onboarding.company, name, account),
				"depreciation_account": (row and row.depreciation_account)
				or guess_depreciation_account(onboarding.company),
				"hsn_code": row and row.hsn_code,
			},
		)


def save_categories(onboarding, rows: list[dict]):
	"""Depreciation by category: change method, life, rate and accounts for all of a category's rows."""
	by_account = {row.asset_account: row for row in onboarding.asset_categories}
	before = {row.asset_account: get_depreciation(row) for row in onboarding.asset_categories}
	posted = get_posted_accounts(onboarding)
	for change in rows:
		change = frappe._dict(change)
		row = by_account.get(change.asset_account)
		if not row:
			continue
		if change.method and change.method not in METHODS:
			frappe.throw(_("Pick Straight Line or Written Down Value."), title=_("Unknown method"))
		if change.get("rate") and not 0 < flt(change.rate) < 100:
			frappe.throw(_("The rate is a percent between 0 and 100."), title=_("Rate out of range"))
		if row.asset_account in posted and any(
			field in change and (change.get(field) or None) != (row.get(field) or None)
			for field in POSTED_CATEGORY_FIELDS
		):
			frappe.throw(
				_(
					"{0} has posted assets, so its name and accounts can't change. Undo its assets first."
				).format(row.asset_category),
				title=_("Category in use"),
			)
		for field in (
			"asset_category",
			"method",
			"life",
			"rate",
			"accumulated_account",
			"depreciation_account",
			"hsn_code",
		):
			if field in change:
				row.set(field, change.get(field))
	for asset in get_drafts(onboarding):
		category = by_account.get(asset.asset_account)
		# a row with a life or rate of its own keeps it, unless it only had the category's
		if (
			category
			and category.method
			and (not (asset.life or asset.rate) or get_depreciation(asset) == before[asset.asset_account])
		):
			asset.method, asset.life, asset.rate = category.method, category.life, category.rate


def get_depreciation(row) -> tuple:
	return (row.method or None, cint(row.life), flt(row.rate))


def get_posted_accounts(onboarding) -> set[str]:
	return {row.asset_account for row in onboarding.fixed_assets if row.status == "Posted"}


def clear_wrong_lines(onboarding):
	"""Guess again the accumulated depreciation line of a category with nothing posted when it is
	another category's own line (see get_own_lines): an old guess."""
	if not onboarding.asset_categories:
		return
	own_lines = get_own_lines(onboarding)
	posted = get_posted_accounts(onboarding)
	for row in onboarding.asset_categories:
		line = row.accumulated_account
		if (
			line
			and row.asset_account not in posted
			and own_lines.get(line, row.asset_account) != row.asset_account
		):
			name = frappe.get_cached_value("Account", row.asset_account, "account_name")
			row.accumulated_account = guess_accumulated_account(onboarding.company, name, row.asset_account)


def keep_posted_categories(onboarding):
	"""A form save keeps the name and accounts of a category with posted assets, as its ERPNext
	Asset Category has them."""
	posted = get_posted_accounts(onboarding)
	if not posted:
		return
	stored = {
		row.asset_account: row
		for row in frappe.get_all(
			"Company Setup Asset Category",
			filters={
				"parent": onboarding.name,
				"parenttype": onboarding.doctype,
				"parentfield": "asset_categories",
			},
			fields=["asset_account", *POSTED_CATEGORY_FIELDS],
		)
	}
	for row in onboarding.asset_categories:
		if row.asset_account in posted and row.asset_account in stored:
			row.update({field: stored[row.asset_account][field] for field in POSTED_CATEGORY_FIELDS})


def guess_accumulated_account(company: str, category: str, asset_account: str) -> str | None:
	"""The category's own accumulated depreciation line (see find_own_line), else the company's one,
	else the only one."""
	lines = get_depreciation_lines(company)
	typed = [row.name for row in lines if row.account_type]
	return (
		find_own_line(lines, category, asset_account)
		or frappe.get_cached_value("Company", company, "accumulated_depreciation_account")
		or (typed[0] if len(typed) == 1 else None)
		or (None if typed else get_named_ledger(company, "accumulated_account"))
	)


def get_depreciation_lines(company: str) -> list[frappe._dict]:
	"""Accumulated depreciation ledgers, and untyped asset ledgers named for depreciation, as Busy and
	Tally charts keep them."""
	return [
		account
		for account in frappe.get_all(
			"Account",
			filters={"company": company, "root_type": "Asset", "is_group": 0, "disabled": 0},
			fields=["name", "account_name", "account_type", "parent_account"],
		)
		if account.account_type == "Accumulated Depreciation"
		or (not account.account_type and "depreciation" in get_similar_key(account.account_name))
	]


def find_own_line(lines: list, category: str, asset_account: str) -> str | None:
	"""The one line named for the category, else the one next to its asset line, else the one
	sharing the most words with the category, as "Acc. Depreciation - Vehicles" for Motor Vehicles."""
	parent = frappe.get_cached_value("Account", asset_account, "parent_account")
	for matches in (
		[row for row in lines if get_similar_key(category) in get_similar_key(row.account_name)],
		[row for row in lines if row.parent_account == parent],
	):
		if len(matches) == 1:
			return matches[0].name
	words = set(get_similar_key(category).split()) - LINE_WORDS
	shared = {row.name: len(words & set(get_similar_key(row.account_name).split())) for row in lines}
	most = max(shared.values(), default=0)
	closest = [name for name, count in shared.items() if count == most]
	return closest[0] if most and len(closest) == 1 else None


def get_own_lines(onboarding) -> dict[str, str]:
	"""The lines that belong to one category, by its asset account: its asset line, and its own
	accumulated depreciation line when no other category's is the same."""
	lines = get_depreciation_lines(onboarding.company)
	owners = {}
	for row in onboarding.asset_categories:
		line = row.asset_account and find_own_line(lines, row.asset_category, row.asset_account)
		if line:
			owners.setdefault(line, set()).add(row.asset_account)
	own = {line: next(iter(accounts)) for line, accounts in owners.items() if len(accounts) == 1}
	return own | {
		row.asset_account: row.asset_account for row in onboarding.asset_categories if row.asset_account
	}


def guess_depreciation_account(company: str) -> str | None:
	default = frappe.get_cached_value("Company", company, "depreciation_expense_account")
	if default:
		return default
	accounts = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": "Depreciation", "is_group": 0, "disabled": 0},
		pluck="name",
	)
	if accounts:
		return accounts[0] if len(accounts) == 1 else None
	return get_named_ledger(company, "depreciation_account")


def get_named_ledger(company: str, field: str) -> str | None:
	"""The untyped ledger named exactly for this account of a category, as a Tally chart has it."""
	return frappe.db.get_value(
		"Account",
		{
			"company": company,
			"account_name": CATEGORY_ACCOUNTS[field],
			"account_type": ("is", "not set"),
			"is_group": 0,
			"disabled": 0,
		},
	)


def get_untyped_accounts(onboarding, rows: list) -> dict[str, str]:
	"""The untyped accounts these rows need, with the type each one takes: their asset accounts,
	and their categories' depreciation accounts."""
	accounts = {row.asset_account: "Fixed Asset" for row in rows if row.asset_account}
	used = set(accounts)
	for category in onboarding.asset_categories:
		if category.asset_account not in used:
			continue
		for field, account_type in CATEGORY_ACCOUNTS.items():
			if category.get(field):
				accounts.setdefault(category.get(field), account_type)
	return {
		account: account_type
		for account, account_type in accounts.items()
		if not frappe.get_cached_value("Account", account, "account_type")
	}


def get_wrong_type_accounts(drafts: list, categories: list, net_value: dict) -> list[str]:
	"""Accounts picked for the drafts whose type is not the one the Asset Category takes; untyped
	ones are marked instead."""
	needs = {row.asset_account: "Fixed Asset" for row in drafts if row.asset_account}
	for category in categories:
		for field, account_type in CATEGORY_ACCOUNTS.items():
			if category.get(field) and not (
				field == "accumulated_account" and category.asset_account in net_value
			):
				needs.setdefault(category.get(field), account_type)
	return sorted(
		account
		for account, account_type in needs.items()
		if frappe.get_cached_value("Account", account, "account_type") not in ("", None, account_type)
	)


def mark_asset_accounts(onboarding) -> list[str]:
	"""Give the untyped accounts the drafts need their type. Returns them."""
	marked = []
	for account, account_type in sorted(get_untyped_accounts(onboarding, get_drafts(onboarding)).items()):
		doc = frappe.get_doc("Account", account)
		doc.account_type = account_type
		doc.save()
		marked.append(account)
	return marked


@frappe.whitelist()
def download_fixed_assets(company: str, undone_from: str | None = None):
	"""The asset list with the drafts, and a column for the depreciation booked by hand since the
	migration date. With undone_from, the rows undone from that source."""
	onboarding = frappe.get_doc("Company Setup", company)
	onboarding.check_permission("read")
	rows = (
		[row for row in onboarding.undone_assets if (row.source or "") == undone_from]
		if undone_from is not None
		else get_drafts(onboarding)
	)
	since = _("Depreciation since {0}").format(
		frappe.format(onboarding.migration_date, {"fieldtype": "Date"})
	)
	download_sheet(
		f"{frappe.scrub(company)}_fixed_assets",
		[
			[*SHEET_COLUMNS, since],
			*(
				[
					row.get(field) or (row.asset_account if field == "asset_account_name" else None)
					for field in SHEET_FIELDS
				]
				+ [row.hand_booked]
				for row in rows
			),
		],
		_("Fixed Assets"),
	)
