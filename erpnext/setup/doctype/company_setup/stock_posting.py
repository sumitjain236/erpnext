# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from itertools import groupby

import frappe
from frappe import _
from frappe.query_builder.functions import Min
from frappe.utils import cstr, flt, formatdate
from frappe.utils.background_jobs import is_job_enqueued

from erpnext.setup.doctype.company_setup.opening_invoices import (
	count_text,
	ensure_fiscal_year,
	get_day_before,
	get_default_dimensions,
	get_posting_blocker,
	get_temporary_account,
)
from erpnext.setup.doctype.company_setup.posted_entries import (
	cancel_doc,
	pop_kept_reasons,
	run_posting_job,
	run_undo_job,
)
from erpnext.setup.doctype.company_setup.stock_checks import (
	get_added_batches,
	get_corrections,
	get_later_entries,
	get_later_reds,
	get_later_text,
	get_own_entries,
	get_stock_blocker,
	get_stock_checks,
	get_stock_posting_blocker,
)
from erpnext.setup.doctype.company_setup.stock_sheet import (
	CORRECT_JOB_ID,
	CORRECTION_TIME,
	JOB_ID,
	OPENING_TIME,
	ROWS_PER_ENTRY,
	SHEET_FIELDS,
	UNDO_JOB_ID,
	get_line_key,
	get_opening_datetime,
	get_serials,
	get_stock_lines,
	prepare_rows,
)
from erpnext.stock import get_warehouse_account_map
from erpnext.stock.serial_batch_identity import SerialBatchIdentity
from erpnext.stock.stock_ledger import is_negative_stock_allowed
from erpnext.stock.utils import get_combine_datetime

# Posting


def is_posting_stock(company: str) -> bool:
	return any(is_job_enqueued(job.format(company)) for job in (JOB_ID, UNDO_JOB_ID, CORRECT_JOB_ID))


def post_opening_stock(company: str):
	"""Post the draft rows, all or none: one Opening Stock reconciliation per upload and warehouse, of
	at most ROWS_PER_ENTRY rows and never splitting an item, dated the day before migration, against
	Temporary Opening. A batch added to an item already posted there goes in a Stock Reconciliation
	after the opening (see get_added_batches)."""
	onboarding = frappe.get_doc("Company Setup", company)
	drafts = [row for row in onboarding.opening_stock if row.status != "Posted"]
	prepared = prepare_rows(onboarding, drafts)
	blocker = get_stock_blocker(get_stock_checks(onboarding, prepared))
	if blocker:
		frappe.throw(blocker, title=_("Can't post yet"))
	day_before = get_day_before(onboarding)
	ensure_fiscal_year(day_before, company)
	temporary = get_temporary_account(company, create=True)
	added = get_added_batches(onboarding, prepared, get_opening_datetime(onboarding))
	batches = make_batches(prepared, day_before)
	prepared.sort(
		key=lambda row: (
			row.row.idx in added,
			row.row.source or "",
			row.warehouse,
			row.item_code,
			row.row.idx,
		)
	)
	for (is_added, _source, _warehouse), rows in groupby(
		prepared, key=lambda row: (row.row.idx in added, row.row.source or "", row.warehouse)
	):
		purpose, time = (
			("Stock Reconciliation", CORRECTION_TIME) if is_added else ("Opening Stock", OPENING_TIME)
		)
		for chunk in split_rows(list(rows)):
			name = make_reconciliation(company, purpose, day_before, time, temporary, chunk, batches)
			for row in chunk:
				row.row.update(
					{
						"item_code": row.item_code,
						"warehouse": row.warehouse,
						"status": "Posted",
						"stock_entry": name,
					}
				)
	onboarding.flags.posting = True
	onboarding.save()


def post_opening_stock_in_background(company: str):
	run_posting_job("company_setup_stock", company, lambda onboarding: post_opening_stock(company))


def split_rows(rows: list) -> list[list]:
	"""Rows in chunks of at most ROWS_PER_ENTRY, an item's rows always in one chunk."""
	chunks, chunk = [], []
	for _item, item_rows in groupby(rows, key=lambda row: row.item_code):
		item_rows = list(item_rows)
		if chunk and len(chunk) + len(item_rows) > ROWS_PER_ENTRY:
			chunks.append(chunk)
			chunk = []
		chunk += item_rows
	return [*chunks, chunk] if chunk else chunks


def make_batches(prepared: list, day_before) -> dict[tuple, str]:
	"""The Batch of each batch number on the sheet, made with its expiry when ERPNext has none, as
	made by the day before. An item whose rows all give expiry dates is set to have them."""
	rows = [row for row in prepared if row.has_batch_no and row.row.batch_no]
	# the sheet gives every batch of an item an expiry: the item tracks them, so expired batches
	# show as such (a batch with none would then be refused)
	dated = {row.item_code for row in rows if row.row.expiry_date} - {
		row.item_code for row in rows if not row.row.expiry_date
	}
	for item in dated:
		if not frappe.db.get_value("Item", item, "has_expiry_date"):
			doc = frappe.get_doc("Item", item)
			doc.has_expiry_date = 1
			doc.save()
	identity = SerialBatchIdentity("Batch")
	batches = {}
	for item, item_rows in groupby(
		sorted(rows, key=lambda row: row.item_code), key=lambda row: row.item_code
	):
		numbers = {row.row.batch_no.strip().lower(): row.row.batch_no for row in item_rows}
		for record in identity.get_records(item, list(numbers.values()), ["name", "batch_id"]):
			batches[(item, numbers.get(record.batch_id.strip().lower()))] = record.name
	for row in rows:
		key = (row.item_code, row.row.batch_no)
		batches[key] = batches.get(key) or (
			frappe.get_doc(
				{
					"doctype": "Batch",
					"item": row.item_code,
					"batch_id": row.row.batch_no,
					"expiry_date": row.row.expiry_date,
					"manufacturing_date": day_before,
				}
			)
			.insert()
			.name
		)
	return batches


def make_reconciliation(
	company: str, purpose: str, posting_date, posting_time: str, temporary: str, rows: list, batches: dict
) -> str:
	"""A submitted stock reconciliation of these rows, its difference on Temporary Opening."""
	doc = frappe.new_doc("Stock Reconciliation")
	doc.update(
		{
			"company": company,
			"purpose": purpose,
			"posting_date": posting_date,
			"posting_time": posting_time,
			"set_posting_time": 1,
			"expense_account": temporary,
			"cost_center": frappe.get_cached_value("Company", company, "cost_center"),
		}
	)
	doc.update(get_default_dimensions(company))
	lines = {}
	for row in rows:
		batch = batches.get((row.item_code, row.row.batch_no))
		serials = "\n".join(get_serials(row.row))
		line = lines.setdefault(
			(row.item_code, row.warehouse, batch, serials), {"qty": 0, "amount": 0, "row": row}
		)
		line["qty"] += row.stock_qty
		line["amount"] += flt(row.row.amount)
	for (item_code, warehouse, batch, serials), line in lines.items():
		doc.append(
			"items",
			{
				"item_code": item_code,
				"warehouse": warehouse,
				"qty": line["qty"],
				"valuation_rate": line["amount"] / line["qty"] if line["qty"] else 0,
				"batch_no": batch,
				"serial_no": serials or None,
				"use_serial_batch_fields": 1 if batch or serials else 0,
				"allow_zero_valuation_rate": 0 if line["amount"] else 1,
			},
		)
	doc.insert()
	# submit() queues one of over 100 rows; posting must know it is done
	doc._submit()
	challans = sorted(
		{
			_("{0} of {1}, return due {2}").format(
				row.row.challan_no, formatdate(row.row.challan_date), formatdate(row.row.return_due)
			)
			for row in rows
			if row.row.challan_no
		}
	)
	if challans:
		doc.add_comment("Comment", _("Job worker challans: {0}").format("; ".join(challans)))
	return doc.name


def get_posted_stock(onboarding) -> dict:
	"""The posted rows by upload, each by warehouse with how many later stock entries it has; the
	posted stock difference; the undone uploads."""
	posted = [row for row in onboarding.opening_stock if row.status == "Posted"]
	later = get_later_entries(onboarding, posted)
	entries = {
		entry.name: entry
		for entry in frappe.get_all(
			"Stock Reconciliation",
			filters={"name": ("in", list({row.stock_entry for row in posted}) or [""])},
			fields=["name", "docstatus", "creation"],
		)
	}
	groups = {}
	for row in posted:
		group = groups.setdefault(
			row.source or "", {"source": row.source or "", "rows": 0, "value": 0, "warehouses": {}}
		)
		group["rows"] += 1
		group["value"] = flt(group["value"] + flt(row.amount), 2)
		entry = entries.get(row.stock_entry) or frappe._dict()
		group["posted_on"] = min(filter(None, [group.get("posted_on"), entry.creation]), default=None)
		warehouse = group["warehouses"].setdefault(
			row.warehouse,
			{"warehouse": row.warehouse, "rows": 0, "value": 0, "later_entries": later.get(row.warehouse, 0)},
		)
		warehouse["rows"] += 1
		warehouse["value"] = flt(warehouse["value"] + flt(row.amount), 2)
	for group in groups.values():
		group["warehouses"] = list(group["warehouses"].values())
	undone = {}
	for row in onboarding.undone_stock:
		group = undone.setdefault(row.source or "", {"source": row.source or "", "rows": 0, "value": 0})
		group["rows"] += 1
		group["value"] = flt(group["value"] + flt(row.amount), 2)
	difference = None
	if onboarding.stock_difference_entry:
		amounts = frappe.parse_json(onboarding.stock_difference) or {}
		difference = {
			"entry": onboarding.stock_difference_entry,
			"amount": flt(sum(amounts.values()), 2),
			"lines": amounts,
			"posted_on": frappe.db.get_value("Journal Entry", onboarding.stock_difference_entry, "creation"),
		}
	return {
		"groups": sorted(groups.values(), key=lambda group: cstr(group.get("posted_on"))),
		"difference": difference,
		"undone": list(undone.values()),
		"cancelled": [name for name, entry in entries.items() if entry.docstatus != 1],
	}


def get_rows_to_undo(onboarding, source: str | None, warehouses: list[str] | None = None) -> list:
	return [
		row
		for row in onboarding.opening_stock
		if row.status == "Posted"
		and (row.source or "") == (source or "")
		and (warehouses is None or row.warehouse in warehouses)
	]


def prepare_posted_rows(rows: list) -> list[frappe._dict]:
	"""Posted rows as prepare_rows gives them, enough for get_later_reds."""
	batch_items = set(
		frappe.get_all(
			"Item",
			filters={"name": ("in", list({row.item_code for row in rows}) or [""]), "has_batch_no": 1},
			pluck="name",
		)
	)
	return [
		frappe._dict(
			row=row,
			item_code=row.item_code,
			warehouse=row.warehouse,
			has_batch_no=row.item_code in batch_items,
		)
		for row in rows
	]


def check_stock_undo(onboarding, rows: list):
	"""Refuse to undo nothing, or a warehouse with later stock entries on these items."""
	if not rows:
		frappe.throw(_("There is nothing posted to undo."), title=_("Nothing to undo"))
	day_before = get_day_before(onboarding)
	later = get_later_text(
		get_later_reds(
			prepare_posted_rows(rows),
			get_opening_datetime(onboarding),
			get_own_entries(onboarding),
		)
	)
	if later:
		frappe.throw(later, title=_("Can't undo"))
	later = get_later_entries(onboarding, rows)
	if later:
		warehouse, count = next(iter(later.items()))
		frappe.throw(
			_(
				"{0} can not be undone. {1} stock entries came after {2}. Correct the wrong rows instead."
			).format(frappe.bold(warehouse), count, formatdate(day_before)),
			title=_("Can't undo"),
		)
	blocker = get_stock_posting_blocker(
		onboarding.company,
		day_before,
		undo=True,
		voucher_types=get_voucher_types(onboarding, {row.warehouse for row in rows}),
	)
	if blocker:
		frappe.throw(blocker, title=_("Can't undo"))


def undo_opening_stock(onboarding, source: str | None, warehouses: list[str] | None = None) -> frappe._dict:
	"""Cancel the reconciliations of this upload, or of its warehouses named, and keep their rows as
	undone. The stock difference goes too when it is on one of their stock lines. Undoing the whole
	upload drops its drafts too."""
	rows = get_rows_to_undo(onboarding, source, warehouses)
	check_stock_undo(onboarding, rows)
	undo_difference_for(onboarding, {row.warehouse for row in rows})
	corrections = frappe.get_all(
		"Stock Reconciliation",
		filters={"name": ("in", [name for row in rows for name in get_corrections(row)] or [""])},
		order_by="creation desc",
		pluck="name",
	)
	for name in [*corrections, *sorted({row.stock_entry for row in rows})]:
		# now, even with over 100 rows, which ERPNext would cancel in the background
		cancel_doc("Stock Reconciliation", name, now=True)
	unposted = (
		[]
		if warehouses is not None
		else [
			row
			for row in onboarding.opening_stock
			if row.status != "Posted" and (row.source or "") == (source or "")
		]
	)
	for row in rows:
		onboarding.append(
			"undone_stock",
			{field: row.get(field) for field in (*SHEET_FIELDS, "item_code", "warehouse", "source")}
			| {"status": "Draft"},
		)
	drop = {row.name for row in rows + unposted}
	onboarding.set("opening_stock", [row for row in onboarding.opening_stock if row.name not in drop])
	onboarding.flags.posting = True
	onboarding.save()
	return frappe._dict(removed=len(rows), unposted=len(unposted), kept_cancelled=pop_kept_reasons(source))


def undo_opening_stock_in_background(company: str, source: str | None, warehouses: list[str] | None = None):
	run_undo_job(
		company, "stock", source, lambda onboarding: undo_opening_stock(onboarding, source, warehouses)
	)


def get_warehouse_lines(company: str, warehouses: set[str]) -> set[str]:
	accounts = get_warehouse_account_map(company)
	return {accounts[warehouse].account for warehouse in warehouses if warehouse in accounts}


def has_difference_on(onboarding, warehouses: set[str]) -> bool:
	"""Whether the posted stock difference is on the stock line of one of these warehouses."""
	if not onboarding.stock_difference_entry:
		return False
	stock_lines = get_stock_lines(onboarding)
	lines = {
		get_line_key(account, stock_lines) for account in frappe.parse_json(onboarding.stock_difference) or {}
	}
	return bool(
		lines
		& {
			get_line_key(account, stock_lines)
			for account in get_warehouse_lines(onboarding.company, warehouses)
		}
	)


def get_voucher_types(onboarding, warehouses: set[str]) -> tuple[str, ...]:
	"""What undoing or correcting these warehouses cancels: the stock difference's entry too."""
	if has_difference_on(onboarding, warehouses):
		return ("Stock Reconciliation", "Journal Entry")
	return ("Stock Reconciliation",)


def undo_difference_for(onboarding, warehouses: set[str]) -> bool:
	"""Cancel the posted stock difference when it is on the stock line of one of these warehouses."""
	if not has_difference_on(onboarding, warehouses):
		return False
	undo_stock_difference(onboarding)
	return True


def undo_stock_difference(onboarding):
	if not onboarding.stock_difference_entry:
		frappe.throw(_("No stock difference is posted."), title=_("Nothing to undo"))
	blocker = get_posting_blocker(onboarding.company, get_day_before(onboarding), undo=True)
	if blocker:
		frappe.throw(blocker, title=_("Can't undo"))
	cancel_doc("Journal Entry", onboarding.stock_difference_entry)
	onboarding.stock_difference_entry = None
	onboarding.stock_difference = None


def check_correction(onboarding, rows: list[dict]) -> frappe._dict:
	"""What correcting posted rows to the qty and value given would do, the rows left as they are: the
	changes, why it is refused (blocker), and the items whose later stock would go below zero
	(below_zero: item_code, item_name, warehouse, and the lowest qty it would reach). Refused at
	once for a qty below zero, a value with no qty, or no change."""
	by_name = {row.name: row for row in onboarding.opening_stock if row.status == "Posted"}
	changes = []
	for change in rows:
		change = frappe._dict(change)
		row = by_name.get(change.name)
		if not row:
			frappe.throw(_("Only posted rows can be corrected."), title=_("Not posted"))
		qty, amount = flt(change.qty), flt(change.amount, 2)
		if qty < 0:
			frappe.throw(
				_("The qty of {0} can not be below zero.").format(frappe.bold(row.item_name)),
				title=_("Can't correct"),
			)
		if not qty and amount:
			frappe.throw(
				_("{0} has a value but no qty. Enter its qty.").format(frappe.bold(row.item_name)),
				title=_("Can't correct"),
			)
		if abs(qty - flt(row.qty)) > 0.0001 or abs(amount - flt(row.amount)) >= 0.01:
			changes.append((row, qty, amount))
	if not changes:
		frappe.throw(_("Change a row's qty or value to correct it."), title=_("Nothing changed"))
	day_before = get_day_before(onboarding)
	warehouses = {row.warehouse for row, _qty, _amount in changes}
	old = {row.name: (row.qty, row.amount) for row, _qty, _amount in changes}
	for row, qty, amount in changes:
		row.qty, row.amount = qty, amount
	prepared = {
		row.row.name: row for row in prepare_rows(onboarding, [row for row, _qty, _amount in changes])
	}
	own = get_own_entries(onboarding)
	below = get_below_zero(onboarding, prepared, old, own)
	blocker = get_correction_blocker(onboarding, prepared, old, own, warehouses)
	for row, _qty, _amount in changes:
		row.qty, row.amount = old[row.name]
	return frappe._dict(
		changes=changes,
		old=old,
		prepared=prepared,
		warehouses=warehouses,
		below_zero=below,
		blocker=blocker
		or (
			below
			and count_text(
				len(below),
				_("1 item would go below zero after {1}: {2}."),
				_("{0} items would go below zero after {1}: {2}."),
				formatdate(day_before),
				", ".join(row["item_name"] for row in below),
			)
		)
		or None,
	)


def get_correction_blocker(onboarding, prepared: dict, old: dict, own: set, warehouses: set) -> str | None:
	day_before = get_day_before(onboarding)
	blocker = get_stock_posting_blocker(
		onboarding.company, day_before, voucher_types=get_voucher_types(onboarding, warehouses)
	)
	if blocker:
		return blocker
	serials = [
		row
		for row in prepared.values()
		if row.has_serial_no and abs(flt(row.row.qty) - flt(old[row.row.name][0])) > 0.0001
	]
	if serials:
		return count_text(
			len(serials),
			_("Change the serial numbers of 1 row on the sheet: undo, fix and post again."),
			_("Change the serial numbers of {0} rows on the sheet: undo, fix and post again."),
		)
	return get_later_text(
		get_later_reds(list(prepared.values()), get_combine_datetime(day_before, CORRECTION_TIME), own)
	)


def correct_opening_stock(onboarding, rows: list[dict]) -> frappe._dict:
	"""Set posted rows to the qty and value given, with one Stock Reconciliation per upload and
	warehouse on the day before, after the opening, against Temporary Opening. Later entries'
	cost is worked out again. Refused as check_correction says."""
	check = check_correction(onboarding, rows)
	if check.blocker:
		frappe.throw(check.blocker, title=_("Can't correct"))
	for row, qty, amount in check.changes:
		row.qty, row.amount = qty, amount
	day_before = get_day_before(onboarding)
	undo_difference_for(onboarding, check.warehouses)
	temporary = get_temporary_account(onboarding.company, create=True)
	keys = {(row.item_code, row.warehouse, cstr(row.row.batch_no)) for row in check.prepared.values()}
	# rows of the same line, like two challans of an item, post together
	siblings = [
		row
		for row in onboarding.opening_stock
		if row.status == "Posted"
		and row.name not in check.prepared
		and (row.item_code, row.warehouse, cstr(row.batch_no)) in keys
	]
	prepared = [*check.prepared.values(), *prepare_rows(onboarding, siblings)]
	batches = make_batches(prepared, day_before)
	ordered = sorted(prepared, key=lambda row: (row.row.source or "", row.warehouse, row.item_code))
	for _key, group in groupby(ordered, key=lambda row: (row.row.source or "", row.warehouse)):
		for chunk in split_rows(list(group)):
			name = make_reconciliation(
				onboarding.company,
				"Stock Reconciliation",
				day_before,
				CORRECTION_TIME,
				temporary,
				chunk,
				batches,
			)
			for row in chunk:
				row.row.rate = flt(row.row.amount / row.row.qty, 9) if row.row.qty else 0
				row.row.correction = "\n".join([*get_corrections(row.row), name])
	onboarding.flags.posting = True
	onboarding.save()
	return frappe._dict(
		rows=len(check.changes),
		amount=flt(sum(amount - flt(check.old[row.name][1]) for row, _qty, amount in check.changes), 2),
	)


def correct_opening_stock_in_background(company: str, changes: list[dict]):
	run_posting_job(
		"company_setup_stock_correction",
		company,
		lambda onboarding: {"result": correct_opening_stock(onboarding, changes)},
	)


def get_below_zero(onboarding, prepared: dict, old: dict, own: set[str]) -> list[dict]:
	"""Items whose later stock in the warehouse, other than this step's, would go below zero with
	less opening qty, with the lowest qty it would reach. None where negative stock is allowed."""
	precision = frappe.get_precision("Stock Ledger Entry", "qty_after_transaction")
	drops = {}
	for row in prepared.values():
		if is_negative_stock_allowed(item_code=row.item_code):
			continue
		old_qty = flt(old[row.row.name][0]) * row.factor
		drop = flt(old_qty - row.stock_qty, precision)
		if drop > 0:
			name, total = drops.get((row.item_code, row.warehouse), (row.row.item_name, 0))
			drops[(row.item_code, row.warehouse)] = (name, total + drop)
	if not drops:
		return []
	sle = frappe.qb.DocType("Stock Ledger Entry")
	lowest = {
		(item_code, warehouse): flt(qty)
		for item_code, warehouse, qty in frappe.qb.from_(sle)
		.select(sle.item_code, sle.warehouse, Min(sle.qty_after_transaction))
		.where(
			sle.item_code.isin([key[0] for key in drops])
			& sle.warehouse.isin([key[1] for key in drops])
			& (sle.is_cancelled == 0)
			& (sle.posting_datetime > get_opening_datetime(onboarding))
			& sle.voucher_no.notin(list(own) or [""])
		)
		.groupby(sle.item_code, sle.warehouse)
		.run()
	}
	below = [
		{"item_code": key[0], "item_name": name, "warehouse": key[1], "qty": flt(lowest[key] - drop, 6)}
		for key, (name, drop) in drops.items()
		if key in lowest and flt(lowest[key] - drop, precision) < 0
	]
	return below + get_batches_below_zero(onboarding, prepared, old, own)


def get_batches_below_zero(onboarding, prepared: dict, old: dict, own: set[str]) -> list[dict]:
	"""Batches whose later stock in the warehouse, other than this step's, would go below zero with
	less opening qty: the item's total can stay above zero while one batch does not."""
	precision = frappe.get_precision("Stock Ledger Entry", "qty_after_transaction")
	batches = {}
	for row in prepared.values():
		if not (row.has_batch_no and row.row.batch_no) or is_negative_stock_allowed(item_code=row.item_code):
			continue
		old_qty = flt(old[row.row.name][0]) * row.factor
		if flt(old_qty - row.stock_qty, precision) > 0:
			batches[(row.item_code, row.warehouse, row.row.batch_no)] = (row.row.item_name, row.stock_qty)
	if not batches:
		return []
	entry = frappe.qb.DocType("Serial and Batch Entry")
	batch = frappe.qb.DocType("Batch")
	later = (
		frappe.qb.from_(entry)
		.inner_join(batch)
		.on(batch.name == entry.batch_no)
		.select(batch.item, entry.warehouse, batch.batch_id, entry.qty)
		.where(
			(entry.is_cancelled == 0)
			& (entry.docstatus == 1)
			& (entry.posting_datetime > get_opening_datetime(onboarding))
			& entry.voucher_no.notin(list(own) or [""])
			& batch.batch_id.isin([key[2] for key in batches])
			& entry.warehouse.isin([key[1] for key in batches])
		)
		.orderby(entry.posting_datetime)
		.orderby(entry.creation)
		.run()
	)
	running, lowest = {}, {}
	for item, warehouse, batch_id, qty in later:
		key = (item, warehouse, batch_id)
		if key in batches:
			running[key] = running.get(key, batches[key][1]) + flt(qty)
			lowest[key] = min(lowest.get(key, running[key]), running[key])
	return [
		{
			"item_code": key[0],
			"item_name": _("{0} (batch {1})").format(batches[key][0], key[2]),
			"warehouse": key[1],
			"batch_no": key[2],
			"qty": flt(qty, 6),
		}
		for key, qty in lowest.items()
		if flt(qty, precision) < 0
	]
