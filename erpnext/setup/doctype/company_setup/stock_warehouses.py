# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.utils import cstr, flt

from erpnext.setup.doctype.company_setup.bank_accounts import get_balances
from erpnext.setup.doctype.company_setup.opening_invoices import (
	get_day_before,
	get_exact_key,
	get_similar_key,
	make_party,
)
from erpnext.setup.doctype.company_setup.stock_sheet import (
	JOB_WORKER,
	KINDS,
	WORK_IN_PROGRESS,
	get_company_warehouses,
	get_stock_lines,
	get_warehouse_index,
	match_warehouse,
)
from erpnext.stock import get_warehouse_account_map


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
