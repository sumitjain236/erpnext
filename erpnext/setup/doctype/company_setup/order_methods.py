# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe

from erpnext.setup.doctype.company_setup import boms as bom_setup
from erpnext.setup.doctype.company_setup import open_orders as orders
from erpnext.setup.doctype.company_setup.chart_sheet import get_file_label
from erpnext.setup.doctype.company_setup.posted_entries import check_undo, run_or_enqueue


class OrdersMixin:
	def get_open_orders(self, order_type: str) -> dict:
		"""What an open orders window shows: see open_orders.get_order_checks."""
		self.check_permission("read")
		return orders.get_order_checks(self, order_type)

	@frappe.whitelist()
	def save_open_orders(self, order_type: str, rows: list | str, source: str | None = None):
		"""Replace the draft orders of this type with these rows."""
		self.check_permission("write")
		self.start_change()
		result = orders.save_open_orders(self, order_type, frappe.parse_json(rows), source)
		self.save()
		return result

	@frappe.whitelist()
	def import_open_orders(self, order_type: str, file_url: str) -> dict:
		"""Add an open orders sheet's rows to the drafts of this type, in place of the drafts with the
		same order number."""
		self.check_permission("write")
		rows = orders.read_order_sheet(file_url)
		merged, replaced = orders.merge_upload(self, order_type, rows)
		result = self.save_open_orders(order_type, merged, get_file_label(file_url))
		return {"added": len(rows) - result.skipped, "skipped": result.skipped, "replaced": replaced}

	@frappe.whitelist()
	def make_open_orders(self, order_type: str) -> list[str] | str:
		"""Make the draft orders of this type in ERPNext: right away, or in the background when many.
		No money moves."""
		self.check_permission("write")
		self.start_change()
		self.require_migration_date()
		check_undo(self)
		to_make = orders.get_orders_to_make(self, order_type)
		return run_or_enqueue(
			len(to_make),
			lambda: orders.make_open_orders(self, order_type, to_make),
			orders.make_open_orders_in_background,
			orders.JOB_ID.format(self.company, order_type),
			company=self.company,
			order_type=order_type,
		)

	@frappe.whitelist()
	def mark_items_subcontracted(self) -> list[str]:
		"""Mark the items of the draft subcontracting orders as subcontracted."""
		self.check_permission("write")
		return orders.mark_items_subcontracted(self)

	def get_job_work_setups(self, items: list | str) -> list[dict]:
		self.check_permission("read")
		return orders.get_job_work_setups(self, frappe.parse_json(items))

	@frappe.whitelist()
	def set_job_work_setups(self, setups: list | str) -> list[str]:
		"""Give each item a job-work BOM and service item: see open_orders.set_job_work_bom."""
		self.check_permission("write")
		self.start_change()
		return orders.set_job_work_setups(self, frappe.parse_json(setups))

	def get_made_orders(self, order_type: str) -> dict:
		self.check_permission("read")
		return orders.get_made_orders(self, order_type)

	@frappe.whitelist()
	def undo_open_orders(
		self, order_type: str, source: str | None = None, documents: list | str | None = None
	) -> dict:
		"""Cancel orders made from one upload, or only those named. Not once live."""
		self.check_permission("write")
		self.start_change()
		check_undo(self)
		documents = frappe.parse_json(documents) if isinstance(documents, str) else documents
		return orders.undo_open_orders(self, order_type, source, documents)

	def get_boms(self) -> dict:
		"""What the BOMs window shows: see boms.get_bom_checks."""
		self.check_permission("read")
		return bom_setup.get_bom_checks(self)

	@frappe.whitelist()
	def save_boms(self, rows: list | str, source: str | None = None):
		"""Replace the draft BOM rows with these rows."""
		self.check_permission("write")
		self.start_change()
		bom_setup.save_boms(self, frappe.parse_json(rows), source)
		self.save()

	@frappe.whitelist()
	def import_boms(self, file_url: str) -> dict:
		"""Add a BOM sheet's rows to the drafts, in place of the drafts of the same items."""
		self.check_permission("write")
		rows = bom_setup.read_bom_sheet(file_url)
		merged, replaced = bom_setup.merge_upload(self, rows)
		self.save_boms(merged, get_file_label(file_url))
		return {"added": len(rows), "replaced": replaced}

	@frappe.whitelist()
	def make_boms(self) -> dict | str:
		"""Make the draft BOMs in ERPNext: right away, or in the background when many."""
		self.check_permission("write")
		self.start_change()
		to_make = bom_setup.get_boms_to_make(self)
		return run_or_enqueue(
			len(to_make),
			lambda: bom_setup.make_boms(self, to_make),
			bom_setup.make_boms_in_background,
			bom_setup.JOB_ID.format(self.company),
			company=self.company,
		)

	def get_made_boms(self) -> dict:
		self.check_permission("read")
		return bom_setup.get_made_boms(self)

	@frappe.whitelist()
	def undo_boms(self, source: str | None = None, documents: list | str | None = None) -> dict:
		"""Cancel BOMs made from one upload, or only those named. Not once live."""
		self.check_permission("write")
		self.start_change()
		check_undo(self)
		documents = frappe.parse_json(documents) if isinstance(documents, str) else documents
		return bom_setup.undo_boms(self, source, documents)
