# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Fixed assets: the asset register behind the asset lines the trial balance already posted.

Each row becomes an Existing Asset with its depreciation till the day before migration, so ERPNext
never books those periods again; its first depreciation is the first period after. Depreciation
booked by hand since the migration date counts as booked too. Nothing moves in the books, but for a
category kept at net value (depreciation credited to the asset line, as Tally books often do): one
entry moves its depreciation to its own accumulated depreciation line.

Unlike the other steps it is checked as on today, and stays open after Go live.
"""

import math
import re
from collections import Counter
from itertools import groupby

import frappe
from frappe import _
from frappe.permissions import get_doctype_roles
from frappe.query_builder.functions import Sum
from frappe.utils import (
	add_days,
	add_months,
	add_years,
	cint,
	cstr,
	date_diff,
	flt,
	get_last_day,
	getdate,
	is_last_day_of_the_month,
	month_diff,
	nowdate,
)
from frappe.utils.background_jobs import is_job_enqueued

from erpnext.accounts.doctype.account.account import get_account_autoname
from erpnext.accounts.utils import get_fiscal_year
from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	get_asset_depr_schedule_doc,
)
from erpnext.setup.doctype.company_setup.bank_accounts import get_balances
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
	add_account,
	append_lines,
	ensure_fiscal_year,
	get_cached,
	get_closed_periods,
	get_day_before,
	get_day_first,
	get_default_dimensions,
	get_error_text,
	get_exact_key,
	get_fiscal_year_red,
	get_missing_fiscal_year,
	get_posting_blocker,
	get_posting_fix,
	get_sheet_date,
	get_similar_key,
	get_singular_key,
	normalize_account_name,
)
from erpnext.setup.doctype.company_setup.posted_entries import (
	cancel_doc,
	pop_kept_reasons,
	run_posting_job,
	run_undo_job,
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
# the Item field of a goods or services code, as India Compliance adds; asked per category when there
HSN_FIELD = "gst_hsn_code"
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


# The schedule


def get_year(onboarding) -> frappe._dict:
	"""The financial year the migration date is in, missing when ERPNext doesn't have it yet: the
	post makes it."""
	missing = get_missing_fiscal_year(onboarding.migration_date, onboarding.company)
	year = missing or get_fiscal_year(onboarding.migration_date, company=onboarding.company, as_dict=True)
	return frappe._dict(
		start=getdate(year.year_start_date), end=getdate(year.year_end_date), missing=bool(missing)
	)


def get_hand_booked(onboarding, after=None, until=None) -> frappe._dict:
	"""Depreciation booked by hand on or after the migration date: journal entries to the
	categories' depreciation accounts with no asset on them. By part (see get_hand_part), the
	lines they credited and how much. With after or until, only those made after or up to it."""
	accounts = {row.depreciation_account for row in onboarding.asset_categories if row.depreciation_account}
	if not accounts:
		return frappe._dict(amount=0, last=None, entries=[], credits={}, sources={})
	je = frappe.qb.DocType("Journal Entry")
	line = frappe.qb.DocType("Journal Entry Account")
	booked = frappe.qb.DocType("Journal Entry Account").as_("booked")
	query = (
		frappe.qb.from_(je)
		.inner_join(line)
		.on(line.parent == je.name)
		.select(je.name, je.posting_date, line.account, line.debit, line.credit)
		.where(
			(je.company == onboarding.company)
			& (je.docstatus == 1)
			& (je.posting_date >= onboarding.migration_date)
			& ((line.reference_type.isnull()) | (line.reference_type != "Asset"))
			& je.name.isin(
				frappe.qb.from_(booked)
				.select(booked.parent)
				.where(
					booked.account.isin(list(accounts))
					& ((booked.reference_type.isnull()) | (booked.reference_type != "Asset"))
				)
			)
		)
	)
	if after:
		query = query.where(je.creation > after)
	if until:
		query = query.where(je.creation <= until)
	rows = query.run(as_dict=True)
	own_lines = get_own_lines(onboarding)
	sources = {}
	for _name, lines in groupby(sorted(rows, key=lambda row: row.name), key=lambda row: row.name):
		lines = list(lines)
		debits = {}
		for row in lines:
			if row.account in accounts:
				debits[row.account] = flt(debits.get(row.account)) + flt(row.debit) - flt(row.credit)
		total = sum(debits.values())
		for row in lines:
			credit = flt(row.credit) - flt(row.debit)
			if row.account in accounts or not credit or not total:
				continue
			# a credit to a category's own line is its own part; the rest shares the debits' parts
			parts = {row.account: total} if row.account in own_lines else debits
			for part, debit in parts.items():
				part_sources = sources.setdefault(part, {})
				part_sources[row.account] = flt(part_sources.get(row.account)) + credit * debit / total
	sources = {
		part: {account: flt(amount, 2) for account, amount in lines.items() if flt(amount, 2)}
		for part, lines in sources.items()
	}
	return frappe._dict(
		amount=flt(sum(flt(row.debit) - flt(row.credit) for row in rows if row.account in accounts), 2),
		last=max((getdate(row.posting_date) for row in rows), default=None),
		entries=sorted({row.name for row in rows}),
		credits={
			account: own_lines[account]
			for account, lines in sources.items()
			if account in own_lines and sum(lines.values()) > 0
		},
		sources=sources,
	)


def get_hand_part(row, categories: dict, credits: dict) -> str | None:
	"""Which part of the hand-booked depreciation a row shares in: its category's own line (see
	get_own_lines) when hand entries credited it, its asset line first; else its category's
	depreciation account."""
	if row.asset_account in credits:
		return row.asset_account
	line = next((line for line, owner in credits.items() if owner == row.asset_account), None)
	category = categories.get(row.asset_account)
	return line or (category and category.depreciation_account)


def get_hand_left(onboarding, hand: frappe._dict) -> dict[str, float]:
	"""By part (see get_hand_part), the depreciation booked by hand the posted rows haven't counted."""
	categories = {row.asset_account: row for row in onboarding.asset_categories}
	left = {part: sum(lines.values()) for part, lines in hand.sources.items()}
	for row in onboarding.fixed_assets:
		part = get_hand_part(row, categories, hand.credits)
		if row.status == "Posted" and part and flt(row.hand_booked):
			left[part] = flt(left.get(part)) - flt(row.hand_booked)
	over = -sum(amount for amount in left.values() if amount < 0)
	positive = sum(amount for amount in left.values() if amount > 0)
	if over and positive - over > -1:
		# posted rows counted some of a part under another: what is left is the net
		share = max(positive - over, 0) / positive if positive else 0
		left = {part: amount * share if amount > 0 else 0 for part, amount in left.items()}
	return {part: flt(amount, 2) for part, amount in left.items()}


def get_post_time(onboarding, pick=min):
	"""When assets were first (or, with max, last) posted from this step: hand entries after the
	first are counted twice."""
	names = [row.asset for row in onboarding.fixed_assets if row.status == "Posted" and row.asset]
	return (
		pick(frappe.get_all("Asset", filters={"name": ("in", names)}, pluck="creation"), default=None)
		if names
		else None
	)


def count_month_ends(start, end) -> int:
	"""Month ends from start's month to end, end's month counted when end is its last day."""
	if getdate(end) < getdate(start):
		return 0
	return month_diff(end, start) - (0 if is_last_day_of_the_month(end) else 1)


def count_year_ends(start, end, year_end) -> int:
	"""Financial year ends from start to end."""
	count, date = 0, getdate(year_end)
	while date > getdate(end):
		date = add_years(date, -1)
	while date >= getdate(start):
		count += 1
		date = add_years(date, -1)
	return count


def get_plan(onboarding) -> frappe._dict:
	"""How assets go from here: monthly or yearly, and the last day counted as booked."""
	year = get_year(onboarding)
	day_before = get_day_before(onboarding)
	# entries made after the first post are booked again by ERPNext, so no later upload counts them
	hand = get_hand_booked(onboarding, until=get_post_time(onboarding))
	mid_year = getdate(onboarding.migration_date) != year.start
	every_month = mid_year and onboarding.depreciation_booked == "Every month"
	if hand.amount:
		monthly, booked_until = True, get_last_day(hand.last)
	elif mid_year:
		monthly, booked_until = every_month, day_before
	else:
		monthly, booked_until = False, day_before
	return frappe._dict(
		year=year,
		day_before=day_before,
		hand=hand,
		mid_year=mid_year,
		# months the old books booked depreciation this year, before the migration date
		months_before=month_diff(day_before, year.start) if every_month else 0,
		monthly=monthly,
		booked_until=booked_until,
		start=get_last_day(add_days(booked_until, 1)) if monthly else next_year_end(booked_until, year.end),
	)


def next_year_end(date, year_end):
	end = getdate(year_end)
	while end <= getdate(date):
		end = add_years(end, 1)
	while add_years(end, -1) > getdate(date):
		end = add_years(end, -1)
	return end


def prepare_assets(onboarding, rows: list, plan: frappe._dict) -> list[frappe._dict]:
	"""Each row's schedule: periods booked, periods in all, first date and amount, and what it books
	a month from the day before, to share the hand-booked depreciation by."""
	categories = {row.asset_account: row for row in onboarding.asset_categories}
	per_year = 12 if plan.monthly else 1
	prepared = []
	for row in rows:
		category = categories.get(row.asset_account) or frappe._dict()
		method = row.method or category.method
		# a row with another method than its category's takes none of its life or rate
		same = not row.method or row.method == category.method
		life = cint(row.life or (same and category.life))
		rate = flt(row.rate or (same and category.rate))
		cost, salvage = flt(row.gross), flt(row.salvage)
		depreciation = flt(row.opening_depreciation) + flt(row.hand_booked)
		value = flt(cost - depreciation, 2)
		purchase = getdate(row.purchase_date) if row.purchase_date else None
		booked = 0
		if purchase:
			booked = (
				count_month_ends(purchase, plan.booked_until)
				if plan.monthly
				else count_year_ends(purchase, plan.booked_until, plan.year.end)
			)
		schedule = []
		if method == "Written Down Value":
			total = life * per_year if life else 0
			if not life and 0 < rate < 100 and value > 0:
				floor = salvage or cost * WDV_SALVAGE
				years = math.ceil(math.log(floor / value) / math.log(1 - rate / 100)) if value > floor else 0
				total = booked + max(years * per_year, 1)
			monthly = flt(cost - flt(row.opening_depreciation)) * rate / 1200
			first = get_first_depreciation(plan, value, rate, total, booked if depreciation else 0, purchase)
			life_over = bool(total) and booked >= total and value > salvage + 0.01
		else:
			schedule = get_straight_line_schedule(plan, cost, salvage, value, life, purchase)
			# as ERPNext counts them, so a later remake (sale, repair, revaluation) keeps the life
			total = max(life * per_year, (booked if depreciation else 0) + 1)
			monthly = (cost - salvage) / (life * 12) if life else 0
			first = schedule[0][1] if schedule else 0
			life_over = bool(life and purchase) and not schedule and value > salvage + 0.01
		prepared.append(
			frappe._dict(
				row=row,
				category=category,
				method=method,
				life=life,
				rate=rate,
				value=value,
				depreciation=depreciation,
				purchase=purchase,
				booked=booked,
				total=total,
				schedule=schedule,
				periods_left=len(schedule) if schedule else total - booked,
				fully_depreciated=value <= salvage + 0.01,
				life_over=life_over,
				first=flt(min(first, max(value - salvage, 0)), 2),
				monthly=flt(monthly, 2),
			)
		)
	return prepared


def get_straight_line_schedule(plan, cost, salvage, value, life, purchase) -> list[tuple]:
	"""The old books' straight line from here: cost less salvage over the life each period, from the
	plan's first date to the end of the life, that last period pro rata and taking all that is left.
	Empty when nothing is left, or the life ended before this period."""
	left = flt(value - salvage, 2)
	if left <= 0 or not life or not purchase:
		return []
	frequency = 1 if plan.monthly else 12
	per_period = (cost - salvage) * frequency / (life * 12)
	life_end = getdate(add_days(add_years(purchase, life), -1))
	date = getdate(plan.start)
	period_start = getdate(add_days(get_last_day(add_months(date, -frequency)), 1))
	if life_end < period_start:
		return []
	schedule = []
	while left > 0:
		if life_end <= date:
			schedule.append((life_end, left))
			break
		days = date_diff(date, max(period_start, purchase)) + 1
		amount = flt(min(per_period * days / (date_diff(date, period_start) + 1), left), 2)
		schedule.append((date, amount))
		left = flt(left - amount, 2)
		period_start = getdate(add_days(date, 1))
		date = getdate(get_last_day(add_months(date, frequency)))
	return schedule


def get_first_depreciation(plan, value, rate, total, booked, purchase) -> float:
	"""What ERPNext books first for a written down value asset: a period at the rate, pro rata from
	the purchase date when no period is booked, all that is left when it is the last."""
	frequency = 1 if plan.monthly else 12
	amount = value * rate / 100 * frequency / 12
	if total and total - booked <= 1:
		amount = value
	period_days = date_diff(plan.start, get_last_day(add_months(plan.start, -frequency)))
	if not booked and purchase and date_diff(plan.start, purchase) + 1 < period_days:
		amount = amount * (date_diff(plan.start, purchase) + 1) / period_days
	return amount


def get_wdv_gap(plan: frappe._dict, prepared: list) -> float:
	"""How much less a written down value asset going monthly books this year than the old method:
	ERPNext works the rest of the year out from today's value, not the year's opening value."""
	if not plan.monthly:
		return 0
	gap = 0
	for asset in prepared:
		if asset.method != "Written Down Value" or asset.fully_depreciated:
			continue
		booked = month_diff(plan.booked_until, plan.year.start)
		value_now = asset.value
		value_then = flt(asset.row.gross) - flt(asset.row.opening_depreciation)
		months = plan.months_before
		value_start = (
			value_then / (1 - asset.rate * months / 1200) if asset.rate * months < 1200 else value_then
		)
		asset.wdv_gap = flt(asset.rate / 100 * max(12 - booked, 0) / 12 * (value_start - value_now), 2)
		gap += asset.wdv_gap
	return flt(gap, 2)


def spread_hand_booked(onboarding):
	"""Share the depreciation booked by hand that the posted rows haven't counted over the draft
	assets, each part (see get_hand_part) over its own rows by what their schedule books in those
	months."""
	plan = get_plan(onboarding)
	drafts = get_drafts(onboarding)
	for row in drafts:
		row.hand_booked = 0
	categories = {row.asset_account: row for row in onboarding.asset_categories}
	prepared = [asset for asset in prepare_assets(onboarding, drafts, plan) if asset.monthly > 0]
	months = month_diff(plan.booked_until, onboarding.migration_date)
	spread = False
	for part, amount in get_hand_left(onboarding, plan.hand).items():
		assets = [
			asset for asset in prepared if get_hand_part(asset.row, categories, plan.hand.credits) == part
		]
		if amount < 0.01 or not assets:
			continue
		share_hand_booked(assets, amount, months)
		spread = True
	if not spread:
		frappe.throw(
			_("No depreciation booked by hand since the migration date is left to share over these assets."),
			title=_("Nothing to spread"),
		)


def share_hand_booked(assets: list, amount: float, months: int):
	"""Share an amount over these assets by what their schedule books a month, none more than its
	value left. Assets whose schedule ends in these months take all they have left first, when the
	amount covers that."""
	left = {id(asset): flt(max(flt(asset.value) - flt(asset.row.salvage), 0), 2) for asset in assets}
	ending = [asset for asset in assets if asset.monthly * months >= left[id(asset)]]
	full = ending if sum(left[id(asset)] for asset in ending) <= amount else []
	while True:
		for asset in full:
			asset.row.hand_booked = left[id(asset)]
			amount -= left[id(asset)]
		done = {id(asset) for asset in full}
		assets = [asset for asset in assets if id(asset) not in done]
		if not assets or amount < 0.01:
			return
		weights = sum(asset.monthly for asset in assets)
		full = [asset for asset in assets if amount * asset.monthly / weights >= left[id(asset)]]
		if not full:
			break
	shared = 0
	for asset in assets:
		asset.row.hand_booked = flt(amount * asset.monthly / weights, 2)
		shared += asset.row.hand_booked
	assets[-1].row.hand_booked = flt(assets[-1].row.hand_booked + amount - shared, 2)


def get_hand_checks(onboarding, prepared: list, plan: frappe._dict) -> frappe._dict:
	"""For each part of the hand-booked depreciation with draft rows (see get_hand_part): what is
	left to count, what the drafts count, and what their schedule gives for those months. With the
	posted rows of a part that counted more than the books now have."""
	categories = {row.asset_account: row for row in onboarding.asset_categories}
	months = month_diff(plan.booked_until, onboarding.migration_date) if plan.hand.amount else 0
	left = get_hand_left(onboarding, plan.hand)
	over = {part for part, amount in left.items() if amount <= -1}
	parts = []
	for part, amount in left.items():
		if part in over:
			continue
		assets = [
			asset for asset in prepared if get_hand_part(asset.row, categories, plan.hand.credits) == part
		]
		if assets:
			parts.append(
				frappe._dict(
					amount=amount,
					counted=flt(sum(flt(asset.row.hand_booked) for asset in assets), 2),
					schedule=flt(sum(asset.monthly for asset in assets) * months, 2),
				)
			)
	return frappe._dict(
		amount=flt(sum(part.amount for part in parts), 2),
		counted=flt(sum(part.counted for part in parts), 2),
		schedule=flt(sum(part.schedule for part in parts), 2),
		uncounted=any(abs(part.amount - part.counted) >= 1 for part in parts),
		off=any(
			abs(part.amount - part.counted) >= 1 and abs(part.amount - part.schedule) >= 1 for part in parts
		),
		overcounted=[
			row.idx
			for row in onboarding.fixed_assets
			if row.status == "Posted" and get_hand_part(row, categories, plan.hand.credits) in over
		],
	)


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


def get_closed_until(company: str) -> frappe._dict:
	"""The latest of: the end of the company's last closed year, of its last Accounting Period closed
	for journal entries (unless the user has its exempted role), and the accounts frozen date (unless
	the user may post into it, as ERPNext checks). With the period's name, or frozen, when that one
	is the latest."""
	roles = frappe.get_roles()
	year_end = frappe.db.get_value(
		"Period Closing Voucher", {"company": company, "docstatus": 1}, [{"MAX": "period_end_date"}]
	)
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


def get_asset_checks(onboarding) -> dict:
	"""What the Fixed assets window shows: the sheet, the books today, the reds that stop posting,
	the yellows and greys to show, and what the confirm says."""
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
			if hsn_label
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


# Posting


def is_posting_assets(company: str) -> bool:
	return is_job_enqueued(JOB_ID.format(company)) or is_job_enqueued(UNDO_JOB_ID.format(company))


def post_fixed_assets(company: str):
	"""Post the draft rows, all or none: the categories and their accounts, one Existing Asset per
	row, and one entry moving the depreciation of net-value categories to their own line."""
	onboarding = frappe.get_doc("Company Setup", company)
	checks = get_asset_checks(onboarding)
	blocker = get_asset_blocker(checks)
	if blocker:
		frappe.throw(blocker, title=_("Can't post yet"))
	ensure_fiscal_year(onboarding.migration_date, company)
	plan = get_plan(onboarding)
	drafts = get_drafts(onboarding)
	if checks["hand_uncounted"]:
		spread_hand_booked(onboarding)
	prepared = prepare_assets(onboarding, drafts, plan)
	get_wdv_gap(plan, prepared)
	net_value = get_net_value_categories(onboarding, prepared, plan)
	accumulated = make_net_value_accounts(onboarding, net_value)
	location = get_location()
	# ERPNext's note on each schedule made is not for the user here
	muted = frappe.flags.mute_messages
	frappe.flags.mute_messages = True
	try:
		for account, rows in groupby(
			sorted(prepared, key=lambda asset: asset.row.asset_account),
			key=lambda asset: asset.row.asset_account,
		):
			rows = list(rows)
			category = make_category(onboarding.company, rows[0].category, accumulated.get(account))
			item = make_asset_item(category, rows[0].category.hsn_code)
			for asset in rows:
				asset.row.asset = make_asset(onboarding.company, asset, plan, category, item, location)
				asset.row.status = "Posted"
				asset.row.wdv_gap = asset.get("wdv_gap") or 0
				asset.row.moved = (
					flt(flt(asset.row.opening_depreciation) + flt(asset.row.hand_booked), 2)
					if account in net_value
					else 0
				)
	finally:
		frappe.flags.mute_messages = muted
	make_net_value_entry(onboarding, [asset.row for asset in prepared], plan.hand)
	# an undone asset posted again is not undone any more
	posted = {get_asset_key(asset.row) for asset in prepared}
	onboarding.set(
		"undone_assets", [row for row in onboarding.undone_assets if get_asset_key(row) not in posted]
	)
	onboarding.flags.posting = True
	onboarding.save()


def post_fixed_assets_in_background(company: str):
	run_posting_job("company_setup_assets", company, lambda onboarding: post_fixed_assets(company))


def make_net_value_accounts(onboarding, net_value: dict) -> dict[str, str]:
	"""An "Accumulated Depreciation - <category>" ledger for each net-value category, next to its
	asset line; one made before is used again, found by name or on the category's ERPNext accounts."""
	made = {}
	for row in onboarding.asset_categories:
		if row.asset_account not in net_value:
			continue
		account = get_net_value_account(onboarding.company, row) or add_account(
			onboarding.company,
			NET_VALUE_ACCOUNT.format(row.asset_category),
			"Accumulated Depreciation",
			"Asset",
			parent=frappe.db.get_value("Account", row.asset_account, "parent_account"),
		)
		row.accumulated_account = account
		made[row.asset_account] = account
	return made


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


def make_category(company: str, category: frappe._dict, accumulated: str | None) -> str:
	"""The Asset Category, made when new; only this company's accounts row is added or set."""
	name = category.asset_category
	doc = (
		frappe.get_doc("Asset Category", name)
		if frappe.db.exists("Asset Category", name)
		else frappe.new_doc("Asset Category")
	)
	if doc.is_new():
		doc.asset_category_name = name
	row = next((row for row in doc.accounts if row.company_name == company), None) or doc.append(
		"accounts", {"company_name": company}
	)
	row.update(
		{
			"fixed_asset_account": category.asset_account,
			"accumulated_depreciation_account": accumulated or category.accumulated_account,
			"depreciation_expense_account": category.depreciation_account,
		}
	)
	doc.save()
	return doc.name


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


def make_asset_item(category: str, hsn_code: str | None = None) -> str:
	"""get_asset_item, with why another app's rules refuse it in plain words: the post stops there
	and nothing is left made."""
	try:
		return get_asset_item(category, hsn_code)
	except (frappe.ValidationError, frappe.DuplicateEntryError) as e:
		frappe.clear_messages()
		frappe.flags.mute_messages = False
		frappe.throw(
			_("The item for {0} can't be made: {1}").format(category, get_error_text(e)),
			title=_("Can't post yet"),
		)


def get_asset_item_group() -> str | None:
	"""A group named for fixed asset items, when there is one."""
	return frappe.db.get_value("Item Group", {"is_group": 0, "item_group_name": ("like", "%asset%")})


def get_location() -> str:
	return (
		frappe.db.exists("Location", LOCATION)
		or frappe.get_doc({"doctype": "Location", "location_name": LOCATION}).insert().name
	)


def make_asset(
	company: str, asset: frappe._dict, plan: frappe._dict, category: str, item: str, location: str
) -> str:
	row = asset.row
	depreciation = flt(row.opening_depreciation) + flt(row.hand_booked)
	doc = frappe.new_doc("Asset")
	doc.update(
		{
			"asset_name": row.asset_name,
			"item_code": item,
			"asset_category": category,
			"company": company,
			"location": location,
			"asset_type": "Existing Asset",
			"purchase_date": row.purchase_date,
			"available_for_use_date": row.purchase_date,
			"net_purchase_amount": flt(row.gross),
			"purchase_amount": flt(row.gross),
			"asset_quantity": cint(row.qty) or 1,
			"opening_accumulated_depreciation": depreciation,
			"opening_number_of_booked_depreciations": asset.booked if depreciation else 0,
			"cost_center": frappe.get_cached_value("Company", company, "cost_center"),
			"calculate_depreciation": 0 if asset.fully_depreciated else 1,
			"is_fully_depreciated": 1 if asset.fully_depreciated else 0,
		}
	)
	if not asset.fully_depreciated:
		doc.append(
			"finance_books",
			{
				"depreciation_method": asset.method,
				"total_number_of_depreciations": asset.total,
				"frequency_of_depreciation": 1 if plan.monthly else 12,
				"depreciation_start_date": plan.start,
				# the schedule is set below; the one ERPNext makes first may stop short of the salvage
				"expected_value_after_useful_life": 0 if asset.schedule else flt(row.salvage),
				"rate_of_depreciation": asset.rate if asset.method == "Written Down Value" else 0,
				"daily_prorata_based": 0,
			},
		)
	doc.insert()
	if asset.schedule:
		set_schedule(doc, asset.schedule, flt(row.salvage))
	doc.submit()
	return doc.name


def set_schedule(doc, schedule: list[tuple], salvage: float):
	"""Put the old books' straight line in place of the draft schedule ERPNext made: ERPNext counts
	the periods left in whole periods, so runs at another rate and past the life. Marked manual, as
	ERPNext makes any other schedule again on submit."""
	draft = get_asset_depr_schedule_doc(doc.name, "Draft")
	draft.depreciation_method = "Manual"
	draft.depreciation_schedule = []
	for date, amount in schedule:
		draft.append("depreciation_schedule", {"schedule_date": date, "depreciation_amount": amount})
	draft.expected_value_after_useful_life = salvage
	draft.set_accumulated_depreciation()
	draft.save()
	doc.finance_books[0].update(
		{
			"depreciation_method": "Manual",
			"expected_value_after_useful_life": salvage,
			"depreciation_start_date": schedule[0][0],
		}
	)


def get_row_moves(onboarding, rows: list, hand: frappe._dict) -> tuple[dict, list]:
	"""What the step moves so the books show each row's depreciation where its register does, on its
	category's accumulated depreciation line, by (from line, to line): the depreciation of a row at
	net value from its asset line, and what each row counts as booked by hand from the lines the
	hand entries credited. With the rows that move."""
	categories = {row.asset_account: row for row in onboarding.asset_categories}
	moves, moving = {}, []
	for row in rows:
		target = categories[row.asset_account].accumulated_account
		sources = hand.sources.get(get_hand_part(row, categories, hand.credits)) or {}
		total = sum(sources.values())
		amounts = [(account, flt(row.hand_booked) * amount / total) for account, amount in sources.items()]
		if flt(row.moved):
			amounts.append((row.asset_account, flt(row.opening_depreciation)))
		amounts = [(account, amount) for account, amount in amounts if account != target and flt(amount, 2)]
		for account, amount in amounts:
			moves[(account, target)] = flt(moves.get((account, target))) + amount
		if amounts:
			moving.append(row)
	return {key: flt(amount, 2) for key, amount in moves.items() if flt(amount, 2)}, moving


def make_net_value_entry(onboarding, rows: list, hand: frappe._dict, amended_from: str | None = None):
	"""One entry making these rows' moves (see get_row_moves); each row that moves links to it."""
	moves, moving = get_row_moves(onboarding, rows, hand)
	for row in rows:
		row.net_value_entry = None
	if not moves:
		return
	cost_center = frappe.get_cached_value("Company", onboarding.company, "cost_center")
	dimensions = get_default_dimensions(onboarding.company)
	date = get_net_value_date(onboarding, hand)
	ensure_fiscal_year(date, onboarding.company)
	je = frappe.new_doc("Journal Entry")
	je.update(
		{
			"voucher_type": "Journal Entry",
			"company": onboarding.company,
			"posting_date": date,
			"amended_from": amended_from,
			"user_remark": _(
				"Fixed assets: depreciation moved to each category's own line, from Company Setup"
			),
		}
	)
	for (account, target), amount in sorted(moves.items()):
		append_lines(je, account, amount, target, dimensions, cost_center)
	je.insert()
	je.submit()
	for row in moving:
		row.net_value_entry = je.name


# Posted assets, undo


def get_depreciated(assets: list[str]) -> list[str]:
	"""Assets with depreciation posted by ERPNext."""
	if not assets:
		return []
	return sorted(
		set(
			frappe.get_all(
				"Journal Entry Account",
				filters={"reference_type": "Asset", "reference_name": ("in", assets), "docstatus": 1},
				pluck="reference_name",
			)
		)
	)


def get_posted_assets(onboarding) -> dict:
	"""The posted assets by upload, with which ones already have depreciation posted."""
	rows = [row for row in onboarding.fixed_assets if row.status == "Posted"]
	depreciated = set(get_depreciated([row.asset for row in rows if row.asset]))
	created = {
		asset.name: asset.creation
		for asset in frappe.get_all(
			"Asset",
			filters={"name": ("in", [row.asset for row in rows] or [""])},
			fields=["name", "creation"],
		)
	}
	groups = {}
	for row in rows:
		group = groups.setdefault(
			row.source or "", {"source": row.source or "", "assets": 0, "cost": 0, "rows": []}
		)
		group["assets"] += 1
		group["cost"] = flt(group["cost"] + flt(row.gross), 2)
		group["posted_on"] = min(filter(None, [group.get("posted_on"), created.get(row.asset)]), default=None)
		group["rows"].append(
			{
				"name": row.name,
				"asset": row.asset,
				"asset_name": row.asset_name,
				"asset_account": row.asset_account,
				"purchase_date": row.purchase_date,
				"gross": flt(row.gross),
				"opening_depreciation": flt(row.opening_depreciation),
				"depreciated": row.asset in depreciated,
				"wdv_gap": flt(row.wdv_gap),
			}
		)
	undone = {}
	for row in onboarding.undone_assets:
		group = undone.setdefault(row.source or "", {"source": row.source or "", "count": 0, "cost": 0})
		group["count"] += 1
		group["cost"] = flt(group["cost"] + flt(row.gross), 2)
	return {"groups": list(groups.values()), "undone": list(undone.values())}


def undo_fixed_assets(onboarding, source: str | None, assets: list[str] | None = None) -> frappe._dict:
	"""Cancel the assets of this upload, or only those named, and keep their rows as undone. A
	net-value entry is made again for the rows that stay. Refused for assets with depreciation posted.
	Allowed after Go live."""
	rows = [
		row
		for row in onboarding.fixed_assets
		if row.status == "Posted"
		and (row.source or "") == (source or "")
		and (assets is None or row.asset in assets)
	]
	if not rows:
		frappe.throw(_("There is nothing posted to undo."), title=_("Nothing to undo"))
	depreciated = get_depreciated([row.asset for row in rows])
	if depreciated:
		frappe.throw(
			_(
				"{0} assets already have depreciation posted. Cancel those depreciation entries first. Ask your CA."
			).format(len(depreciated)),
			title=_("Can't undo"),
		)
	entries = sorted({row.net_value_entry for row in rows if row.net_value_entry})
	staying = [row for row in onboarding.fixed_assets if row.status == "Posted" and row not in rows]
	hand = get_hand_booked(onboarding, until=get_post_time(onboarding))
	if entries:
		dates = frappe.get_all("Journal Entry", filters={"name": ("in", entries)}, pluck="posting_date")
		blocker = get_posting_blocker(onboarding.company, min(dates), undo=True, opening=False)
		if not blocker and any(row.net_value_entry in entries for row in staying):
			blocker = get_posting_blocker(
				onboarding.company, get_net_value_date(onboarding, hand), opening=False
			)
		if blocker:
			frappe.throw(blocker, title=_("Can't undo"))
	for row in rows:
		cancel_doc("Asset", row.asset)
	for name in entries:
		cancel_doc("Journal Entry", name)
		make_net_value_entry(
			onboarding,
			[row for row in staying if row.net_value_entry == name],
			hand,
			amended_from=name if frappe.db.exists("Journal Entry", name) else None,
		)
	again = {get_asset_key(row) for row in rows}
	onboarding.set(
		"undone_assets", [row for row in onboarding.undone_assets if get_asset_key(row) not in again]
	)
	for row in rows:
		onboarding.append(
			"undone_assets",
			{field: row.get(field) for field in (*SHEET_FIELDS, "asset_account", "hand_booked", "source")}
			| {"status": "Draft"},
		)
	onboarding.set("fixed_assets", [row for row in onboarding.fixed_assets if row not in rows])
	onboarding.flags.posting = True
	onboarding.save()
	return frappe._dict(removed=len(rows), kept_cancelled=pop_kept_reasons(source))


def undo_fixed_assets_in_background(company: str, source: str | None, assets: list[str] | None = None):
	run_undo_job(company, "assets", source, lambda onboarding: undo_fixed_assets(onboarding, source, assets))


# Check your books


def get_asset_match(onboarding) -> dict | None:
	"""The register against the asset and accumulated depreciation lines as on today, leaving out
	depreciation booked by hand after the assets were posted. None with nothing posted."""
	return get_cached(onboarding, "asset_match", read_asset_match)


def get_register_books(onboarding, rows: list, accounts: list[str]) -> dict[str, float]:
	"""These lines as the register sees them: as on the day before, with the entries since that
	belong to it. Those are the net-value entries, the depreciation booked by hand up to the last
	post, and the depreciation ERPNext posted for these assets. Live entries such as a new asset
	bought are left out."""
	books = get_balances(onboarding.company, accounts, get_day_before(onboarding))
	vouchers = {row.net_value_entry for row in rows if row.net_value_entry}
	vouchers.update(get_hand_booked(onboarding, until=get_post_time(onboarding)).entries)
	vouchers.update(
		frappe.get_all(
			"Journal Entry Account",
			filters={
				"reference_type": "Asset",
				"reference_name": ("in", [row.asset for row in rows]),
				"docstatus": 1,
			},
			pluck="parent",
		)
	)
	if not vouchers or not accounts:
		return books
	gl = frappe.qb.DocType("GL Entry")
	for row in (
		frappe.qb.from_(gl)
		.select(gl.account, (Sum(gl.debit) - Sum(gl.credit)).as_("amount"))
		.where(
			(gl.company == onboarding.company)
			& (gl.is_cancelled == 0)
			& (gl.voucher_type == "Journal Entry")
			& gl.voucher_no.isin(list(vouchers))
			& gl.account.isin(accounts)
			& (gl.posting_date >= onboarding.migration_date)
		)
		.groupby(gl.account)
		.run(as_dict=True)
	):
		books[row.account] = flt(books.get(row.account)) + flt(row.amount)
	return books


def read_asset_match(onboarding) -> dict | None:
	rows = [row for row in onboarding.fixed_assets if row.status == "Posted" and row.asset]
	if not rows or not onboarding.migration_date:
		return None
	values = {
		asset.name: asset
		for asset in frappe.get_all(
			"Asset",
			filters={"name": ("in", [row.asset for row in rows])},
			fields=["name", "net_purchase_amount", "value_after_depreciation"],
		)
	}
	cost = flt(sum(flt(row.gross) for row in rows), 2)
	depreciation = sum(
		flt(asset.net_purchase_amount) - flt(asset.value_after_depreciation) for asset in values.values()
	)
	categories = {row.asset_account: row for row in onboarding.asset_categories}
	register = {}
	for row in rows:
		asset = values.get(row.asset) or frappe._dict()
		register[row.asset_account] = flt(register.get(row.asset_account)) + flt(row.gross)
		accumulated = (categories.get(row.asset_account) or frappe._dict()).accumulated_account
		if accumulated:
			register[accumulated] = flt(register.get(accumulated)) - (
				flt(asset.net_purchase_amount) - flt(asset.value_after_depreciation)
			)
	asset_accounts = sorted({row.asset_account for row in rows})
	accumulated = sorted(set(register) - set(asset_accounts))
	# asset lines no posted row is on: the register matches only when they hold nothing
	others = [account for account in get_fixed_asset_accounts(onboarding.company) if account not in register]
	books = get_register_books(onboarding, rows, list(register) + others)
	after = get_hand_booked(onboarding, after=get_post_time(onboarding))
	books_cost = flt(sum(flt(books.get(account)) for account in asset_accounts), 2)
	books_depreciation = flt(-sum(flt(books.get(account)) for account in accumulated), 2)
	matches = all(abs(flt(books.get(account)) - amount) < 1 for account, amount in register.items()) and all(
		abs(flt(books.get(account))) < 1 for account in others
	)
	return {
		"cost": cost,
		"depreciation": flt(depreciation, 2),
		"books_cost": books_cost,
		"books_depreciation": books_depreciation,
		"hand_after": after.amount,
		"hand_after_entries": get_entry_details(after.entries),
		"wdv_gap": flt(sum(flt(row.wdv_gap) for row in rows), 2),
		"matches": matches,
	}


def get_entry_details(entries: list[str]) -> list[dict]:
	"""Each journal entry's number, date, amount and accounts, to name it to the user."""
	if not entries:
		return []
	lines = frappe.get_all(
		"Journal Entry Account",
		filters={"parent": ("in", entries)},
		fields=["parent", "account", "debit"],
		order_by="idx",
	)
	dates = dict(
		frappe.get_all(
			"Journal Entry", filters={"name": ("in", entries)}, fields=["name", "posting_date"], as_list=True
		)
	)
	return [
		{
			"name": name,
			"posting_date": dates.get(name),
			"amount": flt(sum(flt(line.debit) for line in lines if line.parent == name), 2),
			"accounts": list(dict.fromkeys(line.account for line in lines if line.parent == name)),
		}
		for name in entries
	]


# Status for the step


def get_assets_status(onboarding) -> frappe._dict:
	return get_cached(onboarding, "assets_status", read_assets_status)


def read_assets_status(onboarding) -> frappe._dict:
	drafts = get_drafts(onboarding)
	posted = [row for row in onboarding.fixed_assets if row.status == "Posted"]
	lines = {}
	for row in onboarding.opening_balances:
		lines[row.account] = flt(lines.get(row.account)) + flt(row.debit) - flt(row.credit)
	fixed = set(get_fixed_asset_accounts(onboarding.company))
	off = {}
	if drafts and onboarding.migration_date:
		plan = frappe._dict(day_before=get_day_before(onboarding))
		prepared = [frappe._dict(row=row) for row in onboarding.fixed_assets]
		off = get_category_off(onboarding, drafts, get_net_value_categories(onboarding, prepared, plan), plan)
	return frappe._dict(
		drafts=len(drafts),
		posted=len(posted),
		line_total=flt(sum(amount for account, amount in lines.items() if account in fixed), 2),
		off=off,
		match=get_asset_match(onboarding),
		posting=is_posting_assets(onboarding.company),
	)
