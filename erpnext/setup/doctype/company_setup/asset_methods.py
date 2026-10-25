# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _

from erpnext.setup.doctype.company_setup import fixed_assets as assets
from erpnext.setup.doctype.company_setup.chart_sheet import get_file_label
from erpnext.setup.doctype.company_setup.posted_entries import run_or_enqueue, set_undo_choice


class AssetsMixin:
	def get_fixed_assets(self) -> dict:
		"""What the Fixed assets window shows, and why posting is stopped: see
		fixed_assets.get_asset_checks."""
		self.check_permission("read")
		self.require_migration_date()
		checks = assets.get_asset_checks(self)
		return checks | {"blocker": assets.get_asset_blocker(checks)}

	@frappe.whitelist()
	def save_fixed_assets(
		self, rows: list | str, source: str | None = None, depreciation_booked: str | None = None
	):
		"""Replace the draft assets with these rows. Allowed after Go live."""
		self.check_permission("write")
		self.start_change()
		assets.save_fixed_assets(self, frappe.parse_json(rows), source)
		if depreciation_booked is not None:
			self.depreciation_booked = depreciation_booked
		self.save()

	@frappe.whitelist()
	def import_fixed_assets(self, file_url: str) -> dict:
		"""Add a sheet's rows, replacing the drafts of this sheet and those of the same asset name;
		rows posted already are left out. Other drafts stay."""
		self.check_permission("write")
		source = get_file_label(file_url)
		upload = assets.sort_upload(self, assets.read_asset_sheet(file_url), source)
		self.save_fixed_assets([*upload.kept, *upload.rows], source)
		return {"added": len(upload.rows), "replaced": upload.replaced, "posted": upload.posted}

	@frappe.whitelist()
	def restore_undone_assets(self, source: str | None = None) -> dict:
		"""Put the undone rows of an upload back as drafts, to fix and post again."""
		self.check_permission("write")
		self.start_change()
		rows = [row for row in self.undone_assets if (row.source or "") == (source or "")]
		if not rows:
			frappe.throw(_("No undone rows from this upload."), title=_("Nothing to restore"))
		restored = {row.name for row in rows}
		upload = assets.sort_upload(self, [row.as_dict() for row in rows], source or "")
		self.save_fixed_assets([*upload.kept, *upload.rows], source)
		self.set("undone_assets", [row for row in self.undone_assets if row.name not in restored])
		# a form save keeps the undone rows as stored
		self.flags.posting = True
		self.save()
		return {"added": len(upload.rows), "replaced": upload.replaced, "posted": upload.posted}

	@frappe.whitelist()
	def save_asset_categories(self, rows: list | str):
		"""Depreciation by category: method, life, rate and accounts for all of a category's rows."""
		self.check_permission("write")
		self.start_change()
		assets.save_categories(self, frappe.parse_json(rows))
		self.save()

	@frappe.whitelist()
	def mark_asset_accounts(self) -> list[str]:
		"""Give the untyped accounts the drafts need their type: asset accounts Fixed Asset, and the
		categories' depreciation accounts Depreciation or Accumulated Depreciation."""
		self.check_permission("write")
		return assets.mark_asset_accounts(self)

	@frappe.whitelist()
	def spread_hand_booked(self):
		"""Share the depreciation booked by hand since the migration date over the assets by schedule."""
		self.check_permission("write")
		self.start_change()
		assets.spread_hand_booked(self)
		self.save()

	@frappe.whitelist()
	def post_fixed_assets(self) -> str:
		"""Post the draft assets: right away, or in the background when many. Allowed after Go live."""
		self.check_permission("write")
		self.start_change()
		self.require_migration_date()
		return run_or_enqueue(
			sum(1 for row in self.fixed_assets if row.status != "Posted"),
			lambda: assets.post_fixed_assets(self.company),
			assets.post_fixed_assets_in_background,
			assets.JOB_ID.format(self.company),
			company=self.company,
		)

	@frappe.whitelist()
	def undo_fixed_assets(
		self,
		source: str | None = None,
		asset_names: list | str | None = None,
		delete_entries: bool | None = None,
	) -> dict | str:
		"""Undo posted assets of one upload, or only those named; refused once depreciation is posted
		for them. Allowed after Go live. delete_entries: see set_undo_choice."""
		self.check_permission("write")
		self.start_change()
		set_undo_choice(delete_entries)
		asset_names = frappe.parse_json(asset_names) if isinstance(asset_names, str) else asset_names
		posted = [
			row
			for row in self.fixed_assets
			if row.status == "Posted" and (row.source or "") == (source or "")
		]
		return run_or_enqueue(
			len(asset_names if asset_names is not None else posted),
			lambda: assets.undo_fixed_assets(self, source, asset_names),
			assets.undo_fixed_assets_in_background,
			assets.UNDO_JOB_ID.format(self.company),
			company=self.company,
			source=source,
			assets=asset_names,
		)
