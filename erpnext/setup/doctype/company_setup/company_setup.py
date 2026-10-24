# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.contacts.doctype.address.address import get_default_address
from frappe.model import default_fields
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt, fmt_money, formatdate, getdate, now_datetime, sbool
from frappe.utils.background_jobs import is_job_enqueued

from erpnext.accounts.doctype.account.account import get_account_autoname
from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import build_tree_from_json
from erpnext.accounts.doctype.chart_of_accounts_importer.chart_of_accounts_importer import replace_chart
from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_fiscal_year
from erpnext.setup.doctype.company_setup import bank_accounts as banks
from erpnext.setup.doctype.company_setup import fixed_assets as assets
from erpnext.setup.doctype.company_setup import open_orders as orders
from erpnext.setup.doctype.company_setup import opening_stock as stock
from erpnext.setup.doctype.company_setup import taxes
from erpnext.setup.doctype.company_setup.chart_sheet import (
	NO_OPENING_BALANCE,
	get_party_group_type,
	read_balance_sheet,
	read_chart_sheet,
	strip_title_rows,
)
from erpnext.setup.doctype.company_setup.company_setup_steps import (
	LAST_STEP,
	PARTY_DEFAULTS,
	get_default_account_gaps,
	get_missing_years,
	get_party_ledger_pick,
	get_step_paths,
	get_temporary_opening,
	get_temporary_opening_balance,
	get_temporary_parts,
	get_unclosed_years,
	money_precision,
)
from erpnext.setup.doctype.company_setup.opening_invoices import (
	BACKGROUND_FROM,
	TABLES,
	UNDONE_TABLES,
	add_account,
	append_line,
	ensure_fiscal_year,
	find_party,
	fix_account_clashes,
	get_account_clashes,
	get_day_before,
	get_default_dimensions,
	get_default_node,
	get_exact_key,
	get_hand_posted,
	get_invoice_accounts,
	get_ledger_over_message,
	get_ledger_overs,
	get_look_alikes,
	get_missing_default_account,
	get_missing_fiscal_year,
	get_parties,
	get_party_accounts,
	get_posted_names,
	get_posting_blocker,
	get_posting_dates,
	get_posting_reds,
	get_sheet_match,
	get_singular_key,
	get_suggestions,
	get_table,
	get_temporary_account,
	get_title_account,
	get_waiting_parties,
	match_party,
	normalize_account_name,
	post_invoices,
	read_invoice_sheet,
	set_ledger_accounts,
)
from erpnext.setup.doctype.company_setup.posted_entries import (
	INVOICE_DOCTYPES,
	INVOICE_JOB_ID,
	INVOICE_UNDO_JOB_ID,
	check_undo,
	create_invoices_in_background,
	enqueue_job,
	get_first_posting_date,
	get_posted,
	needs_background,
	pop_kept_reasons,
	run_or_enqueue,
	set_undo_choice,
	undo_balances,
	undo_invoices,
	undo_invoices_in_background,
)

# fields only posting may change, by table
POSTING_FIELDS = {
	"opening_balances": ("status", "journal_entry"),
	"opening_sales_invoices": ("status", "invoice", "error"),
	"opening_purchase_invoices": ("status", "invoice", "error"),
	"uncleared_cheques": ("status", "journal_entry", "opening_entry"),
	"opening_stock": ("status", "stock_entry", "correction"),
	"open_orders": ("status", "document", "purchase_order"),
	"fixed_assets": ("status", "asset", "net_value_entry", "moved", "wdv_gap"),
}
# tables of undone rows, which only an undo adds to
UNDONE_ROWS = (*UNDONE_TABLES.values(), "undone_cheques", "undone_stock", "undone_orders", "undone_assets")
# fields only posting may change
POSTED_VALUES = ("stock_difference", "stock_difference_entry")
# fields only this form's own methods change: a save of the document from the form or the API
# keeps what is stored, so going live can't be undone and the sheet's totals can't be changed by hand
STORED_VALUES = (
	"status",
	"chart_of_accounts_source",
	"sheet_receivables",
	"sheet_payables",
	"sheet_stock",
	"sheet_accounts",
	"stock_like_kept",
	"taxes_saved",
)
# the row statuses of a posted balance, a posted invoice and a made order
POSTED = ("Posted", "Created", "Made")
# Company fields the Edit dialog fills; the setup wizard does not ask for them
COMPANY_FIELDS = (
	"company_logo",
	"tax_id",
	"phone_no",
	"email",
	"website",
	"date_of_incorporation",
	"registration_details",
)
# the Company's Modules Used checks the Edit dialog fills, of those the Company has
MODULE_FIELDS = ("stock", "manufacturing", "subcontracting", "assets", "projects", "pos")
ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "state", "pincode", "country")
# accounts ERPNext posts to by itself: account type, root type, the Company default it fills, and
# whether only automatic stock accounting needs it (every purchase invoice does then)
SYSTEM_ACCOUNTS = (
	("Stock Received But Not Billed", "Liability", "stock_received_but_not_billed", True),
	("Round Off", "Expense", "round_off_account", False),
)


class CompanySetup(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.setup.doctype.company_setup_asset_category.company_setup_asset_category import (
			CompanySetupAssetCategory,
		)
		from erpnext.setup.doctype.company_setup_bank_account.company_setup_bank_account import (
			CompanySetupBankAccount,
		)
		from erpnext.setup.doctype.company_setup_fixed_asset.company_setup_fixed_asset import (
			CompanySetupFixedAsset,
		)
		from erpnext.setup.doctype.company_setup_open_order.company_setup_open_order import (
			CompanySetupOpenOrder,
		)
		from erpnext.setup.doctype.company_setup_opening_balance.company_setup_opening_balance import (
			CompanySetupOpeningBalance,
		)
		from erpnext.setup.doctype.company_setup_opening_invoice.company_setup_opening_invoice import (
			CompanySetupOpeningInvoice,
		)
		from erpnext.setup.doctype.company_setup_opening_stock.company_setup_opening_stock import (
			CompanySetupOpeningStock,
		)
		from erpnext.setup.doctype.company_setup_step.company_setup_step import CompanySetupStep
		from erpnext.setup.doctype.company_setup_tax_template.company_setup_tax_template import (
			CompanySetupTaxTemplate,
		)
		from erpnext.setup.doctype.company_setup_uncleared_cheque.company_setup_uncleared_cheque import (
			CompanySetupUnclearedCheque,
		)
		from erpnext.setup.doctype.company_setup_warehouse.company_setup_warehouse import (
			CompanySetupWarehouse,
		)

		accounts_source: DF.Literal[
			"", "Tally", "Busy", "Zoho Books", "QuickBooks", "Xero", "Marg", "Excel / other"
		]
		asset_categories: DF.Table[CompanySetupAssetCategory]
		bank_accounts: DF.Table[CompanySetupBankAccount]
		chart_of_accounts_source: DF.Literal["", "Existing", "Imported"]
		company: DF.Link
		depreciation_booked: DF.Literal["", "Every month", "Only at year end"]
		fixed_assets: DF.Table[CompanySetupFixedAsset]
		has_uncleared_cheques: DF.Literal["", "No", "Yes"]
		migration_date: DF.Date | None
		open_orders: DF.Table[CompanySetupOpenOrder]
		opening_balances: DF.Table[CompanySetupOpeningBalance]
		opening_purchase_invoices: DF.Table[CompanySetupOpeningInvoice]
		opening_sales_invoices: DF.Table[CompanySetupOpeningInvoice]
		opening_stock: DF.Table[CompanySetupOpeningStock]
		payables_source: DF.Literal[
			"", "Tally", "Busy", "Zoho Books", "QuickBooks", "Xero", "Marg", "Excel / other"
		]
		receivables_source: DF.Literal[
			"", "Tally", "Busy", "Zoho Books", "QuickBooks", "Xero", "Marg", "Excel / other"
		]
		sheet_accounts: DF.JSON | None
		sheet_payables: DF.Currency
		sheet_receivables: DF.Currency
		sheet_stock: DF.Currency
		status: DF.Literal["In Progress", "Live"]
		steps: DF.Table[CompanySetupStep]
		stock_difference: DF.JSON | None
		stock_difference_entry: DF.Link | None
		stock_like_kept: DF.JSON | None
		tax_templates: DF.Table[CompanySetupTaxTemplate]
		taxes_saved: DF.Check
		uncleared_cheques: DF.Table[CompanySetupUnclearedCheque]
		undone_assets: DF.Table[CompanySetupFixedAsset]
		undone_cheques: DF.Table[CompanySetupUnclearedCheque]
		undone_orders: DF.Table[CompanySetupOpenOrder]
		undone_purchase_invoices: DF.Table[CompanySetupOpeningInvoice]
		undone_sales_invoices: DF.Table[CompanySetupOpeningInvoice]
		undone_stock: DF.Table[CompanySetupOpeningStock]
		warehouses: DF.Table[CompanySetupWarehouse]
	# end: auto-generated types

	_DOCTYPE_NAME = "Company Setup"

	def onload(self):
		self.set_onload("company_details", get_company_details(self.company))
		steps = get_steps(self)
		stored = {row.step_key: row for row in self.steps}
		statuses = {key: get_step_status(step, stored.get(key)) for key, step in steps.items()}
		self.set_onload(
			"steps",
			{
				key: {
					"title": step["title"],
					"description": step["description"],
					"summary": step.get("summary"),
					"warnings": step.get("warnings"),
					"started": step.get("started"),
					"tab": step.get("tab"),
					"required": step.get("required"),
					"needed": is_needed(step, statuses[key]),
				}
				for key, step in steps.items()
			},
		)
		# the form saves the steps (check_steps) only when one changed since it was last saved
		self.set_onload(
			"steps_changed",
			list(statuses.items()) != [(row.step_key, row.status) for row in self.steps],
		)
		self.set_onload("chart_import_blocked", get_chart_import_blocker(self.company))
		# new customers and suppliers get these groups; without one, the names review asks
		self.set_onload(
			"default_groups",
			{
				"Customer": frappe.db.get_single_value("Selling Settings", "customer_group"),
				"Supplier": frappe.db.get_single_value("Buying Settings", "supplier_group"),
				"territory": frappe.db.get_single_value("Selling Settings", "territory")
				or get_default_node("territory", self.company),
			},
		)
		self.set_onload("posting", self.get_posting())
		self.set_onload(
			"sheet_match", {party_type: get_sheet_match(self, party_type) for party_type in TABLES}
		)
		day_before = get_day_before(self)
		missing_year = get_missing_fiscal_year(day_before, self.company)
		self.set_onload(
			"missing_fiscal_year", missing_year and {"year": missing_year.year, "date": day_before}
		)
		if day_before and not missing_year:
			# the reports open on the year the opening entries are in, up to the day before migration
			year = get_fiscal_year(day_before, company=self.company, as_dict=True)
			self.set_onload(
				"report_period",
				{
					"fiscal_year": year.name,
					"from_date": year.year_start_date,
					"to_date": day_before,
					"year_end": year.year_end_date,
				},
			)
		temporary = get_temporary_opening(self)
		bank = banks.get_bank_status(self)
		self.set_onload("bank", bank)
		self.set_onload("bank_match", banks.get_bank_match(self))
		self.set_onload("stock_match", stock.get_stock_match(self))
		self.set_onload("temporary_parts", get_temporary_parts(self))
		self.set_onload("asset_match", assets.get_asset_match(self))
		self.set_onload("stock_like_lines", get_stock_like_lines(self))
		self.set_onload("serial_batch_off", stock.is_serial_batch_off())
		self.set_onload(
			"stats",
			{
				"customers": frappe.db.count("Customer", {"disabled": 0}),
				"suppliers": frappe.db.count("Supplier", {"disabled": 0}),
				"temporary_opening": temporary,
			},
		)
		# what Go live points out; none of it stops going live
		self.set_onload(
			"go_live_reminders",
			{
				"steps": [
					key
					for key, status in statuses.items()
					if status == "Not Started" and steps[key].get("required") and key != LAST_STEP
				],
				"skipped": [key for key, status in statuses.items() if status == "Skipped"],
				# optional steps not done while the trial balance has money for them
				"gaps": [
					key
					for key, status in statuses.items()
					if status != "Done" and steps[key].get("has_lines")
				],
				"temporary_opening": temporary,
				"bank_accounts_without_number": bank.without_number,
				"stock_reposts": day_before and stock.get_stock_reposts(self.company, day_before),
				"missing_years": get_missing_years(self),
			},
		)
		# what Check your books and Go live point out beyond the steps
		self.set_onload(
			"books_reminders",
			{
				"unclosed_years": [
					year
					| {
						"stock": get_year_stock_closing(self.company, year.year_end_date),
						"closing_account": get_closing_account(self.company),
					}
					for year in get_unclosed_years(self)
				],
				"default_account_gaps": get_default_account_gaps(self),
			},
		)
		self.set_onload("posting_blocker", day_before and get_posting_blocker(self.company, day_before))
		self.set_onload("posting_reds", day_before and get_window_reds(self, day_before))
		self.set_onload(
			"missing_default_accounts",
			{party_type: get_missing_default_account(self, party_type) for party_type in TABLES},
		)

	def validate(self):
		self.keep_posting_status()
		assets.clear_wrong_lines(self)
		self.validate_migration_date()
		self.validate_opening_balances()
		self.validate_opening_invoices()
		if not self.flags.ignore_steps:
			self.update_steps()

	def keep_posting_status(self):
		"""Only posting changes whether a row is posted, and a posted row stays as it was posted: a
		form save keeps what is stored, even when it leaves the row out."""
		if self.flags.posting or self.is_new():
			return
		# a save from a request other than this form's own methods (run_doc_method), like the form's
		# Save or a REST call
		saved_from_outside = getattr(frappe.local, "request", None) is not None and (
			frappe.local.form_dict.get("cmd") != "run_doc_method"
		)
		fields = (*POSTED_VALUES, *STORED_VALUES) if saved_from_outside else POSTED_VALUES
		self.update(frappe.db.get_value(self.doctype, self.name, fields, as_dict=True) or {})
		for table in UNDONE_ROWS:
			self.set(
				table,
				frappe.get_all(
					self.meta.get_field(table).options,
					filters={"parent": self.name, "parenttype": self.doctype, "parentfield": table},
					fields=["*"],
					order_by="idx",
				),
			)
		for table, fields in POSTING_FIELDS.items():
			stored = {
				row.name: row
				for row in frappe.get_all(
					self.meta.get_field(table).options,
					filters={"parent": self.name, "parenttype": self.doctype, "parentfield": table},
					fields=["*"],
				)
			}
			for row in self.get(table):
				saved = stored.pop(row.name, None) or {}
				if saved.get("status") in POSTED:
					row.update({key: value for key, value in saved.items() if key not in default_fields})
				else:
					row.update({field: saved.get(field) for field in fields})
					row.status = row.status or "Draft"
			for saved in stored.values():
				if saved.status in POSTED:
					self.append(
						table,
						{key: value for key, value in saved.items() if key not in default_fields}
						| {"name": saved.name},
					)
		assets.keep_posted_categories(self)

	def update_child_table(self, fieldname: str, df=None):
		"""Write only the rows added, changed or removed since the last save: a setup can hold
		thousands."""
		before = self.get_doc_before_save()
		if not before:
			return super().update_child_table(fieldname, df)
		stored = {row.name: get_row_values(row) for row in before.get(fieldname)}
		rows = self.get(fieldname)
		if removed := set(stored) - {row.name for row in rows}:
			frappe.db.delete(self.meta.get_field(fieldname).options, {"name": ("in", list(removed))})
		for row in rows:
			if row.is_new() or stored.get(row.name) != get_row_values(row):
				row.db_update()

	def validate_migration_date(self):
		"""Posted entries are dated the day before the migration date, so it can't move after."""
		if self.is_new() or not self.has_value_changed("migration_date"):
			return
		if any(row.status in POSTED for table in POSTING_FIELDS for row in self.get(table)):
			frappe.throw(
				_(
					"Opening entries are posted already on {0}, so the migration date can't change now."
				).format(formatdate(get_day_before(self.get_doc_before_save()))),
				title=_("Migration date locked"),
			)

	def validate_opening_invoices(self):
		"""A draft invoice was unpaid before the migration date, so it is dated before it and owes
		something, or is an advance paid before it."""
		day_before = get_day_before(self)
		for party_type, table in TABLES.items():
			for row in self.get(table):
				if row.status == "Created":
					continue
				if not flt(row.outstanding_amount):
					frappe.throw(
						_("Row {0}: {1} has nothing outstanding.").format(
							row.idx, frappe.bold(row.party_name)
						),
						title=_("Nothing outstanding"),
					)
				if day_before and row.invoice_date and getdate(row.invoice_date) > day_before:
					message = (
						_("Row {0}: bill {1} is dated after {2}, so it belongs in the new books.")
						if party_type == "Supplier"
						else _("Row {0}: invoice {1} is dated after {2}, so it belongs in the new books.")
					)
					frappe.throw(
						message.format(
							row.idx, frappe.bold(row.invoice_number or row.party_name), formatdate(day_before)
						),
						title=_("Dated after migration"),
					)

	def validate_opening_balances(self):
		"""Each draft balance is on an account of this company that can hold one."""
		names = [row.account for row in self.opening_balances if row.account]
		accounts = {
			account.name: account
			for account in frappe.get_all(
				"Account",
				filters={"name": ("in", names)},
				fields=["name", "company", "is_group", "account_type", "account_currency"],
			)
		}
		currency = frappe.get_cached_value("Company", self.company, "default_currency")
		for row in self.opening_balances:
			if not row.account or row.status == "Posted":
				continue
			account = accounts.get(row.account)
			if not account or account.company != self.company or account.is_group:
				frappe.throw(
					_("Row {0}: {1} is not an account of {2} that can hold a balance.").format(
						row.idx, frappe.bold(row.account), frappe.bold(self.company)
					),
					title=_("Wrong account"),
				)
			if account.account_type in NO_OPENING_BALANCE:
				frappe.throw(
					_("Row {0}: {1} is a {2} account. {3}").format(
						row.idx,
						frappe.bold(row.account),
						_(account.account_type),
						get_left_out_hint(account.account_type),
					),
					title=_("Wrong account"),
				)
			if account.account_currency and account.account_currency != currency:
				frappe.throw(
					_(
						"Row {0}: {1} is in {2}. Only accounts in {3} can take an opening balance here."
					).format(row.idx, frappe.bold(row.account), account.account_currency, currency),
					title=_("Wrong account"),
				)

	def update_steps(self):
		"""Put the registered steps in order and set each one's status from the data."""
		rows = {row.step_key: row for row in self.steps}
		self.steps = []
		for key, step in get_steps(self).items():
			row = rows.get(key)
			if row is None:
				row = frappe._dict(step_key=key)
			status = get_step_status(step, row)

			if row.status != status:
				row.last_checked = now_datetime()
			row.status = status
			row.step = step["title"]
			self.append("steps", row)

		for idx, row in enumerate(self.steps, 1):
			row.idx = idx

	def get_posting(self) -> list[str]:
		"""Customer and/or Supplier, when their opening invoices are being posted in the background, and
		Cheques when uncleared cheques are."""
		posting = [
			party_type
			for party_type in TABLES
			if is_job_enqueued(INVOICE_JOB_ID.format(self.company, party_type))
		]
		if is_job_enqueued(banks.JOB_ID.format(self.company)):
			posting.append("Cheques")
		if is_job_enqueued(stock.JOB_ID.format(self.company)) or is_job_enqueued(
			stock.CORRECT_JOB_ID.format(self.company)
		):
			posting.append("Stock")
		if orders.is_making_orders(self.company):
			posting.append("Orders")
		if assets.is_posting_assets(self.company):
			posting.append("Assets")
		return posting

	def is_posting(self) -> bool:
		"""Opening invoices or uncleared cheques are being posted or undone in the background for this
		company."""
		return (
			any(
				is_job_enqueued(job.format(self.company, party_type))
				for job in (INVOICE_JOB_ID, INVOICE_UNDO_JOB_ID)
				for party_type in TABLES
			)
			or banks.is_posting_cheques(self.company)
			or stock.is_posting_stock(self.company)
			or orders.is_making_orders(self.company)
			or assets.is_posting_assets(self.company)
		)

	def use_latest(self):
		"""Lock the row and start from what is saved, if it changed since this was loaded."""
		modified = frappe.db.get_value(self.doctype, self.name, "modified", for_update=True)
		if str(modified) != str(self.modified):
			self.reload()

	def start_change(self):
		"""Before a change is saved: none while a posting runs, and on top of the latest save."""
		if self.is_posting():
			frappe.throw(
				_("Entries are being posted in the background. Try again once they are done."),
				title=_("Posting in progress"),
			)
		self.use_latest()

	@frappe.whitelist()
	def check_steps(self) -> bool:
		"""Save when a step's status changed. The form calls this each time it opens."""
		if self.is_posting():
			return False
		self.use_latest()
		before = [(row.step_key, row.status) for row in self.steps]
		self.update_steps()
		changed = before != [(row.step_key, row.status) for row in self.steps]
		if not changed or not self.has_permission("write"):
			return changed
		if [key for key, _status in before] == [row.step_key for row in self.steps]:
			# the same steps: only the rows whose status changed are written
			for row, (_key, status) in zip(self.steps, before, strict=True):
				if row.status != status:
					row.db_update()
		else:
			self.save()
		return changed

	@frappe.whitelist()
	def skip_step(self, step_key: str, skip: int = 1):
		"""Skip a step, or bring a skipped step back. A step that is done stays done."""
		self.start_change()
		row = next((row for row in self.steps if row.step_key == step_key), None)
		if not row:
			frappe.throw(_("Step {0} not found").format(step_key), title=_("Step not found"))
		step = get_steps(self).get(step_key, {})
		if cint(skip) and step.get("required"):
			frappe.throw(_("{0} can't be skipped.").format(row.step), title=_("Step Needed"))
		if cint(skip) and step.get("started"):
			frappe.throw(
				_("{0} has rows already, so it can't be skipped.").format(row.step), title=_("Step Started")
			)
		row.status = "Skipped" if cint(skip) else "Not Started"
		self.save()

	@frappe.whitelist()
	def use_existing_chart(self):
		"""Keep the chart of accounts made during setup."""
		self.start_change()
		self.chart_of_accounts_source = "Existing"
		self.save()

	@frappe.whitelist()
	def preview_chart(self, file_url: str, group_kinds: dict | str | None = None) -> dict:
		"""What importing this sheet would change, for the Import dialog. group_kinds: what each group of
		a Busy list that Busy doesn't ship holds, see chart_sheet.get_busy_list_rows."""
		self.check_permission("write")
		sheet = read_chart_sheet(file_url, frappe.parse_json(group_kinds) if group_kinds else None)
		# the sheet's own account types: its accounts don't exist yet
		types = {row[0]: row[5] for row in sheet.rows[1:] if row and row[0]}
		kept = [
			balance for balance in sheet.balances if types.get(balance.account_name) not in NO_OPENING_BALANCE
		]
		left_out = {}
		for balance in sheet.balances:
			account_type = types.get(balance.account_name)
			if account_type in NO_OPENING_BALANCE:
				left_out[account_type] = flt(left_out.get(account_type)) + balance.debit - balance.credit
		return {
			"accounts": build_tree_from_json("", chart_data=sheet.forest, from_coa_importer=True),
			"current_accounts": frappe.db.count("Account", {"company": self.company}),
			"balances": len(kept),
			"debit": sum(balance.debit for balance in kept),
			"credit": sum(balance.credit for balance in kept),
			"left_out": {account_type: amount for account_type, amount in left_out.items() if amount},
			"draft_balances": len(self.opening_balances),
			"added_accounts": get_missing_system_accounts(self.company, set(types.values()))
			+ (
				[_("Stock Adjustment")]
				if "Stock" in types.values() and "Stock Adjustment" not in types.values()
				else []
			),
			"renamed": sheet.renamed,
			"unknown_groups": sheet.unknown_groups,
		}

	@frappe.whitelist()
	def import_chart(self, file_url: str, group_kinds: dict | str | None = None) -> dict:
		"""Replace the chart with the sheet's. Its balances become draft opening balances, and the
		accounts ERPNext needs that the sheet lacks are added. group_kinds: as for preview_chart."""
		self.check_permission("write")
		self.start_change()
		sheet = read_chart_sheet(file_url, frappe.parse_json(group_kinds) if group_kinds else None)
		old_names = {
			row.account: frappe.db.get_value("Account", row.account, "account_name")
			for row in self.opening_balances
			if row.account
		}

		default_names = get_default_account_names(self.company)
		party_accounts = get_party_account_names(self.company)
		replace_chart(sheet.rows, self.company, refill_defaults=True)
		kept = keep_default_accounts(self.company, default_names)
		keep_party_accounts(self.company, party_accounts)
		set_default_bank(self.company)
		# a default kept by name stays; one ERPNext picked for the new chart may be set again
		ours = tuple(fieldname for fieldname in PARTY_DEFAULTS if fieldname not in kept)
		set_default_party_accounts(self.company, {}, ours)
		added = add_system_accounts(self.company)
		added += stock.set_stock_defaults(self.company)
		self.chart_of_accounts_source = "Imported"
		# the replace deleted the tax templates, with the accounts they post to
		self.tax_templates = []
		self.taxes_saved = 0
		if sheet.balances:
			balances = sort_balances(self.company, sheet.balances)
			self.set_sheet_totals(balances.result["left_out"], balances.left_out_accounts, ours)
			self.opening_balances = []
			for row in balances.rows:
				self.append("opening_balances", row | {"source": get_file_label(file_url)})
		else:
			# the old accounts are gone: use the new account with the same name, if there is one
			for row in self.opening_balances:
				name = old_names.get(row.account)
				row.account = get_ledger_by_name(self.company, name)
		self.save()
		return {"added_accounts": added}

	@frappe.whitelist()
	def save_opening_balances(self, rows: list | str, source: str | None = None):
		"""Replace the draft opening balances with these rows. Posted ones stay as they are. Rows
		without a source of their own get this one, like the sheet's file name."""
		self.check_permission("write")
		self.start_change()
		self.opening_balances = [row for row in self.opening_balances if row.status == "Posted"]
		for row in frappe.parse_json(rows):
			# one side per row, as a journal entry needs: a credit typed as a negative debit too
			balance = flt(row.get("debit")) - flt(row.get("credit"))
			debit, credit = max(balance, 0), max(-balance, 0)
			if debit or credit:
				self.append(
					"opening_balances",
					{
						"original_account": row.get("original_account"),
						"account": row.get("account"),
						# the account's own once picked, else the kind of line the sheet says
						"root_type": row.get("root_type"),
						"debit": debit,
						"credit": credit,
						"source": row.get("source") or source,
					},
				)
		self.save()

	@frappe.whitelist()
	def import_opening_balances(self, file_url: str) -> dict:
		"""Replace the draft opening balances with a sheet's, matched to the chart by number and name."""
		self.check_permission("write")
		# before the totals are set: starting a change takes the latest save, which would drop them
		self.start_change()
		balances = sort_balances(self.company, read_balance_sheet(file_url))
		self.set_sheet_totals(balances.result["left_out"], balances.left_out_accounts)
		# an account posted already keeps its posted balance: the sheet's line again would count it twice
		posted = {row.account for row in self.opening_balances if row.status == "Posted" and row.account}
		rows = [row for row in balances.rows if not (row["account"] and row["account"] in posted)]
		self.save_opening_balances(rows, get_file_label(file_url))
		return balances.result | {"added": len(rows), "skipped": len(balances.rows) - len(rows)}

	def set_sheet_totals(self, left_out: dict, by_account: dict, party_defaults: tuple | None = None):
		"""Keep what the sheet says customers owe, suppliers are owed and stock is worth, in all and
		by account, to set against what their own steps post. The company's default receivable and
		payable become the ledgers holding the most: see set_default_party_accounts."""
		self.sheet_receivables = flt(left_out.get("Receivable"))
		self.sheet_payables = -flt(left_out.get("Payable"))
		self.sheet_stock = flt(left_out.get("Stock"))
		self.sheet_accounts = by_account
		set_default_party_accounts(self.company, by_account, party_defaults)

	def preview_opening_balances(self) -> dict:
		"""The books on the day before the migration date as they would be after posting the drafts:
		what is posted already, the drafts, and the difference on Temporary Opening."""
		self.check_permission("read")
		if not self.migration_date:
			frappe.throw(
				_("Set the migration date first, so the balances can be dated the day before."),
				title=_("Migration date missing"),
			)
		posting_date = get_day_before(self)
		# shown under its usual name until posting adds it
		temporary = get_temporary_account(self.company) or _("Temporary Opening")

		balances = banks.get_balances(self.company, None, posting_date)
		drafts = [
			(row.account, flt(row.debit) - flt(row.credit))
			for row in self.opening_balances
			if row.account and row.status != "Posted"
		]
		# invoices land on their customer's or supplier's account, a name confirmed as new on the
		# company's default one; posting leaves out a disabled party and one in another currency, as
		# get_sheet_match counts them, and a draft an invoice made by hand stands for
		draft_invoices = 0
		left_out = {
			"count": 0,
			"amount": 0,
			"currencies": {},
			"disabled": 0,
			"unconfirmed": 0,
			"party_types": [],
		}
		currency = frappe.get_cached_value("Company", self.company, "default_currency")
		for party_type, sign in (("Customer", 1), ("Supplier", -1)):
			parties = get_parties(party_type)
			hand_posted = {row.name for row in get_hand_posted(self, party_type)}
			draft_rows = [
				row
				for row in self.get(TABLES[party_type])
				if row.status != "Created" and row.name not in hand_posted
			]
			waiting = get_waiting_parties(self, party_type, draft_rows)
			party_rows = [
				(row, row.party if row.party in parties.names else find_party(parties, row.party_name))
				for row in draft_rows
			]
			accounts = get_party_accounts(party_type, [party for _row, party in party_rows], self.company)[0]
			for row, party in party_rows:
				account = row.account or (
					accounts[party]
					if party
					else row.create_party and get_party_account(party_type, None, self.company)
				)
				account_currency = account and frappe.get_cached_value("Account", account, "account_currency")
				other_currency = waiting.other_currency.get(row.party) or (
					account_currency != currency and account_currency
				)
				reason = (
					"unconfirmed"
					if not account
					else "currency"
					if other_currency
					else "disabled"
					if party in parties.disabled or row.party in waiting.disabled
					else None
				)
				if not reason:
					drafts.append((account, sign * flt(row.outstanding_amount)))
					draft_invoices += 1
					continue
				left_out["count"] += 1
				left_out["amount"] += flt(row.outstanding_amount)
				if party_type not in left_out["party_types"]:
					left_out["party_types"].append(party_type)
				if reason == "currency":
					left_out["currencies"][other_currency] = left_out["currencies"].get(other_currency, 0) + 1
				else:
					left_out[reason] += 1
		for account, difference in drafts:
			balances[account] = balances.get(account, 0) + difference
			balances[temporary] = balances.get(temporary, 0) - difference

		accounts = frappe.get_all(
			"Account",
			filters={"company": self.company},
			fields=["name", "account_name", "account_number", "parent_account", "root_type", "is_group"],
			order_by="lft",
		)
		names = {account.name: account.account_name for account in accounts}
		rows = [
			{
				"account": account.name,
				"account_name": account.account_name,
				"account_number": account.account_number,
				"parent": names.get(account.parent_account),
				"root_type": account.root_type,
				"balance": flt(balances.get(account.name), money_precision()),
			}
			for account in accounts
			if not account.is_group and flt(balances.get(account.name), money_precision())
		]
		if flt(balances.get(temporary), money_precision()) and not any(
			row["account"] == temporary for row in rows
		):
			rows.append(
				{
					"account": temporary,
					"account_name": temporary,
					"parent": None,
					"root_type": "Asset",
					"balance": flt(balances[temporary], money_precision()),
				}
			)
		return {
			"posting_date": posting_date,
			"temporary_account": temporary,
			"rows": rows,
			"drafts": sum(1 for row in self.opening_balances if row.status != "Posted"),
			"draft_invoices": draft_invoices,
			"left_out_invoices": left_out["count"],
			"left_out": left_out,
		}

	@frappe.whitelist()
	def post_opening_balances(self) -> list[str]:
		"""Post the draft balances on the day before the migration date, balanced against Temporary Opening.
		Balance sheet accounts go in an opening entry; income and expenses so far in an ordinary one."""
		self.check_permission("write")
		self.start_change()
		# post what is saved, checked as when it was saved
		self.reload()
		self.validate_opening_balances()
		drafts = [row for row in self.opening_balances if row.status != "Posted"]
		if not drafts:
			frappe.throw(_("There are no balances to post."), title=_("Nothing to post"))
		if not self.migration_date:
			frappe.throw(
				_("Set the migration date first, so the balances can be dated the day before."),
				title=_("Migration date missing"),
			)
		missing = [row.original_account or str(row.idx) for row in drafts if not row.account]
		if missing:
			frappe.throw(
				_("Pick an account for these rows, or delete the ones that aren't balances: {0}").format(
					", ".join(missing)
				),
				title=_("Account missing"),
			)

		ensure_fiscal_year(get_day_before(self), self.company)
		temporary = get_temporary_account(self.company, create=True)
		cost_center = frappe.get_cached_value("Company", self.company, "cost_center")
		accounts = {
			account.name: account
			for account in frappe.get_all(
				"Account",
				filters={"name": ("in", [row.account for row in drafts])},
				fields=["name", "root_type", "account_type"],
			)
		}
		# an account deleted since is left to the journal entry's link check
		missing = frappe._dict()
		profit_and_loss = [
			row for row in drafts if accounts.get(row.account, missing).root_type in ("Income", "Expense")
		]
		# ERPNext takes a depreciation account only in a depreciation entry
		depreciation = [
			row
			for row in profit_and_loss
			if accounts.get(row.account, missing).account_type == "Depreciation"
		]
		blocker = get_posting_blocker(
			self.company, get_day_before(self), profit_and_loss=bool(profit_and_loss)
		)
		if blocker:
			frappe.throw(blocker, title=_("Can't post yet"))
		dimensions = get_default_dimensions(self.company)
		journal_entries = []
		for voucher_type, is_opening, rows in (
			("Opening Entry", "Yes", [row for row in drafts if row not in profit_and_loss]),
			("Journal Entry", "No", [row for row in profit_and_loss if row not in depreciation]),
			("Depreciation Entry", "No", depreciation),
		):
			if not rows:
				continue
			je = frappe.new_doc("Journal Entry")
			je.update(
				{
					"voucher_type": voucher_type,
					"is_opening": is_opening,
					"company": self.company,
					"posting_date": get_day_before(self),
					"user_remark": _("Opening balances from Company Setup"),
				}
			)
			for row in rows:
				append_line(je, row.account, flt(row.debit) - flt(row.credit), dimensions, cost_center)
			difference = flt(
				sum(flt(row.debit) - flt(row.credit) for row in rows), je.precision("total_debit")
			)
			if difference:
				append_line(je, temporary, -difference, dimensions, cost_center)
			je.insert()
			je.submit()
			journal_entries.append(je.name)
			for row in rows:
				row.status = "Posted"
				row.journal_entry = je.name
		self.flags.posting = True
		self.save()
		return journal_entries

	@frappe.whitelist()
	def save_opening_invoices(
		self,
		party_type: str,
		rows: list | str,
		source: str | None = None,
		post_to: str | None = None,
	) -> frappe._dict:
		"""Replace the draft opening invoices with these rows. Posted ones stay as they are. A row that
		is one of them again is left out (skipped), and so is a row repeating one above it with the same
		invoice number (repeated). Rows without a source of their own get this one. A row's ledger is the
		one picked on it by hand (account_by_hand), else its customer's or supplier's own, else post_to,
		the ledger picked for the whole sheet."""
		self.check_permission("write")
		self.start_change()
		table = get_table(party_type)
		parties = get_parties(party_type)
		accounts = get_invoice_accounts(self.company, party_type)
		posted = [row for row in self.get(table) if row.status == "Created"]
		posted_keys = {get_invoice_key(row) for row in posted}
		posted_names = get_posted_names(self, party_type)
		self.set(table, posted)
		skipped, repeated, seen = 0, 0, set()
		for row in frappe.parse_json(rows):
			row = frappe._dict(row)
			name = " ".join(cstr(row.party_name).split())
			if not name and not flt(row.outstanding_amount):
				continue
			key = get_invoice_key(row)
			if key in posted_keys:
				skipped += 1
				continue
			if row.invoice_number and key in seen:
				repeated += 1
				continue
			seen.add(key)
			if row.account and row.account not in accounts:
				frappe.throw(
					_("{0} isn't a receivable account of {1} in its currency.").format(
						row.account, self.company
					)
					if party_type == "Customer"
					else _("{0} isn't a payable account of {1} in its currency.").format(
						row.account, self.company
					),
					title=_("Wrong account"),
				)
			self.append(
				table,
				{
					"party_type": party_type,
					"party_name": name,
					"party": row.party
					if row.party in parties.names
					else match_party(parties, posted_names, name),
					"create_party": cint(row.create_party) if not row.party else 0,
					"party_group": row.party_group if cint(row.create_party) and not row.party else None,
					"account": row.account,
					"account_by_hand": cint(row.account_by_hand) if row.account else 0,
					"invoice_number": row.invoice_number,
					"invoice_date": row.invoice_date,
					"due_date": row.due_date,
					"outstanding_amount": flt(row.outstanding_amount),
					"source": row.source or source,
				},
			)
		drafts = [row for row in self.get(table) if row.status != "Created" and not row.account_by_hand]
		own = get_party_accounts(party_type, [row.party for row in drafts], self.company)[1]
		for row in drafts:
			if own.get(row.party) in accounts:
				row.account = own[row.party]
			elif post_to:
				row.account = post_to
		self.save()
		return frappe._dict(skipped=skipped, repeated=repeated)

	@frappe.whitelist()
	def import_opening_invoices(self, party_type: str, file_url: str) -> dict:
		"""Replace the draft opening invoices with a receivables or payables report's."""
		self.check_permission("write")
		sheet = read_invoice_sheet(file_url, party_type)
		unknown_ledgers = set_ledger_accounts(self.company, party_type, sheet.invoices)
		post_to = get_title_account(self.company, party_type, sheet.titles)
		left_out = self.save_opening_invoices(party_type, sheet.invoices, get_file_label(file_url), post_to)
		return {
			"added": len(sheet.invoices) - left_out.skipped - left_out.repeated,
			"advances": sheet.advances,
			"skipped": left_out.skipped,
			"repeated": left_out.repeated,
			"unused_columns": sheet.unused_columns,
			"unknown_ledgers": unknown_ledgers,
			"post_to": post_to,
		}

	@frappe.whitelist()
	def remove_hand_posted(self, party_type: str, rows: list | str) -> int:
		"""Remove these draft rows (party, invoice_number, amount), when an opening invoice made by hand
		still stands for each: see opening_invoices.get_hand_posted. Returns how many were removed."""
		self.check_permission("write")
		self.start_change()
		rows = frappe.parse_json(rows) if isinstance(rows, str) else rows
		keys = {
			(row.get("party"), cstr(row.get("invoice_number")), flt(row.get("amount"), 2)) for row in rows
		}
		removed = {
			row.name
			for row in get_hand_posted(self, party_type)
			if (row.party, cstr(row.invoice_number), flt(row.outstanding_amount, 2)) in keys
		}
		table = get_table(party_type)
		self.set(table, [row for row in self.get(table) if row.name not in removed])
		self.save()
		return len(removed)

	def get_invoice_names(self, party_type: str) -> dict:
		"""The draft rows' names: new ones with their invoice count and total, the existing name a new
		one probably means, rows linked by a similar name, rows whose party was added since, and
		disabled parties."""
		self.check_permission("read")
		parties = get_parties(party_type)
		posted_names = get_posted_names(self, party_type)
		new, similar, found = {}, {}, {}
		for row in self.get(get_table(party_type)):
			if row.status == "Created":
				continue
			match = match_party(parties, posted_names, row.party_name)
			party = row.party if row.party in parties.names else match
			if not party:
				totals = new.setdefault(row.party_name, {"invoices": 0, "amount": 0})
				totals["invoices"] += 1
				totals["amount"] += flt(row.outstanding_amount)
				continue
			if party != row.party:
				found[row.party_name] = party
			if party == match and get_exact_key(row.party_name) not in {
				get_exact_key(party),
				get_exact_key(parties.names[party]),
			}:
				similar[row.party_name] = party
		drafts = {row.party for row in self.get(get_table(party_type)) if row.status != "Created"}
		accounts = get_invoice_accounts(self.company, party_type)
		return {
			# with one account there is nothing to choose
			"accounts": accounts if len(accounts) > 1 else [],
			# an account in another currency isn't one to pick; posting leaves those rows out
			"party_accounts": {
				party: account
				for party, account in get_party_accounts(
					party_type, (drafts | set(found.values())) - {None}, self.company
				)[0].items()
				if account in accounts
			}
			if len(accounts) > 1
			else {},
			# a ledger saved on the customer or supplier itself, which "Post to" doesn't change
			"own_accounts": {
				party: account
				for party, account in get_party_accounts(
					party_type, (drafts | set(found.values())) - {None}, self.company
				)[1].items()
				if account in accounts
			}
			if len(accounts) > 1
			else {},
			"can_create": frappe.has_permission(party_type, "create"),
			# drafts an opening invoice made by hand looks to stand for: not posted again
			"hand_posted": [
				{
					"name": row.name,
					"party": row.party,
					"invoice_number": row.invoice_number,
					"amount": flt(row.outstanding_amount),
					"invoice": row.hand_invoice,
				}
				for row in get_hand_posted(self, party_type)
			],
			"new": sorted(new),
			"totals": new,
			"suggestions": get_suggestions(parties, list(new)),
			"look_alikes": get_look_alikes(sorted(new)),
			"similar": similar,
			"found": found,
			"disabled": sorted((drafts | set(found.values())) & parties.disabled),
			"other_currency": get_other_currency_parties(
				self.company, party_type, (drafts | set(found.values())) - {None}
			),
		}

	def get_posted_entries(self, kind: str) -> dict:
		"""What was posted for balances, or for a party type's invoices, by where it came from."""
		self.check_permission("read")
		if kind == "cheques":
			return banks.get_posted_cheques(self)
		if kind == "stock":
			return stock.get_posted_stock(self)
		if kind == "assets":
			return assets.get_posted_assets(self)
		return get_posted(self, kind)

	@frappe.whitelist()
	def undo_posting(
		self,
		kind: str,
		source: str | None = None,
		invoices: list[str] | str | None = None,
		delete_entries: bool | None = None,
	) -> dict | str:
		"""Undo what was posted from one source, or only the invoices named: the invoices are cancelled
		and their rows kept as undone, the balances go back to drafts. Not once live. delete_entries:
		see set_undo_choice."""
		self.check_permission("write")
		self.start_change()
		check_undo(self)
		set_undo_choice(delete_entries)
		if kind != "balances" and kind not in TABLES:
			frappe.throw(_("Unknown kind {0}").format(kind), title=_("Can't undo"))
		invoices = frappe.parse_json(invoices) if isinstance(invoices, str) else invoices
		blocker = get_posting_blocker(
			self.company,
			get_first_posting_date(self, kind, source, invoices),
			undo=True,
			voucher_types=("Journal Entry",)
			if kind == "balances"
			else (INVOICE_DOCTYPES[kind], "Journal Entry"),
			to_date=get_day_before(self),
		)
		if blocker:
			frappe.throw(blocker, title=_("Can't undo"))
		if kind == "balances" and any(row.status == "Posted" for row in self.uncleared_cheques):
			frappe.throw(
				banks.cheque_words(self.company, _("Undo uncleared cheques first.")), title=_("Can't undo")
			)
		if kind == "balances" and any(row.status == "Posted" for row in self.opening_stock):
			frappe.throw(_("Undo opening stock first."), title=_("Can't undo"))
		if kind == "balances":
			return undo_balances(self, source)
		if not needs_background(self, kind, source, invoices):
			return undo_invoices(self, kind, source, invoices)
		enqueue_job(
			undo_invoices_in_background,
			INVOICE_UNDO_JOB_ID.format(self.company, kind),
			len(invoices) if invoices is not None else len(self.get(get_table(kind))),
			company=self.company,
			party_type=kind,
			source=source,
			invoices=invoices,
		)
		return "queued"

	@frappe.whitelist()
	def create_opening_invoices(
		self,
		party_type: str,
		account_fixes: list | str | None = None,
		keep_old_dates: bool = False,
		disabled_parties: list | str | None = None,
		use_post_to: list | str | None = None,
		post_to: str | None = None,
	) -> str:
		"""Post the draft rows as opening invoices: right away, or in the background when many. The
		names confirmed as new are added first, and the account clashes ticked are fixed first. With
		keep_old_dates, invoices dated in a year ERPNext doesn't have add that year and keep their
		dates, instead of going on the day before migration. The disabled customers or suppliers
		named in disabled_parties post too, and stay disabled. The draft rows named in use_post_to go
		to post_to, the sheet's ledger, instead of their customer's or supplier's own."""
		disabled_parties = (
			frappe.parse_json(disabled_parties) if isinstance(disabled_parties, str) else disabled_parties
		)
		self.check_permission("write")
		self.start_change()
		use_post_to = frappe.parse_json(use_post_to) if isinstance(use_post_to, str) else use_post_to
		if use_post_to and post_to:
			if post_to not in get_invoice_accounts(self.company, party_type):
				frappe.throw(_("{0} is not a ledger to post to.").format(post_to), title=_("Wrong account"))
			for row in self.get(get_table(party_type)):
				if row.name in use_post_to and row.status != "Created":
					row.update({"account": post_to, "account_by_hand": 1})
			self.flags.posting = True
			self.save()
		if account_fixes:
			fix_account_clashes(
				self,
				party_type,
				frappe.parse_json(account_fixes) if isinstance(account_fixes, str) else account_fixes,
			)
		confirmed_new = any(
			row.create_party and not row.party
			for row in self.get(get_table(party_type))
			if row.status != "Created"
		)
		if confirmed_new and not frappe.has_permission(party_type, "create"):
			frappe.throw(
				_("You can't add a {0}. Ask someone who can, or post without adding them.").format(
					_(party_type)
				),
				frappe.PermissionError,
				title=_("Not permitted"),
			)
		overs = get_ledger_overs(self, party_type, disabled_parties)
		if overs:
			frappe.throw(
				get_ledger_over_message(
					overs[0], frappe.get_cached_value("Company", self.company, "default_currency")
				),
				title=_("Wrong ledger"),
			)
		drafts = [row for row in self.get(get_table(party_type)) if row.status != "Created"]
		if not drafts:
			frappe.throw(
				_("There are no bills to post.")
				if party_type == "Supplier"
				else _("There are no invoices to post."),
				title=_("Nothing to post"),
			)
		if not self.migration_date:
			frappe.throw(
				_("Set the migration date first, so rows without a date can be dated the day before."),
				title=_("Migration date missing"),
			)
		keep_old_dates = sbool(keep_old_dates)
		dates = get_posting_dates(self, party_type)
		first = min(filter(None, [dates["day_before"], dates["first"]]))
		if keep_old_dates and dates["no_year_rows"]:
			# the oldest invoice in a year ERPNext doesn't have yet keeps its date too
			first = min(first, getdate(dates["no_year_rows"][0]["invoice_date"]))
		blocker = get_missing_default_account(self, party_type) or get_posting_blocker(
			self.company,
			first,
			voucher_types=(INVOICE_DOCTYPES[party_type], "Journal Entry"),
			to_date=dates["day_before"],
		)
		if blocker:
			frappe.throw(blocker, title=_("Can't post yet"))
		return run_or_enqueue(
			len(drafts),
			lambda: post_invoices(
				frappe.get_doc(self.doctype, self.name),
				party_type,
				keep_old_dates,
				disabled_parties=disabled_parties,
			),
			create_invoices_in_background,
			INVOICE_JOB_ID.format(self.company, party_type),
			company=self.company,
			party_type=party_type,
			keep_old_dates=keep_old_dates,
			disabled_parties=disabled_parties,
		)

	def get_posting_dates(self, party_type: str) -> dict:
		"""What the posting confirmation says about dates: see opening_invoices.get_posting_dates."""
		self.check_permission("read")
		return get_posting_dates(self, party_type)

	def get_account_clashes(self, party_type: str) -> list[dict]:
		"""What the posting confirmation asks about: see opening_invoices.get_account_clashes."""
		self.check_permission("read")
		return get_account_clashes(self, party_type)

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

	def get_taxes(self) -> dict:
		"""What the Taxes window shows: the rows saved and the tax accounts to pick from."""
		self.check_permission("read")
		return taxes.get_taxes(self)

	@frappe.whitelist()
	def save_taxes(self, rows: list | str, defaults: dict | str | None = None):
		"""Make the sales and purchase tax templates of these rows. No rows: no taxes charged.
		defaults: the title of the default template, by Sales or Purchase."""
		self.check_permission("write")
		self.start_change()
		taxes.save_taxes(self, frappe.parse_json(rows), frappe.parse_json(defaults) if defaults else None)
		self.save()

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

	def get_job_work_bom(self, item_code: str) -> dict:
		self.check_permission("read")
		return orders.get_job_work_bom(self, item_code)

	@frappe.whitelist()
	def set_job_work_bom(
		self, item_code: str, materials: list | str, service_item: str, finished_good_qty: float = 1
	) -> dict:
		"""Give the item a job-work BOM with only these materials: see open_orders.set_job_work_bom."""
		self.check_permission("write")
		return orders.set_job_work_bom(
			self, item_code, frappe.parse_json(materials), service_item, finished_good_qty
		)

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
		"""What the BOMs window shows: how many BOMs, and the items made here that have none."""
		self.check_permission("read")
		return orders.get_boms_status(self.company)

	def require_migration_date(self):
		if not self.migration_date:
			frappe.throw(
				_("Set the migration date first. Opening entries and orders are dated by it."),
				title=_("Migration date missing"),
			)

	@frappe.whitelist()
	def set_default_party_account(self, fieldname: str, account: str):
		"""Make a receivable (payable) ledger the company's default, from Check your books."""
		self.check_permission("write")
		if fieldname not in PARTY_DEFAULTS or frappe.db.get_value(
			"Account", account, ["company", "account_type"]
		) != (self.company, PARTY_DEFAULTS[fieldname]):
			frappe.throw(
				_("{0} is not a {1} account of {2}.").format(
					account, _(PARTY_DEFAULTS.get(fieldname, "")), self.company
				),
				title=_("Wrong account"),
			)
		company = frappe.get_doc("Company", self.company)
		company.set(fieldname, account)
		company.save()

	@frappe.whitelist()
	def set_dimension_default(self, dimension: str, value: str):
		"""Set this company's default for an accounting dimension every entry needs, from the red that
		asks for it."""
		self.check_permission("write")
		doc = frappe.get_doc("Accounting Dimension", dimension)
		row = next((row for row in doc.dimension_defaults if row.company == self.company), None)
		if not row:
			row = doc.append("dimension_defaults", {"company": self.company})
		row.reference_document = doc.document_type
		row.default_dimension = value
		doc.save()

	@frappe.whitelist()
	def close_year(self, fiscal_year: str, closing_account: str | None = None) -> str:
		"""Close a year whose profit is still in income and expenses, and the years before it that are
		still open, oldest first: ERPNext closes a year only after the one before. Each needs its Stock
		Closing Entry first when the company keeps stock; that builds in the background ("stock").
		Returns the last voucher's name."""
		self.check_permission("write")
		self.start_change()
		years = get_unclosed_years(self)
		index = next((index for index, row in enumerate(years) if row.fiscal_year == fiscal_year), None)
		if index is None:
			frappe.throw(
				_("{0} has no profit or loss left to close.").format(fiscal_year), title=_("Nothing to close")
			)
		name = None
		for year in years[: index + 1]:
			closing = get_year_stock_closing(self.company, year.year_end_date)
			if closing.needed and not closing.done:
				messages = len(frappe.local.message_log)
				stock.make_stock_closing(self.company, year.year_end_date)
				# ERPNext's "queued" note: the card says the stock is closing and refreshes itself
				del frappe.local.message_log[messages:]
				return "stock"
			if not closing_account:
				frappe.throw(
					_("Pick the account the profit goes to, like Retained Earnings."),
					title=_("Account missing"),
				)
			name = make_closing_voucher(self.company, year, closing_account)
		return name

	@frappe.whitelist()
	def go_live(self, accept_temporary_opening: bool = False):
		"""Mark the company as working in ERPNext. Whatever is left is only pointed out, but an
		amount left in Temporary Opening has to be accepted on purpose."""
		self.check_permission("write")
		self.start_change()
		temporary = get_temporary_opening_balance(self.company)
		if temporary and not sbool(accept_temporary_opening):
			currency = frappe.get_cached_value("Company", self.company, "default_currency")
			frappe.throw(
				_("{0} is left in Temporary Opening. Confirm that you want to go live with it.").format(
					fmt_money(abs(temporary), currency=currency)
				),
				title=_("Not explained yet"),
			)
		if self.migration_date:
			# work from the migration date on goes in these years
			ensure_fiscal_year(self.migration_date, self.company)
			ensure_fiscal_year(max(getdate(), getdate(self.migration_date)), self.company)
		self.status = "Live"
		self.save()

	@frappe.whitelist()
	def update_company_details(self, values: dict | str):
		"""Save the company details and address from the Edit dialog. Fields not sent stay as they are."""
		self.check_permission("write")
		values = frappe.parse_json(values)
		company = frappe.get_doc("Company", self.company)
		company.update({field: values[field] for field in get_company_fields() if field in values})
		company.save()
		save_company_address(self.company, values)


def get_steps(onboarding) -> dict[str, dict]:
	"""Every step in order, keyed by its path, with title, description and done. A step that returns
	None is hidden. A step another app adds that fails or returns something else is left out and
	logged, so it can't stop the setup."""
	steps = {}
	for path in get_step_paths():
		try:
			step = frappe.get_attr(path)(onboarding)
		except Exception:
			# ERPNext's own steps fail loudly; only another app's can't stop the setup
			if path.rsplit(".", 1)[0] == LAST_STEP.rsplit(".", 1)[0]:
				raise
			frappe.log_error(title=_("Company Setup step {0} failed").format(path))
			continue
		if step is None:
			continue
		if isinstance(step, dict) and step.get("title") and "done" in step:
			steps[path] = step
		else:
			frappe.log_error(title=_("Company Setup step {0} returned no title and done").format(path))
	return steps


def get_step_status(step: dict, row) -> str:
	if step["done"]:
		return "Done"
	return "Skipped" if row and row.status == "Skipped" else "Not Started"


def is_needed(step: dict, status: str) -> bool:
	"""Counted in the progress: required, or optional and started, done or skipped."""
	return bool(step.get("required") or step.get("started") or status != "Not Started")


def get_stock_like_lines(onboarding) -> list[dict]:
	"""Draft opening balances on work in progress or job worker lines that are not stock accounts:
	Opening balances asks whether to take them from Opening stock, unless told to keep them."""
	kept = stock.get_kept_lines(onboarding)
	drafts = {row.account for row in onboarding.opening_balances if row.status != "Posted"}
	answered = set(frappe.parse_json(onboarding.stock_like_kept) or [])
	return [
		{"account": account, "amount": amount}
		for account, amount in kept.items()
		if account in drafts and amount and account not in answered
	]


def make_closing_voucher(company: str, year, closing_account: str) -> str:
	"""Submit the Period Closing Voucher of a year, dated its end, or the draft one left for it."""
	voucher = frappe.db.get_value(
		"Period Closing Voucher", {"company": company, "fiscal_year": year.fiscal_year, "docstatus": 0}
	)
	doc = (
		frappe.get_doc("Period Closing Voucher", voucher)
		if voucher
		else frappe.new_doc("Period Closing Voucher")
	)
	doc.update(
		{
			"company": company,
			"fiscal_year": year.fiscal_year,
			"transaction_date": year.year_end_date,
			"period_start_date": year.year_start_date,
			"period_end_date": year.year_end_date,
			"closing_account_head": closing_account,
			"remarks": _("Closing {0} from Company Setup").format(year.fiscal_year),
		}
	)
	doc.save()
	doc.submit()
	return doc.name


def get_year_stock_closing(company: str, year_end) -> frappe._dict:
	"""The Stock Closing Entry a year's close needs, as opening_stock.get_stock_closing gives it;
	ERPNext asks for one only with perpetual inventory."""
	closing = stock.get_stock_closing(company, year_end)
	if not frappe.get_cached_value("Company", company, "enable_perpetual_inventory"):
		closing.needed = False
	return closing


# the names the account a year's profit is closed into usually has, the first found is suggested
CLOSING_ACCOUNT_NAMES = ("retained earnings", "reserves and surplus", "surplus", "profit and loss")


def get_closing_account(company: str) -> str | None:
	"""The account to suggest for a year's profit: a ledger under equity or liabilities named like
	Retained Earnings or Reserves & Surplus."""
	accounts = frappe.get_all(
		"Account",
		filters={
			"company": company,
			"is_group": 0,
			"disabled": 0,
			"root_type": ("in", ("Equity", "Liability")),
		},
		fields=["name", "account_name", "root_type"],
		order_by="lft",
	)
	names = {normalize_account_name(account.account_name): account.name for account in reversed(accounts)}
	equity = [account.name for account in accounts if account.root_type == "Equity"]
	return next((names[name] for name in CLOSING_ACCOUNT_NAMES if name in names), None) or (
		equity[0] if len(equity) == 1 else None
	)


def get_window_reds(onboarding, day_before) -> dict:
	"""What stops posting, and undoing, the balances and each party's invoices, as get_posting_reds:
	the windows show them as soon as they open, not one at a time after Post."""
	company = onboarding.company
	drafts = [row.account for row in onboarding.opening_balances if row.account and row.status != "Posted"]
	profit_and_loss = bool(
		drafts
		and frappe.db.exists("Account", {"name": ("in", drafts), "root_type": ("in", ("Income", "Expense"))})
	)
	reds = {
		"balances": get_posting_reds(company, day_before, profit_and_loss=profit_and_loss),
		"undo": {"balances": get_posting_reds(company, day_before, undo=True)},
	}
	for party_type in TABLES:
		voucher_types = (INVOICE_DOCTYPES[party_type], "Journal Entry")
		missing = get_missing_default_account(onboarding, party_type)
		reds[party_type] = [
			*(
				[
					{
						"text": missing,
						"fix": {"label": _("Open company"), "doctype": "Company", "name": company},
					}
				]
				if missing
				else []
			),
			*get_posting_reds(company, day_before, voucher_types=voucher_types),
		]
		reds["undo"][party_type] = get_posting_reds(
			company, day_before, undo=True, voucher_types=voucher_types
		)
	return reds


def get_file_label(file_url: str) -> str:
	"""An uploaded file's name, to tell the rows it brought apart from others."""
	return frappe.db.get_value("File", {"file_url": file_url}, "file_name") or file_url.rsplit("/", 1)[-1]


def sort_balances(company: str, balances: list[dict]) -> frappe._dict:
	"""Split a sheet's balances into rows to keep and amounts left out: customers, suppliers and stock
	come in through their own invoices and entries, and group lines only total their sub-accounts."""
	accounts = {
		account.name: account
		for account in frappe.get_all(
			"Account",
			filters={"company": company},
			fields=[
				"name",
				"account_name",
				"account_type",
				"root_type",
				"is_group",
				"disabled",
				"lft",
				"rgt",
			],
		)
	}
	rows, unmatched, groups, suggestions, new_accounts = [], [], 0, {}, {}
	left_out = {}
	# what each left-out line is on: an account of the chart, or "" for a line no account stands for,
	# like a customer under the debtors group or a group total
	by_account = {}
	lines = [balance for balance in balances if not balance.get("is_group")]
	groups += len(balances) - len(lines)
	names = [find_sheet_account(company, balance, accounts) for balance in lines]
	matched = [accounts[name] for name in names if name in accounts]
	for balance, name in zip(lines, names, strict=True):
		root_type = get_sheet_root_type(balance.get("groups", []))
		account = accounts.get(name) or frappe._dict()
		# an account of the chart keeps its balance whatever group the sheet puts it under, like a
		# provision for bad debts kept with the debtors
		# a customer under a group the chart keeps as a receivable ledger, like Busy's "Insurance &
		# TPA" under Sundry Debtors, is on that ledger
		party_ledger = not account and next(
			(
				ledger
				for ledger in accounts.values()
				if ledger.account_type in ("Receivable", "Payable")
				and not ledger.is_group
				and ledger.account_name in balance.get("groups", [])
			),
			None,
		)
		party_group = (party_ledger and party_ledger.account_type) or (
			not (account and not account.is_group and account.account_type not in NO_OPENING_BALANCE)
			and get_party_group_type([balance.account_name, *balance.get("groups", [])])
		)
		if party_group:
			left_out[party_group] = flt(left_out.get(party_group)) + flt(balance.debit) - flt(balance.credit)
			key = party_ledger.name if party_ledger else name if account and not account.is_group else ""
			by_account.setdefault(party_group, {})
			by_account[party_group][key] = (
				flt(by_account[party_group].get(key)) + flt(balance.debit) - flt(balance.credit)
			)
			continue
		account_type = account.account_type or get_party_account_type(balance.account_name)
		if account.is_group:
			# a total like "Accounts Receivable" stands for what its sub-accounts hold
			account_type = get_group_account_type(account, accounts.values())
		if (
			account.is_group
			and account_type not in NO_OPENING_BALANCE
			and (
				any(other.lft > account.lft and other.rgt < account.rgt for other in matched)
				or not get_ledgers(account, accounts.values())
			)
		):
			groups += 1
		elif account.is_group and account_type not in NO_OPENING_BALANCE:
			# no line of the sheet is under this group, so the line is a ledger the chart keeps as a
			# group, like Cash In Hand
			unmatched.append(balance.label)
			ledgers = get_ledgers(account, accounts.values())
			if len(ledgers) == 1:
				suggestions[balance.label] = ledgers[0]
			rows.append(
				{
					"original_account": balance.label,
					"account": None,
					"root_type": root_type or account.root_type,
					"debit": balance.debit,
					"credit": balance.credit,
				}
			)
		elif account_type in NO_OPENING_BALANCE:
			left_out[account_type] = (
				flt(left_out.get(account_type)) + flt(balance.debit) - flt(balance.credit)
			)
			key = name if name and not account.is_group else ""
			by_account.setdefault(account_type, {})
			by_account[account_type][key] = (
				flt(by_account[account_type].get(key)) + flt(balance.debit) - flt(balance.credit)
			)
		else:
			if not name:
				unmatched.append(balance.label)
				side = "debit" if flt(balance.debit) >= flt(balance.credit) else "credit"
				suggestion = suggest_account(balance.account_name, accounts.values(), root_type, side)
				if suggestion:
					suggestions[balance.label] = suggestion
				elif new_account := suggest_new_account(
					company, balance.account_name, balance.get("groups", []), root_type, side
				):
					new_accounts[balance.label] = new_account
			rows.append(
				{
					"original_account": balance.label,
					"account": name,
					# the sheet's kind of line, until an account is picked: suggestions stay within it
					"root_type": None if name else root_type,
					"debit": balance.debit,
					"credit": balance.credit,
				}
			)
	return frappe._dict(
		rows=rows,
		result={
			"added": len(rows),
			"unmatched": unmatched,
			"suggestions": suggestions,
			"new_accounts": new_accounts,
			"groups": groups,
			"left_out": {account_type: amount for account_type, amount in left_out.items() if amount},
		},
		left_out_accounts={
			account_type: {key: amount for key, amount in amounts.items() if flt(amount, money_precision())}
			for account_type, amounts in by_account.items()
		},
	)


def get_default_account_names(company: str) -> dict[str, str]:
	"""The company's default accounts by field, as account names that a new chart can have again."""
	company_doc = frappe.get_cached_doc("Company", company)
	return {
		df.fieldname: frappe.db.get_value("Account", company_doc.get(df.fieldname), "account_name")
		for df in company_doc.meta.get_link_fields()
		if df.options == "Account" and company_doc.get(df.fieldname)
	}


def keep_default_accounts(company: str, default_names: dict[str, str]):
	"""Point each default back at the new account of the same name, like "Debtors", instead of the
	first account of its type the new chart happens to have. Returns those set, by field."""
	currency = frappe.get_cached_value("Company", company, "default_currency")
	values = {}
	for fieldname, account_name in default_names.items():
		# a ledger in another currency of the same name is not the company's default
		account = get_ledger_by_name(company, account_name, currency)
		if account:
			values[fieldname] = account
	set_company_defaults(company, values)
	return values


def set_company_defaults(company: str, values: dict):
	"""Set some of the company's default accounts, as one who may change the company."""
	if not values:
		return
	frappe.has_permission("Company", "write", company, throw=True)
	frappe.db.set_value("Company", company, values)


def set_default_bank(company: str):
	"""The default bank is a bank the company holds money in: a bank ledger under assets, never a
	credit card or overdraft the new chart keeps as a bank account."""
	bank = frappe.db.get_value("Company", company, "default_bank_account")
	if bank and frappe.db.get_value("Account", bank, "root_type") == "Asset":
		return
	set_company_defaults(
		company,
		{
			"default_bank_account": frappe.db.get_value(
				"Account",
				{
					"company": company,
					"account_type": "Bank",
					"is_group": 0,
					"root_type": "Asset",
					"disabled": 0,
				},
				order_by="lft",
			)
		},
	)


def set_default_party_accounts(company: str, by_account: dict, fields: tuple | None = None):
	"""With more than one receivable (payable) ledger, the company's default becomes the one holding
	the most in the trial balance, by_account as sort_balances gives it, else the one with the usual
	name, like Sundry Debtors; not the first the chart happens to have. Only while the default is
	empty or as a chart import left it, and no opening invoice (bill) is posted: else Check your books
	points it out. fields: the defaults to set whatever they are now, like those ERPNext just picked
	for a new chart; the others are left."""
	for fieldname, account_type in PARTY_DEFAULTS.items():
		if fields is not None and fieldname not in fields:
			continue
		pick = get_party_ledger_pick(company, account_type, (by_account or {}).get(account_type))
		if not pick:
			continue
		current = frappe.db.get_value("Company", company, fieldname)
		if current and fields is None and current not in (pick.usual, pick.first):
			continue
		doctype = "Sales Invoice" if account_type == "Receivable" else "Purchase Invoice"
		if frappe.db.exists(doctype, {"company": company, "is_opening": "Yes", "docstatus": 1}):
			continue
		set_company_defaults(company, {fieldname: pick.biggest or pick.usual})


def get_party_account_names(company: str) -> list[dict]:
	"""The company's customer, supplier and group accounts, as account names that a new chart can have again."""
	rows = frappe.get_all(
		"Party Account",
		filters={"company": company},
		fields=["parent", "parenttype", "parentfield", "idx", "account", "advance_account"],
	)
	for row in rows:
		for field in ("account", "advance_account"):
			row[field] = row[field] and frappe.db.get_value("Account", row[field], "account_name")
	return rows


def get_ledger_by_name(company: str, account_name: str | None, currency: str | None = None) -> str | None:
	"""The company's ledger of this name, in this currency when given."""
	if not account_name:
		return None
	filters = {"company": company, "account_name": account_name, "is_group": 0}
	if currency:
		filters["account_currency"] = currency
	return frappe.db.get_value("Account", filters)


def keep_party_accounts(company: str, rows: list[dict]):
	"""Add back the Party Account rows the chart replace deletes, pointing at the new accounts of the
	same name. A row whose account the new chart lacks is left out, so the company default applies."""

	for row in rows:
		account = get_ledger_by_name(company, row.account)
		advance_account = get_ledger_by_name(company, row.advance_account)
		if not (account or advance_account):
			continue
		frappe.get_doc(
			{
				"doctype": "Party Account",
				"parent": row.parent,
				"parenttype": row.parenttype,
				"parentfield": row.parentfield,
				"idx": row.idx,
				"company": company,
				"account": account,
				"advance_account": advance_account,
			}
		).db_insert()


def get_missing_system_accounts(company: str, account_types: set) -> list[str]:
	"""The accounts add_system_accounts would add for a chart with these account types."""
	perpetual = frappe.get_cached_value("Company", company, "enable_perpetual_inventory")
	return [
		_(account_type)
		for account_type, _root_type, _fieldname, for_stock in SYSTEM_ACCOUNTS
		if (perpetual or not for_stock) and account_type not in account_types
	]


def add_system_accounts(company: str) -> list[str]:
	"""Add the accounts ERPNext posts to by itself that a chart from other software has no place for,
	and make them the company's defaults. Returns the names added."""
	company_doc = frappe.get_cached_doc("Company", company)
	added = []
	for account_type, root_type, fieldname, for_stock in SYSTEM_ACCOUNTS:
		if for_stock and not company_doc.enable_perpetual_inventory:
			continue
		account = frappe.db.get_value(
			"Account", {"company": company, "account_type": account_type, "is_group": 0}
		)
		if not account:
			account = add_account(company, _(account_type), account_type, root_type)
			added.append(account)
		if not frappe.db.get_value("Company", company, fieldname):
			set_company_defaults(company, {fieldname: account})
	return added


def get_group_account_type(group, accounts) -> str | None:
	"""Receivable, Payable or Stock when every account under the group is of that kind, like
	"Accounts Receivable": its total is left out with them, not taken for a mere sum of sub-accounts."""
	types = {
		account.account_type
		for account in accounts
		if account.lft > group.lft and account.rgt < group.rgt and not account.is_group
	}
	if len(types) == 1 and next(iter(types)) in ("Receivable", "Payable", "Stock"):
		return types.pop()
	return None


def find_sheet_account(company: str, balance, accounts: dict) -> str | None:
	"""The account a sheet line is on: by number and name, as "Child (Parent)" for a QuickBooks
	sub-account whose name the chart has under more than one parent, or by its name tidied."""
	if balance.get("parent"):
		name = find_account(company, balance.account_number, f"{balance.account_name} ({balance.parent})")
		if name:
			return name
	return find_account(company, balance.account_number, balance.account_name) or match_account_name(
		balance.account_name, accounts.values(), get_sheet_root_type(balance.get("groups", []))
	)


def get_ledgers(group, accounts) -> list[str]:
	"""The ledgers under a group that can take an opening balance, like Cash under Cash In Hand."""
	return [
		account.name
		for account in accounts
		if account.lft > group.lft
		and account.rgt < group.rgt
		and not account.is_group
		and not account.disabled
		and account.account_type not in NO_OPENING_BALANCE
	]


# what a trial balance group says a line is, by the words in its name (Tally, Busy), the first that
# fits: "Loans & Advances (Asset)" is an asset, "Bank OD A/c" a liability; unknown when none
SHEET_ROOT_TYPES = (
	("Asset", {"asset", "assets"}),
	("Liability", {"liability", "liabilities"}),
	("Equity", {"equity", "equities", "capital", "reserves"}),
	("Expense", {"expense", "expenses", "purchase", "purchases"}),
	("Income", {"income", "incomes", "sales"}),
	# not "Duties & Taxes": it holds input tax credits (assets) as well as tax owed
	("Liability", {"od", "overdraft", "loans", "provisions", "creditors"}),
	("Asset", {"investments", "deposits", "cash", "bank"}),
	# Tally's trading account holds the opening and closing stock of last year's profit
	("Expense", {"trading"}),
)
# what other software calls a ledger the standard chart has under another name
ACCOUNT_SYNONYMS = (
	("share capital", "capital stock"),
	("reserves and surplus", "retained earnings"),
	("provision for depreciation", "accumulated depreciation"),
	("salary payable", "payroll payable"),
	("wage payable", "payroll payable"),
	("salary and wage", "salary"),
	("wage", "salary"),
	("electricity", "utility"),
	("power and fuel", "utility"),
	("misc", "miscellaneous"),
	("travelling", "travel"),
	("traveling", "travel"),
	("printing", "print"),
	("fd", "fixed deposits"),
	("freight inward", "freight"),
	("carriage inward", "freight"),
	("cash credit", "overdraft"),
	("cc", "overdraft"),
	("od", "overdraft"),
	("purchase", "cost of goods sold"),
	("purchases", "cost of goods sold"),
	("opening stock", "stock adjustment"),
	("closing stock", "stock adjustment"),
)
# names of tax ledgers, for a line no ledger fits
TAX_WORDS = {"tds", "tcs", "gst", "cgst", "sgst", "igst", "utgst", "cess", "vat"}
# words that say nothing about which account a name is
FILLER_WORDS = {"and", "a", "c", "ac", "the", "of", "account", "accounts"}
# words many names share, so they don't make two names alike
COMMON_WORDS = {"expense", "charge", "fee", "cost", "other", "misc", "miscellaneous", "general"}


def get_sheet_root_type(groups: list[str]) -> str | None:
	"""Asset, Liability, Equity, Income or Expense, from the nearest sheet group that says it."""
	for group in reversed(groups):
		words = set(normalize_account_name(group).split())
		for root_type, markers in SHEET_ROOT_TYPES:
			if words & markers:
				return root_type
	return None


def match_account_name(account_name: str, accounts, root_type: str | None = None) -> str | None:
	"""The one account whose name is this one once tidied, and of the kind the sheet's group says.
	None when there are two, so a line is never put on a guess."""
	key = normalize_account_name(account_name)
	found = [
		account.name
		for account in accounts
		if key
		and normalize_account_name(account.account_name) == key
		and (not root_type or account.root_type == root_type)
	]
	return found[0] if len(found) == 1 else None


def suggest_account(
	account_name: str, accounts, root_type: str | None = None, side: str | None = None
) -> str | None:
	"""An account to suggest, never to use without the user: the one ledger whose name is this one in
	the singular or plural, like "Salaries" and "Salary". When the sheet says what kind of line it is,
	or the line's side ("debit" or "credit") does, else the one ledger of that kind whose name is
	inside this one or this one inside it, like "Rent Expense" and "Office Rent". A reverse charge
	(RCM) ledger only for a reverse charge line. None when more than one fits."""
	words = get_account_words(account_name)
	if not words:
		return None
	root_types = (
		(root_type,)
		if root_type
		else ("Asset", "Expense")
		if side == "debit"
		else ("Liability", "Equity", "Income")
		if side == "credit"
		else None
	)
	found, closest, most = [], [], 0
	for account in accounts:
		if account.is_group or account.disabled or account.account_type in NO_OPENING_BALANCE:
			continue
		if root_types and account.root_type not in root_types:
			continue
		if is_reverse_charge(account.account_name) != is_reverse_charge(account_name):
			continue
		theirs = get_account_words(account.account_name)
		if theirs == words:
			found.append(account.name)
		common = set(get_singular_key(" ".join(f"{word} {word}s" for word in COMMON_WORDS)).split())
		mine, others = words - common, theirs - common
		shared = len(mine & others) if mine and others and (mine <= others or others <= mine) else 0
		if root_types and shared and shared >= most:
			closest = [*closest, account.name] if shared == most else [account.name]
			most = shared
	if found:
		return found[0] if len(found) == 1 else None
	return closest[0] if len(closest) == 1 else None


def get_account_words(name: str) -> set[str]:
	"""The words of a name that say which account it is, in the singular, with other software's
	names for the standard chart's ledgers put in, like "Share Capital" for "Capital Stock"."""
	key = f" {get_singular_key(name)} "
	for words, synonym in ACCOUNT_SYNONYMS:
		key = key.replace(f" {get_singular_key(words)} ", f" {get_singular_key(synonym)} ")
	return set(key.split()) - FILLER_WORDS


def suggest_new_account(
	company: str, account_name: str, groups: list[str], root_type: str | None, side: str | None
) -> dict | None:
	"""A ledger to add for a line no ledger fits: its type from the sheet's group or its name, like
	Bank under Bank Accounts or Tax for TDS, and the group it goes under on its side. None when the
	side isn't known."""
	group_words = set(normalize_account_name(groups[-1] if groups else "").split())
	name_words = set(get_singular_key(account_name).split())
	# a customer's or supplier's ledger comes in with their invoices, not as a balance
	if name_words & {"debtor", "creditor"} or "receivable" in name_words and not name_words & TAX_WORDS:
		return None
	account_type = (
		"Bank"
		if "bank" in group_words
		and not group_words & {"od", "overdraft", "cc"}
		or "bank" in name_words
		and root_type in (None, "Asset")
		and not name_words & {"charge", "fee", "interest", "commission"}
		else "Cash"
		if "cash" in group_words
		else "Fixed Asset"
		if {"fixed", "assets"} <= group_words and "depreciation" not in name_words
		else "Tax"
		if name_words & TAX_WORDS or group_words & {"duties", "tax", "taxes"}
		else None
	)
	if not root_type and account_type in ("Bank", "Cash", "Fixed Asset"):
		root_type = "Asset"
	elif not root_type and account_type == "Tax" and side:
		root_type = "Asset" if side == "debit" else "Liability"
	if not root_type:
		return None
	return {
		"account_name": account_name,
		"account_type": account_type,
		"root_type": root_type,
		"parent_account": get_account_parent(company, account_type, root_type),
	}


def is_reverse_charge(name: str) -> bool:
	words = normalize_account_name(name).split()
	return "rcm" in words or "reverse charge" in " ".join(words)


# where a new ledger goes when no group has its type: words of the group's name, by root type
ACCOUNT_PARENT_WORDS = {
	"Fixed Asset": (("fixed", "asset"),),
	"Bank": (("bank",),),
	"Cash": (("cash",),),
	"Tax": (("tax",), ("duties",)),
	"Asset": (("current", "asset"),),
	"Liability": (("current", "liabilit"),),
	"Income": (("indirect", "income"), ("income",)),
	"Expense": (("indirect", "expense"), ("expense",)),
}


@frappe.whitelist()
def get_account_parent(
	company: str, account_type: str | None = None, root_type: str | None = None
) -> str | None:
	"""The group a new ledger of this type goes under, on its side (root type): the group of that type,
	like Bank Accounts, or one named for it, like Fixed Assets or Duties and Taxes; else Current
	Assets or Current Liabilities and the like; else the top-level group."""
	frappe.has_permission("Company Setup", "read", company, throw=True)
	groups = [
		group
		for group in frappe.get_all(
			"Account",
			filters={"company": company, "is_group": 1, "disabled": 0},
			fields=["name", "account_name", "account_type", "root_type", "parent_account"],
			order_by="lft",
		)
		if not root_type or group.root_type == root_type
	]

	def named(words):
		return next(
			(
				group.name
				for group in groups
				if all(word in normalize_account_name(group.account_name) for word in words)
			),
			None,
		)

	typed = account_type and next(
		(group.name for group in groups if group.account_type == account_type), None
	)
	return (
		typed
		or next(
			(found for words in ACCOUNT_PARENT_WORDS.get(account_type, ()) if (found := named(words))), None
		)
		or next((found for words in ACCOUNT_PARENT_WORDS.get(root_type, ()) if (found := named(words))), None)
		or next((group.name for group in groups if not group.parent_account), None)
	)


@frappe.whitelist()
def suggest_balance_accounts(
	company: str,
	labels: list | str,
	root_types: dict | str | None = None,
	sides: dict | str | None = None,
) -> dict:
	"""Suggestions for opening balance rows without an account, for a window opened again, and the
	ledger to add for a row no ledger fits, as {"suggestions": ..., "new_accounts": ...}. root_types:
	the kind of line the sheet said each label is, so a suggestion stays within it; sides: "debit" or
	"credit" for each label, for a line the sheet said no kind of."""
	frappe.get_doc("Company Setup", company).check_permission("read")
	accounts = frappe.get_all(
		"Account",
		filters={"company": company},
		fields=["name", "account_name", "account_type", "root_type", "is_group", "disabled"],
	)
	root_types = (frappe.parse_json(root_types) if isinstance(root_types, str) else root_types) or {}
	sides = (frappe.parse_json(sides) if isinstance(sides, str) else sides) or {}
	suggestions, new_accounts = {}, {}
	for label in frappe.parse_json(labels) if isinstance(labels, str) else labels:
		name = get_label_name(label)
		suggestion = suggest_account(name, accounts, root_types.get(label), sides.get(label))
		if suggestion:
			suggestions[label] = suggestion
		elif new_account := suggest_new_account(company, name, [], root_types.get(label), sides.get(label)):
			new_accounts[label] = new_account
	return {"suggestions": suggestions, "new_accounts": new_accounts}


def get_label_name(label: str) -> str:
	"""The account name in a sheet's label: "1001 - Rent Deposit" carries the account number, but
	"Security Deposit - Office Rent" is all name; "Payroll:Taxes" is a QuickBooks sub-account."""
	number, _sep, name = cstr(label).partition(" - ")
	if name and re.fullmatch(r"[\w.]*\d[\w.]*", number):
		label = name
	return cstr(label).rsplit(":", 1)[-1].strip()


def find_account(company: str, account_number: str, account_name: str) -> str | None:
	"""The account of the company with this number and name, or with this name when the number differs."""
	# as stored: the sheet may write the name in another case
	return frappe.db.exists(
		"Account", get_account_autoname(account_number, account_name, company)
	) or frappe.db.get_value("Account", {"company": company, "account_name": account_name})


def release_posted_rows(doc, method=None):
	"""A cancelled opening entry or invoice can be posted again: its Company Setup rows go back to draft."""
	# runs on every cancel on the site: only a company in setup, and only opening invoices, can have rows
	if doc.doctype != "Journal Entry" and doc.get("is_opening") != "Yes":
		return
	if not frappe.db.exists("Company Setup", doc.company):
		return
	if doc.doctype == "Journal Entry":
		frappe.db.set_value(
			"Company Setup Opening Balance",
			{"journal_entry": doc.name},
			{"status": "Draft", "journal_entry": None},
		)
	frappe.db.set_value(
		"Company Setup Opening Invoice", {"invoice": doc.name}, {"status": "Draft", "invoice": None}
	)


def get_other_currency_parties(company: str, party_type: str, parties: set[str]) -> dict[str, str]:
	"""The customers or suppliers whose invoices go to an account in another currency than the
	company's, with that currency."""
	currency = frappe.get_cached_value("Company", company, "default_currency")
	accounts = get_party_accounts(party_type, parties, company)[0]
	account_currencies = dict(
		frappe.get_all(
			"Account",
			filters={"name": ("in", list(set(accounts.values())) or [""])},
			fields=["name", "account_currency"],
			as_list=True,
		)
	)
	billed_in = dict(
		frappe.get_all(
			party_type,
			filters={"name": ("in", list(parties) or [""])},
			fields=["name", "default_currency"],
			as_list=True,
		)
	)
	other = {}
	for party in sorted(parties):
		# as get_party_currency: the account's currency, else the one the party is billed in
		party_currency = account_currencies.get(accounts.get(party))
		if party_currency == currency:
			party_currency = billed_in.get(party) or currency
		if party_currency != currency:
			other[party] = party_currency
	return other


def get_row_values(row) -> dict:
	"""What a save writes for a child row, but the save time it stamps on every row."""
	values = row.get_valid_dict(convert_dates_to_str=True, ignore_virtual=True)
	values.pop("modified", None)
	values.pop("modified_by", None)
	return values


def get_invoice_key(row) -> tuple:
	"""What makes two opening invoice rows the same invoice."""
	return (
		get_exact_key(row.get("party_name")),
		cstr(row.get("invoice_number")).strip(),
		cstr(getdate(row.get("invoice_date")) if row.get("invoice_date") else ""),
		flt(row.get("outstanding_amount"), 2),
	)


def get_party_account_type(name: str) -> str | None:
	"""Receivable for a customer's name and Payable for a supplier's: a trial balance often lists them."""
	if name and frappe.db.exists("Customer", {"customer_name": name}):
		return "Receivable"
	if name and frappe.db.exists("Supplier", {"supplier_name": name}):
		return "Payable"
	return None


def get_left_out_hint(account_type: str) -> str:
	return {
		"Receivable": _("Add what customers owe as opening sales invoices in Selling."),
		"Payable": _("Add what you owe suppliers as unpaid supplier bills in Buying."),
		"Stock": _("Stock comes in with an opening stock entry."),
		"Temporary": _("It takes the difference by itself."),
	}[account_type]


def get_chart_import_blocker(company: str) -> str | None:
	"""Why the chart can't be imported now, or None when it can."""
	if "Accounts Manager" not in frappe.get_roles():
		return _("Only an Accounts Manager can import a chart.")
	if frappe.get_all("GL Entry", {"company": company}, limit=1):
		return _(
			"This company already has accounting entries, so its chart can't be replaced. Edit it instead."
		)
	return None


def get_company_fields() -> list[str]:
	"""The Company fields the Edit dialog shows: the details, and the Modules Used checks it has."""
	meta = frappe.get_meta("Company")
	return [*COMPANY_FIELDS, *(field for field in MODULE_FIELDS if meta.has_field(field))]


def get_company_details(company: str) -> dict:
	"""What the Company card shows."""
	details = frappe.db.get_value(
		"Company",
		company,
		["company_name", "abbr", "country", "default_currency", *get_company_fields()],
		as_dict=True,
	)
	if not details:
		return {}

	address = get_default_address("Company", company)
	if address:
		details.update(frappe.db.get_value("Address", address, ADDRESS_FIELDS, as_dict=True))
	return details


def save_company_address(company: str, values: dict):
	"""Update the company's own address, or add one when a first line is given."""
	name = get_default_address("Company", company)
	if not name and not values.get("address_line1"):
		return

	address = frappe.get_doc("Address", name) if name else frappe.new_doc("Address")
	address.update({field: values[field] for field in ADDRESS_FIELDS if field in values})
	if address.is_new():
		address.address_title = company
		address.address_type = "Billing"
		address.is_your_company_address = 1
		address.append("links", {"link_doctype": "Company", "link_name": company})
	address.save()


def create_company_setup(company: str):
	"""Start the onboarding of a new company. Whoever makes the company gets its setup: the steps
	are worked out when the form first opens (check_steps)."""
	if frappe.db.exists("Company Setup", company):
		return
	doc = frappe.new_doc("Company Setup")
	doc.company = company
	doc.flags.ignore_steps = True
	doc.insert(ignore_permissions=True)
