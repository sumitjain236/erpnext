# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_last_day

from erpnext.setup.doctype.company_setup.chart_sheet import (
	download_sheet,
	find_heading,
	get_amount,
	get_cell,
	get_labels_above,
	get_qty,
	is_total_row,
	read_sheet,
)
from erpnext.setup.doctype.company_setup.opening_invoices import (
	get_cached,
	get_day_before,
	get_exact_key,
	get_sheet_date,
	get_similar_key,
)
from erpnext.stock.doctype.item.item import get_uom_conv_factor
from erpnext.stock.utils import get_combine_datetime

JOB_ID = "company_setup_stock::{0}"
UNDO_JOB_ID = "company_setup_undo::{0}::stock"
CORRECT_JOB_ID = "company_setup_correct::{0}::stock"
# keeps each reconciliation small; an item's rows stay in one
ROWS_PER_ENTRY = 100
# the opening comes first on the day before, a correction after anything else that day
OPENING_TIME = "00:00:00"
CORRECTION_TIME = "23:59:59"
KINDS = ("Our warehouse", "Job worker", "Work in progress")
WORK_IN_PROGRESS = re.compile(r"(?<!capital )\b(work in (progress|process)|wip)\b")
JOB_WORKER = re.compile(r"\b(job ?work(s|ers?)?|with processors?)\b")
DIFFERENCE_ACCOUNT = "Opening Stock Difference"
# a gap over this share of its line is more likely a warehouse not uploaded than a count difference
BIG_GAP = 0.05
# what a stock sheet may call each column, the likeliest first
STOCK_COLUMNS = {
	"item_name": ("item", "item_name", "item_code", "particulars", "name_of_item", "product", "description"),
	"warehouse_name": ("warehouse", "godown", "location", "store", "warehouse_name"),
	"qty": (
		"qty",
		"quantity",
		"closing_qty",
		"closing_quantity",
		"balance_qty",
		"stock_qty",
		"stock_on_hand",
		"quantity_on_hand",
		"qty_on_hand",
	),
	"uom": ("unit", "uom", "units", "stock_uom"),
	"rate": ("rate", "valuation_rate", "closing_rate"),
	"amount": (
		"value",
		"amount",
		"closing_value",
		"stock_value",
		"balance_value",
		"asset_value",
		"inventory_value",
	),
	"stock_group": ("stock_group", "item_group", "group", "category"),
	"batch_no": ("batch", "batch_no", "batch_number", "lot_no"),
	"expiry_date": ("expiry", "expiry_date", "exp_date", "expiry_dt", "exp"),
	"serial_no": ("serial_no", "serial", "serial_nos", "serial_number", "serial_numbers"),
	"challan_no": ("challan_no", "challan", "challan_number"),
	"challan_date": ("challan_date",),
	"return_due": ("return_due", "return_due_date", "due_date"),
}
SHEET_COLUMNS = (
	"Item",
	"Warehouse",
	"Qty",
	"Unit",
	"Rate",
	"Value",
	"Stock Group",
	"Batch",
	"Expiry Date",
	"Serial No",
	"Challan No",
	"Challan Date",
	"Return Due",
)
SHEET_FIELDS = (
	"item_name",
	"warehouse_name",
	"qty",
	"uom",
	"rate",
	"amount",
	"stock_group",
	"batch_no",
	"expiry_date",
	"serial_no",
	"challan_no",
	"challan_date",
	"return_due",
)
# a quantity cell with its unit, like Tally's "2,450 Kg"
MONTH_YEAR = re.compile(r"^\s*(\d{1,2})\s*[/\-.]\s*(\d{2}|\d{4})\s*$")
# short units sheets use for a UOM ERPNext names in full
SHORT_UNITS = {
	"mtr": "Meter",
	"mtrs": "Meter",
	"ltr": "Litre",
	"ltrs": "Litre",
	"kgs": "Kg",
	"gm": "Gram",
	"gms": "Gram",
	"pcs": "Nos",
	"pc": "Nos",
	"no": "Nos",
}
TITLE_WAREHOUSE = re.compile(r"^(godown|location|warehouse)\s*:\s*(.+)$", re.IGNORECASE)


def get_opening_datetime(onboarding):
	return get_combine_datetime(get_day_before(onboarding), OPENING_TIME)


# Items and warehouses


def get_company_items(company: str) -> list[frappe._dict]:
	"""The company's enabled items, stock items and services: those with an Item Default for it, or
	every one on a site with one company."""
	filters = {"disabled": 0}
	if frappe.db.count("Company") > 1:
		filters["name"] = ("in", frappe.get_all("Item Default", {"company": company}, pluck="parent") or [""])
	return frappe.get_all(
		"Item", filters=filters, fields=["name", "item_group", "stock_uom", "is_stock_item", "is_fixed_asset"]
	)


def get_item_index(rows: list | None = None) -> frappe._dict:
	"""Every enabled stock item, by code, by name, and by its name loosely, to match sheet names.
	With rows, only the items named on them: enough to match, not to suggest a close name."""
	filters, or_filters = {"is_stock_item": 1, "disabled": 0}, None
	if rows is not None:
		keys = {get_exact_key(name) for row in rows for name in (row.item_code, row.item_name)} - {""}
		names = [*keys, *(f"{key}." for key in keys)]
		or_filters = {"name": ("in", names or [""]), "item_name": ("in", names or [""])}
	items = frappe.get_all(
		"Item",
		filters=filters,
		or_filters=or_filters,
		fields=["name", "item_name", "stock_uom", "has_batch_no", "has_serial_no"],
	)
	index = frappe._dict(details={}, codes={}, names={}, similar={})
	for item in items:
		index.details[item.name] = item
		index.codes[get_exact_key(item.name)] = item.name
		index.names.setdefault(get_exact_key(item.item_name), set()).add(item.name)
		for key in {get_similar_key(item.name), get_similar_key(item.item_name)}:
			index.similar.setdefault(key, set()).add(item.name)
	return index


def match_item(index: frappe._dict, row) -> tuple[str | None, str | None]:
	"""The item a row is, when sure: by code, then by its one item of that name. Else an item with a
	close name, only to suggest."""
	if row.get("item_code") in index.details:
		return row.item_code, None
	key = get_exact_key(row.get("item_name"))
	if key in index.codes:
		return index.codes[key], None
	names = index.names.get(key, set())
	if len(names) == 1:
		return next(iter(names)), None
	close = index.similar.get(get_similar_key(row.get("item_name")), set())
	return None, next(iter(close)) if len(close) == 1 else None


def get_company_warehouses(company: str) -> list[frappe._dict]:
	return frappe.get_all(
		"Warehouse",
		filters={"company": company, "is_group": 0, "disabled": 0},
		fields=["name", "warehouse_name", "account"],
		order_by="lft",
	)


def get_warehouse_index(onboarding) -> dict[str, str]:
	"""The company's warehouses by name, short name and loose name; a loose name two share is left out."""
	index, similar = {}, {}
	warehouses = get_company_warehouses(onboarding.company)
	for warehouse in warehouses:
		for key in {get_exact_key(warehouse.name), get_exact_key(warehouse.warehouse_name)}:
			index[key] = warehouse.name
		similar.setdefault(get_similar_key(warehouse.warehouse_name), set()).add(warehouse.name)
	for key, names in similar.items():
		if len(names) == 1:
			index.setdefault(f"~{key}", next(iter(names)))
	names = {warehouse.name for warehouse in warehouses}
	for row in onboarding.warehouses:
		if row.warehouse in names:
			index.setdefault(get_exact_key(row.warehouse_name), row.warehouse)
	return index


def match_warehouse(index: dict[str, str], row) -> str | None:
	if row.get("warehouse") in index.values():
		return row.warehouse
	name = row.get("warehouse_name")
	return index.get(get_exact_key(name)) or index.get(f"~{get_similar_key(name)}")


def get_sheet_rows(onboarding) -> list:
	"""Every stock row on the sheets, posted or not."""
	return list(onboarding.opening_stock)


def get_items_status(onboarding) -> frappe._dict:
	"""Counts for the Items card."""
	return get_cached(onboarding, "items_status", read_items_status)


def read_items_status(onboarding) -> frappe._dict:
	items = get_company_items(onboarding.company)
	drafts = [row for row in onboarding.opening_stock if row.status != "Posted"]
	index = get_item_index(drafts)
	missing = {get_exact_key(row.item_name) for row in drafts if not match_item(index, row)[0]}
	return frappe._dict(
		item_count=len(items),
		stock_items=sum(1 for item in items if item.is_stock_item),
		# an asset item is neither stock nor a service
		services=sum(1 for item in items if not item.is_stock_item and not item.is_fixed_asset),
		item_groups=len({item.item_group for item in items}),
		units=len({item.stock_uom for item in items}),
		missing=len(missing),
	)


def get_warehouses_status(onboarding) -> frappe._dict:
	"""Counts for the Warehouses and Job workers cards."""
	return get_cached(onboarding, "warehouses_status", read_warehouses_status)


def read_warehouses_status(onboarding) -> frappe._dict:
	warehouses = get_company_warehouses(onboarding.company)
	drafts = [row for row in onboarding.opening_stock if row.status != "Posted"]
	index = get_warehouse_index(onboarding) if drafts else {}
	missing = {get_exact_key(row.warehouse_name) for row in drafts if not match_warehouse(index, row)}
	job_workers = [row for row in onboarding.warehouses if row.kind == "Job worker" and row.warehouse]
	return frappe._dict(
		warehouses=len(warehouses),
		job_workers=sorted({row.supplier or row.job_worker for row in job_workers}),
		work_in_progress=sum(
			1 for row in onboarding.warehouses if row.kind == "Work in progress" and row.warehouse
		),
		missing=len(missing),
		saved=bool(onboarding.warehouses),
	)


# Stock sheets


def read_stock_sheet(file_url: str) -> frappe._dict:
	"""The rows of a stock sheet: a plain sheet with Item, Warehouse, Qty and Value columns, or a
	Tally Godown Summary, where items are indented under their stock group and warehouse, and a
	quantity cell holds its unit. Totals are left out."""
	rows, keys = read_sheet(file_url)
	heading = find_heading(keys, find_columns, above=2)
	if heading is None:
		frappe.throw(
			_("The sheet needs an Item column and a Qty or Value column."), title=_("Columns not found")
		)
	columns = find_columns(keys[max(heading - 2, 0) : heading + 1])
	title_warehouse = next(
		(
			match.group(2).strip()
			for row in rows[:heading]
			for value in row.cells
			if isinstance(value, str) and (match := TITLE_WAREHOUSE.match(value.strip()))
		),
		None,
	)
	lines = []
	for row in rows[heading + 1 :]:
		name = cstr(get_cell(row.cells, columns["item_name"])).strip()
		if not name or is_total_row(row.cells, columns["item_name"]):
			continue
		qty, unit = get_qty(get_cell(row.cells, columns["qty"]))
		lines.append(
			frappe._dict(
				name=name,
				indent=get_cell(row.indents, columns["item_name"]) or 0,
				cells=row.cells,
				qty=qty,
				unit=unit,
				amount=get_amount(get_cell(row.cells, columns["amount"])),
			)
		)
	stock_rows, parents = [], []
	for line, after in zip(lines, [*lines[1:], None], strict=True):
		while parents and parents[-1].indent >= line.indent:
			parents.pop()
		if after and after.indent > line.indent:
			parents.append(line)
			continue
		if not line.qty and not line.amount:
			continue
		groups = [parent.name for parent in parents]
		warehouse = cstr(get_cell(line.cells, columns["warehouse_name"])).strip()
		if not warehouse and columns["warehouse_name"] is None:
			warehouse = title_warehouse or (groups.pop(0) if groups else "")
		rate = get_amount(get_cell(line.cells, columns["rate"]))
		amount = line.amount or flt(line.qty * rate, 2)
		stock_rows.append(
			{
				"item_name": line.name,
				"warehouse_name": warehouse,
				"qty": line.qty,
				"uom": cstr(get_cell(line.cells, columns["uom"])).strip() or line.unit,
				"rate": flt(amount / line.qty, 9) if line.qty else rate,
				"amount": amount,
				"stock_group": cstr(get_cell(line.cells, columns["stock_group"])).strip()
				or (groups[-1] if groups else None),
				"batch_no": cstr(get_cell(line.cells, columns["batch_no"])).strip() or None,
				"expiry_date": get_expiry_date(get_cell(line.cells, columns["expiry_date"])),
				"serial_no": cstr(get_cell(line.cells, columns["serial_no"])).strip() or None,
				"challan_no": cstr(get_cell(line.cells, columns["challan_no"])).strip() or None,
				"challan_date": get_sheet_date(get_cell(line.cells, columns["challan_date"])),
				"return_due": get_sheet_date(get_cell(line.cells, columns["return_due"])),
			}
		)
	return frappe._dict(rows=stock_rows)


def get_expiry_date(value) -> str | None:
	"""An expiry cell; a month and year alone, like 11/2027, is the last day of that month."""
	match = MONTH_YEAR.match(cstr(value)) if isinstance(value, str) else None
	if match and 1 <= cint(match.group(1)) <= 12:
		year = cint(match.group(2))
		return str(get_last_day(f"{year + 2000 if year < 100 else year}-{cint(match.group(1)):02d}-01"))
	return get_sheet_date(value)


def find_columns(heading_rows: list[list[str]]) -> dict | None:
	"""The column of each field, from the last heading row, the item column from any of them: Tally
	writes "Particulars" a row above "Quantity Rate Value". A closing column wins over an opening one."""
	width = max(len(row) for row in heading_rows)
	above = get_labels_above(heading_rows[:-1], width)
	last = heading_rows[-1]
	columns = {}
	for field, names in STOCK_COLUMNS.items():
		found = [index for name in names for index, key in enumerate(last) if key == name]
		if field == "item_name" and not found:
			found = [
				index
				for row in heading_rows
				for name in names
				for index, key in enumerate(row)
				if key == name
			]
		closing = [index for index in found if index < width and "closing" in above[index]]
		columns[field] = (closing or found or [None])[0]
	if columns["item_name"] is None or (columns["qty"] is None and columns["amount"] is None):
		return None
	if sum(1 for key in last if key) < 2:
		return None
	return columns


def get_row_key(row, item: str | None, warehouse: str | None) -> tuple:
	"""What makes two stock rows the same line: the item in the same warehouse, batch and serial
	numbers."""
	return (
		item or get_exact_key(row.get("item_name")),
		warehouse or get_exact_key(row.get("warehouse_name")),
		get_exact_key(row.get("batch_no")),
		tuple(sorted(get_serials(frappe._dict(serial_no=row.get("serial_no"))))),
	)


def get_other_drafts(onboarding, rows: list[dict], source: str) -> tuple[list[dict], int]:
	"""The drafts a new upload keeps, and how many it replaces: those of the same sheet, and those of
	the same line (see get_row_key) under any file name."""
	item_index, warehouse_index = get_item_index(), get_warehouse_index(onboarding)

	def get_key(row):
		row = frappe._dict(row)
		return get_row_key(row, match_item(item_index, row)[0], match_warehouse(warehouse_index, row))

	new = {get_key(row) for row in rows}
	kept, replaced = [], 0
	for row in onboarding.opening_stock:
		if row.status == "Posted":
			continue
		if (row.source or "") == source or get_key(row.as_dict()) in new:
			replaced += 1
		else:
			kept.append(row.as_dict())
	return kept, replaced


def save_opening_stock(onboarding, rows: list[dict], source: str | None = None) -> frappe._dict:
	"""Replace the draft rows with these. Posted rows stay; a row already posted (see get_row_key) is
	left out, and counted as differing when its qty or value is not what was posted. A row with no
	serial numbers matches a posted serial row of the same qty: its numbers were made here."""
	item_index, warehouse_index = get_item_index(), get_warehouse_index(onboarding)
	posted = [row for row in onboarding.opening_stock if row.status == "Posted"]
	posted_by_key = {get_row_key(row, row.item_code, row.warehouse): row for row in posted}
	# serial numbers made here are not on the sheet: such a row is the posted one of its qty
	made_serials = {
		(*get_row_key(row, row.item_code, row.warehouse)[:3], flt(row.qty)): row
		for row in posted
		if get_serials(row)
	}
	onboarding.set("opening_stock", posted)
	skipped, differ = 0, 0
	for row in rows:
		row = frappe._dict(row)
		if not cstr(row.item_name).strip() and not row.item_code:
			continue
		item = match_item(item_index, row)[0]
		warehouse = match_warehouse(warehouse_index, row)
		key = get_row_key(row, item, warehouse)
		same = posted_by_key.get(key) or (not key[3] and made_serials.get((*key[:3], flt(row.qty))))
		if same:
			skipped += 1
			if abs(flt(same.qty) - flt(row.qty)) > 0.0001 or abs(flt(same.amount) - flt(row.amount)) >= 1:
				differ += 1
			continue
		amount = flt(row.amount) if row.amount not in (None, "") else flt(flt(row.qty) * flt(row.rate), 2)
		onboarding.append(
			"opening_stock",
			{
				**{field: row.get(field) for field in SHEET_FIELDS},
				"item_name": " ".join(cstr(row.item_name or row.item_code).split()),
				"warehouse_name": " ".join(cstr(row.warehouse_name or row.warehouse).split()),
				"item_code": item,
				"warehouse": warehouse,
				"qty": flt(row.qty),
				"amount": amount,
				"rate": flt(amount / flt(row.qty), 9) if flt(row.qty) else flt(row.rate),
				"source": row.source or source,
				"status": "Draft",
			},
		)
	return frappe._dict(skipped=skipped, differ=differ)


@frappe.whitelist()
def download_opening_stock(company: str, undone_from: str | None = None):
	"""A stock sheet to fill, with the drafts entered. With undone_from, the rows undone from that
	source instead, to fix and upload again."""
	onboarding = frappe.get_doc("Company Setup", company)
	onboarding.check_permission("read")
	if undone_from is not None:
		rows = [row for row in onboarding.undone_stock if (row.source or "") == undone_from]
	else:
		rows = [row for row in onboarding.opening_stock if row.status != "Posted"]
	download_sheet(
		f"{frappe.scrub(company)}_opening_stock",
		[list(SHEET_COLUMNS), *([row.get(field) for field in SHEET_FIELDS] for row in rows)],
		_("Opening Stock"),
	)


# Checks


def get_uom_index() -> frappe._dict:
	uoms = frappe.get_all("UOM", fields=["name", "symbol", "must_be_whole_number"])
	keys = {}
	for uom in uoms:
		for value in (uom.name, uom.symbol):
			if value:
				keys.setdefault(get_unit_key(value), uom.name)
	names = {uom.name for uom in uoms}
	for short, name in SHORT_UNITS.items():
		if name in names:
			keys.setdefault(short, name)
	return frappe._dict(by_key=keys, whole={uom.name for uom in uoms if uom.must_be_whole_number})


def get_unit_key(unit: str) -> str:
	"""A unit as written: case, spaces and one last dot set aside, so "NOS." is Nos."""
	return re.sub(r"\s+", "", cstr(unit).casefold()).removesuffix(".")


def get_unit_factors(pairs: set[tuple]) -> dict[tuple, float | None]:
	"""How many stock units one of each (item, unit) is, as ERPNext finds it: the item's own factor,
	its template's, else the site's UOM Conversion Factor. None when none is known."""
	pairs = {(item, uom) for item, uom in pairs if item and uom}
	items = {
		item.name: item
		for item in frappe.get_all(
			"Item",
			filters={"name": ("in", list({item for item, _uom in pairs}) or [""])},
			fields=["name", "stock_uom", "variant_of"],
		)
	}
	codes = set(items) | {item.variant_of for item in items.values() if item.variant_of}
	own = {
		(detail.parent, detail.uom): flt(detail.conversion_factor)
		for detail in frappe.get_all(
			"UOM Conversion Detail",
			filters={"parent": ("in", list(codes) or [""]), "parenttype": "Item"},
			fields=["parent", "uom", "conversion_factor"],
		)
	}
	factors = {}
	for item_code, uom in pairs:
		item = items.get(item_code)
		if not item:
			factors[(item_code, uom)] = None
		elif uom == item.stock_uom:
			factors[(item_code, uom)] = 1.0
		else:
			factors[(item_code, uom)] = (
				own.get((item_code, uom))
				or own.get((item.variant_of, uom))
				or flt(get_uom_conv_factor(uom, item.stock_uom))
				or None
			)
	return factors


def prepare_rows(onboarding, rows: list) -> list[frappe._dict]:
	"""Each row with its item and warehouse when sure, a close item to suggest, and its qty in the
	item's stock unit: converted by the item's own factor, the value kept."""
	item_index, warehouse_index, uoms = get_item_index(), get_warehouse_index(onboarding), get_uom_index()
	units = []
	for row in rows:
		item = match_item(item_index, row)[0]
		details = item_index.details.get(item) or frappe._dict()
		units.append((item, uoms.by_key.get(get_unit_key(row.uom)) if row.uom else details.stock_uom))
	factors = get_unit_factors(set(units))
	precision = frappe.get_precision("Stock Reconciliation Item", "qty")
	prepared = []
	for row, (item, uom) in zip(rows, units, strict=True):
		close = match_item(item_index, row)[1]
		details = item_index.details.get(item) or frappe._dict()
		factor = 1 if not item else factors.get((item, uom))
		stock_qty = flt(flt(row.qty) * (factor or 1), precision)
		prepared.append(
			frappe._dict(
				row=row,
				item_code=item,
				close_item=close,
				warehouse=match_warehouse(warehouse_index, row),
				stock_uom=details.stock_uom,
				stock_qty=stock_qty,
				factor=factor or 1,
				unit_unknown=bool(item and row.uom and not factor),
				part_qty=bool(item and details.stock_uom in uoms.whole and stock_qty != int(stock_qty)),
				has_batch_no=details.has_batch_no,
				has_serial_no=details.has_serial_no,
			)
		)
	return prepared


def get_stock_lines(onboarding) -> dict[str, float]:
	"""The trial balance's stock lines, by account; "" for stock the trial balance has on no account."""
	return {
		account: flt(amount)
		for account, amount in (
			(frappe.parse_json(onboarding.sheet_accounts) or {}).get("Stock") or {}
		).items()
		if flt(amount, 2)
	}


def get_serials(row) -> list[str]:
	return [serial for serial in re.split(r"[\s,]+", cstr(row.serial_no)) if serial]


def get_line_key(account: str | None, lines: dict) -> str:
	"""The trial balance line a stock account's balance counts against: its own line, else the line
	with no account ("")."""
	return account if account and account in lines else ""
