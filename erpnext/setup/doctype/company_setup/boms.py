# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""BOMs: one row per material of each item the company makes. Each item gets one BOM, submitted and
set as its default; an item that has a default BOM already keeps it. Each made BOM can be undone
until Go live, unless a work order or stock entry uses it."""

import frappe
from frappe import _
from frappe.permissions import get_doctype_roles
from frappe.utils import cstr, flt
from frappe.utils.background_jobs import is_job_enqueued

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
from erpnext.setup.doctype.company_setup.opening_invoices import count_text, get_error_text
from erpnext.setup.doctype.company_setup.opening_stock import (
	get_item_index,
	get_unit_factors,
	get_unit_key,
	get_uom_index,
	match_item,
)
from erpnext.setup.doctype.company_setup.posted_entries import (
	cancel_doc,
	pop_kept_reasons,
	run_posting_job,
)

JOB_ID = "company_setup_boms::{0}"
# what a BOM sheet may call each column, the likeliest first. Data Import's BOM sheet names the
# materials' columns "Item Code (items)" and so on.
BOM_COLUMNS = {
	"item_name": (
		"item_you_make",
		"item_to_manufacture",
		"item_to_make",
		"finished_good",
		"finished_item",
		"fg_item",
		"product",
		"parent_item",
		"bom_item",
		"stock_item",
	),
	"bom_qty": ("makes", "quantity_output_qty", "output_qty", "bom_qty", "qty_to_make", "fg_qty"),
	"material_name": (
		"material",
		"item_code_items",
		"item_name_items",
		"raw_material",
		"component",
		"material_item",
		"item_code",
		"item_name",
		"item",
		"particulars",
	),
	"qty": ("qty_items", "qty", "quantity", "material_qty", "consumption"),
	"uom": ("uom_items", "unit", "uom", "units"),
	"rate": ("rate_items", "rate", "price"),
}
FIELDS = ("item_name", "bom_qty", "material_name", "qty", "uom", "rate")
SHEET_COLUMNS = {
	"item_name": "Item you make",
	"bom_qty": "Makes",
	"material_name": "Material",
	"qty": "Qty",
	"uom": "Unit",
	"rate": "Rate",
}


def read_bom_sheet(file_url: str) -> list[dict]:
	"""The rows of a BOM sheet. The item to make may be written once, above or on its first material,
	and holds for the rows below it."""
	rows, keys = read_sheet(file_url)
	start = find_heading(
		keys,
		lambda row: all(find_column(row, BOM_COLUMNS[field]) is not None for field in ("item_name", "qty"))
		and find_column(row, BOM_COLUMNS["material_name"]) is not None,
	)
	if start is None:
		frappe.throw(
			_("The sheet needs an Item you make column, a Material column and a Qty column."),
			title=_("Columns not found"),
		)
	columns = {field: find_column(keys[start], names) for field, names in BOM_COLUMNS.items()}
	result, item, makes = [], None, 1
	for cells in [row.cells for row in rows[start + 1 :]]:
		if is_total_row(cells):
			continue
		if name := cstr(get_cell(cells, columns["item_name"])).strip():
			item, makes = name, get_amount(get_cell(cells, columns["bom_qty"])) or 1
		material = cstr(get_cell(cells, columns["material_name"])).strip()
		if not item or not material:
			continue
		qty_cell = get_cell(cells, columns["qty"])
		qty, unit = get_qty(qty_cell)
		result.append(
			{
				"item_name": item,
				"bom_qty": makes,
				"material_name": material,
				"qty": qty,
				"uom": cstr(get_cell(cells, columns["uom"])).strip() or unit,
				"rate": get_amount(get_cell(cells, columns["rate"])),
			}
		)
	return result


def save_boms(onboarding, rows: list[dict], source: str | None = None):
	"""Replace the draft rows with these. Made rows stay."""
	onboarding.set("boms", [row for row in onboarding.boms if row.status == "Made"])
	for row in rows:
		row = frappe._dict(row)
		item, material = clean(row.item_name), clean(row.material_name)
		if not item and not material:
			continue
		onboarding.append(
			"boms",
			{
				"item_name": item,
				"bom_qty": flt(row.bom_qty) if flt(row.bom_qty) > 0 else 1,
				"material_name": material,
				"qty": flt(row.qty),
				"uom": clean(row.uom),
				"rate": flt(row.rate),
				"source": row.source or source,
				"status": "Draft",
			},
		)
	for row in prepare_boms(onboarding):
		row.row.item_code, row.row.material_code = row.item_code, row.material_code


def merge_upload(onboarding, rows: list[dict]) -> tuple[list[dict], int]:
	"""The drafts to keep with a sheet's rows, and how many it replaces: the sheet's items get the
	sheet's materials."""
	items = {get_key(row.get("item_name")) for row in rows}
	drafts = [row for row in onboarding.boms if row.status != "Made"]
	kept = [row.as_dict() for row in drafts if get_key(row.item_name) not in items]
	return kept + rows, len(drafts) - len(kept)


def clean(value) -> str | None:
	return " ".join(cstr(value).split()) or None


def get_key(value) -> str:
	return " ".join(cstr(value).split()).casefold()


def prepare_boms(onboarding) -> list[frappe._dict]:
	"""Each draft row with its item and material when found, and the material's unit and how many
	stock units one of it is."""
	drafts = [row for row in onboarding.boms if row.status != "Made"]
	names = [frappe._dict(item_name=name) for row in drafts for name in (row.item_name, row.material_name)]
	index, uoms = get_item_index(names), get_uom_index()
	prepared = []
	for row in drafts:
		material = match_item(index, frappe._dict(item_name=row.material_name))[0]
		stock_uom = material and index.details[material].stock_uom
		prepared.append(
			frappe._dict(
				row=row,
				item_code=match_item(index, frappe._dict(item_name=row.item_name))[0],
				material_code=material,
				uom=uoms.by_key.get(get_unit_key(row.uom)) if row.uom else stock_uom,
			)
		)
	factors = get_unit_factors({(row.material_code, row.uom) for row in prepared if row.material_code})
	for row in prepared:
		row.conversion_factor = factors.get((row.material_code, row.uom))
	return prepared


def group_boms(prepared: list) -> list[frappe._dict]:
	"""The rows of each item to make, in the sheet's order."""
	groups = {}
	for row in prepared:
		key = row.item_code or get_key(row.row.item_name)
		group = groups.setdefault(
			key, frappe._dict(item_code=row.item_code, item_name=row.row.item_name, rows=[])
		)
		group.rows.append(row)
	for group in groups.values():
		group.bom_qty = next((flt(row.row.bom_qty) for row in group.rows if flt(row.row.bom_qty) > 0), 1)
	return list(groups.values())


def get_items_with_bom(company: str, items: list[str]) -> set[str]:
	return set(
		frappe.get_all(
			"BOM",
			filters={
				"company": company,
				"item": ("in", items or [""]),
				"docstatus": 1,
				"is_active": 1,
				"is_default": 1,
			},
			pluck="item",
		)
	)


def get_bom_checks(onboarding, prepared: list | None = None) -> dict:
	"""What the BOMs window shows: the drafts, what is made, the reds that stop making, and the items
	kept as they have a BOM already."""
	prepared = prepare_boms(onboarding) if prepared is None else prepared
	groups = group_boms(prepared)
	kept = get_items_with_bom(onboarding.company, [group.item_code for group in groups if group.item_code])
	to_make = [group for group in groups if group.item_code not in kept]
	reds = get_reds([row for group in to_make for row in group.rows])
	blocker = reds[0]["text"] if reds else None
	if not prepared:
		blocker = _("Upload a sheet or add rows to make BOMs.")
	elif not to_make:
		blocker = _("Every item on the sheet has a BOM already.")
	kept_groups = [group for group in groups if group.item_code in kept]
	return {
		"drafts": len(prepared),
		"boms": len(groups),
		"to_make": len(to_make),
		"sources": sorted({row.row.source for row in prepared if row.row.source}),
		"has_rate": any(flt(row.row.rate) for row in prepared),
		"made": len({row.bom for row in onboarding.boms if row.status == "Made"}),
		"reds": reds,
		"kept": {
			"text": count_text(
				len(kept_groups),
				_("{1} has a BOM already: it is kept."),
				_("{0} items have a BOM already: they are kept."),
				kept_groups[0].item_name,
			),
			"rows": [row.row.idx for group in kept_groups for row in group.rows],
		}
		if kept_groups
		else None,
		"failed": [
			{
				"text": _("The BOM of {0} was not made: {1}").format(
					group.item_name, group.rows[0].row.error
				),
				"rows": [row.row.idx for row in group.rows],
			}
			for group in to_make
			if group.rows[0].row.error
		],
		"blocker": blocker,
	}


def get_reds(prepared: list) -> list[dict]:
	"""What stops making the BOMs, in the order to fix it: each with its text, hint and row numbers."""
	checks = {
		"missing_items": lambda row: not row.item_code,
		"missing_materials": lambda row: not row.material_code,
		"no_qty": lambda row: flt(row.row.qty) <= 0,
		"unknown_units": lambda row: row.material_code and not row.conversion_factor,
	}
	reds = []
	for key, check in checks.items():
		if failing := [row for row in prepared if check(row)]:
			reds.append({"key": key, "rows": [row.row.idx for row in failing], **get_red_text(key, failing)})
	return reds


def get_red_text(key: str, failing: list) -> dict:
	if key in ("missing_items", "missing_materials"):
		field = "item_name" if key == "missing_items" else "material_name"
		names = sorted({row.row.get(field) for row in failing})
		listed = ", ".join(names[:5]) + (", …" if len(names) > 5 else "")
		return {
			"text": count_text(
				len(names),
				_("1 item you make is not in ERPNext: {1}")
				if key == "missing_items"
				else _("1 material is not in ERPNext: {1}"),
				_("{0} items you make are not in ERPNext: {1}")
				if key == "missing_items"
				else _("{0} materials are not in ERPNext: {1}"),
				listed,
			),
			"hint": count_text(
				len(names),
				_("Add it in Items, or remove its rows.")
				if key == "missing_items"
				else _("Add it in Items, or remove its row."),
				_("Add them in Items, or remove their rows."),
			),
		}
	if key == "no_qty":
		return {
			"text": count_text(
				len(failing), _("Qty is missing for 1 row."), _("Qty is missing for {0} rows.")
			)
		}
	units = sorted({(row.row.uom, row.material_code) for row in failing})
	if len(units) == 1:
		return {
			"text": _("Unit {0} is not a unit of {1}. Add it to the item, or fix the sheet.").format(
				*units[0]
			)
		}
	return {
		"text": _(
			"These units are not units of their items: {0}. Add them to the items, or fix the sheet."
		).format(", ".join(_("{0} for {1}").format(*unit) for unit in units))
	}


def check_bom_permission():
	"""Making a BOM needs the usual create and submit permission on BOM."""
	if all(frappe.has_permission("BOM", ptype) for ptype in ("create", "submit")):
		return
	roles = [_(role) for role in get_doctype_roles("BOM", "submit") if role != "Administrator"]
	frappe.throw(
		_("To make BOMs, you need the {0} role.").format(" / ".join(roles))
		if roles
		else _("No role can make BOMs."),
		frappe.PermissionError,
		title=_("Not permitted"),
	)


def get_boms_to_make(onboarding) -> list[frappe._dict]:
	"""The BOMs to make, sub-assemblies first. Throws why they can't be made yet."""
	check_bom_permission()
	prepared = prepare_boms(onboarding)
	blocker = get_bom_checks(onboarding, prepared)["blocker"]
	if blocker:
		frappe.throw(blocker, title=_("Can't make BOMs yet"))
	groups = group_boms(prepared)
	kept = get_items_with_bom(onboarding.company, [group.item_code for group in groups])
	return order_sub_assemblies_first([group for group in groups if group.item_code not in kept])


def order_sub_assemblies_first(groups: list) -> list:
	"""An item's BOM after the BOMs of its materials, so it uses them as its sub-assemblies."""
	by_item = {group.item_code: group for group in groups}
	ordered, seen = [], set()

	def add(group):
		if group.item_code in seen:
			return
		seen.add(group.item_code)
		for row in group.rows:
			if row.material_code in by_item:
				add(by_item[row.material_code])
		ordered.append(group)

	for group in groups:
		add(group)
	return ordered


def make_boms(onboarding, groups: list | None = None) -> dict:
	"""Make, submit and set as default a BOM for each item, each in its own savepoint: a failed one
	stays a draft with its error. The rows of items that have a BOM already are dropped."""
	groups = get_boms_to_make(onboarding) if groups is None else groups
	currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
	made, failed = [], 0
	for group in groups:
		savepoint = f"company_setup_bom_{group.rows[0].row.idx}"
		frappe.db.savepoint(savepoint)
		try:
			name = make_bom(onboarding.company, currency, group)
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			frappe.clear_messages()
			failed += 1
			for row in group.rows:
				row.row.error = get_error_text(e)
			continue
		made.append(name)
		for row in group.rows:
			row.row.update(
				{
					"status": "Made",
					"bom": name,
					"error": None,
					"item_code": row.item_code,
					"material_code": row.material_code,
				}
			)
	making = {id(row.row) for group in groups for row in group.rows}
	drafts = [row for row in onboarding.boms if row.status != "Made"]
	kept = [row for row in drafts if id(row) not in making]
	onboarding.set("boms", [row for row in onboarding.boms if row not in kept])
	onboarding.flags.posting = True
	onboarding.save()
	return {"made": len(made), "failed": failed, "kept": len({row.item_name for row in kept})}


def make_bom(company: str, currency: str, group) -> str:
	bom = frappe.new_doc("BOM")
	bom.update(
		{
			"item": group.item_code,
			"company": company,
			"currency": currency,
			"quantity": group.bom_qty,
			"is_active": 1,
			"is_default": 1,
		}
	)
	for row in group.rows:
		bom.append(
			"items",
			{
				"item_code": row.material_code,
				"qty": flt(row.row.qty),
				"uom": row.uom,
				"conversion_factor": row.conversion_factor,
				"rate": flt(row.row.rate),
			},
		)
	bom.insert()
	bom.submit()
	return bom.name


def make_boms_in_background(company: str):
	run_posting_job("company_setup_boms", company, make_boms)


def is_making_boms(company: str) -> bool:
	return is_job_enqueued(JOB_ID.format(company))


def get_made_boms(onboarding) -> dict:
	"""The made BOMs by upload, each with how many work orders and stock entries use it."""
	rows = [row for row in onboarding.boms if row.status == "Made"]
	used = get_bom_use({row.bom for row in rows})
	groups = {}
	for row in rows:
		group = groups.setdefault(row.source or "", {"source": row.source or "", "boms": {}})
		bom = group["boms"].setdefault(
			row.bom,
			{
				"document": row.bom,
				"item_code": row.item_code,
				"bom_qty": flt(row.bom_qty),
				"materials": 0,
				"used": used.get(row.bom, 0),
			},
		)
		bom["materials"] += 1
	for group in groups.values():
		group["posted_on"] = min(
			frappe.get_all("BOM", filters={"name": ("in", list(group["boms"]))}, pluck="creation") or [None]
		)
		group["rows"] = list(group.pop("boms").values())
		group["count"] = len(group["rows"])
	return {"groups": list(groups.values())}


def get_bom_use(names: set) -> dict[str, int]:
	"""By BOM, the work orders and stock entries not cancelled that use it."""
	used = {}
	for doctype in ("Work Order", "Stock Entry"):
		for row in frappe.get_all(
			doctype,
			filters={"bom_no": ("in", list(names) or [""]), "docstatus": ("<", 2)},
			fields=["bom_no"],
		):
			used[row.bom_no] = used.get(row.bom_no, 0) + 1
	return used


def undo_boms(onboarding, source: str | None, documents: list[str] | None = None) -> frappe._dict:
	"""Cancel the BOMs made from this upload, or only those named; their rows become drafts again.
	Refused for a BOM a work order, stock entry or another BOM uses."""
	rows = [
		row
		for row in onboarding.boms
		if row.status == "Made"
		and (row.source or "") == (source or "")
		and (documents is None or row.bom in documents)
	]
	if not rows:
		frappe.throw(_("There is nothing made to undo."), title=_("Nothing to undo"))
	names = {row.bom for row in rows}
	if used := get_bom_use(names):
		frappe.throw(
			count_text(
				len(used),
				_("1 BOM is used by work orders or stock entries. Cancel those first."),
				_("{0} BOMs are used by work orders or stock entries. Cancel those first."),
			),
			title=_("Can't undo"),
		)
	parents = frappe.get_all(
		"BOM Item",
		filters={"bom_no": ("in", list(names)), "parent": ("not in", list(names)), "docstatus": 1},
		fields=["bom_no", "parent"],
		limit=1,
	)
	if parents:
		frappe.throw(
			_("{0} is a sub-assembly of {1}. Undo {1} first.").format(parents[0].bom_no, parents[0].parent),
			title=_("Can't undo"),
		)
	# the BOMs that use a sub-assembly go first: they were made after it
	for name in frappe.get_all(
		"BOM", filters={"name": ("in", list(names))}, order_by="creation desc", pluck="name"
	):
		cancel_doc("BOM", name)
	for row in rows:
		row.update({"status": "Draft", "bom": None})
	onboarding.flags.posting = True
	onboarding.save()
	return frappe._dict(removed=len(names), kept_cancelled=pop_kept_reasons())


@frappe.whitelist()
def download_boms(company: str):
	"""A BOM sheet with the draft rows entered, or only its headings when there are none."""
	onboarding = frappe.get_doc("Company Setup", company)
	onboarding.check_permission("read")
	download_sheet(
		f"{frappe.scrub(company)}_boms",
		[
			list(SHEET_COLUMNS.values()),
			*([row.get(field) for field in SHEET_COLUMNS] for row in onboarding.boms if row.status != "Made"),
		],
		_("BOMs"),
	)
