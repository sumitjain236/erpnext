# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import sbool

from erpnext.setup.doctype.company_setup import bank_accounts as banks
from erpnext.setup.doctype.company_setup.chart_sheet import get_file_label
from erpnext.setup.doctype.company_setup.opening_invoices import get_day_before, get_posting_blocker
from erpnext.setup.doctype.company_setup.posted_entries import check_undo, run_or_enqueue, set_undo_choice


class BankMixin:
	def get_bank_accounts(self) -> dict:
		"""What the bank details window shows: see bank_accounts.get_bank_details."""
		self.check_permission("read")
		return banks.get_bank_details(self)

	@frappe.whitelist()
	def save_bank_accounts(
		self, rows: list | str, has_uncleared_cheques: str | None = None, draft: bool = False
	):
		"""Keep the bank details window's rows. Unless a draft, make the Banks and Bank Accounts, set the
		default bank and keep the statement balances: see bank_accounts.save_bank_details."""
		self.check_permission("write")
		self.start_change()
		banks.save_bank_details(self, frappe.parse_json(rows), has_uncleared_cheques, sbool(draft))
		self.save()

	@frappe.whitelist()
	def mark_bank_accounts(self) -> list[str]:
		"""Make the untyped ledgers under bank groups bank accounts. Returns them."""
		self.check_permission("write")
		return banks.mark_bank_accounts(self.company)

	def get_uncleared_cheques(self) -> dict:
		"""What the uncleared cheques window shows, and why posting is stopped: see
		bank_accounts.get_cheque_checks."""
		self.check_permission("read")
		self.require_migration_date()
		checks = banks.get_cheque_checks(self)
		return checks | {"blocker": banks.get_cheque_blocker(self, checks)}

	@frappe.whitelist()
	def save_uncleared_cheques(self, rows: list | str, source: str | None = None) -> dict:
		"""Replace the draft cheques and deposits with these rows. Posted ones stay as they are."""
		self.check_permission("write")
		self.start_change()
		skipped = banks.save_uncleared_cheques(self, frappe.parse_json(rows), source)
		banks.validate_uncleared_cheques(self)
		if self.uncleared_cheques:
			self.has_uncleared_cheques = "Yes"
		self.save()
		return {"skipped": skipped}

	@frappe.whitelist()
	def import_uncleared_cheques(self, file_url: str, bank_account: str | None = None) -> dict:
		"""Replace the draft rows of the banks on the sheet with its rows. A sheet without a bank
		column is for the bank its title names, else for bank_account."""
		self.check_permission("write")
		sheet = banks.read_cheque_sheet(self.company, file_url, bank_account)
		on_sheet = {row["bank_account"] for row in sheet.cheques}
		drafts = [row for row in self.uncleared_cheques if row.status != "Posted"]
		kept = [row.as_dict() for row in drafts if row.bank_account not in on_sheet]
		result = self.save_uncleared_cheques([*kept, *sheet.cheques], get_file_label(file_url))
		return {
			"added": len(sheet.cheques) - result["skipped"],
			"skipped": result["skipped"],
			"replaced": len(drafts) - len(kept),
			"unknown_banks": sheet.unknown_banks,
			"banks": sorted(on_sheet),
			"no_bank_column": sheet.no_bank_column,
			"title_bank": sheet.title_bank,
		}

	@frappe.whitelist()
	def post_uncleared_cheques(self) -> str:
		"""Post the draft cheques and deposits: right away, or in the background when many. See
		bank_accounts.post_cheques."""
		self.check_permission("write")
		self.start_change()
		self.require_migration_date()
		blocker = banks.get_cheque_blocker(self, banks.get_cheque_checks(self))
		if blocker:
			frappe.throw(blocker, title=_("Can't post yet"))
		return run_or_enqueue(
			sum(1 for row in self.uncleared_cheques if row.status != "Posted"),
			lambda: banks.post_cheques(self.company),
			banks.post_cheques_in_background,
			banks.JOB_ID.format(self.company),
			company=self.company,
		)

	@frappe.whitelist()
	def undo_uncleared_cheques(
		self,
		source: str | None = None,
		entries: list[str] | str | None = None,
		delete_entries: bool | None = None,
	) -> dict | str:
		"""Undo the cheques and deposits posted from one source, or only those whose entries are named:
		see bank_accounts.undo_cheques. Not once live, nor for a cheque the bank cleared.
		delete_entries: see set_undo_choice."""
		self.check_permission("write")
		self.start_change()
		check_undo(self)
		set_undo_choice(delete_entries)
		entries = frappe.parse_json(entries) if isinstance(entries, str) else entries
		if entries is not None and not entries:
			frappe.throw(
				banks.cheque_words(self.company, _("Pick the cheques to undo.")), title=_("Nothing picked")
			)
		rows = banks.get_cheques_to_undo(self, source, entries)
		accounts = {row.bank_account for row in rows}
		# a bank keeping posted cheques gets a new opening entry, which needs the full check
		reposts = any(
			row.status == "Posted" and row.bank_account in accounts and row not in rows
			for row in self.uncleared_cheques
		)
		day_before = get_day_before(self)
		blocker = get_posting_blocker(self.company, day_before, undo=True) or (
			reposts and get_posting_blocker(self.company, day_before)
		)
		if blocker:
			frappe.throw(blocker, title=_("Can't undo"))
		banks.check_cleared(rows)
		return run_or_enqueue(
			len(rows),
			lambda: banks.undo_cheques(self, source, entries),
			banks.undo_cheques_in_background,
			banks.UNDO_JOB_ID.format(self.company),
			company=self.company,
			source=source,
			entries=entries,
		)
