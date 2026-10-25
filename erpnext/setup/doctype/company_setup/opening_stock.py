# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Stock: the items and warehouses on the stock sheets, and the opening stock behind the trial
balance's stock lines.

Each upload posts one Opening Stock reconciliation per warehouse, dated the day before migration,
against Temporary Opening. Once later entries exist a warehouse can't be undone, so wrong rows are
corrected with a Stock Reconciliation on the same day, again against Temporary Opening. What is left
between the stock posted and the trial balance can go to an Opening Stock Difference ledger.
"""

import difflib
import re
from itertools import groupby

import frappe
from frappe import _
from frappe.core.doctype.role.role import get_users
from frappe.query_builder.functions import Max, Min
from frappe.utils import cint, cstr, flt, formatdate, get_last_day, getdate
from frappe.utils.background_jobs import is_job_enqueued

from erpnext.accounts.utils import get_stock_accounts
from erpnext.setup.doctype.company_setup.bank_accounts import get_balances
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
	add_account,
	append_lines,
	count_text,
	ensure_fiscal_year,
	get_cached,
	get_day_before,
	get_default_dimensions,
	get_exact_key,
	get_posting_blocker,
	get_posting_fix,
	get_sheet_date,
	get_similar_key,
	get_temporary_account,
	make_party,
)
from erpnext.setup.doctype.company_setup.posted_entries import (
	cancel_doc,
	pop_kept_reasons,
	run_posting_job,
	run_undo_job,
)
from erpnext.stock import get_warehouse_account_map
from erpnext.stock.doctype.item.item import get_uom_conv_factor
from erpnext.stock.doctype.serial_no.serial_no import get_available_serial_nos
from erpnext.stock.doctype.stock_closing_entry.stock_closing_entry import (
	get_closing_entry_for_closed_period,
)
from erpnext.stock.serial_batch_identity import SerialBatchIdentity
from erpnext.stock.stock_ledger import is_negative_stock_allowed
from erpnext.stock.utils import get_combine_datetime
from erpnext.stock.valuation_adjustment import get_adjusted_until

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


def get_warehouse_rows(onboarding) -> dict:
	"""What the Warehouses window shows: one row per warehouse of the company, then the warehouses
	on the stock sheets that are not in ERPNext, each with its kind, job worker and stock line."""
	company = onboarding.company
	saved = {row.warehouse or get_exact_key(row.warehouse_name): row for row in onboarding.warehouses}
	index = get_warehouse_index(onboarding)
	on_sheet, new_names = {}, {}
	for row in onboarding.opening_stock:
		warehouse = match_warehouse(index, row)
		key = warehouse or get_exact_key(row.warehouse_name)
		on_sheet[key] = on_sheet.get(key, 0) + 1
		if not warehouse and row.warehouse_name:
			new_names.setdefault(key, row.warehouse_name)
	lines = get_stock_lines(onboarding)
	with_entries = get_warehouses_with_entries(company)
	suppliers = {get_exact_key(name): name for name in frappe.get_all("Supplier", pluck="name")}
	rows = []
	for warehouse in get_company_warehouses(company):
		row = saved.get(warehouse.name) or frappe._dict()
		kind = row.kind or guess_kind(warehouse.warehouse_name)
		rows.append(
			{
				"warehouse": warehouse.name,
				"warehouse_name": warehouse.warehouse_name,
				"kind": kind,
				"job_worker": row.job_worker
				or (kind == "Job worker" and guess_job_worker(warehouse.warehouse_name))
				or None,
				"account": warehouse.account or guess_stock_line(kind, warehouse.warehouse_name, lines),
				"rows_on_sheet": on_sheet.get(warehouse.name, 0),
				"locked": warehouse.name in with_entries,
			}
		)
	for key, name in new_names.items():
		row = saved.get(key) or frappe._dict()
		kind = row.kind or guess_kind(name)
		rows.append(
			{
				"warehouse": None,
				"warehouse_name": name,
				"kind": kind,
				"job_worker": row.job_worker or (kind == "Job worker" and guess_job_worker(name)) or None,
				"account": row.account or guess_stock_line(kind, name, lines),
				"rows_on_sheet": on_sheet.get(key, 0),
				"locked": False,
			}
		)
	for row in rows:
		row["new_job_worker"] = bool(row["job_worker"]) and get_exact_key(row["job_worker"]) not in suppliers
	return {
		"rows": rows,
		"new_warehouses": len(new_names),
		"new_job_workers": sorted({row["job_worker"] for row in rows if row["new_job_worker"]}),
		"stock_lines": list(lines),
		"can_create_supplier": frappe.has_permission("Supplier", "create"),
		"can_create_warehouse": frappe.has_permission("Warehouse", "create"),
	}


def guess_kind(name: str) -> str:
	key = get_similar_key(name)
	if JOB_WORKER.search(key):
		return "Job worker"
	if WORK_IN_PROGRESS.search(key):
		return "Work in progress"
	return "Our warehouse"


def guess_job_worker(name: str) -> str:
	"""A job worker warehouse is usually named after the job worker."""
	return re.sub(r"\s+-\s+\S+$", "", cstr(name)).strip()


def guess_stock_line(kind: str, name: str, lines: dict[str, float]) -> str | None:
	"""The trial balance stock line a warehouse of this kind posts to, when one is clear: the line of
	its own name, the job worker or work in progress line, or the one line left for our warehouses."""
	names = {
		account: frappe.get_cached_value("Account", account, "account_name") for account in lines if account
	}
	same = [
		account for account, line_name in names.items() if get_similar_key(line_name) == get_similar_key(name)
	]
	if same:
		return same[0]
	pattern = {"Job worker": JOB_WORKER, "Work in progress": WORK_IN_PROGRESS}.get(kind)
	of_kind = {
		account
		for account, line_name in names.items()
		if pattern and pattern.search(get_similar_key(line_name))
	}
	if kind != "Our warehouse":
		return next(iter(of_kind)) if len(of_kind) == 1 else None
	ours = [
		account
		for account, line_name in names.items()
		if not JOB_WORKER.search(get_similar_key(line_name))
		and not WORK_IN_PROGRESS.search(get_similar_key(line_name))
	]
	return ours[0] if len(ours) == 1 else None


def get_warehouses_with_entries(company: str) -> set[str]:
	"""Warehouses with stock entries: their stock line can't move, the old entries stay on the old one."""
	sle = frappe.qb.DocType("Stock Ledger Entry")
	return {
		row[0]
		for row in frappe.qb.from_(sle)
		.select(sle.warehouse)
		.distinct()
		.where((sle.company == company) & (sle.is_cancelled == 0))
		.run()
	}


def set_line_on_warehouses(onboarding, account: str):
	"""Point the warehouses of the Warehouses window that have no stock line, and whose kind or name
	goes with this new stock line, at it."""
	lines = get_stock_lines(onboarding)
	with_entries = get_warehouses_with_entries(onboarding.company)
	for row in onboarding.warehouses:
		if not row.warehouse or row.warehouse in with_entries:
			continue
		if frappe.db.get_value("Warehouse", row.warehouse, "account"):
			continue
		if guess_stock_line(row.kind, row.warehouse_name, lines) == account:
			doc = frappe.get_doc("Warehouse", row.warehouse)
			doc.account = account
			doc.save()
			row.account = account


def get_movable_lines(onboarding) -> list[str]:
	"""Work in progress and job worker stock lines with no stock on them: they can go back to
	Opening balances as plain balances."""
	lines = [account for account in get_stock_lines(onboarding) if account]
	if not lines:
		return []
	books = get_balances(onboarding.company, lines, get_day_before(onboarding))
	movable = []
	for account in lines:
		name, root_type = frappe.get_cached_value("Account", account, ["account_name", "root_type"])
		key = get_similar_key(name)
		if (
			root_type == "Asset"
			and (WORK_IN_PROGRESS.search(key) or JOB_WORKER.search(key))
			and not flt(books.get(account), 2)
		):
			movable.append(account)
	return movable


def clear_line_on_warehouses(onboarding, account: str):
	"""Take a stock line off the warehouses with no entries that post to it."""
	with_entries = get_warehouses_with_entries(onboarding.company)
	for warehouse in frappe.get_all(
		"Warehouse", filters={"company": onboarding.company, "account": account}, pluck="name"
	):
		if warehouse not in with_entries:
			doc = frappe.get_doc("Warehouse", warehouse)
			doc.account = None
			doc.save()
	for row in onboarding.warehouses:
		if row.account == account and row.warehouse not in with_entries:
			row.account = None


def get_wrong_lines(onboarding, prepared: list, lines: dict) -> dict:
	"""Draft warehouses whose stock would post to another trial balance line than their own: the
	line set in Warehouses or the one their kind and name point to, or no line of the trial balance."""
	if not lines:
		return {}
	accounts = get_warehouse_account_map(onboarding.company)
	saved = {row.warehouse: row for row in onboarding.warehouses if row.warehouse}
	wrong = {}
	for row in prepared:
		if not row.warehouse:
			continue
		if row.warehouse not in wrong:
			account = (accounts.get(row.warehouse) or frappe._dict()).account
			own = saved.get(row.warehouse) or frappe._dict()
			name = own.warehouse_name or frappe.get_cached_value("Warehouse", row.warehouse, "warehouse_name")
			line = own.account or guess_stock_line(own.kind or guess_kind(name), name, lines)
			if (line and line != account) or ("" not in lines and account not in lines):
				wrong[row.warehouse] = {
					"warehouse": row.warehouse,
					"account": account,
					"line": line,
					"rows": [],
				}
			else:
				wrong[row.warehouse] = None
		if wrong[row.warehouse]:
			wrong[row.warehouse]["rows"].append(row.row.idx)
	return {warehouse: value for warehouse, value in wrong.items() if value}


def save_warehouses(onboarding, rows: list[dict]):
	"""Keep the rows of the Warehouses window, make the warehouses not in ERPNext, the job workers
	who are not suppliers yet, and set each warehouse's stock line while it has no entries."""
	company = onboarding.company
	rows = [frappe._dict(row) for row in rows if cstr(row.get("warehouse_name")).strip()]
	for row in rows:
		if row.kind not in KINDS:
			frappe.throw(
				_("Pick a kind for {0}.").format(frappe.bold(row.warehouse_name)), title=_("Kind missing")
			)
		if row.kind == "Job worker" and not cstr(row.job_worker).strip():
			frappe.throw(
				_("Name the job worker of {0}.").format(frappe.bold(row.warehouse_name)),
				title=_("Job worker missing"),
			)
	suppliers = {get_exact_key(name): name for name in frappe.get_all("Supplier", pluck="name")}
	new_suppliers = sorted(
		{
			" ".join(cstr(row.job_worker).split())
			for row in rows
			if row.kind == "Job worker" and get_exact_key(row.job_worker) not in suppliers
		}
	)
	if new_suppliers and not frappe.has_permission("Supplier", "create"):
		frappe.throw(
			_("You can't add a Supplier, so {0} can't be made. Ask someone who can.").format(
				", ".join(new_suppliers)
			),
			frappe.PermissionError,
			title=_("Not permitted"),
		)
	for name in new_suppliers:
		supplier = make_party("Supplier", name, company)
		supplier.insert()
		suppliers[get_exact_key(name)] = supplier.name
	with_entries = get_warehouses_with_entries(company)
	parent = frappe.db.get_value(
		"Warehouse", {"company": company, "is_group": 1, "parent_warehouse": ("is", "not set")}
	)
	onboarding.warehouses = []
	for row in rows:
		warehouse = row.warehouse and frappe.db.exists(
			"Warehouse", {"name": row.warehouse, "company": company}
		)
		if not warehouse:
			disabled = frappe.db.get_value(
				"Warehouse",
				{
					"company": company,
					"warehouse_name": " ".join(cstr(row.warehouse_name).split()),
					"disabled": 1,
				},
			)
			if disabled:
				frappe.throw(
					_("{0} is disabled. Enable it in Warehouse.").format(frappe.bold(disabled)),
					title=_("Warehouse disabled"),
				)
			warehouse = (
				frappe.get_doc(
					{
						"doctype": "Warehouse",
						"warehouse_name": " ".join(cstr(row.warehouse_name).split()),
						"company": company,
						"parent_warehouse": parent,
						"account": row.account,
					}
				)
				.insert()
				.name
			)
		elif row.account and warehouse not in with_entries:
			if frappe.db.get_value("Warehouse", warehouse, "account") != row.account:
				doc = frappe.get_doc("Warehouse", warehouse)
				doc.account = row.account
				doc.save()
		onboarding.append(
			"warehouses",
			{
				"warehouse_name": row.warehouse_name,
				"warehouse": warehouse,
				"kind": row.kind,
				"job_worker": row.job_worker if row.kind == "Job worker" else None,
				"supplier": suppliers.get(get_exact_key(row.job_worker))
				if row.kind == "Job worker"
				else None,
				"account": row.account,
			},
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


def get_serials(row) -> list[str]:
	return [serial for serial in re.split(r"[\s,]+", cstr(row.serial_no)) if serial]


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


# Stock difference and Check your books


def get_stock_match(onboarding) -> dict | None:
	"""The stock in the books on the day before against the trial balance's stock lines, each line on
	its own, with the difference posted. None without stock lines or posted stock."""
	return get_cached(onboarding, "stock_match", read_stock_match)


def get_line_key(account: str | None, lines: dict) -> str:
	"""The trial balance line a stock account's balance counts against: its own line, else the line
	with no account ("")."""
	return account if account and account in lines else ""


def read_stock_match(onboarding) -> dict | None:
	lines = get_stock_lines(onboarding)
	posted = [row for row in onboarding.opening_stock if row.status == "Posted"]
	if not onboarding.migration_date or not (lines or posted):
		return None
	company = onboarding.company
	day_before = get_day_before(onboarding)
	accounts = get_stock_accounts(company)
	books, differences = {}, {}
	for account, amount in get_balances(company, accounts, day_before).items():
		key = get_line_key(account, lines)
		books[key] = flt(books.get(key)) + flt(amount)
	for account, amount in (frappe.parse_json(onboarding.stock_difference) or {}).items():
		key = get_line_key(account, lines)
		differences[key] = flt(differences.get(key)) + flt(amount)
	warehouse_lines = get_warehouse_account_map(company)
	by_warehouse = {}
	for row in posted:
		account = (warehouse_lines.get(row.warehouse) or frappe._dict()).account
		warehouse = by_warehouse.setdefault(
			row.warehouse,
			{
				"warehouse": row.warehouse,
				"rows": 0,
				"posted": 0,
				"account": account,
				"line": get_line_key(account, lines),
			},
		)
		warehouse["rows"] += 1
		warehouse["posted"] = flt(warehouse["posted"] + flt(row.amount), 2)
	rows = []
	for account in sorted(
		set(lines) | {key for key, book in books.items() if flt(book, 2)} | set(differences)
	):
		line = flt(lines.get(account))
		book = flt(books.get(account), 2)
		rows.append(
			{
				"account": account,
				"line": line,
				"books": book,
				"difference_posted": flt(differences.get(account)),
				"difference": flt(line - book - flt(differences.get(account)), 2),
				"nothing_posted": bool(line and not book),
			}
		)
	total_line = flt(sum(lines.values()), 2)
	total_books = flt(sum(books.values()), 2)
	total_difference = flt(sum(differences.values()), 2)
	return {
		"lines": rows,
		"warehouses": list(by_warehouse.values()),
		"line": total_line,
		"books": total_books,
		"difference_posted": total_difference,
		"off": flt(total_line - total_books - total_difference, 2),
		"lines_off": [row["account"] for row in rows if abs(row["difference"]) >= 1],
		"matches": all(abs(row["difference"]) < 1 for row in rows),
	}


def get_difference_lines(onboarding) -> list[dict]:
	"""The stock lines off by more than ₹1 that Post the difference would post, with the share."""
	match = get_stock_match(onboarding)
	if not match:
		return []
	return [
		{
			"account": row["account"],
			"books": row["books"],
			"line": row["line"],
			"difference": row["difference"],
			"percent": flt(abs(row["difference"]) * 100 / abs(row["line"]), 1) if row["line"] else None,
			"nothing_posted": row["nothing_posted"],
			"big": abs(row["difference"]) > BIG_GAP * abs(row["line"]),
		}
		for row in match["lines"]
		if abs(row["difference"]) >= 1
	]


def get_difference_blocker(onboarding) -> str | None:
	"""Why the stock difference can't be posted, or None."""
	if any(row.status != "Posted" for row in onboarding.opening_stock):
		return _("Post every stock row first.")
	lines = get_difference_lines(onboarding)
	if not lines:
		return _("Your stock matches your trial balance.")
	if abs(sum(line["difference"] for line in lines)) < 1:
		return _(
			"The total matches, but stock is on the wrong accounts. Undo the warehouses on the wrong account, set their stock account in Warehouses, and post them again."
		)
	empty = next((line for line in lines if line["nothing_posted"]), None)
	if empty:
		return _("{0} has {1} in your trial balance and no stock posted. Upload its rows.").format(
			frappe.get_cached_value("Account", empty["account"], "account_name")
			if empty["account"]
			else _("Stock"),
			frappe.format_value(empty["line"], {"fieldtype": "Currency"}),
		)
	return None


def get_empty_warehouses(onboarding) -> list[str]:
	"""Warehouses of the Warehouses window with no stock posted, on a line Post the difference would
	post: their stock may still be to upload."""
	lines = get_stock_lines(onboarding)
	off = {line["account"] for line in get_difference_lines(onboarding)}
	posted = {row.warehouse for row in onboarding.opening_stock if row.status == "Posted"}
	accounts = get_warehouse_account_map(onboarding.company)
	# the warehouses every company gets, when they never held stock, are not missing an upload
	standard = {_("Stores"), _("Work In Progress"), _("Finished Goods"), _("Goods In Transit")}
	held = get_warehouses_with_entries(onboarding.company)
	return [
		row.warehouse
		for row in onboarding.warehouses
		if row.warehouse
		and row.warehouse not in posted
		and not (row.warehouse_name in standard and row.warehouse not in held)
		and get_line_key((accounts.get(row.warehouse) or frappe._dict()).account, lines) in off
	]


def has_profit_and_loss_rows(onboarding) -> bool:
	accounts = [row.account for row in onboarding.opening_balances if row.account]
	return bool(
		accounts
		and frappe.db.exists(
			"Account", {"name": ("in", accounts), "root_type": ("in", ("Income", "Expense"))}
		)
	)


def get_difference_account(onboarding, create: bool = False) -> str | None:
	"""The Opening Stock Difference ledger: in P&L when the trial balance has P&L rows, else with the
	capital. Made when missing; refused when one of that name is on the other side."""
	company = onboarding.company
	profit_and_loss = has_profit_and_loss_rows(onboarding)
	account = frappe.db.get_value(
		"Account",
		{"company": company, "account_name": _(DIFFERENCE_ACCOUNT), "is_group": 0},
		["name", "report_type"],
		as_dict=True,
	)
	if account:
		if (account.report_type == "Profit and Loss") != profit_and_loss:
			frappe.throw(
				_("{0} is a {1} account. Rename it, or pick the other side.").format(
					frappe.bold(account.name), _(account.report_type)
				),
				title=_("Wrong side"),
			)
		return account.name
	if not create:
		return None
	if profit_and_loss:
		return add_account(company, _(DIFFERENCE_ACCOUNT), "", "Expense")
	return add_account(company, _(DIFFERENCE_ACCOUNT), "", "Equity", parent=get_capital_group(onboarding))


def get_capital_group(onboarding) -> str | None:
	"""Where the difference goes with the capital: the group of the trial balance's capital or reserves
	ledger, else the top Equity group, else the top Liability group (the India chart has no Equity root)."""
	company = onboarding.company
	accounts = [row.account for row in onboarding.opening_balances if row.account]
	for account in frappe.get_all(
		"Account",
		filters={"name": ("in", accounts or [""])},
		fields=["name", "account_name", "root_type", "parent_account"],
	):
		if account.root_type == "Equity" or re.search(
			r"\b(capital|reserves?)\b", get_similar_key(account.account_name)
		):
			if account.parent_account:
				return account.parent_account
	for root_type in ("Equity", "Liability"):
		group = frappe.db.get_value(
			"Account",
			{"company": company, "root_type": root_type, "is_group": 1, "parent_account": ("is", "not set")},
		)
		if group:
			return group
	return None


def post_stock_difference(onboarding, checked: bool = False) -> str:
	"""One entry on the day before between Opening Stock Difference and Temporary Opening for what the
	stock lines are off by. With the capital it is an opening entry; in P&L an ordinary one. A gap over
	BIG_GAP of its line posts only when checked: the user ticked that no warehouse is missing."""
	blocker = get_difference_blocker(onboarding)
	if blocker:
		frappe.throw(blocker, title=_("Can't post the difference"))
	lines = get_difference_lines(onboarding)
	big = next((line for line in lines if line["big"]), None)
	if big and not checked:
		frappe.throw(
			_(
				"{0} is a big gap on {1}. A warehouse may be missing: upload it, or tick that none is missing."
			).format(
				frappe.format_value(abs(big["difference"]), {"fieldtype": "Currency"}),
				frappe.get_cached_value("Account", big["account"], "account_name")
				if big["account"]
				else _("Stock"),
			),
			title=_("Big gap"),
		)
	day_before = get_day_before(onboarding)
	profit_and_loss = has_profit_and_loss_rows(onboarding)
	blocker = get_posting_blocker(onboarding.company, day_before, profit_and_loss=profit_and_loss)
	if blocker:
		frappe.throw(blocker, title=_("Can't post the difference"))
	account = get_difference_account(onboarding, create=True)
	temporary = get_temporary_account(onboarding.company, create=True)
	amount = flt(sum(line["difference"] for line in lines), 2)
	dimensions = get_default_dimensions(onboarding.company)
	cost_center = frappe.get_cached_value("Company", onboarding.company, "cost_center")
	je = frappe.new_doc("Journal Entry")
	je.update(
		{
			"voucher_type": "Journal Entry" if profit_and_loss else "Opening Entry",
			"is_opening": "No" if profit_and_loss else "Yes",
			"company": onboarding.company,
			"posting_date": day_before,
			"user_remark": _("Opening stock difference from Company Setup"),
		}
	)
	append_lines(je, account, amount, temporary, dimensions, cost_center)
	je.insert()
	je.submit()
	onboarding.stock_difference = {line["account"]: line["difference"] for line in lines}
	onboarding.stock_difference_entry = je.name
	return je.name


def set_stock_defaults(company: str) -> list[str]:
	"""A default inventory account that is our own stock, not a job worker or work in progress line,
	and a stock adjustment account, which stock entries need. Returns the accounts added."""
	values, added = {}, []
	inventory = frappe.db.get_value("Company", company, "default_inventory_account")
	if (
		not inventory
		or JOB_WORKER.search(get_similar_key(inventory))
		or WORK_IN_PROGRESS.search(get_similar_key(inventory))
	):
		values["default_inventory_account"] = next(
			(
				account.name
				for account in frappe.get_all(
					"Account",
					filters={"company": company, "account_type": "Stock", "is_group": 0, "disabled": 0},
					fields=["name", "account_name"],
					order_by="lft",
				)
				if not JOB_WORKER.search(get_similar_key(account.account_name))
				and not WORK_IN_PROGRESS.search(get_similar_key(account.account_name))
			),
			inventory,
		)
	has_stock = values.get("default_inventory_account") or inventory
	if has_stock and not frappe.db.get_value("Company", company, "stock_adjustment_account"):
		values["stock_adjustment_account"] = frappe.db.get_value(
			"Account",
			{"company": company, "account_type": "Stock Adjustment", "is_group": 0, "disabled": 0},
			order_by="lft",
		)
		if not values["stock_adjustment_account"]:
			values["stock_adjustment_account"] = add_account(
				company, _("Stock Adjustment"), "Stock Adjustment", "Expense"
			)
			added.append(values["stock_adjustment_account"])
	if values:
		frappe.has_permission("Company", "write", doc=company, throw=True)
		frappe.db.set_value("Company", company, values)
	return added


def is_serial_batch_off() -> bool:
	return not cint(frappe.db.get_single_value("Stock Settings", "enable_serial_and_batch_no_for_item"))


def turn_on_serial_and_batch():
	settings = frappe.get_doc("Stock Settings")
	settings.enable_serial_and_batch_no_for_item = 1
	settings.save()


def turn_on_perpetual_inventory(company: str):
	doc = frappe.get_doc("Company", company)
	doc.enable_perpetual_inventory = 1
	doc.save()
	frappe.local.enable_perpetual_inventory = {}
	set_stock_defaults(company)


# Status for the steps


def get_stock_status(onboarding) -> frappe._dict:
	return get_cached(onboarding, "stock_status", read_stock_status)


def read_stock_status(onboarding) -> frappe._dict:
	drafts = [row for row in onboarding.opening_stock if row.status != "Posted"]
	posted = [row for row in onboarding.opening_stock if row.status == "Posted"]
	lines = get_stock_lines(onboarding)
	match = get_stock_match(onboarding)
	return frappe._dict(
		lines=lines,
		line_total=flt(sum(lines.values()), 2),
		drafts=len(drafts),
		draft_sources=sorted({row.source or "" for row in drafts}),
		posted=len(posted),
		posted_sources=len({row.source or "" for row in posted}),
		warehouses=len({row.warehouse for row in posted}),
		off=flt(sum(lines.values()) - sum(flt(row.amount) for row in onboarding.opening_stock), 2)
		- flt(match["difference_posted"] if match else 0),
		matches=bool(match and match["matches"]),
		posting=is_posting_stock(onboarding.company),
	)
