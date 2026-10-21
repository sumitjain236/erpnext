# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.contacts.doctype.address.address import get_default_address
from frappe.model import default_fields
from frappe.model.document import Document
from frappe.query_builder.functions import Sum
from frappe.utils import add_days, cint, cstr, flt, fmt_money, formatdate, getdate, now_datetime, sbool
from frappe.utils.background_jobs import is_job_enqueued

from erpnext.accounts.doctype.account.account import get_account_autoname
from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import build_tree_from_json
from erpnext.accounts.doctype.chart_of_accounts_importer.chart_of_accounts_importer import replace_chart
from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_fiscal_year
from erpnext.setup.doctype.company_setup.chart_sheet import (
	NO_OPENING_BALANCE,
	get_party_group_type,
	read_balance_sheet,
	read_chart_sheet,
)
from erpnext.setup.doctype.company_setup.company_setup_steps import (
	get_step_paths,
	get_temporary_opening_balance,
)
from erpnext.setup.doctype.company_setup.opening_invoices import (
	BACKGROUND_FROM,
	TABLES,
	UNDONE_TABLES,
	add_account,
	create_invoices,
	create_invoices_in_background,
	ensure_fiscal_year,
	find_party,
	fix_account_clashes,
	get_account_clashes,
	get_default_node,
	get_exact_key,
	get_invoice_accounts,
	get_look_alikes,
	get_missing_fiscal_year,
	get_parties,
	get_party_currency,
	get_posted_names,
	get_posting_dates,
	get_sheet_match,
	get_suggestions,
	get_table,
	get_temporary_account,
	match_party,
	read_invoice_sheet,
	set_ledger_accounts,
)
from erpnext.setup.doctype.company_setup.posted_entries import (
	check_undo,
	get_posted,
	needs_background,
	undo_balances,
	undo_invoices,
	undo_invoices_in_background,
)

# fields only posting may change, by table
POSTING_FIELDS = {
	"opening_balances": ("status", "journal_entry"),
	"opening_sales_invoices": ("status", "invoice", "error"),
	"opening_purchase_invoices": ("status", "invoice", "error"),
}
# the row statuses of a posted balance and a posted invoice
POSTED = ("Posted", "Created")
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

		from erpnext.setup.doctype.company_setup_opening_balance.company_setup_opening_balance import (
			CompanySetupOpeningBalance,
		)
		from erpnext.setup.doctype.company_setup_opening_invoice.company_setup_opening_invoice import (
			CompanySetupOpeningInvoice,
		)
		from erpnext.setup.doctype.company_setup_step.company_setup_step import CompanySetupStep

		accounts_source: DF.Literal["", "Tally", "Busy", "Zoho Books", "QuickBooks", "Marg", "Excel / other"]
		chart_of_accounts_source: DF.Literal["", "Existing", "Imported"]
		company: DF.Link
		migration_date: DF.Date | None
		opening_balances: DF.Table[CompanySetupOpeningBalance]
		opening_purchase_invoices: DF.Table[CompanySetupOpeningInvoice]
		opening_sales_invoices: DF.Table[CompanySetupOpeningInvoice]
		payables_source: DF.Literal["", "Tally", "Busy", "Zoho Books", "QuickBooks", "Marg", "Excel / other"]
		receivables_source: DF.Literal[
			"", "Tally", "Busy", "Zoho Books", "QuickBooks", "Marg", "Excel / other"
		]
		sheet_accounts: DF.JSON | None
		sheet_payables: DF.Currency
		sheet_receivables: DF.Currency
		sheet_stock: DF.Currency
		status: DF.Literal["In Progress", "Live"]
		steps: DF.Table[CompanySetupStep]
		undone_purchase_invoices: DF.Table[CompanySetupOpeningInvoice]
		undone_sales_invoices: DF.Table[CompanySetupOpeningInvoice]
	# end: auto-generated types

	_DOCTYPE_NAME = "Company Setup"

	def onload(self):
		self.set_onload("company_details", get_company_details(self.company))
		steps = get_steps(self)
		self.set_onload(
			"steps",
			{
				key: {
					"title": step["title"],
					"description": step["description"],
					"summary": step.get("summary"),
					"started": step.get("started"),
					"tab": step.get("tab"),
					"required": step.get("required"),
				}
				for key, step in steps.items()
			},
		)
		# the form saves the steps (check_steps) only when one changed since it was last saved
		stored = {row.step_key: row for row in self.steps}
		self.set_onload(
			"steps_changed",
			[(key, get_step_status(step, stored.get(key))) for key, step in steps.items()]
			!= [(row.step_key, row.status) for row in self.steps],
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
		day_before = self.migration_date and add_days(self.migration_date, -1)
		missing_year = get_missing_fiscal_year(day_before, self.company)
		self.set_onload(
			"missing_fiscal_year", missing_year and {"year": missing_year.year, "date": day_before}
		)
		if day_before and not missing_year:
			# the reports open on the year the opening entries are in, up to the day before migration
			year = get_fiscal_year(day_before, company=self.company, as_dict=True)
			self.set_onload(
				"report_period",
				{"fiscal_year": year.name, "from_date": year.year_start_date, "to_date": day_before},
			)
		self.set_onload(
			"stats",
			{
				"customers": frappe.db.count("Customer", {"disabled": 0}),
				"suppliers": frappe.db.count("Supplier", {"disabled": 0}),
				"temporary_opening": get_temporary_opening_balance(self.company),
			},
		)

	def validate(self):
		self.keep_posting_status()
		self.validate_migration_date()
		self.validate_opening_balances()
		self.validate_opening_invoices()
		self.update_steps()

	def keep_posting_status(self):
		"""Only posting changes whether a row is posted, and a posted row stays as it was posted: a
		form save keeps what is stored, even when it leaves the row out."""
		if self.flags.posting or self.is_new():
			return
		for table in UNDONE_TABLES.values():
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
				).format(formatdate(add_days(self.get_doc_before_save().migration_date, -1)))
			)

	def validate_opening_invoices(self):
		"""A draft invoice was unpaid before the migration date, so it is dated before it and owes
		something, or is an advance paid before it."""
		day_before = self.migration_date and getdate(add_days(self.migration_date, -1))
		for party_type, table in TABLES.items():
			for row in self.get(table):
				if row.status == "Created":
					continue
				if not flt(row.outstanding_amount):
					frappe.throw(
						_("Row {0}: {1} has nothing outstanding.").format(
							row.idx, frappe.bold(row.party_name)
						)
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
						)
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
					)
				)
			if account.account_type in NO_OPENING_BALANCE:
				frappe.throw(
					_("Row {0}: {1} is a {2} account. {3}").format(
						row.idx,
						frappe.bold(row.account),
						_(account.account_type),
						get_left_out_hint(account.account_type),
					)
				)
			if account.account_currency and account.account_currency != currency:
				frappe.throw(
					_(
						"Row {0}: {1} is in {2}. Only accounts in {3} can take an opening balance here."
					).format(row.idx, frappe.bold(row.account), account.account_currency, currency)
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
		"""Customer and/or Supplier, when their opening invoices are being posted in the background."""
		return [
			party_type
			for party_type in TABLES
			if is_job_enqueued(f"company_setup_invoices::{self.company}::{party_type}")
		]

	def is_posting(self) -> bool:
		"""Opening invoices are being posted or undone in the background for this company."""
		return bool(self.get_posting()) or any(
			is_job_enqueued(f"company_setup_undo::{self.company}::{party_type}") for party_type in TABLES
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
				_("Invoices are being posted in the background. Try again once they are done."),
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
		if changed and self.has_permission("write"):
			self.save()
		return changed

	@frappe.whitelist()
	def skip_step(self, step_key: str, skip: int = 1):
		"""Skip a step, or bring a skipped step back. A step that is done stays done."""
		self.start_change()
		row = next((row for row in self.steps if row.step_key == step_key), None)
		if not row:
			frappe.throw(_("Step {0} not found").format(step_key))
		if cint(skip) and get_steps(self)[step_key].get("required"):
			frappe.throw(_("{0} can't be skipped.").format(row.step), title=_("Step Needed"))
		row.status = "Skipped" if cint(skip) else "Not Started"
		self.save()

	@frappe.whitelist()
	def use_existing_chart(self):
		"""Keep the chart of accounts made during setup."""
		self.start_change()
		self.chart_of_accounts_source = "Existing"
		self.save()

	@frappe.whitelist()
	def preview_chart(self, file_url: str) -> dict:
		"""What importing this sheet would change, for the Import dialog."""
		self.check_permission("write")
		sheet = read_chart_sheet(file_url)
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
			"added_accounts": get_missing_system_accounts(self.company, set(types.values())),
		}

	@frappe.whitelist()
	def import_chart(self, file_url: str) -> dict:
		"""Replace the chart with the sheet's. Its balances become draft opening balances, and the
		accounts ERPNext needs that the sheet lacks are added."""
		self.check_permission("write")
		self.start_change()
		sheet = read_chart_sheet(file_url)
		old_names = {
			row.account: frappe.db.get_value("Account", row.account, "account_name")
			for row in self.opening_balances
			if row.account
		}

		default_names = get_default_account_names(self.company)
		party_accounts = get_party_account_names(self.company)
		replace_chart(sheet.rows, self.company, refill_defaults=True)
		keep_default_accounts(self.company, default_names)
		keep_party_accounts(self.company, party_accounts)
		added = add_system_accounts(self.company)
		self.chart_of_accounts_source = "Imported"
		if sheet.balances:
			balances = sort_balances(self.company, sheet.balances)
			self.set_sheet_totals(balances.result["left_out"], balances.left_out_accounts)
			self.opening_balances = []
			for row in balances.rows:
				self.append("opening_balances", row | {"source": get_file_label(file_url)})
		else:
			# the old accounts are gone: use the new account with the same name, if there is one
			for row in self.opening_balances:
				name = old_names.get(row.account)
				row.account = name and frappe.db.get_value(
					"Account", {"company": self.company, "account_name": name, "is_group": 0}
				)
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
		balances = sort_balances(self.company, read_balance_sheet(file_url))
		self.set_sheet_totals(balances.result["left_out"], balances.left_out_accounts)
		# an account posted already keeps its posted balance: the sheet's line again would count it twice
		posted = {row.account for row in self.opening_balances if row.status == "Posted" and row.account}
		rows = [row for row in balances.rows if not (row["account"] and row["account"] in posted)]
		self.save_opening_balances(rows, get_file_label(file_url))
		return balances.result | {"added": len(rows), "skipped": len(balances.rows) - len(rows)}

	def set_sheet_totals(self, left_out: dict, by_account: dict):
		"""Keep what the sheet says customers owe, suppliers are owed and stock is worth, in all and
		by account, to set against what their own steps post."""
		self.sheet_receivables = flt(left_out.get("Receivable"))
		self.sheet_payables = -flt(left_out.get("Payable"))
		self.sheet_stock = flt(left_out.get("Stock"))
		self.sheet_accounts = by_account

	@frappe.whitelist()
	def preview_opening_balances(self) -> dict:
		"""The books on the day before the migration date as they would be after posting the drafts:
		what is posted already, the drafts, and the difference on Temporary Opening."""
		self.check_permission("read")
		if not self.migration_date:
			frappe.throw(_("Set the migration date first, so the balances can be dated the day before."))
		posting_date = add_days(self.migration_date, -1)
		# shown under its usual name until posting adds it
		temporary = get_temporary_account(self.company) or _("Temporary Opening")

		gle = frappe.qb.DocType("GL Entry")
		balances = {
			account: flt(balance)
			for account, balance in (
				frappe.qb.from_(gle)
				.select(gle.account, Sum(gle.debit) - Sum(gle.credit))
				.where(
					(gle.company == self.company)
					& (gle.is_cancelled == 0)
					& (gle.posting_date <= posting_date)
				)
				.groupby(gle.account)
			).run()
		}
		drafts = [
			(row.account, flt(row.debit) - flt(row.credit))
			for row in self.opening_balances
			if row.account and row.status != "Posted"
		]
		# invoices land on their customer's or supplier's account, a name confirmed as new on the
		# company's default one; posting leaves out a disabled party and one in another currency
		draft_invoices, left_out = 0, 0
		currency = frappe.get_cached_value("Company", self.company, "default_currency")
		for party_type, sign in (("Customer", 1), ("Supplier", -1)):
			parties, accounts = get_parties(party_type), {}
			for row in self.get(TABLES[party_type]):
				if row.status == "Created":
					continue
				party = row.party if row.party in parties.names else find_party(parties, row.party_name)
				if party and party not in accounts:
					accounts[party] = get_party_account(party_type, party, self.company)
				account = row.account or (
					accounts[party]
					if party
					else row.create_party and get_party_account(party_type, None, self.company)
				)
				if (
					not account
					or party in parties.disabled
					or frappe.get_cached_value("Account", account, "account_currency") != currency
				):
					left_out += 1
					continue
				drafts.append((account, sign * flt(row.outstanding_amount)))
				draft_invoices += 1
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
				"balance": flt(balances.get(account.name), 2),
			}
			for account in accounts
			if not account.is_group and flt(balances.get(account.name), 2)
		]
		if flt(balances.get(temporary), 2) and not any(row["account"] == temporary for row in rows):
			rows.append(
				{
					"account": temporary,
					"account_name": temporary,
					"parent": None,
					"root_type": "Asset",
					"balance": flt(balances[temporary], 2),
				}
			)
		return {
			"posting_date": posting_date,
			"temporary_account": temporary,
			"rows": rows,
			"drafts": sum(1 for row in self.opening_balances if row.status != "Posted"),
			"draft_invoices": draft_invoices,
			"left_out_invoices": left_out,
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
			frappe.throw(_("There are no balances to post."))
		if not self.migration_date:
			frappe.throw(_("Set the migration date first, so the balances can be dated the day before."))
		missing = [row.original_account or str(row.idx) for row in drafts if not row.account]
		if missing:
			frappe.throw(
				_("Pick an account for these rows, or delete the ones that aren't balances: {0}").format(
					", ".join(missing)
				)
			)

		ensure_fiscal_year(add_days(self.migration_date, -1), self.company)
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
					"posting_date": add_days(self.migration_date, -1),
					"user_remark": _("Opening balances from Company Setup"),
				}
			)
			for row in rows:
				je.append(
					"accounts",
					{
						"account": row.account,
						"cost_center": cost_center,
						"debit_in_account_currency": row.debit,
						"credit_in_account_currency": row.credit,
					},
				)
			difference = flt(
				sum(flt(row.debit) - flt(row.credit) for row in rows), je.precision("total_debit")
			)
			if difference:
				je.append(
					"accounts",
					{
						"account": temporary,
						"cost_center": cost_center,
						"debit_in_account_currency": -difference if difference < 0 else 0,
						"credit_in_account_currency": difference if difference > 0 else 0,
					},
				)
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
		self, party_type: str, rows: list | str, source: str | None = None
	) -> frappe._dict:
		"""Replace the draft opening invoices with these rows. Posted ones stay as they are. A row that
		is one of them again is left out (skipped), and so is a row repeating one above it with the same
		invoice number (repeated). Rows without a source of their own get this one."""
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
					"invoice_number": row.invoice_number,
					"invoice_date": row.invoice_date,
					"due_date": row.due_date,
					"outstanding_amount": flt(row.outstanding_amount),
					"source": row.source or source,
				},
			)
		self.save()
		return frappe._dict(skipped=skipped, repeated=repeated)

	@frappe.whitelist()
	def import_opening_invoices(self, party_type: str, file_url: str) -> dict:
		"""Replace the draft opening invoices with a receivables or payables report's."""
		self.check_permission("write")
		sheet = read_invoice_sheet(file_url, party_type)
		unknown_ledgers = set_ledger_accounts(self.company, party_type, sheet.invoices)
		left_out = self.save_opening_invoices(party_type, sheet.invoices, get_file_label(file_url))
		return {
			"added": len(sheet.invoices) - left_out.skipped - left_out.repeated,
			"advances": sheet.advances,
			"skipped": left_out.skipped,
			"repeated": left_out.repeated,
			"unused_columns": sheet.unused_columns,
			"unknown_ledgers": unknown_ledgers,
		}

	@frappe.whitelist()
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
				for party in sorted((drafts | set(found.values())) - {None})
				if (account := get_party_account(party_type, party, self.company)) in accounts
			}
			if len(accounts) > 1
			else {},
			"can_create": frappe.has_permission(party_type, "create"),
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

	@frappe.whitelist()
	def get_posted_entries(self, kind: str) -> dict:
		"""What was posted for balances, or for a party type's invoices, by where it came from."""
		self.check_permission("read")
		return get_posted(self, kind)

	@frappe.whitelist()
	def undo_posting(
		self, kind: str, source: str | None = None, invoices: list[str] | str | None = None
	) -> dict | str:
		"""Undo what was posted from one source, or only the invoices named: the invoices are cancelled
		and their rows kept as undone, the balances go back to drafts. Not once live."""
		self.check_permission("write")
		self.start_change()
		check_undo(self)
		if kind == "balances":
			return undo_balances(self, source)
		if kind not in TABLES:
			frappe.throw(_("Unknown kind {0}").format(kind))
		invoices = frappe.parse_json(invoices) if isinstance(invoices, str) else invoices
		if not needs_background(self, kind, source, invoices):
			return undo_invoices(self, kind, source, invoices)
		frappe.enqueue(
			undo_invoices_in_background,
			queue="long",
			job_id=f"company_setup_undo::{self.company}::{kind}",
			deduplicate=True,
			company=self.company,
			party_type=kind,
			source=source,
			invoices=invoices,
			now=frappe.in_test,
		)
		return "queued"

	@frappe.whitelist()
	def create_opening_invoices(
		self, party_type: str, account_fixes: list | str | None = None, keep_old_dates: bool = False
	) -> str:
		"""Post the draft rows as opening invoices: right away, or in the background when many. The
		names confirmed as new are added first, and the account clashes ticked are fixed first. With
		keep_old_dates, invoices dated in a year ERPNext doesn't have add that year and keep their
		dates, instead of going on the day before migration."""
		self.check_permission("write")
		self.start_change()
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
			)
		drafts = [row for row in self.get(get_table(party_type)) if row.status != "Created"]
		if not drafts:
			frappe.throw(
				_("There are no bills to post.")
				if party_type == "Supplier"
				else _("There are no invoices to post.")
			)
		if not self.migration_date:
			frappe.throw(
				_("Set the migration date first, so rows without a date can be dated the day before.")
			)
		keep_old_dates = sbool(keep_old_dates)
		if len(drafts) < BACKGROUND_FROM:
			create_invoices(self.company, party_type, keep_old_dates)
			return "done"
		frappe.enqueue(
			create_invoices_in_background,
			queue="long",
			job_id=f"company_setup_invoices::{self.company}::{party_type}",
			deduplicate=True,
			company=self.company,
			party_type=party_type,
			keep_old_dates=keep_old_dates,
			now=frappe.in_test,
		)
		return "queued"

	@frappe.whitelist()
	def get_posting_dates(self, party_type: str) -> dict:
		"""What the posting confirmation says about dates: see opening_invoices.get_posting_dates."""
		self.check_permission("read")
		return get_posting_dates(self, party_type)

	@frappe.whitelist()
	def get_account_clashes(self, party_type: str) -> list[dict]:
		"""What the posting confirmation asks about: see opening_invoices.get_account_clashes."""
		self.check_permission("read")
		return get_account_clashes(self, party_type)

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
		self.status = "Live"
		self.save()

	@frappe.whitelist()
	def update_company_details(self, values: dict | str):
		"""Save the company details and address from the Edit dialog."""
		values = frappe.parse_json(values)
		company = frappe.get_doc("Company", self.company)
		company.update({field: values.get(field) for field in COMPANY_FIELDS})
		company.save()
		save_company_address(self.company, values)


def get_steps(onboarding) -> dict[str, dict]:
	"""Every step in order, keyed by its path, with title, description and done. A step another app
	adds that fails or returns something else is left out and logged, so it can't stop the setup."""
	steps = {}
	for path in get_step_paths():
		try:
			step = frappe.get_attr(path)(onboarding)
		except Exception:
			frappe.log_error(title=_("Company Setup step {0} failed").format(path))
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
	rows, unmatched, groups, suggestions = [], [], 0, {}
	left_out = {}
	# what each left-out line is on: an account of the chart, or "" for a line no account stands for,
	# like a customer under the debtors group or a group total
	by_account = {}
	for balance in balances:
		if balance.get("is_group"):
			groups += 1
			continue
		root_type = get_sheet_root_type(balance.get("groups", []))
		name = find_account(company, balance.account_number, balance.account_name) or match_account_name(
			balance.account_name, accounts.values(), root_type
		)
		account = accounts.get(name) or frappe._dict()
		# an account of the chart keeps its balance whatever group the sheet puts it under, like a
		# provision for bad debts kept with the debtors
		party_group = not (
			account and not account.is_group and account.account_type not in NO_OPENING_BALANCE
		) and get_party_group_type([balance.account_name, *balance.get("groups", [])])
		if party_group:
			left_out[party_group] = flt(left_out.get(party_group)) + flt(balance.debit) - flt(balance.credit)
			key = name if account and not account.is_group else ""
			by_account.setdefault(party_group, {})
			by_account[party_group][key] = (
				flt(by_account[party_group].get(key)) + flt(balance.debit) - flt(balance.credit)
			)
			continue
		account_type = account.account_type or get_party_account_type(balance.account_name)
		if account.is_group:
			# a total like "Accounts Receivable" stands for what its sub-accounts hold
			account_type = get_group_account_type(account, accounts.values())
		if account.is_group and account_type not in NO_OPENING_BALANCE:
			groups += 1
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
				suggestion = suggest_account(balance.account_name, accounts.values(), root_type)
				if suggestion:
					suggestions[balance.label] = suggestion
			rows.append(
				{
					"original_account": balance.label,
					"account": name,
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
			"groups": groups,
			"left_out": {account_type: amount for account_type, amount in left_out.items() if amount},
		},
		left_out_accounts={
			account_type: {key: amount for key, amount in amounts.items() if flt(amount, 2)}
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
	first account of its type the new chart happens to have."""
	values = {}
	for fieldname, account_name in default_names.items():
		account = frappe.db.get_value(
			"Account", {"company": company, "account_name": account_name, "is_group": 0}
		)
		if account:
			values[fieldname] = account
	if values:
		frappe.db.set_value("Company", company, values)


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


def keep_party_accounts(company: str, rows: list[dict]):
	"""Add back the Party Account rows the chart replace deletes, pointing at the new accounts of the
	same name. A row whose account the new chart lacks is left out, so the company default applies."""

	def find(account_name):
		return account_name and frappe.db.get_value(
			"Account", {"company": company, "account_name": account_name, "is_group": 0}
		)

	for row in rows:
		account, advance_account = find(row.account), find(row.advance_account)
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
			frappe.db.set_value("Company", company, fieldname, account)
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


# what a trial balance group says a line is, by the words in its name (Tally, Busy); unknown when none
SHEET_ROOT_TYPES = (
	("Asset", {"asset", "assets", "investments", "deposits", "cash", "bank"}),
	("Liability", {"liability", "liabilities", "loans", "duties", "provisions", "creditors"}),
	("Equity", {"capital", "reserves"}),
	("Expense", {"expense", "expenses", "purchase", "purchases"}),
	("Income", {"income", "incomes", "sales"}),
)
# words that say nothing about which account a name is
FILLER_WORDS = {"and", "a", "c", "ac", "the", "of", "account", "accounts"}


def normalize_account_name(name: str) -> str:
	"""A name as people mean it: case, punctuation and "&" set aside, and a trailing "A/c" or
	"Account" dropped, so "Furniture & Fixtures A/c" is "furniture and fixtures"."""
	words = re.sub(r"[^a-z0-9 ]", " ", cstr(name).lower().replace("&", " and ")).split()
	if words[-2:] == ["a", "c"]:
		words = words[:-2]
	elif words[-1:] in (["ac"], ["account"]):
		words = words[:-1]
	return " ".join(words)


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


def suggest_account(account_name: str, accounts, root_type: str | None = None) -> str | None:
	"""An account to suggest, never to use without the user: the one ledger whose name is this one in
	the singular or plural, like "Salaries" and "Salary". None when more than one fits."""
	words = set(stem(word) for word in normalize_account_name(account_name).split()) - FILLER_WORDS
	if not words:
		return None
	found = []
	for account in accounts:
		if account.is_group or account.disabled or account.account_type in NO_OPENING_BALANCE:
			continue
		if root_type and account.root_type != root_type:
			continue
		theirs = (
			set(stem(word) for word in normalize_account_name(account.account_name).split()) - FILLER_WORDS
		)
		if theirs == words:
			found.append(account.name)
	return found[0] if len(found) == 1 else None


def stem(word: str) -> str:
	"""A word in the singular: "salaries" is "salary", "taxes" is "tax"."""
	if len(word) > 4 and word.endswith("ies"):
		return word[:-3] + "y"
	if len(word) > 4 and word.endswith(("ses", "xes", "ches", "shes")):
		return word[:-2]
	if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
		return word[:-1]
	return word


@frappe.whitelist()
def suggest_balance_accounts(company: str, labels: list | str) -> dict:
	"""Suggestions for opening balance rows without an account, for a window opened again."""
	frappe.get_doc("Company Setup", company).check_permission("read")
	accounts = frappe.get_all(
		"Account",
		filters={"company": company},
		fields=["name", "account_name", "account_type", "root_type", "is_group", "disabled"],
	)
	suggestions = {}
	for label in frappe.parse_json(labels) if isinstance(labels, str) else labels:
		# the sheet's label may carry the account number: "1001 - Rent Deposit"
		suggestion = suggest_account(cstr(label).split(" - ")[-1], accounts)
		if suggestion:
			suggestions[label] = suggestion
	return suggestions


def find_account(company: str, account_number: str, account_name: str) -> str | None:
	"""The account of the company with this number and name, or with this name when the number differs."""
	name = get_account_autoname(account_number, account_name, company)
	if frappe.db.exists("Account", name):
		return name
	return frappe.db.get_value("Account", {"company": company, "account_name": account_name})


def release_posted_rows(doc, method=None):
	"""A cancelled opening entry or invoice can be posted again: its Company Setup rows go back to draft."""
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
	return {
		party: party_currency
		for party in sorted(parties)
		if (party_currency := get_party_currency(party_type, party, company)) != currency
	}


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
		"Payable": _("Add what you owe suppliers as opening purchase invoices in Buying."),
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


def get_company_details(company: str) -> dict:
	"""What the Company card shows."""
	details = frappe.db.get_value(
		"Company",
		company,
		["company_name", "abbr", "country", "default_currency", *COMPANY_FIELDS],
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
	address.update({field: values.get(field) for field in ADDRESS_FIELDS})
	if address.is_new():
		address.address_title = company
		address.address_type = "Billing"
		address.is_your_company_address = 1
		address.append("links", {"link_doctype": "Company", "link_name": company})
	address.save()


def create_company_setup(company: str):
	"""Start the onboarding of a new company."""
	if frappe.db.exists("Company Setup", company):
		return
	doc = frappe.new_doc("Company Setup")
	doc.company = company
	doc.insert()
