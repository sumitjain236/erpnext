# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import difflib
import re

import frappe
from frappe import _
from frappe.core.doctype.role.role import get_users
from frappe.query_builder.functions import Max
from frappe.utils import cint, cstr, flt, formatdate, getdate

from erpnext.setup.doctype.company_setup.opening_invoices import (
	count_text,
	get_day_before,
	get_posting_blocker,
	get_posting_fix,
	get_similar_key,
)
from erpnext.setup.doctype.company_setup.stock_sheet import (
	JOB_WORKER,
	WORK_IN_PROGRESS,
	get_opening_datetime,
	get_serials,
	get_stock_lines,
	prepare_rows,
)
from erpnext.setup.doctype.company_setup.stock_warehouses import (
	get_movable_lines,
	get_wrong_lines,
	guess_kind,
)
from erpnext.stock.doctype.serial_no.serial_no import get_available_serial_nos
from erpnext.stock.doctype.stock_closing_entry.stock_closing_entry import get_closing_entry_for_closed_period
from erpnext.stock.serial_batch_identity import SerialBatchIdentity
from erpnext.stock.valuation_adjustment import get_adjusted_until


def get_kept_lines(onboarding) -> dict[str, float]:
	"""Work in progress and job worker asset lines kept as balances in Opening balances: not stock
	here."""
	kept = {}
	for row in onboarding.opening_balances:
		if not row.account:
			continue
		name, root_type = frappe.get_cached_value("Account", row.account, ["account_name", "root_type"])
		key = get_similar_key(name)
		if root_type == "Asset" and (WORK_IN_PROGRESS.search(key) or JOB_WORKER.search(key)):
			kept[row.account] = flt(kept.get(row.account)) + flt(row.debit) - flt(row.credit)
	return kept


def get_stock_checks(onboarding, prepared: list | None = None) -> dict:
	"""What the Opening stock window shows: the drafts, what is posted, the trial balance, and the
	reds that stop posting and the lines that only point something out. prepared: the drafts as
	prepare_rows gives them, when the caller has them."""
	company = onboarding.company
	day_before = get_day_before(onboarding)
	opening = get_opening_datetime(onboarding)
	drafts = [row for row in onboarding.opening_stock if row.status != "Posted"]
	posted = [row for row in onboarding.opening_stock if row.status == "Posted"]
	if prepared is None:
		prepared = prepare_rows(onboarding, drafts)
	pairs = {(row.item_code, row.warehouse) for row in prepared if row.item_code and row.warehouse}
	job_workers = {
		row.warehouse for row in onboarding.warehouses if row.kind == "Job worker" and row.warehouse
	}
	lines = get_stock_lines(onboarding)
	reds = frappe._dict(
		missing_items=sorted({row.row.item_name for row in prepared if not row.item_code}),
		missing_items_rows=[row.row.idx for row in prepared if not row.item_code],
		missing_warehouses=sorted({row.row.warehouse_name for row in prepared if not row.warehouse}),
		missing_warehouses_rows=[row.row.idx for row in prepared if not row.warehouse],
		part_qty=[row.row.idx for row in prepared if row.part_qty],
		part_qty_units=sorted({row.stock_uom for row in prepared if row.part_qty}),
		challan=[
			row.row.idx
			for row in prepared
			if row.warehouse in job_workers
			and not (row.row.challan_no and row.row.challan_date and row.row.return_due)
		],
		no_batch=[row.row.idx for row in prepared if row.has_batch_no and not row.row.batch_no],
		untracked=[
			row.row.idx
			for row in prepared
			if row.item_code
			and (
				(row.row.batch_no and not row.has_batch_no)
				or (get_serials(row.row) and not row.has_serial_no)
			)
		],
		negative=[row.row.idx for row in prepared if flt(row.row.qty) < 0],
		no_qty=[row.row.idx for row in prepared if not row.stock_qty and flt(row.row.amount)],
		unknown_units=[row.row.idx for row in prepared if row.unit_unknown],
		unknown_unit_names=sorted({cstr(row.row.uom).strip() for row in prepared if row.unit_unknown}),
		repeated=get_repeated_rows(prepared),
		no_serials=[
			row.row.idx
			for row in prepared
			if row.has_serial_no and row.stock_qty > 0 and not get_serials(row.row)
		],
		serial_count=[
			row.row.idx
			for row in prepared
			if row.has_serial_no and get_serials(row.row) and len(get_serials(row.row)) != row.stock_qty
		],
		kept=get_kept_rows(onboarding, prepared),
		wrong_lines=list(get_wrong_lines(onboarding, prepared, lines).values()),
		serials_moved=get_moved_serials(prepared, opening),
		in_stock=sorted(
			set(get_rows_in_stock(prepared, pairs, opening))
			- get_added_batches(onboarding, prepared, opening)
		),
		**get_later_reds(prepared, opening),
	)
	reds.serials_moved_rows = [
		row.row.idx for row in prepared if set(get_serials(row.row)) & set(reds.serials_moved)
	]
	return {
		"day_before": day_before,
		"perpetual_inventory": cint(
			frappe.get_cached_value("Company", company, "enable_perpetual_inventory")
		),
		"drafts": {
			"rows": len(drafts),
			"value": flt(sum(flt(row.amount) for row in drafts), 2),
			"sources": sorted({row.source or "" for row in drafts}),
			"warehouses": len({row.warehouse or row.row.warehouse_name for row in prepared}),
		},
		"posted": {
			"rows": len(posted),
			"value": flt(sum(flt(row.amount) for row in posted), 2),
			"sources": sorted({row.source or "" for row in posted}),
			"warehouses": len({row.warehouse for row in posted}),
		},
		"trial_balance": {
			"total": flt(sum(lines.values()), 2),
			"lines": lines,
			"kept": get_kept_lines(onboarding),
			"movable": get_movable_lines(onboarding),
		},
		"off": flt(sum(lines.values()) - sum(flt(row.amount) for row in onboarding.opening_stock), 2)
		if lines
		else None,
		"reds": reds,
		"close_items": {row.row.item_name: row.close_item for row in prepared if row.close_item},
		"expired": [
			row.row.idx
			for row in prepared
			if row.row.expiry_date and getdate(row.row.expiry_date) <= getdate(day_before)
		],
		"later_entries": sum(
			get_later_entries(
				onboarding, [row for row in prepared if row.item_code and row.warehouse]
			).values()
		),
		"posting_blocker": get_stock_posting_blocker(company, day_before),
		"posting_fix": get_posting_fix(company, day_before, voucher_types=("Stock Reconciliation",))
		or get_stock_closing_fix(company, day_before),
		"reposting_timeslot": get_reposting_timeslot(),
	}


def get_stock_posting_blocker(
	company: str, day_before, undo: bool = False, voucher_types: tuple[str, ...] = ("Stock Reconciliation",)
) -> str | None:
	"""The closed-books checks for stock dated the day before, and ERPNext's stock freeze: a Stock
	Closing Entry of a year whose Period Closing Voucher is posted."""
	blocker = get_posting_blocker(company, day_before, undo=undo, voucher_types=voucher_types)
	if blocker:
		return blocker
	closing = get_stock_closed_on(company, day_before)
	if closing:
		return _(
			"Stock is closed up to {0} (Stock Closing Entry {1}). Cancel that year's Period Closing Voucher first."
		).format(formatdate(closing.to_date), closing.name)
	return None


def get_stock_closed_on(company: str, day_before) -> frappe._dict | None:
	"""The Stock Closing Entry of a closed year that covers the day before, if any."""
	closing = get_closing_entry_for_closed_period(company)
	return closing if closing and getdate(day_before) <= getdate(closing.to_date) else None


def get_stock_closing_fix(company: str, day_before) -> dict | None:
	"""The Period Closing Voucher whose stock closing stops stock dated the day before, to open and
	cancel."""
	closing = get_stock_closed_on(company, day_before)
	if not closing:
		return None
	name = frappe.db.get_value(
		"Period Closing Voucher",
		{"company": company, "docstatus": 1, "period_end_date": (">=", closing.to_date)},
		order_by="period_end_date asc",
	)
	return name and {"label": _("Open {0}").format(name), "doctype": "Period Closing Voucher", "name": name}


def get_stock_blocker(checks: dict) -> str | None:
	"""Why the drafts can't be posted, or None: the first red."""
	return next((red["text"] for red in get_stock_reds(checks)), None)


def get_stock_reds(checks: dict) -> list[dict]:
	"""Each reason the drafts can't be posted, in order, with its text and the rows (idx) it is about."""
	reds, found = checks["reds"], []

	def add(key: str, text: str, rows: list | None = None):
		found.append({"key": key, "text": text, "rows": rows or []})

	if not checks["perpetual_inventory"]:
		add(
			"perpetual_inventory",
			_(
				"Turn on perpetual inventory for this company. Stock values reach your books only when it is on."
			),
		)
	if checks["posting_blocker"]:
		add("posting_blocker", checks["posting_blocker"])
	if not checks["drafts"]["rows"]:
		add("no_drafts", _("Upload a sheet or add rows to post."))
	if reds.missing_warehouses:
		add(
			"missing_warehouses",
			count_text(
				len(reds.missing_warehouses),
				_("1 warehouse is not in ERPNext. Add it in Warehouses first."),
				_("{0} warehouses are not in ERPNext. Add them in Warehouses first."),
			),
			reds.missing_warehouses_rows,
		)
	if reds.missing_items:
		add(
			"missing_items",
			count_text(
				len(reds.missing_items),
				_("1 item is not in ERPNext. Import it in Items first."),
				_("{0} items are not in ERPNext. Import them in Items first."),
			),
			reds.missing_items_rows,
		)
	if reds.negative:
		add(
			"negative",
			count_text(len(reds.negative), _("Negative qty in 1 row."), _("Negative qty in {0} rows.")),
			reds.negative,
		)
	if reds.no_qty:
		add(
			"no_qty",
			count_text(
				len(reds.no_qty), _("1 row has a value but no qty."), _("{0} rows have a value but no qty.")
			),
			reds.no_qty,
		)
	if reds.unknown_units:
		add(
			"unknown_units",
			count_text(
				len(reds.unknown_units),
				_(
					"1 row uses a unit the item does not have: {1}. Add the unit to the item, or change the row."
				),
				_(
					"{0} rows use a unit the item does not have: {1}. Add the unit to the item, or change the rows."
				),
				get_unit_names(reds.unknown_unit_names),
			),
			reds.unknown_units,
		)
	if reds.part_qty:
		add(
			"part_qty",
			count_text(
				len(reds.part_qty),
				_("1 row has a part quantity for a whole-number unit ({1})."),
				_("{0} rows have part quantities for a whole-number unit ({1})."),
				", ".join(reds.part_qty_units),
			),
			reds.part_qty,
		)
	if reds.repeated:
		add(
			"repeated",
			count_text(
				len(reds.repeated),
				_("1 row repeats an item in the same warehouse."),
				_("{0} rows repeat an item in the same warehouse."),
			),
			reds.repeated,
		)
	if reds.challan:
		add(
			"challan",
			count_text(
				len(reds.challan),
				_("1 job worker row needs challan details."),
				_("{0} job worker rows need challan details."),
			),
			reds.challan,
		)
	if reds.no_batch:
		add(
			"no_batch",
			count_text(
				len(reds.no_batch),
				_("1 batch item has no batch number."),
				_("{0} batch items have no batch number."),
			),
			reds.no_batch,
		)
	if reds.untracked:
		add(
			"untracked",
			count_text(
				len(reds.untracked),
				_(
					"1 row has a batch or serial number, but its item does not track them. Tick Has Batch No or Has Serial No on the item, or clear the row's number."
				),
				_(
					"{0} rows have batch or serial numbers, but their items do not track them. Tick Has Batch No or Has Serial No on the items, or clear the rows' numbers."
				),
			),
			reds.untracked,
		)
	if reds.no_serials:
		add(
			"no_serials",
			count_text(
				len(reds.no_serials),
				_("1 serial item has no serial numbers."),
				_("{0} serial items have no serial numbers."),
			),
			reds.no_serials,
		)
	if reds.serial_count:
		add(
			"serial_count",
			count_text(
				len(reds.serial_count),
				_("Serial count does not match qty in 1 row."),
				_("Serial count does not match qty in {0} rows."),
			),
			reds.serial_count,
		)
	if reds.kept:
		add(
			"kept",
			count_text(
				len(reds.kept["rows"]),
				_("{1} ({2}) is already in your opening balances, so its row can not post as stock."),
				_("{1} ({2}) is already in your opening balances, so its {0} rows can not post as stock."),
				frappe.get_cached_value("Account", reds.kept["account"], "account_name"),
				frappe.format_value(reds.kept["amount"], {"fieldtype": "Currency"}),
			),
			reds.kept["rows"],
		)
		found[-1]["account"] = reds.kept["account"]
	if reds.wrong_lines:
		first = reds.wrong_lines[0]
		account_name = (
			frappe.get_cached_value("Account", first["account"], "account_name")
			if first["account"]
			else _("no account")
		)
		line_name = (
			frappe.get_cached_value("Account", first["line"], "account_name")
			if first["line"]
			else _("a stock account of your trial balance")
		)
		add(
			"wrong_lines",
			count_text(
				len(reds.wrong_lines),
				_("{1} would post to {2}, not {3}. Set its stock account in Warehouses."),
				_(
					"{0} warehouses would post to the wrong stock account, like {1} to {2}, not {3}. Set their stock accounts in Warehouses."
				),
				first["warehouse"],
				account_name,
				line_name,
			),
			[idx for wrong in reds.wrong_lines for idx in wrong["rows"]],
		)
	if reds.serials_moved:
		add(
			"serials_moved",
			count_text(
				len(reds.serials_moved),
				_("1 serial number already moved after {1}. Undo those entries first."),
				_("{0} serial numbers already moved after {1}. Undo those entries first."),
				formatdate(checks["day_before"]),
			),
			reds.serials_moved_rows,
		)
	if reds.in_stock:
		add(
			"in_stock",
			count_text(
				len(reds.in_stock),
				_(
					"1 row is for an item already in stock in that warehouse. Undo that warehouse, then post all its rows together."
				),
				_(
					"{0} rows are for items already in stock in that warehouse. Undo that warehouse, then post all its rows together."
				),
			),
			reds.in_stock,
		)
	found += get_later_list(reds)
	return found


def get_unit_names(units: list[str]) -> str:
	"""The units, each with the UOM it may be a typo of, like "Bxo (Box?)"."""
	names = frappe.get_all("UOM", pluck="name")
	texts = []
	for unit in units:
		close = difflib.get_close_matches(unit, names, n=1, cutoff=0.6)
		texts.append(_("{0} ({1}?)").format(unit, close[0]) if close else unit)
	return ", ".join(texts)


def get_later_reds(prepared: list, after, own: set | None = None) -> dict:
	"""Later entries, other than own, that refuse stock posted or cancelled at after on these rows:
	an Adjustment Entry, a reconciliation of the same batch, or a back-dated entry the user may not
	make."""
	pairs = {(row.item_code, row.warehouse) for row in prepared if row.item_code and row.warehouse}
	return {
		"adjustment": get_later_adjustment(pairs, after),
		"later_batches": get_later_batches(prepared, after, own),
		"back_dated": get_back_dated(pairs, after, own),
	}


def get_later_text(reds: dict) -> str | None:
	return next((red["text"] for red in get_later_list(reds)), None)


def get_later_list(reds: dict) -> list[dict]:
	found = []
	if reds["adjustment"]:
		text = _("An adjustment entry on {0} covers {1} of these items. Undo it first.").format(
			formatdate(reds["adjustment"]["date"]), reds["adjustment"]["items"]
		)
		found.append({"key": "adjustment", "text": text, "rows": []})
	for warehouse, count in reds["later_batches"].items():
		text = count_text(
			count,
			_("1 batch has a later stock reconciliation in {1}. Undo it first."),
			_("{0} batches have a later stock reconciliation in {1}. Undo it first."),
			warehouse,
		)
		found.append({"key": "later_batches", "text": text, "rows": []})
	if reds["back_dated"]:
		text = _(
			"You can not post stock dated before your last stock entry of {0} items. Ask for the back-dated stock role."
		).format(len(reds["back_dated"]))
		found.append({"key": "back_dated", "text": text, "rows": []})
	return found


def get_later_batches(prepared: list, after, own: set | None = None) -> dict[str, int]:
	"""By warehouse, the sheet's batches that a later stock reconciliation, not this step's, counts
	in that warehouse."""
	keys = {
		(row.item_code, row.warehouse, row.row.batch_no)
		for row in prepared
		if row.has_batch_no and row.row.batch_no and row.warehouse
	}
	if not keys:
		return {}
	entry = frappe.qb.DocType("Serial and Batch Entry")
	batch = frappe.qb.DocType("Batch")
	found = (
		frappe.qb.from_(entry)
		.inner_join(batch)
		.on(batch.name == entry.batch_no)
		.select(batch.item, entry.warehouse, batch.batch_id)
		.distinct()
		.where(
			(entry.voucher_type == "Stock Reconciliation")
			& (entry.is_cancelled == 0)
			& (entry.docstatus == 1)
			& (entry.posting_datetime > after)
			& entry.voucher_no.notin(list(own or []) or [""])
			& batch.batch_id.isin([key[2] for key in keys])
		)
		.run()
	)
	later = {}
	for item, warehouse, batch_id in found:
		if (item, warehouse, batch_id) in keys:
			later[warehouse] = later.get(warehouse, 0) + 1
	return later


def make_serial_numbers(onboarding) -> int:
	"""New serial numbers for draft serial item rows that have none, from the item's series, else
	ITEM-CODE-##### (spaces as dashes: the sheet splits serial numbers on spaces)."""
	drafts = [row for row in onboarding.opening_stock if row.status != "Posted"]
	filled = 0
	for row in prepare_rows(onboarding, drafts):
		if not (row.has_serial_no and row.stock_qty > 0 and not get_serials(row.row)):
			continue
		series = (
			frappe.get_cached_value("Item", row.item_code, "serial_no_series")
			or re.sub(r"\s+", "-", row.item_code) + "-.#####"
		)
		row.row.serial_no = "\n".join(get_available_serial_nos(series, row.stock_qty, row.item_code))
		filled += 1
	return filled


def get_repeated_rows(prepared: list) -> list[int]:
	"""Rows ERPNext would refuse as a line twice: the same item, warehouse, batch, serial numbers and
	challan in one upload (rows of two challans post as one line), or the same item and warehouse in two uploads, as each upload posts its own
	Opening Stock."""
	lines, sources = {}, {}
	for row in prepared:
		if not (row.item_code and row.warehouse):
			continue
		batch = row.row.batch_no if row.has_batch_no else None
		key = (
			row.row.source or "",
			row.item_code,
			row.warehouse,
			batch,
			tuple(get_serials(row.row)),
			cstr(row.row.challan_no),
		)
		lines.setdefault(key, []).append(row.row.idx)
		sources.setdefault((row.item_code, row.warehouse), set()).add(row.row.source or "")
	repeated = {idx for rows in lines.values() for idx in rows[1:]}
	repeated |= {row.row.idx for row in prepared if len(sources.get((row.item_code, row.warehouse), ())) > 1}
	return sorted(repeated)


def get_kept_rows(onboarding, prepared: list) -> dict | None:
	"""The first work in progress or job worker line kept as a balance in Opening balances, with the
	draft rows in warehouses of that kind: as stock too, their value would count twice."""
	kinds = {row.warehouse: row.kind for row in onboarding.warehouses if row.warehouse}
	for account, amount in get_kept_lines(onboarding).items():
		key = get_similar_key(frappe.get_cached_value("Account", account, "account_name"))
		kind = "Work in progress" if WORK_IN_PROGRESS.search(key) else "Job worker"
		rows = [
			row.row.idx
			for row in prepared
			if (kinds.get(row.warehouse) or guess_kind(row.row.warehouse_name)) == kind
		]
		if rows:
			return {"account": account, "amount": flt(amount, 2), "rows": rows}
	return None


def get_moved_serials(prepared: list, opening) -> list[str]:
	"""Serial numbers on the sheet that moved after the day before."""
	by_item = {}
	for row in prepared:
		if row.item_code:
			by_item.setdefault(row.item_code, set()).update(get_serials(row.row))
	identity = SerialBatchIdentity("Serial No")
	numbers = {
		record.name: record.serial_no
		for item, serials in by_item.items()
		if serials
		for record in identity.get_records(item, list(serials), ["name", "serial_no"])
	}
	if not numbers:
		return []
	entry = frappe.qb.DocType("Serial and Batch Entry")
	return sorted(
		{
			numbers[row[0]]
			for row in frappe.qb.from_(entry)
			.select(entry.serial_no)
			.where(
				entry.serial_no.isin(list(numbers))
				& (entry.is_cancelled == 0)
				& (entry.posting_datetime > opening)
				& (entry.docstatus == 1)
			)
			.run()
		}
	)


def get_rows_in_stock(prepared: list, pairs: set, opening) -> list[int]:
	"""Rows whose item has stock entries in that warehouse up to the opening: Opening Stock refuses them."""
	if not pairs:
		return []
	sle = frappe.qb.DocType("Stock Ledger Entry")
	found = set(
		frappe.qb.from_(sle)
		.select(sle.item_code, sle.warehouse)
		.distinct()
		.where(
			sle.item_code.isin([pair[0] for pair in pairs])
			& sle.warehouse.isin([pair[1] for pair in pairs])
			& (sle.is_cancelled == 0)
			& (sle.posting_datetime <= opening)
		)
		.run()
	)
	return [row.row.idx for row in prepared if (row.item_code, row.warehouse) in found]


def get_added_batches(onboarding, prepared: list, opening) -> set[int]:
	"""Rows of a batch not posted yet, of an item whose stock in that warehouse up to the opening is
	only this step's: Opening Stock refuses them, so they post as a Stock Reconciliation after the
	opening, which sets that batch alone."""
	own = get_own_entries(onboarding)
	rows = [
		row for row in prepared if row.has_batch_no and row.row.batch_no and row.item_code and row.warehouse
	]
	if not rows or not own:
		return set()
	posted = {
		(row.item_code, row.warehouse, row.batch_no)
		for row in onboarding.opening_stock
		if row.status == "Posted"
	}
	sle = frappe.qb.DocType("Stock Ledger Entry")
	vouchers = {}
	for item_code, warehouse, voucher_no in (
		frappe.qb.from_(sle)
		.select(sle.item_code, sle.warehouse, sle.voucher_no)
		.distinct()
		.where(
			sle.item_code.isin([row.item_code for row in rows])
			& sle.warehouse.isin([row.warehouse for row in rows])
			& (sle.is_cancelled == 0)
			& (sle.posting_datetime <= opening)
		)
		.run()
	):
		vouchers.setdefault((item_code, warehouse), set()).add(voucher_no)
	return {
		row.row.idx
		for row in rows
		if vouchers.get((row.item_code, row.warehouse))
		and vouchers[(row.item_code, row.warehouse)] <= own
		and (row.item_code, row.warehouse, row.row.batch_no) not in posted
	}


def get_later_adjustment(pairs: set, after) -> dict | None:
	"""Adjustment Entries after this moment on one of these items in its warehouse: no stock can be
	posted or cancelled before one."""
	later = [row for row in get_adjusted_until(list(pairs)).values() if row.posting_datetime > after]
	if not later:
		return None
	return {
		"date": min(row.posting_datetime for row in later).date(),
		"items": len({row.item_code for row in later}),
	}


def get_back_dated(pairs: set, opening, own: set | None = None) -> list[str]:
	"""Items whose last entry in the warehouse is after the opening, when only a role may post back-dated
	stock, some user has it and this user does not: as ERPNext checks it."""
	role = frappe.get_single_value("Stock Settings", "role_allowed_to_create_edit_back_dated_transactions")
	if not role or not pairs:
		return []
	users = get_users(role)
	if not users or frappe.session.user in users:
		return []
	sle = frappe.qb.DocType("Stock Ledger Entry")
	return sorted(
		{
			row[0]
			for row in frappe.qb.from_(sle)
			.select(sle.item_code, sle.warehouse)
			.distinct()
			.where(
				sle.item_code.isin([pair[0] for pair in pairs])
				& (sle.is_cancelled == 0)
				& (sle.posting_datetime > opening)
				& sle.voucher_no.notin(list(own or []) or [""])
			)
			.run()
			if (row[0], row[1]) in pairs
		}
	)


def get_reposting_timeslot() -> dict | None:
	"""When the later entries' cost is worked out again, if only in a time slot."""
	settings = frappe.get_cached_doc("Stock Reposting Settings")
	if not settings.limit_reposting_timeslot:
		return None
	return {"start_time": str(settings.start_time), "end_time": str(settings.end_time)}


def get_stock_reposts(company: str, day_before) -> dict:
	"""Cost updates of this company from the opening on that are not done: Go live points them out."""
	reposts = frappe.get_all(
		"Repost Item Valuation",
		filters={
			"company": company,
			"docstatus": 1,
			"posting_date": (">=", day_before),
			"status": ("in", ("Queued", "In Progress", "Failed")),
		},
		fields=["status"],
	)
	return {
		"running": sum(1 for repost in reposts if repost.status != "Failed"),
		"failed": sum(1 for repost in reposts if repost.status == "Failed"),
	}


def get_stock_closing(company: str, to_date) -> frappe._dict:
	"""What closing the year to to_date needs for stock: needed when the company has stock entries up to
	then; the Stock Closing Entry for that date (name, status: Draft, Queued, In Progress, Completed,
	Failed), whether stock changed after it was built (outdated), the cost updates still running up
	to then, and why it can't be made now (blocker)."""
	needed = bool(
		frappe.db.exists(
			"Stock Ledger Entry", {"company": company, "is_cancelled": 0, "posting_date": ("<=", to_date)}
		)
	)
	entry = frappe.db.get_value(
		"Stock Closing Entry",
		{"company": company, "to_date": to_date, "docstatus": ("<", 2)},
		["name", "status", "docstatus", "modified"],
		order_by="docstatus desc, creation desc",
		as_dict=True,
	)
	reposts = frappe.db.count(
		"Repost Item Valuation",
		{
			"company": company,
			"docstatus": 1,
			"status": ("in", ("Queued", "In Progress")),
			"posting_date": ("<=", to_date),
		},
	)
	outdated = bool(
		entry and entry.status == "Completed" and is_stock_changed_after(company, to_date, entry.modified)
	)
	blocker = None
	if needed and not frappe.has_permission("Stock Closing Entry", "submit"):
		blocker = _("Closing stock needs the Stock Manager role. Ask someone who has it.")
	elif needed and reposts:
		blocker = count_text(
			reposts,
			_(
				"1 stock cost update up to {1} is still running. Close the year when it is done, usually within the hour."
			),
			_(
				"{0} stock cost updates up to {1} are still running. Close the year when they are done, usually within the hour."
			),
			formatdate(to_date),
		)
	return frappe._dict(
		needed=needed,
		name=entry and entry.name,
		status=entry and entry.status,
		outdated=outdated,
		done=bool(entry and entry.status == "Completed" and not outdated),
		reposts=reposts,
		blocker=blocker,
	)


def is_stock_changed_after(company: str, to_date, built) -> bool:
	"""Whether stock entries up to to_date changed after the closing was built: the year close refuses it."""
	sle = frappe.qb.DocType("Stock Ledger Entry")
	last = (
		frappe.qb.from_(sle)
		.select(Max(sle.modified))
		.where((sle.company == company) & (sle.posting_date <= to_date))
	).run()
	return bool(last and last[0][0] and last[0][0] > built)


def make_stock_closing(company: str, to_date) -> str:
	"""Submit the Stock Closing Entry to to_date, or build again one that failed or is outdated; it
	builds in the background. Refused while cost updates up to to_date run."""
	closing = get_stock_closing(company, to_date)
	if closing.blocker:
		frappe.throw(closing.blocker, title=_("Not yet"))
	if closing.status in ("Queued", "In Progress") or closing.done:
		return closing.name
	if closing.name and closing.status in ("Failed", "Completed", "Outdated"):
		frappe.get_doc("Stock Closing Entry", closing.name).regenerate_closing_balance()
		return closing.name
	doc = (
		frappe.get_doc("Stock Closing Entry", closing.name)
		if closing.name
		else frappe.get_doc({"doctype": "Stock Closing Entry", "company": company, "to_date": to_date})
	)
	doc.save()
	doc.submit()
	return doc.name


# Posted stock, undo and corrections


def get_corrections(row) -> list[str]:
	"""The row's corrections, oldest first."""
	return cstr(row.correction).split()


def get_own_entries(onboarding) -> set[str]:
	return {
		name for row in onboarding.opening_stock for name in [row.stock_entry, *get_corrections(row)] if name
	}


def get_later_entries(onboarding, rows: list) -> dict[str, int]:
	"""By warehouse, the stock entries after the opening on these rows' items, other than this step's."""
	pairs = {(row.item_code, row.warehouse) for row in rows}
	if not pairs:
		return {}
	sle = frappe.qb.DocType("Stock Ledger Entry")
	found = (
		frappe.qb.from_(sle)
		.select(sle.item_code, sle.warehouse, sle.voucher_no)
		.where(
			sle.item_code.isin([pair[0] for pair in pairs])
			& sle.warehouse.isin([pair[1] for pair in pairs])
			& (sle.is_cancelled == 0)
			& (sle.posting_datetime > get_opening_datetime(onboarding))
			& sle.voucher_no.notin(list(get_own_entries(onboarding)) or [""])
		)
		.run(as_dict=True)
	)
	later = {}
	for entry in found:
		if (entry.item_code, entry.warehouse) in pairs:
			later.setdefault(entry.warehouse, set()).add(entry.voucher_no)
	return {warehouse: len(vouchers) for warehouse, vouchers in later.items()}
