# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.contacts.doctype.address.address import get_default_address
from frappe.model.document import Document
from frappe.query_builder.functions import Sum
from frappe.utils import add_days, cint, cstr, flt, fmt_money, formatdate, getdate, now_datetime, sbool

from erpnext.accounts.doctype.account.account import get_account_autoname
from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import build_tree_from_json
from erpnext.accounts.doctype.chart_of_accounts_importer.chart_of_accounts_importer import replace_chart
from erpnext.accounts.party import get_party_account
from erpnext.setup.doctype.company_setup.chart_sheet import (
	NO_OPENING_BALANCE,
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
	add_account,
	create_invoices,
	create_invoices_in_background,
	ensure_fiscal_year,
	find_party,
	get_exact_key,
	get_parties,
	get_suggestions,
	get_table,
	get_temporary_account,
	read_invoice_sheet,
)

# fields only posting may change, by table
POSTING_FIELDS = {
	"opening_balances": ("status", "journal_entry"),
	"opening_sales_invoices": ("status", "invoice", "error"),
	"opening_purchase_invoices": ("status", "invoice", "error"),
}
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
		from erpnext.setup.doctype.company_setup_step.company_setup_step import (
			CompanySetupStep,
		)

		chart_of_accounts_source: DF.Literal["", "Existing", "Imported"]
		company: DF.Link
		no_opening_data: DF.Check
		opening_balances: DF.Table[CompanySetupOpeningBalance]
		opening_purchase_invoices: DF.Table[CompanySetupOpeningInvoice]
		opening_sales_invoices: DF.Table[CompanySetupOpeningInvoice]
		migration_date: DF.Date | None
		status: DF.Literal["In Progress", "Live"]
		steps: DF.Table[CompanySetupStep]
	# end: auto-generated types

	_DOCTYPE_NAME = "Company Setup"

	def onload(self):
		self.set_onload("company_details", get_company_details(self.company))
		self.set_onload(
			"steps",
			{
				key: {
					"title": step["title"],
					"description": step["description"],
					"summary": step.get("summary"),
					"started": step.get("started"),
					"tab": step.get("tab"),
				}
				for key, step in get_steps(self).items()
			},
		)
		self.set_onload("chart_import_blocked", get_chart_import_blocker(self.company))
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
		"""Only posting changes whether a row is posted; a form save keeps what is stored."""
		if self.flags.posting or self.is_new():
			return
		for table, fields in POSTING_FIELDS.items():
			stored = {
				row.name: row
				for row in frappe.get_all(
					self.meta.get_field(table).options,
					filters={"parent": self.name, "parenttype": self.doctype, "parentfield": table},
					fields=["name", *fields],
				)
			}
			for row in self.get(table):
				saved = stored.get(row.name) or {}
				row.update({field: saved.get(field) for field in fields})
				row.status = row.status or "Draft"

	def validate_migration_date(self):
		"""Posted entries are dated the day before the migration date, so it can't move after."""
		if self.is_new() or not self.has_value_changed("migration_date"):
			return
		if any(row.status in ("Posted", "Created") for table in POSTING_FIELDS for row in self.get(table)):
			frappe.throw(
				_(
					"Opening entries are posted already on {0}, so the migration date can't change now."
				).format(formatdate(add_days(self.get_doc_before_save().migration_date, -1)))
			)

	def validate_opening_invoices(self):
		"""A draft invoice was unpaid before the migration date, so it is dated before it and owes
		something, or is an advance paid before it."""
		day_before = self.migration_date and getdate(add_days(self.migration_date, -1))
		for table in TABLES.values():
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
					frappe.throw(
						_("Row {0}: invoice {1} is dated after {2}, so it belongs in the new books.").format(
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
			if step["done"]:
				status = "Done"
			elif row.status == "Skipped":
				status = "Skipped"
			else:
				status = "Not Started"

			if row.status != status:
				row.last_checked = now_datetime()
			row.status = status
			row.step = step["title"]
			self.append("steps", row)

		for idx, row in enumerate(self.steps, 1):
			row.idx = idx

	@frappe.whitelist()
	def check_steps(self) -> bool:
		"""Save when a step's status changed. The form calls this each time it opens."""
		before = [(row.step_key, row.status) for row in self.steps]
		self.update_steps()
		changed = before != [(row.step_key, row.status) for row in self.steps]
		if changed and self.has_permission("write"):
			self.save()
		return changed

	@frappe.whitelist()
	def skip_step(self, step_key: str, skip: int = 1):
		"""Skip a step, or bring a skipped step back. A step that is done stays done."""
		row = next((row for row in self.steps if row.step_key == step_key), None)
		if not row:
			frappe.throw(_("Step {0} not found").format(step_key))
		row.status = "Skipped" if cint(skip) else "Not Started"
		self.save()

	@frappe.whitelist()
	def use_existing_chart(self):
		"""Keep the chart of accounts made during setup."""
		self.chart_of_accounts_source = "Existing"
		self.save()

	@frappe.whitelist()
	def change_chart_choice(self):
		"""Undo the chart choice, so the step asks again."""
		self.chart_of_accounts_source = ""
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
		}

	@frappe.whitelist()
	def import_chart(self, file_url: str) -> dict:
		"""Replace the chart with the sheet's. Its balances become draft opening balances, and the
		accounts ERPNext needs that the sheet lacks are added."""
		self.check_permission("write")
		sheet = read_chart_sheet(file_url)
		old_names = {
			row.account: frappe.db.get_value("Account", row.account, "account_name")
			for row in self.opening_balances
			if row.account
		}

		replace_chart(sheet.rows, self.company, refill_defaults=True)
		added = add_system_accounts(self.company)
		self.chart_of_accounts_source = "Imported"
		if sheet.balances:
			self.opening_balances = []
			for row in sort_balances(self.company, sheet.balances).rows:
				self.append("opening_balances", row)
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
	def save_opening_balances(self, rows: list | str):
		"""Replace the draft opening balances with these rows. Posted ones stay as they are."""
		self.check_permission("write")
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
					},
				)
		self.save()

	@frappe.whitelist()
	def import_opening_balances(self, file_url: str) -> dict:
		"""Replace the draft opening balances with a sheet's, matched to the chart by number and name."""
		self.check_permission("write")
		balances = sort_balances(self.company, read_balance_sheet(file_url))
		self.save_opening_balances(balances.rows)
		return balances.result

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
		# invoices land on their customer's or supplier's account; one without an enabled one won't post
		draft_invoices, left_out = 0, 0
		for party_type, sign in (("Customer", 1), ("Supplier", -1)):
			parties, accounts = get_parties(party_type), {}
			for row in self.get(TABLES[party_type]):
				if row.status == "Created":
					continue
				party = row.party if row.party in parties.names else find_party(parties, row.party_name)
				if not party or party in parties.disabled:
					left_out += 1
					continue
				if party not in accounts:
					accounts[party] = get_party_account(party_type, party, self.company)
				drafts.append((accounts[party], sign * flt(row.outstanding_amount)))
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
			frappe.throw(_("Pick an account for: {0}").format(", ".join(missing)))

		ensure_fiscal_year(add_days(self.migration_date, -1), self.company)
		temporary = get_temporary_account(self.company, create=True)
		cost_center = frappe.get_cached_value("Company", self.company, "cost_center")
		root_types = dict(
			frappe.get_all(
				"Account",
				filters={"name": ("in", [row.account for row in drafts])},
				fields=["name", "root_type"],
				as_list=True,
			)
		)
		profit_and_loss = [row for row in drafts if root_types.get(row.account) in ("Income", "Expense")]
		journal_entries = []
		for is_opening, rows in (
			("Yes", [row for row in drafts if row not in profit_and_loss]),
			("No", profit_and_loss),
		):
			if not rows:
				continue
			je = frappe.new_doc("Journal Entry")
			je.update(
				{
					"voucher_type": "Opening Entry" if is_opening == "Yes" else "Journal Entry",
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
	def save_opening_invoices(self, party_type: str, rows: list | str) -> int:
		"""Replace the draft opening invoices with these rows. Posted ones stay as they are, and a row
		that is one of them again is left out. Returns how many were left out."""
		self.check_permission("write")
		table = get_table(party_type)
		parties = get_parties(party_type)
		posted = [row for row in self.get(table) if row.status == "Created"]
		posted_keys = {get_invoice_key(row) for row in posted}
		self.set(table, posted)
		skipped = 0
		for row in frappe.parse_json(rows):
			row = frappe._dict(row)
			name = " ".join(cstr(row.party_name).split())
			if not name and not flt(row.outstanding_amount):
				continue
			if get_invoice_key(row) in posted_keys:
				skipped += 1
				continue
			self.append(
				table,
				{
					"party_type": party_type,
					"party_name": name,
					"party": row.party if row.party in parties.names else find_party(parties, name),
					"invoice_number": row.invoice_number,
					"invoice_date": row.invoice_date,
					"due_date": row.due_date,
					"outstanding_amount": flt(row.outstanding_amount),
				},
			)
		self.save()
		return skipped

	@frappe.whitelist()
	def import_opening_invoices(self, party_type: str, file_url: str) -> dict:
		"""Replace the draft opening invoices with a receivables or payables report's."""
		self.check_permission("write")
		sheet = read_invoice_sheet(file_url, party_type)
		skipped = self.save_opening_invoices(party_type, sheet.invoices)
		return {"added": len(sheet.invoices) - skipped, "advances": sheet.advances, "skipped": skipped}

	@frappe.whitelist()
	def get_invoice_names(self, party_type: str) -> dict:
		"""The draft rows' names: new ones with their invoice count and total, the existing name a new
		one probably means, rows linked by a similar name, rows whose party was added since, and
		disabled parties."""
		self.check_permission("read")
		parties = get_parties(party_type)
		new, similar, found = {}, {}, {}
		for row in self.get(get_table(party_type)):
			if row.status == "Created":
				continue
			match = find_party(parties, row.party_name)
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
		return {
			"new": sorted(new),
			"totals": new,
			"suggestions": get_suggestions(parties, list(new)),
			"similar": similar,
			"found": found,
			"disabled": sorted((drafts | set(found.values())) & parties.disabled),
		}

	@frappe.whitelist()
	def create_opening_invoices(self, party_type: str, add_missing: bool = False) -> str:
		"""Post the draft rows as opening invoices: right away, or in the background when many. With
		add_missing, first add the customers or suppliers ERPNext doesn't have yet."""
		self.check_permission("write")
		add_missing = sbool(add_missing)
		if add_missing and not frappe.has_permission(party_type, "create"):
			frappe.throw(
				_("You can't add a {0}. Ask someone who can, or post without adding them.").format(
					_(party_type)
				),
				frappe.PermissionError,
			)
		drafts = [row for row in self.get(get_table(party_type)) if row.status != "Created"]
		if not drafts:
			frappe.throw(_("There are no invoices to post."))
		if not self.migration_date:
			frappe.throw(
				_("Set the migration date first, so invoices without a date can be dated the day before.")
			)
		if len(drafts) < BACKGROUND_FROM:
			create_invoices(self.company, party_type, add_missing)
			return "done"
		frappe.enqueue(
			create_invoices_in_background,
			queue="long",
			job_id=f"company_setup_invoices::{self.company}::{party_type}",
			deduplicate=True,
			company=self.company,
			party_type=party_type,
			add_missing=add_missing,
			now=frappe.in_test,
		)
		return "queued"

	@frappe.whitelist()
	def go_live(self, accept_temporary_opening: bool = False):
		"""Mark the company as working in ERPNext. Whatever is left is only pointed out, but an
		amount left in Temporary Opening has to be accepted on purpose."""
		self.check_permission("write")
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
	"""Every step in order, keyed by its path, with title, description and done."""
	return {path: frappe.get_attr(path)(onboarding) for path in get_step_paths()}


def sort_balances(company: str, balances: list[dict]) -> frappe._dict:
	"""Split a sheet's balances into rows to keep and amounts left out: customers, suppliers and stock
	come in through their own invoices and entries, and group lines only total their sub-accounts."""
	accounts = {
		account.name: account
		for account in frappe.get_all(
			"Account",
			filters={"company": company},
			fields=["name", "account_name", "account_type", "is_group", "lft", "rgt"],
		)
	}
	rows, unmatched, groups = [], [], 0
	left_out = {}
	for balance in balances:
		name = find_account(company, balance.account_number, balance.account_name)
		account = accounts.get(name) or frappe._dict()
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
		else:
			if not name:
				unmatched.append(balance.label)
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
			"groups": groups,
			"left_out": {account_type: amount for account_type, amount in left_out.items() if amount},
		},
	)


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


def find_account(company: str, account_number: str, account_name: str) -> str | None:
	"""The account of the company with this number and name, or with this name when the number differs."""
	name = get_account_autoname(account_number, account_name, company)
	if frappe.db.exists("Account", name):
		return name
	return frappe.db.get_value("Account", {"company": company, "account_name": account_name})


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
