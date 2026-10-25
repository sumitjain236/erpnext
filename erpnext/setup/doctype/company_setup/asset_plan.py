# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import math
from itertools import groupby

import frappe
from frappe import _
from frappe.utils import (
	add_days,
	add_months,
	add_years,
	cint,
	date_diff,
	flt,
	get_last_day,
	getdate,
	is_last_day_of_the_month,
	month_diff,
)

from erpnext.accounts.utils import get_fiscal_year
from erpnext.setup.doctype.company_setup.asset_sheet import WDV_SALVAGE, get_drafts, get_own_lines
from erpnext.setup.doctype.company_setup.opening_invoices import get_day_before, get_missing_fiscal_year

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
