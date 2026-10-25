# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext.setup.doctype.company_setup import opening_stock as stock
from erpnext.setup.doctype.company_setup.chart_sheet import get_file_label, strip_title_rows
from erpnext.setup.doctype.company_setup.opening_invoices import BACKGROUND_FROM
from erpnext.setup.doctype.company_setup.posted_entries import (
	check_undo,
	pop_kept_reasons,
	run_or_enqueue,
	set_undo_choice,
)


class StockMixin:
	def get_warehouses(self) -> dict:
		"""What the Warehouses window shows: see opening_stock.get_warehouse_rows."""
		self.check_permission("read")
		return stock.get_warehouse_rows(self)

	@frappe.whitelist()
	def save_warehouses(self, rows: list | str):
		"""Keep the Warehouses window's rows; make the new warehouses and job workers."""
		self.check_permission("write")
		self.start_change()
		stock.save_warehouses(self, frappe.parse_json(rows))
		self.save()

	def get_opening_stock(self) -> dict:
		"""What the Opening stock window shows, and why posting is stopped: see
		opening_stock.get_stock_checks. reds: each reason, as {key, text, rows}; blocker: the first."""
		self.check_permission("read")
		self.require_migration_date()
		checks = stock.get_stock_checks(self)
		reds = stock.get_stock_reds(checks)
		return checks | {
			"reds": reds,
			"blocker": reds[0]["text"] if reds else None,
			"match": stock.get_stock_match(self),
		}

	@frappe.whitelist()
	def save_opening_stock(self, rows: list | str, source: str | None = None) -> dict:
		"""Replace the draft stock rows with these. Rows for an item and warehouse already posted are
		left out."""
		self.check_permission("write")
		self.start_change()
		result = stock.save_opening_stock(self, frappe.parse_json(rows), source)
		self.save()
		return result

	@frappe.whitelist()
	def restore_undone_stock(self, source: str | None = None) -> dict:
		"""Put the undone rows of an upload back as drafts, to fix and post again."""
		self.check_permission("write")
		self.start_change()
		rows = [row for row in self.undone_stock if (row.source or "") == (source or "")]
		if not rows:
			frappe.throw(_("No undone rows from this upload."), title=_("Nothing to restore"))
		kept, _replaced = stock.get_other_drafts(self, [row.as_dict() for row in rows], source or "")
		result = self.save_opening_stock([*kept, *(row.as_dict() for row in rows)], source)
		self.set("undone_stock", [row for row in self.undone_stock if (row.source or "") != (source or "")])
		# a plain save keeps the stored undone rows
		self.flags.posting = True
		self.save()
		return result

	@frappe.whitelist()
	def make_serial_numbers(self) -> int:
		"""Fill the draft serial item rows that have no serial numbers with new ones from each item's
		serial number series. Returns how many rows were filled."""
		self.check_permission("write")
		self.start_change()
		filled = stock.make_serial_numbers(self)
		self.save()
		return filled

	@frappe.whitelist()
	def import_opening_stock(self, file_url: str) -> dict:
		"""Add a sheet's rows, replacing the drafts of this sheet and those of the same item and
		warehouse (and batch); other drafts stay."""
		self.check_permission("write")
		sheet = stock.read_stock_sheet(file_url)
		source = get_file_label(file_url)
		kept, replaced = stock.get_other_drafts(self, sheet.rows, source)
		result = self.save_opening_stock([*kept, *sheet.rows], source)
		return {
			"read": len(sheet.rows),
			"replaced": replaced,
			"added": len(sheet.rows) - result.skipped,
			"skipped": result.skipped,
			"differ": result.differ,
		}

	@frappe.whitelist()
	def post_opening_stock(self) -> str:
		"""Post the draft stock rows: right away, or in the background when many."""
		self.check_permission("write")
		self.start_change()
		self.require_migration_date()
		drafts = sum(1 for row in self.opening_stock if row.status != "Posted")
		if drafts >= BACKGROUND_FROM:
			# posting right away checks this itself
			blocker = stock.get_stock_blocker(stock.get_stock_checks(self))
			if blocker:
				frappe.throw(blocker, title=_("Can't post yet"))
		return run_or_enqueue(
			drafts,
			lambda: stock.post_opening_stock(self.company),
			stock.post_opening_stock_in_background,
			stock.JOB_ID.format(self.company),
			company=self.company,
		)

	@frappe.whitelist()
	def undo_opening_stock(
		self,
		source: str | None = None,
		warehouses: list | str | None = None,
		delete_entries: bool | None = None,
	) -> dict | str:
		"""Undo an upload's posted stock, or only its warehouses named. Refused once later stock
		entries exist on them: correct the rows instead. delete_entries: see set_undo_choice."""
		self.check_permission("write")
		self.start_change()
		check_undo(self)
		set_undo_choice(delete_entries)
		warehouses = frappe.parse_json(warehouses) if isinstance(warehouses, str) else warehouses
		rows = stock.get_rows_to_undo(self, source, warehouses)
		stock.check_stock_undo(self, rows)
		return run_or_enqueue(
			len(rows),
			lambda: stock.undo_opening_stock(self, source, warehouses),
			stock.undo_opening_stock_in_background,
			stock.UNDO_JOB_ID.format(self.company),
			company=self.company,
			source=source,
			warehouses=warehouses,
		)

	@frappe.whitelist()
	def correct_opening_stock(self, rows: list | str) -> dict | str:
		"""Set posted stock rows to the qty and value given: see opening_stock.correct_opening_stock.
		Many rows are corrected in the background: "queued", and the form hears when done."""
		self.check_permission("write")
		self.start_change()
		check_undo(self)
		rows = frappe.parse_json(rows)
		if len(rows) >= BACKGROUND_FROM:
			# correcting right away checks this itself
			check = stock.check_correction(self, rows)
			if check.blocker:
				frappe.throw(check.blocker, title=_("Can't correct"))
		return run_or_enqueue(
			len(rows),
			lambda: stock.correct_opening_stock(self, rows),
			stock.correct_opening_stock_in_background,
			stock.CORRECT_JOB_ID.format(self.company),
			company=self.company,
			changes=rows,
		)

	def check_stock_correction(self, rows: list | str) -> dict:
		"""What Correct rows would refuse, before its confirm: the blocker text, the items that would
		go below zero (item_code, item_name, warehouse, qty), and how many later stock entries change."""
		self.check_permission("read")
		check = stock.check_correction(self, frappe.parse_json(rows))
		return {
			"blocker": check.blocker,
			"below_zero": check.below_zero,
			"later_entries": sum(
				stock.get_later_entries(self, [row for row, _qty, _amount in check.changes]).values()
			),
		}

	@frappe.whitelist()
	def keep_as_balance(self, account: str):
		"""Keep a stock-like line, like Work in Progress, in Opening balances: it is not asked again."""
		self.check_permission("write")
		self.start_change()
		kept = frappe.parse_json(self.stock_like_kept) or []
		if account not in kept:
			self.stock_like_kept = [*kept, account]
			self.save()

	def get_stock_difference(self) -> dict:
		"""What Post the difference would post, by stock line, and why it can't yet."""
		self.check_permission("read")
		return {
			"lines": stock.get_difference_lines(self),
			"blocker": stock.get_difference_blocker(self),
			"empty_warehouses": stock.get_empty_warehouses(self),
			"account": stock.get_difference_account(self) or _(stock.DIFFERENCE_ACCOUNT),
		}

	@frappe.whitelist()
	def post_stock_difference(self, checked: bool = False) -> str:
		"""Post what the stock lines are off by to Opening Stock Difference. checked: the user ticked
		that no warehouse is missing, needed for a big gap."""
		self.check_permission("write")
		self.start_change()
		check_undo(self)
		name = stock.post_stock_difference(self, cint(checked))
		self.flags.posting = True
		self.save()
		return name

	@frappe.whitelist()
	def undo_stock_difference(self):
		self.check_permission("write")
		self.start_change()
		check_undo(self)
		stock.undo_stock_difference(self)
		self.flags.posting = True
		self.save()
		return {"kept_cancelled": pop_kept_reasons()}

	@frappe.whitelist()
	def turn_on_perpetual_inventory(self):
		"""Turn on perpetual inventory for the company, so stock values reach the books."""
		self.check_permission("write")
		stock.turn_on_perpetual_inventory(self.company)

	@frappe.whitelist()
	def prepare_import(self, doctype: str, file_url: str) -> str:
		"""A new Data Import of the sheet for this doctype, without the title rows above its headings
		that other software exports. Returns its name, for the import window to open."""
		self.check_permission("read")
		frappe.has_permission("Data Import", "create", throw=True)
		frappe.has_permission(doctype, "create", throw=True)
		return (
			frappe.get_doc(
				{
					"doctype": "Data Import",
					"reference_doctype": doctype,
					"import_type": "Insert New Records",
					"import_file": strip_title_rows(doctype, file_url),
				}
			)
			.insert()
			.name
		)

	@frappe.whitelist()
	def turn_on_serial_and_batch(self):
		"""Turn on batch and serial numbers in Stock Settings, so items that track them can be added."""
		self.check_permission("write")
		stock.turn_on_serial_and_batch()

	@frappe.whitelist()
	def move_to_opening_stock(self, account: str):
		"""Take a stock-like line, like Work in Progress, from Opening balances to Opening stock: its
		account becomes a stock account and its balance a stock line."""
		self.check_permission("write")
		self.start_change()
		rows = [row for row in self.opening_balances if row.account == account]
		if not rows or any(row.status == "Posted" for row in rows):
			frappe.throw(_("Only a line not posted yet can move to Opening stock."), title=_("Can't move it"))
		if account not in stock.get_kept_lines(self):
			frappe.throw(
				_("Only a work in progress or job worker asset line can move to Opening stock."),
				title=_("Can't move it"),
			)
		doc = frappe.get_doc("Account", account)
		doc.account_type = "Stock"
		doc.save()
		amount = flt(sum(flt(row.debit) - flt(row.credit) for row in rows), 2)
		lines = frappe.parse_json(self.sheet_accounts) or {}
		lines.setdefault("Stock", {})[account] = flt(lines["Stock"].get(account)) + amount
		self.sheet_accounts = lines
		self.sheet_stock = flt(self.sheet_stock) + amount
		self.opening_balances = [row for row in self.opening_balances if row.account != account]
		stock.set_line_on_warehouses(self, account)
		self.save()

	@frappe.whitelist()
	def move_to_opening_balances(self, account: str):
		"""Take a work in progress or job worker stock line with no stock posted back to Opening
		balances, kept there as a plain balance: what Move it did, undone."""
		self.check_permission("write")
		self.start_change()
		if account not in stock.get_movable_lines(self):
			frappe.throw(
				_("Only a work in progress or job worker stock line with no stock posted can move back."),
				title=_("Can't move it"),
			)
		lines = frappe.parse_json(self.sheet_accounts)
		amount = flt(lines["Stock"].pop(account))
		self.sheet_accounts = lines
		self.sheet_stock = flt(self.sheet_stock) - amount
		stock.clear_line_on_warehouses(self, account)
		doc = frappe.get_doc("Account", account)
		doc.account_type = ""
		doc.save()
		self.append(
			"opening_balances",
			{
				"original_account": doc.account_name,
				"account": account,
				"debit": max(amount, 0),
				"credit": max(-amount, 0),
			},
		)
		kept = frappe.parse_json(self.stock_like_kept) or []
		if account not in kept:
			self.stock_like_kept = [*kept, account]
		self.save()
