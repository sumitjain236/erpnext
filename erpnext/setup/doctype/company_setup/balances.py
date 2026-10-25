# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.utils import cstr, flt

from erpnext.accounts.doctype.account.account import get_account_autoname
from erpnext.accounts.party import get_party_account
from erpnext.setup.doctype.company_setup import bank_accounts as banks
from erpnext.setup.doctype.company_setup.chart_sheet import (
	NO_OPENING_BALANCE,
	get_file_label,
	get_party_group_type,
	read_balance_sheet,
)
from erpnext.setup.doctype.company_setup.company_setup_steps import money_precision
from erpnext.setup.doctype.company_setup.opening_invoices import (
	TABLES,
	append_line,
	ensure_fiscal_year,
	find_party,
	get_day_before,
	get_default_dimensions,
	get_hand_posted,
	get_parties,
	get_party_accounts,
	get_posting_blocker,
	get_singular_key,
	get_temporary_account,
	get_waiting_parties,
	normalize_account_name,
)


class BalancesMixin:
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

	def set_sheet_totals(self, left_out: dict, by_account: dict):
		"""Keep what the sheet says customers owe, suppliers are owed and stock is worth, in all and
		by account, to set against what their own steps post."""
		self.sheet_receivables = flt(left_out.get("Receivable"))
		self.sheet_payables = -flt(left_out.get("Payable"))
		self.sheet_stock = flt(left_out.get("Stock"))
		self.sheet_accounts = by_account

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
			fields=[
				"name",
				"account_name",
				"account_number",
				"parent_account",
				"root_type",
				"is_group",
				"account_type",
			],
			order_by="lft",
		)
		names = {account.name: account.account_name for account in accounts}
		in_books = {}
		for account in accounts:
			if account.account_type in ("Receivable", "Payable", "Stock"):
				in_books[account.account_type] = flt(in_books.get(account.account_type)) + flt(
					balances.get(account.name)
				)
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
			# what customers, suppliers and stock still to come leave on Temporary Opening, by the sheet
			"waiting": {
				"Receivable": flt(self.sheet_receivables) - flt(in_books.get("Receivable")),
				"Payable": flt(self.sheet_payables) + flt(in_books.get("Payable")),
				"Stock": flt(self.sheet_stock) - flt(in_books.get("Stock")),
			},
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
				"parent_account",
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
					accounts.values(), balance.account_name, balance.get("groups", []), root_type, side
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
COMMON_KEYS = set(get_singular_key(" ".join(f"{word} {word}s" for word in COMMON_WORDS)).split())


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
		mine, others = words - COMMON_KEYS, theirs - COMMON_KEYS
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
	accounts, account_name: str, groups: list[str], root_type: str | None, side: str | None
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
		"parent_account": pick_account_parent(accounts, account_type, root_type),
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
	company: str,
	account_type: str | None = None,
	root_type: str | None = None,
	account_name: str | None = None,
) -> str | None:
	"""The group a new ledger of this type goes under, on its side (root type): the group of that type,
	like Bank Accounts, or one named for it, like Fixed Assets or Duties and Taxes; else Current
	Assets or Current Liabilities and the like; else the top-level group. With no side, the side is
	read from the account name, like "Staff Welfare Expenses"; None when that says none either."""
	frappe.has_permission("Company Setup", "read", company, throw=True)
	root_type = root_type or (account_name and get_sheet_root_type([account_name]))
	if account_name is not None and not root_type and not account_type:
		return None
	accounts = frappe.get_all(
		"Account",
		filters={"company": company, "is_group": 1, "disabled": 0},
		fields=["name", "account_name", "account_type", "root_type", "is_group", "parent_account", "lft"],
	)
	return pick_account_parent(accounts, account_type, root_type)


def pick_account_parent(accounts, account_type: str | None, root_type: str | None) -> str | None:
	"""get_account_parent from the company's accounts already loaded, so a sheet's lines need no query each."""
	groups = sorted(
		(
			account
			for account in accounts
			if account.is_group and not account.disabled and (not root_type or account.root_type == root_type)
		),
		key=lambda account: account.lft,
	)

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
		fields=[
			"name",
			"account_name",
			"account_type",
			"root_type",
			"is_group",
			"disabled",
			"parent_account",
			"lft",
		],
	)
	root_types = (frappe.parse_json(root_types) if isinstance(root_types, str) else root_types) or {}
	sides = (frappe.parse_json(sides) if isinstance(sides, str) else sides) or {}
	suggestions, new_accounts = {}, {}
	for label in frappe.parse_json(labels) if isinstance(labels, str) else labels:
		name = get_label_name(label)
		suggestion = suggest_account(name, accounts, root_types.get(label), sides.get(label))
		if suggestion:
			suggestions[label] = suggestion
		elif new_account := suggest_new_account(accounts, name, [], root_types.get(label), sides.get(label)):
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


def get_party_account_type(name: str) -> str | None:
	"""Receivable for a customer's name and Payable for a supplier's: a trial balance often lists them."""
	if name and frappe.db.exists("Customer", {"customer_name": name}):
		return "Receivable"
	if name and frappe.db.exists("Supplier", {"supplier_name": name}):
		return "Payable"
	return None
