# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.contacts.doctype.address.address import get_default_address
from frappe.model import default_fields
from frappe.model.document import Document
from frappe.permissions import get_doctype_roles
from frappe.utils import cint, flt, fmt_money, formatdate, getdate, now_datetime, sbool
from frappe.utils.background_jobs import is_job_enqueued

from erpnext.accounts.utils import get_fiscal_year
from erpnext.setup.doctype.company_setup import bank_accounts as banks
from erpnext.setup.doctype.company_setup import fixed_assets as assets
from erpnext.setup.doctype.company_setup import open_orders as orders
from erpnext.setup.doctype.company_setup import opening_stock as stock
from erpnext.setup.doctype.company_setup import taxes
from erpnext.setup.doctype.company_setup.asset_methods import AssetsMixin
from erpnext.setup.doctype.company_setup.balances import (
	BalancesMixin,
	get_account_parent,
	get_sheet_root_type,
	match_account_name,
	suggest_account,
	suggest_balance_accounts,
)
from erpnext.setup.doctype.company_setup.bank_methods import BankMixin
from erpnext.setup.doctype.company_setup.chart_import import ChartMixin
from erpnext.setup.doctype.company_setup.chart_sheet import NO_OPENING_BALANCE
from erpnext.setup.doctype.company_setup.close_year import (
	enqueue_year_close,
	get_closing_account,
	get_year_close,
	get_year_close_steps,
	get_year_stock_closing,
	is_closing_year,
	resume_year_close_on_stock_close,
	resume_year_closes,
	run_year_close,
	save_year_close,
)
from erpnext.setup.doctype.company_setup.company_setup_steps import (
	LAST_STEP,
	get_dated_drafts,
	get_default_account_gaps,
	get_draft_lines,
	get_draft_rows_by,
	get_drafts_by,
	get_missing_years,
	get_step_paths,
	get_temporary_opening,
	get_temporary_opening_balance,
	get_temporary_parts,
	get_unclosed_years,
)
from erpnext.setup.doctype.company_setup.invoice_methods import InvoicesMixin, get_other_currency_parties
from erpnext.setup.doctype.company_setup.opening_invoices import (
	TABLES,
	UNDONE_TABLES,
	ensure_fiscal_year,
	get_day_before,
	get_default_node,
	get_missing_default_account,
	get_missing_fiscal_year,
	get_posting_blocker,
	get_posting_reds,
	get_sheet_match,
)
from erpnext.setup.doctype.company_setup.order_methods import OrdersMixin
from erpnext.setup.doctype.company_setup.posted_entries import (
	INVOICE_DOCTYPES,
	INVOICE_JOB_ID,
	INVOICE_UNDO_JOB_ID,
)
from erpnext.setup.doctype.company_setup.stock_methods import StockMixin

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
# the sheets the steps bring in through Data Import
IMPORTED_DOCTYPES = ("Customer", "Supplier", "Item Group", "UOM", "Item", "BOM")
# what each step makes, as the permission it needs
STEP_PERMISSIONS = {
	"chart_of_accounts": [("Account", "create")],
	"taxes": [
		("Sales Taxes and Charges Template", "create"),
		("Purchase Taxes and Charges Template", "create"),
	],
	"opening_balances": [("Journal Entry", "submit")],
	"bank_accounts": [("Bank Account", "create"), ("Journal Entry", "submit")],
	"customers": [("Customer", "import")],
	"opening_sales_invoices": [("Sales Invoice", "submit")],
	"suppliers": [("Supplier", "import")],
	"opening_purchase_invoices": [("Purchase Invoice", "submit")],
	"items": [("Item", "import"), ("Item Group", "import"), ("UOM", "import")],
	"warehouses": [("Warehouse", "create")],
	"opening_stock": [("Stock Reconciliation", "submit")],
	"job_workers": [("Supplier", "write")],
	"fixed_assets": [("Asset", "submit"), ("Asset Category", "create"), ("Item", "create")],
	"boms": [("BOM", "submit")],
	"open_sales_orders": [("Sales Order", "submit")],
	"open_purchase_orders": [("Purchase Order", "submit")],
	"open_work_orders": [("Work Order", "submit")],
	"open_subcontracting_orders": [("Subcontracting Order", "submit")],
}
ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "state", "pincode", "country")


class CompanySetup(
	BalancesMixin, ChartMixin, InvoicesMixin, BankMixin, StockMixin, AssetsMixin, OrdersMixin, Document
):
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
		assets_source: DF.Literal[
			"", "Tally", "Busy", "Zoho Books", "QuickBooks", "Xero", "Marg", "Excel / other"
		]
		bank_accounts: DF.Table[CompanySetupBankAccount]
		boms_source: DF.Literal[
			"", "Tally", "Busy", "Zoho Books", "QuickBooks", "Xero", "Marg", "Excel / other"
		]
		chart_of_accounts_source: DF.Literal["", "Existing", "Imported"]
		cheques_source: DF.Literal[
			"", "Tally", "Busy", "Zoho Books", "QuickBooks", "Xero", "Marg", "Excel / other"
		]
		company: DF.Link
		depreciation_booked: DF.Literal["", "Every month", "Only at year end"]
		fixed_assets: DF.Table[CompanySetupFixedAsset]
		has_uncleared_cheques: DF.Literal["", "No", "Yes"]
		job_work_source: DF.Literal[
			"", "Tally", "Busy", "Zoho Books", "QuickBooks", "Xero", "Marg", "Excel / other"
		]
		migration_date: DF.Date | None
		open_orders: DF.Table[CompanySetupOpenOrder]
		opening_balances: DF.Table[CompanySetupOpeningBalance]
		opening_purchase_invoices: DF.Table[CompanySetupOpeningInvoice]
		opening_sales_invoices: DF.Table[CompanySetupOpeningInvoice]
		opening_stock: DF.Table[CompanySetupOpeningStock]
		orders_source: DF.Literal[
			"", "Tally", "Busy", "Zoho Books", "QuickBooks", "Xero", "Marg", "Excel / other"
		]
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
		stock_source: DF.Literal[
			"", "Tally", "Busy", "Zoho Books", "QuickBooks", "Xero", "Marg", "Excel / other"
		]
		tax_templates: DF.Table[CompanySetupTaxTemplate]
		taxes_saved: DF.Check
		uncleared_cheques: DF.Table[CompanySetupUnclearedCheque]
		undone_assets: DF.Table[CompanySetupFixedAsset]
		undone_cheques: DF.Table[CompanySetupUnclearedCheque]
		undone_orders: DF.Table[CompanySetupOpenOrder]
		undone_purchase_invoices: DF.Table[CompanySetupOpeningInvoice]
		undone_sales_invoices: DF.Table[CompanySetupOpeningInvoice]
		year_close: DF.JSON | None
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
					"blocker": step.get("blocker"),
					"started": step.get("started"),
					"tab": step.get("tab"),
					"required": step.get("required"),
					"needed": is_needed(step, statuses[key]),
					"status": statuses[key],
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
		self.set_onload("import_blocked", get_import_blockers())
		self.set_onload("missing_roles", get_missing_roles(steps, statuses))
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
		self.set_onload("year_close", get_year_close(self))
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
		closed_to = assets.get_closed_to(self.company)
		dated_drafts = get_dated_drafts(self)
		closed_rows = get_draft_rows_by(self, closed_to, dated_drafts) if closed_to else {}
		bank = banks.get_bank_status(self)
		self.set_onload("bank", bank)
		self.set_onload("bank_match", banks.get_bank_match(self))
		self.set_onload("stock_match", stock.get_stock_match(self))
		self.set_onload("temporary_parts", get_temporary_parts(self))
		self.set_onload("asset_match", assets.get_asset_match(self))
		self.set_onload("stock_like_lines", get_stock_like_lines(self))
		self.set_onload("serial_batch_off", stock.is_serial_batch_off())
		# the batch and serial question is asked before the first items import only
		self.set_onload("has_items", bool(stock.get_items_status(self).item_count))
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
				"closed_drafts": closed_to and get_draft_lines(closed_rows),
				"closed_draft_counts": closed_to
				and {table: len(rows) for table, rows in closed_rows.items()},
				"closed_to": closed_to,
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
						"drafts": get_drafts_by(self, year.year_end_date, dated_drafts),
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
		self.keep_year_close()
		self.keep_posting_status()
		assets.clear_wrong_lines(self)
		self.validate_migration_date()
		self.validate_opening_balances()
		self.validate_opening_invoices()
		if not self.flags.ignore_steps:
			self.update_steps()

	def keep_year_close(self):
		"""Only the year close job writes year_close, without touching modified: a save keeps what
		is stored, so a page loaded before the job's last step can't bring an old step back."""
		if not self.is_new():
			self.year_close = frappe.db.get_value(self.doctype, self.name, "year_close")

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
			or is_closing_year(self.company)
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

	def require_migration_date(self):
		if not self.migration_date:
			frappe.throw(
				_("Set the migration date first. Opening entries and orders are dated by it."),
				title=_("Migration date missing"),
			)

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
	def close_year(self, fiscal_year: str, closing_account: str) -> str:
		"""Close a year whose profit is still in income and expenses, in the background: each year's
		stock to its end when the company keeps stock, and the years before it still open, oldest
		first, as ERPNext closes years in order. The steps and how far they got stay in year_close, so
		the close goes on after a reload; one that stopped goes on from where it stopped."""
		self.check_permission("write")
		self.start_change()
		if not closing_account:
			frappe.throw(
				_("Pick the account the profit goes to, like Retained Earnings."), title=_("Account missing")
			)
		state = frappe._dict(
			frappe.parse_json(frappe.db.get_value(self.doctype, self.name, "year_close")) or {}
		)
		done = state.steps[: state.done] if state.fiscal_year == fiscal_year and state.error else []
		steps = done + get_year_close_steps(self, fiscal_year)
		state = {"fiscal_year": fiscal_year, "closing_account": closing_account, "steps": steps}
		self.db_set("year_close", frappe.as_json(state | {"done": len(done)}), update_modified=False)
		enqueue_year_close(self.company)
		return "queued"

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
		# work from the migration date on goes in these years; going live never waits for them
		if self.migration_date and frappe.has_permission("Fiscal Year", "create"):
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


def get_row_values(row) -> dict:
	"""What a save writes for a child row, but the save time it stamps on every row."""
	values = row.get_valid_dict(convert_dates_to_str=True, ignore_virtual=True)
	values.pop("modified", None)
	values.pop("modified_by", None)
	return values


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


def get_import_blockers() -> dict[str, str]:
	"""Why each sheet the steps import can't be imported by this user, for the ones that can't.
	Data Import needs the import permission, which create doesn't give."""
	blocked = {}
	for doctype in IMPORTED_DOCTYPES:
		if frappe.has_permission(doctype, "import"):
			continue
		roles = [_(role) for role in get_doctype_roles(doctype, "import")]
		blocked[doctype] = (
			_("To import {0}, you need the {1} role.").format(_(doctype), " / ".join(roles))
			if roles
			else _("No role can import {0}.").format(_(doctype))
		)
	return blocked


def get_missing_roles(steps: dict, statuses: dict) -> list[dict]:
	"""The roles this user lacks for the steps not done yet, each with the steps it is for. A missing
	permission takes a role already named when one gives it, else the role that gives the most."""
	gaps = {}
	for key, step in steps.items():
		if statuses[key] != "Not Started":
			continue
		for need in STEP_PERMISSIONS.get(key.rsplit(".", 1)[-1], ()):
			if not frappe.has_permission(*need):
				gaps.setdefault(need, []).append(step["title"])

	givers = {need: [role for role in get_doctype_roles(*need) if role != "Administrator"] for need in gaps}
	roles = {}
	for need, titles in gaps.items():
		role = next((role for role in givers[need] if role in roles), None) or max(
			givers[need],
			key=lambda role: sum(role in other for other in givers.values()),
			default=None,
		)
		if role:
			named = roles.setdefault(role, [])
			named.extend(title for title in titles if title not in named)
	return [{"role": role, "steps": titles} for role, titles in roles.items()]


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
