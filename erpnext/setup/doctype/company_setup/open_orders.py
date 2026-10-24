# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Open orders: the sales, purchase, work and subcontracting orders still running on the day before
migration, made again in ERPNext for the qty left. No money moves. Each made order can be undone
until Go live, unless something was delivered, received or made against it."""

import re
from itertools import groupby

import frappe
from frappe import _
from frappe.utils import cstr, flt, get_datetime, get_datetime_str, getdate
from frappe.utils.background_jobs import is_job_enqueued

from erpnext.accounts.services.taxes import TaxService
from erpnext.accounts.utils import get_fiscal_year
from erpnext.manufacturing.doctype.work_order.mapper import make_work_order
from erpnext.setup.doctype.brand.brand import get_brand_defaults
from erpnext.setup.doctype.company_setup.chart_sheet import (
	download_sheet,
	find_column,
	find_heading,
	get_amount,
	get_cell,
	get_qty,
	is_total_row,
	read_sheet,
)
from erpnext.setup.doctype.company_setup.opening_invoices import (
	count_text,
	ensure_fiscal_year,
	find_party,
	get_cached,
	get_day_before,
	get_fiscal_year_red,
	get_parties,
	get_sheet_date,
	get_similar_key,
)
from erpnext.setup.doctype.company_setup.opening_stock import (
	get_item_index,
	get_unit_factors,
	get_unit_key,
	get_uom_index,
	get_warehouse_index,
	match_item,
	match_warehouse,
)
from erpnext.setup.doctype.company_setup.posted_entries import (
	cancel_doc,
	pop_kept_reasons,
	run_posting_job,
)
from erpnext.setup.doctype.item_group.item_group import get_item_group_defaults
from erpnext.stock.doctype.item.item import get_item_defaults
from erpnext.stock.get_item_details import get_default_bom
from erpnext.subcontracting.doctype.subcontracting_bom.subcontracting_bom import get_finished_good_bom

ORDER_TYPES = ("Sales Order", "Purchase Order", "Work Order", "Subcontracting Order")
JOB_ID = "company_setup_orders::{0}::{1}"
PARTY_TYPES = {"Sales Order": "Customer", "Purchase Order": "Supplier", "Subcontracting Order": "Supplier"}
# what an open orders sheet may call each column, the likeliest first
ORDER_COLUMNS = {
	"order_no": (
		"order_no",
		"order",
		"order_number",
		"sales_order",
		"purchase_order",
		"work_order",
		"voucher_no",
		"so_no",
		"po_no",
		"salesorder_number",
		"sales_order_number",
		"purchase_order_number",
		"job_card_no",
		"job_card",
	),
	"party_name": (
		"customer",
		"supplier",
		"job_worker",
		"party",
		"party_name",
		"party_s_name",
		"customer_name",
		"supplier_name",
		"vendor_name",
		"vendor",
		"name",
	),
	"item_name": (
		"item",
		"item_to_make",
		"item_to_receive",
		"item_name",
		"name_of_item",
		"item_code",
		"particulars",
	),
	"qty": ("qty_left", "pending_qty", "balance_qty", "qty_pending", "pending", "qty", "quantity"),
	# when there is no qty left column: ordered less what was done against it
	"qty_ordered": ("quantity_ordered", "ordered_qty", "qty_ordered", "planned_qty"),
	"qty_done": (
		"quantity_shipped",
		"quantity_delivered",
		"quantity_received",
		"shipped_qty",
		"delivered_qty",
		"received_qty",
		"produced_qty",
	),
	"uom": ("unit", "uom", "usage_unit"),
	"rate": ("rate", "price", "item_price"),
	"order_date": (
		"order_date",
		"salesorder_date",
		"sales_order_date",
		"purchase_order_date",
		"so_date",
		"po_date",
		"voucher_date",
		"date",
	),
	"delivery_date": (
		"delivery_date",
		"due_date",
		"due_on",
		"schedule_date",
		"required_by",
		"expected_shipment_date",
		"expected_delivery_date",
		"expected_receipt_date",
		"expected_date",
		"required_date",
		"required_by_date",
		"reqd_by_date",
	),
	"bom": ("bom", "bom_no"),
	"wip_warehouse": ("wip_warehouse",),
	"warehouse": ("warehouse", "warehouse_name", "godown"),
	"service_item": ("service_item",),
	"material": ("material_at_the_job_worker", "material"),
}
FIELDS = (
	"order_no",
	"party_name",
	"item_name",
	"qty",
	"uom",
	"rate",
	"order_date",
	"delivery_date",
	"bom",
	"wip_warehouse",
	"warehouse",
	"service_item",
	"material",
)
# the sheet's columns, which read_order_sheet reads back
SHEET_COLUMNS = {
	"order_no": "Order No",
	"party_name": "Party",
	"item_name": "Item",
	"qty": "Qty left",
	"uom": "Unit",
	"rate": "Rate",
	"order_date": "Order date",
	"delivery_date": "Delivery date",
	"bom": "BOM",
	"wip_warehouse": "WIP warehouse",
	"warehouse": "Warehouse",
	"service_item": "Service item",
	"material": "Material at the job worker",
}
# what done against an order since stops undoing it
DONE_FIELDS = {
	"Sales Order": ("per_delivered", "per_billed", "advance_paid"),
	"Purchase Order": ("per_received", "per_billed", "advance_paid"),
	"Work Order": ("produced_qty", "material_transferred_for_manufacturing"),
	"Subcontracting Order": ("per_received",),
}


def check_order_type(order_type: str):
	if order_type not in ORDER_TYPES:
		frappe.throw(_("Unknown order type {0}").format(order_type), title=_("Unknown order type"))


def read_order_sheet(file_url: str) -> list[dict]:
	"""The rows of an open orders sheet, totals left out."""
	rows, keys = read_sheet(file_url)
	start = find_heading(
		keys,
		lambda row: find_column(row, ORDER_COLUMNS["item_name"]) is not None
		and (
			find_column(row, ORDER_COLUMNS["qty"]) is not None
			or find_column(row, ORDER_COLUMNS["qty_ordered"]) is not None
		),
	)
	if start is None:
		frappe.throw(_("The sheet needs an Item column and a Qty column."), title=_("Columns not found"))
	columns = {field: find_column(keys[start], names) for field, names in ORDER_COLUMNS.items()}
	orders = []
	for row in [row.cells for row in rows[start + 1 :]]:
		item = cstr(get_cell(row, columns["item_name"])).strip()
		if not item or is_total_row(row, columns["item_name"]):
			continue
		qty_cell = get_cell(row, columns["qty"])
		if columns["qty"] is None:
			qty_cell = get_cell(row, columns["qty_ordered"])
			qty = get_amount(qty_cell) - get_amount(get_cell(row, columns["qty_done"]))
			if qty <= 0:
				continue
		else:
			qty = get_amount(qty_cell)
		order = {field: cstr(get_cell(row, columns[field])).strip() or None for field in ORDER_COLUMNS}
		order.update(
			{
				"qty": qty,
				"uom": order["uom"] or get_qty(qty_cell)[1],
				"rate": get_amount(get_cell(row, columns["rate"])),
				"order_date": get_sheet_date(get_cell(row, columns["order_date"])),
				"delivery_date": get_sheet_date(get_cell(row, columns["delivery_date"])),
			}
		)
		orders.append(order)
	return orders


def save_open_orders(
	onboarding, order_type: str, rows: list[dict], source: str | None = None
) -> frappe._dict:
	"""Replace the draft orders of this type with these rows. Made ones stay, and numbered rows
	already made are left out."""
	check_order_type(order_type)
	onboarding.set(
		"open_orders",
		[row for row in onboarding.open_orders if row.order_type != order_type or row.status == "Made"],
	)
	made = {get_order_key(row) for row in onboarding.open_orders if row.order_type == order_type}
	undone = {get_order_key(row): row for row in onboarding.undone_orders if row.order_type == order_type}
	skipped = 0
	warehouses = get_warehouse_index(onboarding)

	def find_warehouse(name):
		return name and match_warehouse(warehouses, frappe._dict(warehouse=name, warehouse_name=name))

	for row in rows:
		row = frappe._dict(row)
		if not cstr(row.item_name or row.item_code).strip():
			continue
		if row.order_no and get_order_key(row) in made:
			skipped += 1
			continue
		before = undone.get(get_order_key(row)) or {}
		onboarding.append(
			"open_orders",
			{field: row.get(field) or before.get(field) for field in FIELDS}
			| {
				"order_type": order_type,
				"item_name": " ".join(cstr(row.item_name or row.item_code).split()),
				"item_code": row.item_code,
				"party_name": " ".join(cstr(row.party_name).split()) or None,
				"qty": flt(row.qty),
				"rate": flt(row.rate),
				"warehouse": find_warehouse(row.warehouse or before.get("warehouse")),
				"wip_warehouse": find_warehouse(row.wip_warehouse or before.get("wip_warehouse")),
				"source": row.source or source,
				"status": "Draft",
			},
		)
	for order in prepare_orders(onboarding, order_type):
		order.row.warehouse = order.warehouse
	return frappe._dict(skipped=skipped)


def merge_upload(onboarding, order_type: str, rows: list[dict]) -> tuple[list[dict], int]:
	"""The drafts to keep with a sheet's rows, and how many it replaces: a draft goes when the sheet
	has its order number. Rows with no number are never taken for one another."""
	numbers = {get_number_key(row) for row in rows} - {""}
	drafts = get_drafts(onboarding, order_type)
	kept = [row.as_dict() for row in drafts if get_number_key(row) not in numbers]
	return kept + rows, len(drafts) - len(kept)


def get_number_key(row) -> str:
	return " ".join(cstr(row.get("order_no")).split()).casefold()


def get_order_key(row) -> tuple:
	"""A sheet row as made: its order number, party and item."""
	return tuple(
		" ".join(cstr(value).split()).casefold()
		for value in (row.order_no, row.party_name, row.item_name or row.item_code)
	)


def get_drafts(onboarding, order_type: str) -> list:
	return [row for row in onboarding.open_orders if row.order_type == order_type and row.status != "Made"]


def prepare_orders(onboarding, order_type: str) -> list[frappe._dict]:
	"""Each draft with its item, party, BOM and warehouses when found."""
	items, uoms = get_item_index(), get_uom_index()
	parties = get_parties(PARTY_TYPES[order_type]) if order_type in PARTY_TYPES else None
	job_workers = {
		row.supplier: row.warehouse
		for row in onboarding.warehouses
		if row.kind == "Job worker" and row.supplier
	}
	wip = [row.warehouse for row in onboarding.warehouses if row.kind == "Work in progress" and row.warehouse]
	default_wip = (
		wip[0]
		if len(wip) == 1
		else frappe.get_cached_value("Company", onboarding.company, "default_wip_warehouse")
	)
	prepared = []
	for row in get_drafts(onboarding, order_type):
		item = match_item(items, row)[0] or find_item(row, order_type)
		party = find_party(parties, row.party_name) if parties else None
		order = frappe._dict(row=row, item_code=item, party=party, warehouse=row.warehouse)
		if item:
			order.stock_uom = frappe.get_cached_value("Item", item, "stock_uom")
			order.uom = uoms.by_key.get(get_unit_key(row.uom)) if row.uom else order.stock_uom
		if order_type == "Work Order":
			order.bom = row.bom or (item and get_default_bom(item))
			order.wip_warehouse = row.wip_warehouse or default_wip
		if order_type == "Subcontracting Order":
			# the job work's BOM, as ERPNext picks it for the subcontracting order
			order.bom = row.bom or (item and get_finished_good_bom(frappe.get_cached_doc("Item", item)))
			order.supplier_warehouse = job_workers.get(party)
			order.service_item = row.service_item or (item and get_service_item(item))
			order.subcontracted = bool(item and frappe.db.get_value("Item", item, "is_sub_contracted_item"))
		prepared.append(order)
	set_conversion_factors(prepared)
	set_warehouses(onboarding, order_type, prepared)
	if order_type == "Work Order":
		for order, short in zip(prepared, get_short_material(prepared, "wip_warehouse"), strict=True):
			order.from_wip = bool(order.bom and order.wip_warehouse and not short)
	if order_type == "Subcontracting Order":
		for order, short in zip(prepared, get_short_material(prepared, "supplier_warehouse"), strict=True):
			order.short_material = short
	return prepared


def get_short_material(prepared: list, warehouse_field: str) -> list[list[str]]:
	"""For each row in turn, the BOM materials its warehouse does not hold enough of for the qty
	left, after the rows before it took theirs. At a job worker, only the materials ERPNext
	supplies to it: not those the job worker sources itself."""
	per_unit = {}
	if warehouse_field == "supplier_warehouse":
		order_doc = frappe.new_doc("Subcontracting Order")
		for order in prepared:
			if order.bom and order.item_code and order.bom not in per_unit:
				needs = per_unit.setdefault(order.bom, {})
				for line in order_doc._get_materials_from_bom(order.item_code, order.bom):
					needs[line.rm_item_code] = needs.get(line.rm_item_code, 0) + flt(
						line.qty_consumed_per_unit
					)
	else:
		boms = {order.bom for order in prepared if order.bom}
		for line in frappe.get_all(
			"BOM Item",
			filters={"parent": ("in", list(boms) or [""]), "parenttype": "BOM"},
			fields=["parent", "item_code", "stock_qty"],
		):
			quantity = flt(frappe.get_cached_value("BOM", line.parent, "quantity")) or 1
			per_unit.setdefault(line.parent, {}).setdefault(line.item_code, 0)
			per_unit[line.parent][line.item_code] += flt(line.stock_qty) / quantity
	warehouses = {order.get(warehouse_field) for order in prepared if order.get(warehouse_field)}
	held = {
		(bin.warehouse, bin.item_code): flt(bin.actual_qty)
		for bin in frappe.get_all(
			"Bin",
			filters={"warehouse": ("in", list(warehouses) or [""]), "actual_qty": (">", 0)},
			fields=["warehouse", "item_code", "actual_qty"],
		)
	}
	result = []
	for order in prepared:
		warehouse = order.get(warehouse_field)
		qty = flt(order.row.qty) * flt(order.conversion_factor)
		short = []
		for item, needed in per_unit.get(order.bom, {}).items():
			key = (warehouse, item)
			if held.get(key, 0) + 0.000001 < needed * qty:
				short.append(item)
			held[key] = held.get(key, 0) - needed * qty
		result.append(short if order.bom and warehouse else [])
	return result


def set_warehouses(onboarding, order_type: str, prepared: list):
	"""Where each row's goods go or come from, when the row names none: the item's default
	warehouse, else the warehouse of ours that holds most of it, else the company's usual one. Never a
	transit, job worker or work in progress warehouse: goods are not received or sent from there."""
	company = onboarding.company
	ours = get_receiving_warehouses(onboarding)
	codes = list({order.item_code for order in prepared if order.item_code and not order.warehouse}) or [""]
	defaults = {code: get_item_default_warehouse(code, company) for code in codes if code}
	most = {}
	for row in frappe.get_all(
		"Bin",
		filters={
			"item_code": ("in", codes),
			"warehouse": ("in", ours or [""]),
			"actual_qty": (">", 0),
		},
		fields=["item_code", "warehouse"],
		order_by="actual_qty asc",
	):
		most[row.item_code] = row.warehouse
	fallback = get_default_warehouse(onboarding)
	if order_type == "Work Order":
		fallback = frappe.get_cached_value("Company", company, "default_fg_warehouse") or fallback
	for order in prepared:
		if not order.warehouse:
			default = defaults.get(order.item_code)
			order.warehouse = (default if default in ours else None) or most.get(order.item_code) or fallback


def get_item_default_warehouse(item_code: str, company: str) -> str | None:
	"""The item's default warehouse for the company, else its item group's, else its brand's."""
	return (
		get_item_defaults(item_code, company).get("default_warehouse")
		or get_item_group_defaults(item_code, company).get("default_warehouse")
		or get_brand_defaults(item_code, company).get("default_warehouse")
	)


def get_receiving_warehouses(onboarding) -> list[str]:
	"""The company's warehouses goods are received into or sent from: not in transit, at a job worker
	or for work in progress."""
	other = {
		row.warehouse for row in onboarding.warehouses if row.kind != "Our warehouse" and row.warehouse
	} | {frappe.get_cached_value("Company", onboarding.company, "default_in_transit_warehouse")}
	return [
		warehouse.name
		for warehouse in frappe.get_all(
			"Warehouse",
			filters={"company": onboarding.company, "is_group": 0, "disabled": 0},
			fields=["name", "warehouse_name", "warehouse_type"],
		)
		if warehouse.name not in other
		and warehouse.warehouse_type != "Transit"
		and not re.search(r"\btransit\b", get_similar_key(warehouse.warehouse_name))
	]


def find_item(row, order_type: str) -> str | None:
	"""An item the stock items don't have: by code, or for a sales or purchase order a non-stock
	item by its one item of that name."""
	if row.item_code and frappe.db.exists("Item", row.item_code):
		return row.item_code
	if order_type not in ("Sales Order", "Purchase Order"):
		return None
	names = frappe.get_all("Item", filters={"item_name": row.item_name, "disabled": 0}, pluck="name", limit=2)
	return names[0] if len(names) == 1 else None


def set_conversion_factors(prepared: list):
	"""Set how many stock units one of each row's unit is: None when no factor is known."""
	factors = get_unit_factors({(order.item_code, order.uom) for order in prepared if order.item_code})
	for order in prepared:
		if order.item_code:
			order.conversion_factor = factors.get((order.item_code, order.uom))


def get_service_item(item: str) -> str | None:
	return frappe.db.get_value("Subcontracting BOM", {"finished_good": item, "is_active": 1}, "service_item")


def get_order_checks(onboarding, order_type: str, prepared: list | None = None) -> dict:
	"""What an open orders window shows: the drafts, what is made, and the reds that stop making."""
	check_order_type(order_type)
	prepared = prepare_orders(onboarding, order_type) if prepared is None else prepared
	made = [row for row in onboarding.open_orders if row.order_type == order_type and row.status == "Made"]
	reds = get_reds(order_type, prepared)
	if year_red := get_fiscal_year_red(onboarding.company, get_day_before(onboarding)):
		reds.insert(0, {"key": "fiscal_year", "rows": [], "items": [], **year_red})
	blocker = reds[0]["text"] if reds else None
	if not prepared:
		blocker = _("Upload a sheet or add rows to make orders.")
	return {
		"drafts": len(prepared),
		"orders": len(group_orders(order_type, prepared)),
		"quantities": get_quantities(prepared),
		"unnumbered": sum(1 for row in prepared if order_type != "Work Order" and not row.row.order_no),
		"made": len({row.document for row in made}),
		"reds": reds,
		"blocker": blocker,
	}


def get_quantities(prepared: list) -> list[tuple[float, str | None]]:
	"""The qty left by unit, as qty in different units does not add up."""
	quantities = {}
	for row in prepared:
		unit = row.row.uom or row.stock_uom
		quantities[unit] = quantities.get(unit, 0) + flt(row.row.qty)
	return [(qty, unit) for unit, qty in sorted(quantities.items(), key=lambda pair: cstr(pair[0]))]


def get_reds(order_type: str, prepared: list) -> list[dict]:
	"""What stops making the drafts, in the order to fix it: each with its text and its row numbers."""
	reds = []
	for key, failing in get_failing_rows(order_type, prepared).items():
		if failing:
			reds.append(
				{
					"key": key,
					"text": get_red_text(order_type, key, failing),
					"rows": [row.row.idx for row in failing],
					"items": sorted({row.item_code for row in failing if row.item_code}),
				}
			)
	return reds


def get_failing_rows(order_type: str, prepared: list) -> dict[str, list]:
	subcontracting = order_type == "Subcontracting Order"
	backflush = (
		subcontracting
		and frappe.db.get_single_value("Buying Settings", "backflush_raw_materials_of_subcontract_based_on")
		== "Material Transferred for Subcontract"
	)
	checks = {
		"no_party_name": lambda row: order_type in PARTY_TYPES and not row.row.party_name,
		"missing_parties": lambda row: order_type in PARTY_TYPES and row.row.party_name and not row.party,
		"missing_items": lambda row: not row.item_code,
		"no_qty": lambda row: flt(row.row.qty) <= 0,
		"unknown_units": lambda row: row.item_code and not row.conversion_factor,
		"no_bom": lambda row: order_type in ("Work Order", "Subcontracting Order")
		and row.item_code
		and not row.bom,
		"no_wip": lambda row: order_type == "Work Order" and not row.wip_warehouse,
		"no_warehouse": lambda row: not row.warehouse,
		"no_job_worker": lambda row: subcontracting and row.party and not row.supplier_warehouse,
		"not_subcontracted": lambda row: subcontracting and row.item_code and not row.subcontracted,
		"no_service_item": lambda row: subcontracting and row.item_code and not row.service_item,
		"material_not_at_job_worker": lambda row: subcontracting and row.short_material,
		"backflush_transferred": lambda row: backflush,
	}
	return {key: [row for row in prepared if check(row)] for key, check in checks.items()}


def get_red_text(order_type: str, key: str, failing: list) -> str:
	"""A red's plain text, counting parties or items once."""
	parties = sorted({row.row.party_name or "" for row in failing})
	items = sorted({row.row.item_name for row in failing})
	if key == "no_party_name":
		if PARTY_TYPES[order_type] == "Customer":
			return count_text(
				len(failing),
				_("1 row has no customer. Fill it in, or add a Customer column to the sheet."),
				_("{0} rows have no customer. Fill them in, or add a Customer column to the sheet."),
			)
		if order_type == "Subcontracting Order":
			return count_text(
				len(failing),
				_("1 row has no job worker. Fill it in, or add a Job worker column to the sheet."),
				_("{0} rows have no job worker. Fill them in, or add a Job worker column to the sheet."),
			)
		return count_text(
			len(failing),
			_("1 row has no supplier. Fill it in, or add a Supplier column to the sheet."),
			_("{0} rows have no supplier. Fill them in, or add a Supplier column to the sheet."),
		)
	if key == "missing_parties":
		if PARTY_TYPES[order_type] == "Customer":
			return count_text(
				len(parties),
				_("1 customer is not in ERPNext. Import it in Customers first."),
				_("{0} customers are not in ERPNext. Import them in Customers first."),
			)
		return count_text(
			len(parties),
			_("1 supplier is not in ERPNext. Import it in Suppliers first."),
			_("{0} suppliers are not in ERPNext. Import them in Suppliers first."),
		)
	if key == "missing_items":
		return count_text(
			len(items),
			_("1 item is not in ERPNext. Import it in Items first."),
			_("{0} items are not in ERPNext. Import them in Items first."),
		)
	if key == "no_qty":
		return count_text(
			len(failing), _("Qty left is missing for 1 row."), _("Qty left is missing for {0} rows.")
		)
	if key == "unknown_units":
		units = sorted({(row.row.uom, row.item_code) for row in failing})
		if len(units) == 1:
			return _("Unit {0} is not a unit of {1}. Add it to the item, or fix the sheet.").format(*units[0])
		return _(
			"These units are not units of their items: {0}. Add them to the items, or fix the sheet."
		).format(", ".join(_("{0} for {1}").format(*unit) for unit in units))
	if key == "no_bom":
		return count_text(
			len(items),
			_("1 item has no BOM. Add its BOM first."),
			_("{0} items have no BOM. Add their BOMs first."),
		)
	if key == "no_wip":
		return count_text(
			len(failing),
			_("Pick a work in progress warehouse for 1 row."),
			_("Pick a work in progress warehouse for {0} rows."),
		)
	if key == "no_warehouse":
		return count_text(len(failing), _("Pick a warehouse for 1 row."), _("Pick a warehouse for {0} rows."))
	if key == "no_job_worker":
		return _("Add {0} as a job worker in Warehouses first.").format(", ".join(parties))
	if key == "material_not_at_job_worker":
		names = sorted(
			{
				frappe.get_cached_value("Item", item, "item_name") or item
				for row in failing
				for item in row.short_material
			}
		)
		return count_text(
			len({row.party for row in failing}),
			_(
				"The job worker does not hold all the material its orders need: {1}. Add it at the job worker in Opening stock. If you add it in-house, give the item a Subcontracting BOM whose BOM leaves it out."
			),
			_(
				"{0} job workers do not hold all the material their orders need: {1}. Add it at the job workers in Opening stock. If you add it in-house, give the item a Subcontracting BOM whose BOM leaves it out."
			),
			", ".join(names[:5]) + (", …" if len(names) > 5 else ""),
		)
	if key == "not_subcontracted":
		return count_text(
			len(items),
			_("Mark 1 item as subcontracted first."),
			_("Mark {0} items as subcontracted first."),
		)
	if key == "no_service_item":
		return count_text(
			len(items),
			_("Pick the service item for 1 item, or add its Subcontracting BOM."),
			_("Pick the service item for {0} items, or add their Subcontracting BOM."),
		)
	return _(
		"The material is at the job worker already. Set Backflush Raw Materials of Subcontract Based On to BOM in Buying Settings first."
	)


def group_orders(order_type: str, prepared: list) -> list[list]:
	"""The rows of each order: one work order per row; else by order number and party."""
	if order_type == "Work Order":
		return [[row] for row in prepared]
	key = lambda row: (row.row.source or "", cstr(row.row.order_no), row.party or "")  # noqa: E731
	return [list(rows) for _key, rows in groupby(sorted(prepared, key=key), key=key)]


def get_orders_to_make(onboarding, order_type: str) -> list[list]:
	"""The draft orders of this type, each with its rows. Throws why they can't be made yet."""
	check_order_type(order_type)
	prepared = prepare_orders(onboarding, order_type)
	blocker = get_order_checks(onboarding, order_type, prepared)["blocker"]
	if blocker:
		frappe.throw(blocker, title=_("Can't make orders yet"))
	return group_orders(order_type, prepared)


def make_open_orders(onboarding, order_type: str, orders: list[list] | None = None) -> list[str]:
	"""Make and submit the draft orders of this type, all or none. Returns their names."""
	orders = get_orders_to_make(onboarding, order_type) if orders is None else orders
	ensure_fiscal_year(get_day_before(onboarding), onboarding.company)
	made = []
	for rows in orders:
		purchase_order, name = make_order(onboarding, order_type, rows)
		made.append(name)
		for row in rows:
			row.row.update(
				{
					"status": "Made",
					"document": name,
					"item_code": row.item_code,
					"purchase_order": purchase_order,
				}
			)
	onboarding.flags.posting = True
	onboarding.save()
	return made


def make_open_orders_in_background(company: str, order_type: str):
	run_posting_job(
		"company_setup_orders",
		company,
		lambda onboarding: {"made": len(make_open_orders(onboarding, order_type))},
		order_type=order_type,
	)


def is_making_orders(company: str) -> bool:
	return any(is_job_enqueued(JOB_ID.format(company, order_type)) for order_type in ORDER_TYPES)


def make_order(onboarding, order_type: str, rows: list) -> tuple[str | None, str]:
	"""One submitted order of these rows. A subcontracting order comes with its purchase order."""
	company = onboarding.company
	migration_date = getdate(onboarding.migration_date)
	day_before = get_day_before(onboarding)
	first = rows[0].row
	year_start = get_fiscal_year(day_before, company=company)[1]
	if order_type == "Work Order":
		in_wip = rows[0].from_wip
		doc = make_work_order(
			rows[0].bom,
			rows[0].item_code,
			flt(flt(first.qty) * rows[0].conversion_factor, frappe.get_precision("Work Order", "qty")),
			company,
			use_multi_level_bom=0,
		)
		doc.update(
			{
				"skip_transfer": in_wip,
				"from_wip_warehouse": in_wip,
				"wip_warehouse": rows[0].wip_warehouse,
				"fg_warehouse": rows[0].warehouse,
				"planned_start_date": get_datetime_str(get_datetime(migration_date)),
			}
		)
		doc.insert()
		doc.submit()
		add_old_number(doc, first.order_no)
		return None, doc.name
	doctype = "Sales Order" if order_type == "Sales Order" else "Purchase Order"
	doc = frappe.new_doc(doctype)
	warehouses = {row.warehouse for row in rows}
	date_field = "delivery_date" if doctype == "Sales Order" else "schedule_date"
	doc.update(
		{
			"company": company,
			"customer" if doctype == "Sales Order" else "supplier": rows[0].party,
			"transaction_date": max(
				min(getdate(first.order_date or day_before), getdate(day_before)), getdate(year_start)
			),
			date_field: max(getdate(first.delivery_date or migration_date), migration_date),
			"set_warehouse": rows[0].warehouse if len(warehouses) == 1 else None,
		}
	)
	if order_type == "Subcontracting Order":
		doc.is_subcontracted = 1
		doc.supplier_warehouse = rows[0].supplier_warehouse
	for row in rows:
		item = {
			"qty": flt(row.row.qty),
			"uom": row.uom,
			"conversion_factor": row.conversion_factor,
			"rate": flt(row.row.rate),
			date_field: max(getdate(row.row.delivery_date or migration_date), migration_date),
			"warehouse": row.warehouse,
		}
		if order_type == "Subcontracting Order":
			qty = flt(
				flt(row.row.qty) * row.conversion_factor,
				frappe.get_precision("Purchase Order Item", "fg_item_qty"),
			)
			item |= {
				"item_code": None
				if row.service_item == get_service_item(row.item_code)
				else row.service_item,
				"qty": qty,
				"uom": None,
				"conversion_factor": None,
				"fg_item": row.item_code,
				"fg_item_qty": qty,
				"bom": row.bom,
			}
		else:
			item["item_code"] = row.item_code
		doc.append("items", item)
	if order_type == "Subcontracting Order":
		doc.set_service_items_for_finished_goods()
		for item, row in zip(doc.items, rows, strict=True):
			# the job work rate is per unit of the sheet; the line is in service units
			item.rate = flt(row.row.qty) * flt(row.row.rate) / flt(item.qty)
	# taxes as the order form picks them: the party's template, else the company default
	doc.set_missing_values()
	TaxService(doc).set_taxes()
	doc.insert()
	doc.submit()
	add_old_number(doc, first.order_no)
	if order_type != "Subcontracting Order":
		return None, doc.name
	from erpnext.buying.doctype.purchase_order.mapper import get_mapped_subcontracting_order

	# Buying Settings can have the purchase order's submit make it as a draft
	if draft := frappe.db.get_value("Subcontracting Order", {"purchase_order": doc.name, "docstatus": 0}):
		order = frappe.get_doc("Subcontracting Order", draft)
	else:
		order = get_mapped_subcontracting_order(doc.name)
		order.insert()
	order.submit()
	add_old_number(order, first.order_no)
	return doc.name, order.name


def add_old_number(doc, order_no: str | None):
	if order_no:
		doc.add_comment("Comment", _("Order {0} in your old books.").format(order_no))


def get_default_warehouse(onboarding) -> str | None:
	"""The one warehouse of ours, not in transit, else the company's default warehouse."""
	receiving = set(get_receiving_warehouses(onboarding))
	ours = [
		row.warehouse
		for row in onboarding.warehouses
		if row.kind == "Our warehouse" and row.warehouse in receiving
	]
	if len(ours) == 1:
		return ours[0]
	return frappe.get_cached_value("Company", onboarding.company, "default_warehouse")


def get_made_orders(onboarding, order_type: str) -> dict:
	"""The made orders by upload, each with its rows and what was done against it since, and the rows
	undone by upload."""
	check_order_type(order_type)
	rows = [row for row in onboarding.open_orders if row.order_type == order_type and row.status == "Made"]
	progress = get_progress(order_type, {row.document for row in rows})
	groups = {}
	for row in rows:
		group = groups.setdefault(
			row.source or "", {"source": row.source or "", "orders": set(), "qty": 0, "rows": []}
		)
		group["orders"].add(row.document)
		group["qty"] += flt(row.qty)
		group["rows"].append(
			{
				"name": row.name,
				"document": row.document,
				"order_no": row.order_no,
				"party_name": row.party_name,
				"item_code": row.item_code,
				"qty": flt(row.qty),
				"uom": row.uom,
				"done_since": progress.get(row.document, 0),
			}
		)
	for group in groups.values():
		group["posted_on"] = min(
			frappe.get_all(order_type, filters={"name": ("in", list(group["orders"]))}, pluck="creation")
			or [None]
		)
		group["orders"] = len(group["orders"])
	undone = {}
	for row in onboarding.undone_orders:
		if row.order_type == order_type:
			group = undone.setdefault(row.source or "", {"source": row.source or "", "count": 0, "qty": 0})
			group["count"] += 1
			group["qty"] += flt(row.qty)
	return {"groups": list(groups.values()), "undone": list(undone.values())}


def get_progress(order_type: str, names: set) -> dict[str, float]:
	"""By order, what was delivered, received or made against it since."""
	if not names:
		return {}
	field = DONE_FIELDS[order_type][0]
	return {
		row.name: flt(row.get(field))
		for row in frappe.get_all(order_type, filters={"name": ("in", list(names))}, fields=["name", field])
	}


def get_started(order_type: str, rows: list) -> set[str]:
	"""The orders of these rows with anything done against them since: delivered, received, made,
	billed, paid in advance or material sent. They can't be undone."""
	started = get_done(order_type, {row.document for row in rows})
	if order_type == "Subcontracting Order":
		started |= set(
			frappe.get_all(
				"Subcontracting Order Supplied Item",
				filters={"parent": ("in", [row.document for row in rows]), "supplied_qty": (">", 0)},
				pluck="parent",
			)
		)
		billed = get_done("Purchase Order", {row.purchase_order for row in rows if row.purchase_order})
		started |= {row.document for row in rows if row.purchase_order in billed}
	return started


def get_done(doctype: str, names: set) -> set[str]:
	if not names:
		return set()
	fields = DONE_FIELDS[doctype]
	return {
		order.name
		for order in frappe.get_all(doctype, filters={"name": ("in", list(names))}, fields=["name", *fields])
		if any(flt(order.get(field)) for field in fields)
	}


def undo_open_orders(
	onboarding, order_type: str, source: str | None, documents: list[str] | None = None
) -> frappe._dict:
	"""Cancel the orders made from this upload, or only those named, and keep their rows as undone.
	Refused for an order with anything delivered, received or made against it."""
	check_order_type(order_type)
	rows = [
		row
		for row in onboarding.open_orders
		if row.order_type == order_type
		and row.status == "Made"
		and (row.source or "") == (source or "")
		and (documents is None or row.document in documents)
	]
	if not rows:
		frappe.throw(_("There is nothing made to undo."), title=_("Nothing to undo"))
	names = {row.document for row in rows}
	done = get_started(order_type, rows)
	if done:
		frappe.throw(
			count_text(
				len(done),
				_(
					"1 order already has deliveries, receipts, production, invoices or payments. Cancel those first."
				),
				_(
					"{0} orders already have deliveries, receipts, production, invoices or payments. Cancel those first."
				),
			),
			title=_("Can't undo"),
		)
	for name in sorted(names):
		cancel_doc(order_type, name)
	for name in sorted({row.purchase_order for row in rows if row.purchase_order}):
		cancel_doc("Purchase Order", name)
	for row in rows:
		onboarding.append(
			"undone_orders",
			{field: row.get(field) for field in (*FIELDS, "order_type", "item_code", "source")}
			| {"status": "Draft"},
		)
	onboarding.set("open_orders", [row for row in onboarding.open_orders if row not in rows])
	onboarding.flags.posting = True
	onboarding.save()
	return frappe._dict(removed=len(names), kept_cancelled=pop_kept_reasons())


@frappe.whitelist()
def download_open_orders(company: str, order_type: str, undone_from: str | None = None):
	"""An open orders sheet with the drafts of this type entered. With undone_from, the rows undone
	from that source instead, to fix and upload again."""
	check_order_type(order_type)
	onboarding = frappe.get_doc("Company Setup", company)
	onboarding.check_permission("read")
	if undone_from is not None:
		rows = [
			row
			for row in onboarding.undone_orders
			if row.order_type == order_type and (row.source or "") == undone_from
		]
	else:
		rows = get_drafts(onboarding, order_type)
	download_sheet(
		f"{frappe.scrub(company)}_{frappe.scrub(order_type)}s",
		[
			list(SHEET_COLUMNS.values()),
			*([row.get(field) for field in SHEET_COLUMNS] for row in rows),
		],
		_(order_type),
	)


def get_orders_status(onboarding, order_type: str) -> frappe._dict:
	return get_cached(
		onboarding, f"orders_status_{order_type}", lambda doc: read_orders_status(doc, order_type)
	)


def read_orders_status(onboarding, order_type: str) -> frappe._dict:
	rows = [row for row in onboarding.open_orders if row.order_type == order_type]
	made = [row for row in rows if row.status == "Made"]
	return frappe._dict(
		drafts=sum(1 for row in rows if row.status != "Made"),
		orders=len({row.document for row in made}),
		quantities=get_quantities(
			[
				frappe._dict(row=row, stock_uom=frappe.get_cached_value("Item", row.item_code, "stock_uom"))
				for row in made
			]
		),
		parties=sorted({row.party_name for row in made if row.party_name}),
	)


def get_boms_status(company: str) -> frappe._dict:
	"""BOMs of the company, and the items it makes that have none."""
	job_work_boms = set(frappe.get_all("Subcontracting BOM", pluck="finished_good_bom"))
	boms = sum(
		1
		for bom in frappe.get_all(
			"BOM", {"company": company, "docstatus": 1, "is_active": 1}, ["name", "is_default"]
		)
		if bom.is_default or bom.name not in job_work_boms
	)
	if not boms:
		# items are shared by every company, so one with no BOMs is not told about another's items
		return frappe._dict(boms=0, without_bom=[])
	with_bom = set(
		frappe.get_all(
			"BOM", {"company": company, "docstatus": 1, "is_active": 1, "is_default": 1}, pluck="item"
		)
	)
	made = frappe.get_all(
		"Item",
		filters={"disabled": 0, "is_stock_item": 1, "default_material_request_type": "Manufacture"},
		fields=["name", "item_name", "item_group", "stock_uom"],
	)
	return frappe._dict(boms=boms, without_bom=[item for item in made if item.name not in with_bom])


def mark_items_subcontracted(onboarding) -> list[str]:
	"""Mark the items of the draft subcontracting orders that are not yet subcontracted."""
	failing = get_failing_rows("Subcontracting Order", prepare_orders(onboarding, "Subcontracting Order"))
	items = sorted({row.item_code for row in failing["not_subcontracted"]})
	for name in items:
		item = frappe.get_doc("Item", name)
		item.is_sub_contracted_item = 1
		item.save()
	return items


def get_job_work_bom(onboarding, item_code: str) -> dict:
	"""For the job-work BOM dialog: the item's default BOM materials, each with whether the job
	worker holds it, and the service item and finished goods per service unit to start with."""
	bom = get_default_bom(item_code)
	if not bom:
		return {"bom": None}
	rows = [row for row in prepare_orders(onboarding, "Subcontracting Order") if row.item_code == item_code]
	short = {material for row in rows for material in row.short_material or []}
	current = frappe.db.get_value(
		"Subcontracting BOM",
		{"finished_good": item_code, "is_active": 1},
		["service_item", "finished_good_qty", "service_item_qty"],
		as_dict=True,
	)
	return {
		"bom": bom,
		"materials": [
			{
				"item_code": line.item_code,
				"item_name": line.item_name,
				"qty": line.qty,
				"uom": line.uom,
				"held": line.item_code not in short,
			}
			for line in frappe.get_doc("BOM", bom).items
		],
		"service_item": (current and current.service_item)
		or next((row.service_item for row in rows if row.service_item), None),
		"finished_good_qty": flt(current.finished_good_qty) / flt(current.service_item_qty)
		if current and flt(current.service_item_qty)
		else 1,
	}


def set_job_work_bom(
	onboarding, item_code: str, materials: list[str], service_item: str, finished_good_qty: float
) -> dict:
	"""Point the item's active Subcontracting BOM at a copy of its default BOM with only these
	materials, made only when its current BOM has other materials. Returns the BOM and whether the
	job worker still lacks material."""
	if flt(finished_good_qty) <= 0:
		frappe.throw(
			_("Finished goods per service unit must be more than 0."), title=_("Wrong finished goods")
		)
	default_bom = get_default_bom(item_code)
	if not default_bom:
		frappe.throw(_("Item {0} has no default BOM.").format(item_code), title=_("No BOM"))
	name = frappe.db.get_value("Subcontracting BOM", {"finished_good": item_code, "is_active": 1})
	subcontracting_bom = (
		frappe.get_doc("Subcontracting BOM", name) if name else frappe.new_doc("Subcontracting BOM")
	)
	bom_name = subcontracting_bom.finished_good_bom
	if not bom_name or set(get_bom_materials(bom_name)) != set(materials):
		bom = frappe.copy_doc(frappe.get_doc("BOM", default_bom))
		bom.items = [line for line in bom.items if line.item_code in set(materials)]
		if not bom.items:
			frappe.throw(_("Tick at least one material."), title=_("No materials"))
		for idx, line in enumerate(bom.items, 1):
			line.idx = idx
		bom.is_default = 0
		bom.insert()
		bom.submit()
		bom_name = bom.name
	subcontracting_bom.update(
		{
			"finished_good": item_code,
			"finished_good_bom": bom_name,
			"finished_good_qty": flt(finished_good_qty),
			"finished_good_uom": frappe.get_cached_value("Item", item_code, "stock_uom"),
			"service_item": service_item,
			"service_item_qty": 1,
			"service_item_uom": frappe.get_cached_value("Item", service_item, "stock_uom"),
		}
	)
	subcontracting_bom.save()
	short = any(
		row.short_material
		for row in prepare_orders(onboarding, "Subcontracting Order")
		if row.item_code == item_code
	)
	return {"bom": bom_name, "short": short}


def get_bom_materials(bom: str) -> list[str]:
	return frappe.get_all("BOM Item", {"parent": bom, "parenttype": "BOM"}, pluck="item_code")
