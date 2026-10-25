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

from itertools import groupby

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt
from frappe.utils.background_jobs import is_job_enqueued

from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	get_asset_depr_schedule_doc,
)
from erpnext.setup.doctype.company_setup.asset_checks import (
	get_asset_blocker,
	get_asset_checks,
	get_asset_item,
	get_category_off,
	get_closed_to,
	get_net_value_account,
	get_net_value_categories,
	get_net_value_date,
)
from erpnext.setup.doctype.company_setup.asset_plan import (
	get_hand_booked,
	get_hand_part,
	get_plan,
	get_post_time,
	get_wdv_gap,
	prepare_assets,
	spread_hand_booked,
)
from erpnext.setup.doctype.company_setup.asset_sheet import (
	JOB_ID,
	LOCATION,
	NET_VALUE_ACCOUNT,
	SHEET_FIELDS,
	UNDO_JOB_ID,
	clear_wrong_lines,
	download_fixed_assets,
	get_asset_key,
	get_drafts,
	get_fixed_asset_accounts,
	keep_posted_categories,
	mark_asset_accounts,
	read_asset_sheet,
	save_categories,
	save_fixed_assets,
	sort_upload,
)
from erpnext.setup.doctype.company_setup.bank_accounts import get_balances
from erpnext.setup.doctype.company_setup.opening_invoices import (
	add_account,
	append_lines,
	ensure_fiscal_year,
	get_cached,
	get_day_before,
	get_default_dimensions,
	get_error_text,
	get_posting_blocker,
)
from erpnext.setup.doctype.company_setup.posted_entries import (
	cancel_doc,
	pop_kept_reasons,
	run_posting_job,
	run_undo_job,
)

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
