# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Bank accounts: the bank details of each bank ledger, and the cheques and deposits the bank had
not cleared on the day before migration.

Bank Clearance and the bank reconciliation tools leave out opening entries, so an uncleared cheque
inside the opening balance could never be cleared. Each one is posted as its own Bank Entry against
Temporary Opening, and one opening entry per bank adds the amount back the other way, so the bank
balance and Temporary Opening do not change.
"""

import re
from itertools import groupby

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import add_months, cstr, flt, fmt_money, formatdate, getdate
from frappe.utils.background_jobs import is_job_enqueued

from erpnext.accounts.doctype.bank_account.bank_account import _set_closing_balance_as_per_statement
from erpnext.setup.doctype.company_setup.chart_sheet import (
	download_sheet,
	find_column,
	get_amount,
	get_cell,
	get_column_key,
	is_total_row,
	read_sheet,
)
from erpnext.setup.doctype.company_setup.opening_invoices import (
	append_lines,
	ensure_fiscal_year,
	get_cached,
	get_day_before,
	get_day_first,
	get_default_dimensions,
	get_posting_blocker,
	get_posting_fix,
	get_sheet_date,
	get_similar_key,
	get_temporary_account,
)
from erpnext.setup.doctype.company_setup.posted_entries import (
	cancel_doc,
	pop_kept_reasons,
	run_posting_job,
	run_undo_job,
)

# a group whose leaves are bank accounts, overdrafts or cash credits, like Tally's "Bank OD A/c";
# not bank loans or deposits
BANK_GROUP = re.compile(r"^(bank (accounts?|od|occ|overdrafts?)|overdrafts?|cash credits?)\b")
# the bank a ledger's name starts with, like "Axis Bank" or "Bank of Baroda"
NEW_BANK = re.compile(r"^(.*?\bbank(?:\s+of\s+[a-z]+)?)\b", re.IGNORECASE)
# what an uncleared cheques sheet may call each column, the likeliest first
CHEQUE_COLUMNS = {
	"bank_account": ("bank_account", "bank", "bank_ledger", "account", "ledger"),
	"entry_type": ("issued_deposited", "issued_or_deposited", "type", "vch_type", "voucher_type"),
	"reference_no": (
		"cheque_or_reference_no",
		"cheque_no",
		"cheque_number",
		"chq_no",
		"instrument_no",
		"reference_no",
		"ref_no",
		"reference",
	),
	"reference_date": ("cheque_date", "chq_date", "instrument_date", "reference_date", "date"),
	"party_name": ("party", "party_name", "particulars", "payee", "name"),
	# the voucher's own date, for a cheque whose date is blank (Busy)
	"voucher_date": ("date", "voucher_date", "vch_date"),
	"amount": ("amount",),
	# a bank book's two amount columns: money into the bank, and out of it
	"deposited": ("deposited", "deposit", "deposits", "receipts", "debit", "dr"),
	"issued": ("issued", "withdrawal", "withdrawals", "payments", "credit", "cr"),
}
CHEQUE_SHEET_COLUMNS = (
	"Bank Account",
	"Issued/Deposited",
	"Cheque or Reference No",
	"Date",
	"Party",
	"Amount",
)
# a cheque older than this may have lapsed: three months in India, six elsewhere
STALE_MONTHS = {"India": 3}
DEFAULT_STALE_MONTHS = 6
# a bank ledger that is a credit card, by its name: what it "issues" is a charge, not a cheque
CARD_LEDGER = re.compile(r"\b(credit card|card|amex|american express|visa|mastercard)\b", re.IGNORECASE)
# the countries whose English says "check"
CHECK_COUNTRIES = ("United States", "Canada")
# a section of a reconciliation report listing what the bank cleared already (QuickBooks)
CLEARED_SECTION = re.compile(r"\bcleared\b", re.IGNORECASE)
# words that make such a title one of what the bank had not cleared yet
UNCLEARED_WORDS = re.compile(r"\b(not|un|pending)\W*cleared\b|\bpending\b", re.IGNORECASE)
JOB_ID = "company_setup_cheques::{0}"
UNDO_JOB_ID = "company_setup_undo::{0}::cheques"


def get_stale_months(company: str) -> int:
	return STALE_MONTHS.get(frappe.get_cached_value("Company", company, "country"), DEFAULT_STALE_MONTHS)


def cheque_words(company: str, text: str) -> str:
	"""Text as the company's country writes it: "check" in the US and Canada."""
	if frappe.get_cached_value("Company", company, "country") not in CHECK_COUNTRIES:
		return text
	return text.replace("cheque", "check").replace("Cheque", "Check")


def get_card_ledgers(company: str) -> set[str]:
	"""The bank ledgers that are credit cards, as their Bank Account says; one without a Bank Account
	yet by its name."""
	ledgers = get_bank_ledgers(company)
	saved = get_company_bank_accounts(company, [ledger.name for ledger in ledgers])
	return {
		ledger.name
		for ledger in ledgers
		if (
			saved[ledger.name].is_credit_card
			if ledger.name in saved
			else CARD_LEDGER.search(ledger.account_name or "")
		)
	}


def get_bank_ledgers(company: str) -> list[frappe._dict]:
	"""The company's bank ledgers: accounts, overdrafts and cash credits of type Bank."""
	return frappe.get_all(
		"Account",
		filters={"company": company, "account_type": "Bank", "is_group": 0, "disabled": 0},
		fields=["name", "account_name", "root_type", "account_currency"],
		order_by="lft",
	)


def get_unmarked_accounts(company: str) -> list[str]:
	"""Ledgers with no account type under a bank, overdraft or cash credit group: bank accounts that
	ERPNext's bank tools would not offer."""
	accounts = frappe.get_all(
		"Account",
		filters={"company": company, "root_type": ("in", ("Asset", "Liability"))},
		fields=["name", "account_name", "account_type", "is_group", "disabled", "lft", "rgt"],
	)
	groups = [
		account
		for account in accounts
		if account.is_group
		and (account.account_type == "Bank" or BANK_GROUP.search(get_similar_key(account.account_name)))
	]
	return [
		account.name
		for account in sorted(accounts, key=lambda account: account.lft)
		if not account.is_group
		and not account.disabled
		and not account.account_type
		and any(group.lft < account.lft and account.rgt < group.rgt for group in groups)
	]


def mark_bank_accounts(company: str) -> list[str]:
	"""Make the unmarked ledgers bank accounts. Each keeps its parent, so an overdraft stays a liability."""
	marked = get_unmarked_accounts(company)
	for name in marked:
		account = frappe.get_doc("Account", name)
		account.account_type = "Bank"
		account.save()
	return marked


def get_balances(
	company: str, accounts: list[str] | None, date, in_account_currency: bool = False
) -> dict[str, float]:
	"""Debit minus credit of each account up to the date; of every account when accounts is None. In
	the company's currency, or with in_account_currency in each account's own, as a bank statement is."""
	if accounts is not None and not accounts:
		return {}
	gle = frappe.qb.DocType("GL Entry")
	debit, credit = (
		(gle.debit_in_account_currency, gle.credit_in_account_currency)
		if in_account_currency
		else (gle.debit, gle.credit)
	)
	query = (
		frappe.qb.from_(gle)
		.select(gle.account, Sum(debit) - Sum(credit))
		.where((gle.company == company) & (gle.is_cancelled == 0) & (gle.posting_date <= date))
		.groupby(gle.account)
	)
	if accounts is not None:
		query = query.where(gle.account.isin(accounts))
	return {account: flt(balance, 2) for account, balance in query.run()}


def get_lines(onboarding) -> dict[str, float]:
	"""Each account's line in the trial balance, as debit minus credit."""
	lines = {}
	for row in onboarding.opening_balances:
		if row.account:
			lines[row.account] = flt(lines.get(row.account)) + flt(row.debit) - flt(row.credit)
	return lines


def get_company_bank_accounts(company: str, ledgers: list[str]) -> dict[str, frappe._dict]:
	"""The company's Bank Accounts by their ledger."""
	return {
		bank_account.account: bank_account
		for bank_account in frappe.get_all(
			"Bank Account",
			filters={"account": ("in", ledgers or [""]), "is_company_account": 1, "company": company},
			fields=[
				"name",
				"account",
				"bank",
				"bank_account_no",
				"branch_code",
				"is_default",
				"is_credit_card",
			],
		)
	}


def get_statement_balances(onboarding, bank_accounts: dict[str, frappe._dict]) -> dict[str, float]:
	"""The statement balance typed for each ledger on the day before: in the bank details window, or
	else as kept on its Bank Account. A ledger with none is left out."""
	day_before = get_day_before(onboarding)
	kept = {
		balance.bank_account: flt(balance.balance)
		for balance in frappe.get_all(
			"Bank Account Balance",
			filters={
				"bank_account": ("in", [row.name for row in bank_accounts.values()] or [""]),
				"date": day_before,
			},
			fields=["bank_account", "balance"],
		)
	}
	balances = {
		account: kept[bank_account.name]
		for account, bank_account in bank_accounts.items()
		if bank_account.name in kept
	}
	for row in onboarding.bank_accounts:
		if row.has_statement_balance:
			balances[row.account] = flt(row.statement_balance)
		else:
			balances.pop(row.account, None)
	return balances


def get_bank_details(onboarding) -> dict:
	"""What the bank details window shows: one row per bank ledger, with what is saved for it."""
	company = onboarding.company
	day_before = get_day_before(onboarding)
	ledgers = get_bank_ledgers(company)
	names = [ledger.name for ledger in ledgers]
	saved = get_company_bank_accounts(company, names)
	drafts = {row.account: row for row in onboarding.bank_accounts}
	statements = get_statement_balances(onboarding, saved) if day_before else {}
	# set against the statement balance, so in each account's own currency
	books = get_balances(company, names, day_before, in_account_currency=True) if day_before else {}
	lines = get_lines(onboarding)
	banks = {get_similar_key(bank): bank for bank in frappe.get_all("Bank", pluck="name")}
	default_bank = frappe.get_cached_value("Company", company, "default_bank_account")
	rows = []
	for ledger in ledgers:
		source = drafts.get(ledger.name) or saved.get(ledger.name) or frappe._dict()
		bank = (
			source.bank
			or guess_bank(ledger.account_name, list(banks.values()))
			or guess_new_bank(ledger.account_name)
		)
		bank = banks.get(get_similar_key(bank), bank)
		rows.append(
			{
				"account": ledger.name,
				"account_name": ledger.account_name,
				"root_type": ledger.root_type,
				"account_currency": ledger.account_currency,
				"book": flt(books.get(ledger.name)),
				"line": lines.get(ledger.name),
				"bank": bank,
				"new_bank": bool(bank) and get_similar_key(bank) not in banks,
				"bank_account_no": source.bank_account_no,
				"branch_code": source.branch_code,
				"statement_balance": statements.get(ledger.name),
				"is_default": source.is_default
				if ledger.name in drafts
				else int(ledger.name == default_bank),
				# what was saved; else its name's guess, for the user to confirm
				"is_credit_card": source.is_credit_card
				if ledger.name in drafts or ledger.name in saved
				else int(bool(CARD_LEDGER.search(ledger.account_name or ""))),
				"bank_account": saved[ledger.name].name if ledger.name in saved else None,
			}
		)
	return {
		"rows": rows,
		"day_before": day_before,
		"unmarked": get_unmarked_accounts(company),
		"new_banks": sorted({row["bank"] for row in rows if row["new_bank"]}),
		"can_create_bank": frappe.has_permission("Bank", "create"),
		"default_bank": default_bank,
		"has_uncleared_cheques": onboarding.has_uncleared_cheques,
	}


def guess_bank(account_name: str, banks: list[str]) -> str | None:
	"""The Bank named in the ledger's name, like HDFC Bank in "HDFC Bank A/c": the longest one, so
	"State Bank of India A/c" is State Bank of India, not Bank of India. Only an exact Bank."""
	key = f" {get_similar_key(account_name)} "
	found = sorted(
		(bank for bank in banks if f" {get_similar_key(bank)} " in key), key=lambda bank: -len(bank)
	)
	if not found or (len(found) > 1 and len(found[0]) == len(found[1])):
		return None
	return found[0]


def guess_new_bank(account_name: str) -> str | None:
	"""The bank a ledger's name clearly names when ERPNext has no such Bank yet, like Axis Bank in
	"Axis Bank Current A/c": the words up to "Bank", and "of India" after it. Only a new name to show."""
	match = NEW_BANK.match(cstr(account_name).strip())
	name = match and " ".join(match.group(1).split())
	return name if name and name.casefold() != "bank" else None


def get_overdrawn(onboarding, ledgers: dict) -> set[str]:
	"""The bank ledgers that are overdrafts or cash credits, or that the books, with the trial
	balance's draft lines, show overdrawn on the day before."""
	books = (
		get_balances(onboarding.company, list(ledgers), get_day_before(onboarding), in_account_currency=True)
		if onboarding.migration_date
		else {}
	)
	for row in onboarding.opening_balances:
		if row.status != "Posted" and row.account in ledgers:
			books[row.account] = flt(books.get(row.account)) + flt(row.debit) - flt(row.credit)
	return {
		name
		for name, ledger in ledgers.items()
		if ledger.root_type == "Liability" or flt(books.get(name)) < 0
	}


def save_bank_details(onboarding, rows: list[dict], has_uncleared_cheques: str | None, draft: bool):
	"""Keep the rows of the bank details window. Unless a draft, also make a Bank Account for each row
	with a bank (and the Bank when new), set the default bank and its payment modes, and keep each
	statement balance on its Bank Account."""
	ledgers = {ledger.name: ledger for ledger in get_bank_ledgers(onboarding.company)}
	overdrawn = get_overdrawn(onboarding, ledgers)
	onboarding.bank_accounts = []
	for row in rows:
		row = frappe._dict(row)
		if row.account not in ledgers:
			frappe.throw(
				_("{0} is not a bank account of {1}.").format(frappe.bold(row.account), onboarding.company),
				title=_("Not a bank account"),
			)
		onboarding.append(
			"bank_accounts",
			{
				"account": row.account,
				"bank": " ".join(cstr(row.bank).split()),
				"bank_account_no": cstr(row.bank_account_no).strip(),
				"branch_code": cstr(row.branch_code).strip(),
				# the bank shows what an overdrawn account owes as a plain number
				"statement_balance": -abs(flt(row.statement_balance))
				if row.account in overdrawn
				else flt(row.statement_balance),
				"has_statement_balance": 0 if row.statement_balance in (None, "") else 1,
				"is_default": 1 if row.is_default else 0,
				"is_credit_card": 1 if row.is_credit_card else 0,
			},
		)
	defaults = [row for row in onboarding.bank_accounts if row.is_default]
	if len(defaults) > 1:
		frappe.throw(_("Pick one default bank."), title=_("More than one default"))
	if defaults and not defaults[0].bank:
		frappe.throw(
			_("Pick a bank for {0}, or make another account the default.").format(
				frappe.bold(defaults[0].account)
			),
			title=_("Default bank has no bank"),
		)
	numbers = {}
	for row in onboarding.bank_accounts:
		if row.bank_account_no:
			numbers.setdefault((row.bank_account_no, row.branch_code), []).append(row.account)
	for (number, _code), accounts in numbers.items():
		if len(accounts) > 1:
			frappe.throw(
				_("Account number {0} is on {1}. Each bank account has its own number.").format(
					number, ", ".join(accounts)
				),
				title=_("Same account number"),
			)
	if has_uncleared_cheques is not None:
		onboarding.has_uncleared_cheques = has_uncleared_cheques
	if draft:
		return

	if not onboarding.has_uncleared_cheques:
		frappe.throw(
			cheque_words(
				onboarding.company, _("Say whether any cheques or deposits were not cleared by the bank.")
			),
			title=_("Answer the question"),
		)
	if onboarding.has_uncleared_cheques == "No":
		if any(row.status == "Posted" for row in onboarding.uncleared_cheques):
			frappe.throw(
				cheque_words(onboarding.company, _("Undo the posted cheques first.")),
				title=cheque_words(onboarding.company, _("Cheques are posted")),
			)
		onboarding.uncleared_cheques = []
	new_banks = sorted(
		{row.bank for row in onboarding.bank_accounts if row.bank and not frappe.db.exists("Bank", row.bank)}
	)
	if new_banks and not frappe.has_permission("Bank", "create"):
		frappe.throw(
			_(
				"You can't add a Bank, so {0} can't be made. Ask someone who can, or pick an existing Bank."
			).format(", ".join(new_banks)),
			frappe.PermissionError,
			title=_("Can't add a bank"),
		)
	day_before = get_day_before(onboarding)
	for row in onboarding.bank_accounts:
		if row.bank:
			row.bank_account = make_bank_account(onboarding.company, row, ledgers[row.account])
			if day_before:
				keep_statement_balance(row.bank_account, day_before, row)
	previous = frappe.get_cached_value("Company", onboarding.company, "default_bank_account")
	frappe.db.set_value(
		"Company", onboarding.company, "default_bank_account", defaults[0].account if defaults else None
	)
	if defaults:
		set_payment_modes(onboarding.company, ledgers[defaults[0].account], previous)


def get_statement_mismatches(onboarding, short: bool = False) -> list[str]:
	"""A line for each bank ledger whose typed statement balance is not its book balance on the day
	before with the uncleared cheques and deposits listed for it added back. short: one line pointing
	to the Bank accounts step, for lists of everything still open."""
	lines = []
	for account, off in get_statement_offs(onboarding).items():
		name, currency = frappe.get_cached_value("Account", account, ["account_name", "account_currency"])
		amount = fmt_money(abs(off), currency=currency, precision=0)
		if short:
			lines.append(_("{0}: statement off by {1} · see Bank accounts").format(name, amount))
			continue
		lines.append(
			(
				_("{0}: statement is {1} more than the books.")
				if off > 0
				else _("{0}: statement is {1} less than the books.")
			).format(name, amount)
			+ " "
			+ cheque_words(
				onboarding.company,
				_(
					"Usually a bank charge or interest not booked yet, or an uncleared cheque missing from the list. Check it, or go ahead."
				),
			)
		)
	return lines


def get_statement_offs(onboarding) -> dict[str, float]:
	"""By bank ledger, what its statement balance is more than its books, as get_statement_mismatches
	says it."""
	if not onboarding.migration_date:
		return {}
	day_before = get_day_before(onboarding)
	accounts = [row.account for row in onboarding.bank_accounts if row.has_statement_balance]
	# the books once the balances are posted, else the trial balance's line
	posted = bool(onboarding.opening_balances) and all(
		row.status == "Posted" for row in onboarding.opening_balances
	)
	books = (
		get_balances(onboarding.company, accounts, day_before, in_account_currency=True)
		if posted
		else get_lines(onboarding)
	)
	uncleared = {}
	for cheque in onboarding.uncleared_cheques:
		sign = 1 if cheque.entry_type == "Issued" else -1
		uncleared[cheque.bank_account] = uncleared.get(cheque.bank_account, 0) + sign * flt(cheque.amount)
	offs = {}
	for row in onboarding.bank_accounts:
		if not row.has_statement_balance or row.account not in books:
			continue
		off = flt(row.statement_balance) - flt(books[row.account]) - uncleared.get(row.account, 0)
		if abs(off) >= 1:
			offs[row.account] = off
	return offs


def make_bank_account(company: str, row, ledger: frappe._dict) -> str:
	"""Make or update the ledger's Bank Account, and its Bank when the name is new."""
	bank = (
		frappe.db.exists("Bank", row.bank)
		or frappe.get_doc({"doctype": "Bank", "bank_name": row.bank}).insert().name
	)
	name = frappe.db.get_value("Bank Account", {"account": row.account})
	doc = frappe.get_doc("Bank Account", name) if name else frappe.new_doc("Bank Account")
	if doc.is_new():
		# named "<account name> - <bank>": another company's ledger of the same name takes its full name
		doc.account_name = (
			ledger.name
			if frappe.db.exists("Bank Account", f"{ledger.account_name} - {bank}")
			else ledger.account_name
		)
	doc.update(
		{
			"bank": bank,
			"bank_account_no": row.bank_account_no,
			"branch_code": row.branch_code,
			"is_company_account": 1,
			"company": company,
			"account": row.account,
			"is_default": row.is_default,
			"is_credit_card": row.is_credit_card,
		}
	)
	doc.save()
	return doc.name


def keep_statement_balance(bank_account: str, date, row):
	"""Keep the statement balance where ERPNext's bank reconciliation reads it; one not typed is removed."""
	if row.has_statement_balance:
		_set_closing_balance_as_per_statement(bank_account, date, flt(row.statement_balance))
	else:
		frappe.db.delete("Bank Account Balance", {"bank_account": bank_account, "date": date})


def set_payment_modes(company: str, ledger: frappe._dict, previous: str | None = None):
	"""Point the payment modes of type Bank at the default bank, and those of type Cash at the default
	cash account, found by type so a "Check" or a translated name counts too. A mode already pointed at
	another account is left as it is, unless that was the previous default bank. The default cash
	account fills itself when the company has just one. A default bank in another currency is left out
	of the payment modes."""
	currency = frappe.get_cached_value("Company", company, "default_currency")
	bank = ledger.name if ledger.account_currency == currency else None
	cash = frappe.get_cached_value("Company", company, "default_cash_account")
	if not cash:
		accounts = frappe.get_all(
			"Account",
			filters={"company": company, "account_type": "Cash", "is_group": 0, "disabled": 0},
			pluck="name",
		)
		if len(accounts) == 1:
			cash = accounts[0]
			frappe.db.set_value("Company", company, "default_cash_account", cash)
	modes = frappe.get_all(
		"Mode of Payment", filters={"enabled": 1, "type": ("in", ("Bank", "Cash"))}, fields=["name", "type"]
	)
	for mode in modes:
		account = bank if mode.type == "Bank" else cash
		if not account:
			continue
		doc = frappe.get_doc("Mode of Payment", mode.name)
		row = next((row for row in doc.accounts if row.company == company), None)
		if row and row.default_account == account:
			continue
		if row and row.default_account and row.default_account != previous:
			continue
		if not row:
			row = doc.append("accounts", {"company": company})
		row.default_account = account
		doc.save()


def get_bank_status(onboarding) -> frappe._dict:
	"""Counts for the Bank accounts card and Go live."""
	return get_cached(onboarding, "bank_status", read_bank_status)


def read_bank_status(onboarding) -> frappe._dict:
	company = onboarding.company
	ledgers = [ledger.name for ledger in get_bank_ledgers(company)]
	saved = get_company_bank_accounts(company, ledgers)
	posted = [row for row in onboarding.uncleared_cheques if row.status == "Posted"]
	cards = get_card_ledgers(company)
	return frappe._dict(
		accounts=len(ledgers),
		added=len(saved),
		# a row with no bank gets no Bank Account
		no_bank=[
			ledger
			for ledger in ledgers
			if ledger not in saved
			and not any(row.bank for row in onboarding.bank_accounts if row.account == ledger)
		],
		without_number=sum(1 for bank_account in saved.values() if not bank_account.bank_account_no),
		unmarked=len(get_unmarked_accounts(company)),
		default_bank=frappe.get_cached_value("Company", company, "default_bank_account"),
		statement_off=get_statement_mismatches(onboarding),
		statement_off_short=get_statement_mismatches(onboarding, short=True),
		has_uncleared_cheques=onboarding.has_uncleared_cheques,
		drafts=sum(1 for row in onboarding.uncleared_cheques if row.status != "Posted"),
		cheques=sum(1 for row in posted if row.entry_type == "Issued" and row.bank_account not in cards),
		charges=sum(1 for row in posted if row.entry_type == "Issued" and row.bank_account in cards),
		deposits=sum(1 for row in posted if row.entry_type == "Deposited"),
		card_ledgers=sorted(cards),
		posting=is_posting_cheques(company),
	)


def get_bank_match(onboarding) -> dict | None:
	"""Each bank ledger's balance on the day before against its trial balance line, for Check your
	books. None without a trial balance."""
	return get_cached(onboarding, "bank_match", read_bank_match)


def read_bank_match(onboarding) -> dict | None:
	if not onboarding.opening_balances or not onboarding.migration_date:
		return None
	ledgers = [ledger.name for ledger in get_bank_ledgers(onboarding.company)]
	lines = get_lines(onboarding)
	books = get_balances(onboarding.company, ledgers, get_day_before(onboarding))
	rows = [
		{
			"account": account,
			"books": flt(books.get(account)),
			"line": flt(lines.get(account)),
			"matches": abs(flt(books.get(account)) - flt(lines.get(account))) < 1,
		}
		for account in ledgers
		if account in lines or flt(books.get(account))
	]
	return {
		"lines": rows,
		"matched": sum(1 for row in rows if row["matches"]),
		"total": len(rows),
		"statement_off": [
			{
				"account": account,
				"off": off,
				"currency": frappe.get_cached_value("Account", account, "account_currency"),
			}
			for account, off in get_statement_offs(onboarding).items()
		],
	}


def save_uncleared_cheques(onboarding, rows: list[dict], source: str | None = None) -> int:
	"""Replace the draft cheques and deposits with these rows. Posted ones stay; a row that is one of
	them again is left out. Returns how many were left out."""
	posted = [row for row in onboarding.uncleared_cheques if row.status == "Posted"]
	posted_dates = {}
	for row in posted:
		posted_dates.setdefault(get_cheque_key(row), set()).add(get_cheque_date(row))
	onboarding.set("uncleared_cheques", posted)
	skipped = 0
	for row in rows:
		row = frappe._dict(row)
		if not row.bank_account and not flt(row.amount):
			continue
		dates, date = posted_dates.get(get_cheque_key(row)), get_cheque_date(row)
		if dates and (not date or None in dates or date in dates):
			skipped += 1
			continue
		onboarding.append(
			"uncleared_cheques",
			{
				"bank_account": row.bank_account,
				"entry_type": row.entry_type,
				"reference_no": cstr(row.reference_no).strip(),
				"reference_date": row.reference_date,
				"party_name": " ".join(cstr(row.party_name).split()),
				"amount": flt(row.amount),
				"source": row.source or source,
				"status": "Draft",
			},
		)
	return skipped


def validate_uncleared_cheques(onboarding):
	"""Each draft row is on a bank ledger of the company, with an amount."""
	ledgers = {ledger.name for ledger in get_bank_ledgers(onboarding.company)}
	for row in onboarding.uncleared_cheques:
		if row.status == "Posted":
			continue
		if row.bank_account not in ledgers:
			frappe.throw(
				_("Row {0}: {1} is not a bank account of {2}.").format(
					row.idx, frappe.bold(row.bank_account), onboarding.company
				),
				title=_("Not a bank account"),
			)
		if flt(row.amount) <= 0:
			frappe.throw(
				_("Row {0}: the amount of {1} must be more than zero.").format(
					row.idx, frappe.bold(row.reference_no or row.party_name or row.bank_account)
				),
				title=_("No amount"),
			)


def get_cheque_key(row) -> tuple:
	"""What makes two rows the same cheque or deposit, with the same date when both have one: a date
	missing on the sheet may have been filled in before posting."""
	return (
		row.get("bank_account"),
		row.get("entry_type") or "",
		cstr(row.get("reference_no")).strip(),
		flt(row.get("amount"), 2),
	)


def get_cheque_date(row):
	return getdate(row.get("reference_date")) if row.get("reference_date") else None


def read_cheque_sheet(company: str, file_url: str, bank_account: str | None = None) -> frappe._dict:
	"""The rows of an uncleared cheques sheet, or a bank reconciliation report's uncleared items
	(Tally, Busy, QuickBooks). A sheet without a bank column is for the bank its title names, else
	for bank_account. Rows whose bank is not found are left out and named, and so are a report's
	balance lines and the sections of what the bank cleared already."""
	report, keys = read_sheet(file_url, get_cheque_column_key)
	rows = [row.cells for row in report]
	headings = [i for i, row in enumerate(keys) if is_cheque_heading(row) and is_text_row(rows[i])]
	if not headings:
		frappe.throw(
			_("The sheet needs an Amount column, or Deposited and Issued columns."),
			title=_("Columns not found"),
		)
	start = headings[0]
	columns = get_cheque_columns(keys[start])
	ledgers = get_bank_ledgers(company)
	title_bank, no_bank_column = None, columns["bank_account"] is None
	if no_bank_column:
		title_bank = find_title_bank(rows[:start], ledgers)
		bank_account = title_bank or bank_account or (ledgers[0].name if len(ledgers) == 1 else None)
		if not bank_account:
			frappe.throw(
				_("This sheet has no bank column. Pick its bank account, then upload it again."),
				title=_("Which bank?"),
			)
	# each date column read the way it writes dates, whatever the site's format
	day_first = {
		index: get_day_first(get_cell(row, index) for row in rows[start + 1 :])
		for index in range(max(len(row) for row in rows))
	}
	cheques, unknown, section, skip = [], set(), "", False
	for index, row in enumerate(rows):
		if index in headings:
			columns = get_cheque_columns(keys[index])
			skip = bool(CLEARED_SECTION.search(section)) and not UNCLEARED_WORDS.search(section)
			continue
		filled = [value for value in row if cstr(value).strip()]
		if len(filled) == 1 and isinstance(filled[0], str):
			section = filled[0]
			continue
		if index < start or skip:
			continue
		if is_total_row(row):
			continue
		entry_type, amount = get_cheque_amount(row, columns)
		if not amount:
			continue
		name = cstr(get_cell(row, columns["bank_account"])).strip()
		account = find_bank_ledger(name, ledgers) if name else bank_account
		reference_no = cstr(get_cell(row, columns["reference_no"])).strip()
		party_name = cstr(get_cell(row, columns["party_name"])).strip()
		reference_date = get_sheet_date(
			get_cell(row, columns["reference_date"]), day_first.get(columns["reference_date"])
		) or get_sheet_date(get_cell(row, columns["voucher_date"]), day_first.get(columns["voucher_date"]))
		# a report's own lines, like Tally's "Balance as per Bank", name no cheque
		if not (reference_no or party_name or reference_date):
			continue
		if not account:
			unknown.add(name or _("No bank account"))
			continue
		cheques.append(
			{
				"bank_account": account,
				"entry_type": entry_type,
				"reference_no": reference_no,
				"reference_date": reference_date,
				"party_name": party_name,
				"amount": amount,
			}
		)
	return frappe._dict(
		cheques=cheques,
		unknown_banks=sorted(unknown),
		no_bank_column=no_bank_column,
		title_bank=title_bank,
	)


def get_cheque_column_key(value) -> str:
	"""A heading without what it says in brackets, so "Debit (Rs.)" is debit and "AMOUNT (USD)" amount."""
	return get_column_key(re.sub(r"\(.*?\)", " ", value) if isinstance(value, str) else value)


def is_text_row(cells: list) -> bool:
	"""A row of headings has only text; a row of a report has an amount."""
	return all(isinstance(value, str) for value in cells if value not in (None, ""))


def get_cheque_columns(headers: list[str]) -> dict:
	columns = {field: find_column(headers, names) for field, names in CHEQUE_COLUMNS.items()}
	if columns["voucher_date"] == columns["reference_date"]:
		columns["voucher_date"] = None
	return columns


def find_title_bank(rows: list[list], ledgers: list[frappe._dict]) -> str | None:
	"""The bank ledger a report's title rows name, like "Bank Reconciliation - ICICI Bank OD A/c
	7741": the longest name found, when only one is that long."""
	titles = [f" {get_similar_key(value)} " for row in rows for value in row if isinstance(value, str)]
	found = sorted(
		(
			(len(key), ledger.name)
			for ledger in ledgers
			if (key := get_similar_key(ledger.account_name)) and any(f" {key} " in title for title in titles)
		),
		reverse=True,
	)
	if not found or (len(found) > 1 and found[0][0] == found[1][0]):
		return None
	return found[0][1]


def is_cheque_heading(keys: list[str]) -> bool:
	return sum(1 for key in keys if key) > 1 and (
		find_column(keys, CHEQUE_COLUMNS["amount"]) is not None
		or (
			find_column(keys, CHEQUE_COLUMNS["deposited"]) is not None
			and find_column(keys, CHEQUE_COLUMNS["issued"]) is not None
		)
	)


def get_cheque_amount(row: list, columns: dict) -> tuple[str | None, float]:
	"""Issued or Deposited, and the amount: from the amount and its type column, or from the bank
	book's two amount columns, where money into the bank is a deposit. With no type column the sign
	says which; a type cell that is unknown leaves it to the sign when the amount is negative (money
	out, as QuickBooks shows it), else empty, for the user to fill."""
	if columns["amount"] is not None:
		amount = get_amount(get_cell(row, columns["amount"]))
		if columns["entry_type"] is None:
			return ("Deposited" if amount > 0 else "Issued"), abs(amount)
		kind = cstr(get_cell(row, columns["entry_type"])).lower()
		if any(word in kind for word in ("deposit", "receipt")):
			return "Deposited", abs(amount)
		if any(word in kind for word in ("issue", "payment", "cheque", "check", "expense", "charge")):
			return "Issued", abs(amount)
		return ("Issued" if amount < 0 else None), abs(amount)
	deposited = get_amount(get_cell(row, columns["deposited"]))
	if deposited:
		return "Deposited", abs(deposited)
	return "Issued", abs(get_amount(get_cell(row, columns["issued"])))


def find_bank_ledger(name: str, ledgers: list[frappe._dict]) -> str | None:
	"""The bank ledger a sheet names: by its name, or its name without the company's abbreviation."""
	key = get_similar_key(name)
	found = [
		ledger.name
		for ledger in ledgers
		if key in (get_similar_key(ledger.name), get_similar_key(ledger.account_name))
	]
	return found[0] if len(found) == 1 else None


@frappe.whitelist()
def download_uncleared_cheques(company: str, undone_from: str | None = None):
	"""A sheet to fill with uncleared cheques and deposits, with the drafts already entered. With
	undone_from, the rows undone from that source instead, to fix and upload again."""
	onboarding = frappe.get_doc("Company Setup", company)
	onboarding.check_permission("read")
	if undone_from is not None:
		rows = [row for row in onboarding.undone_cheques if (row.source or "") == undone_from]
	else:
		rows = [row for row in onboarding.uncleared_cheques if row.status != "Posted"]
	download_sheet(
		f"{frappe.scrub(company)}_uncleared_cheques",
		[
			list(CHEQUE_SHEET_COLUMNS),
			*(
				[
					row.bank_account,
					row.entry_type,
					row.reference_no,
					row.reference_date,
					row.party_name,
					row.amount,
				]
				for row in rows
			),
		],
		_("Uncleared Cheques"),
	)


def get_cheque_checks(onboarding) -> dict:
	"""What the uncleared cheques window shows for each bank: the book balance on the day before,
	what is uncleared, and the statement balance worked out from them against the one typed. With the
	reds that stop posting and the lines that only point something out."""
	company = onboarding.company
	day_before = get_day_before(onboarding)
	ledgers = get_bank_ledgers(company)
	names = [ledger.name for ledger in ledgers]
	saved = get_company_bank_accounts(company, names)
	statements = get_statement_balances(onboarding, saved)
	books = get_balances(company, names, day_before, in_account_currency=True)
	# until the balances are posted, a bank's book balance is as it will be: with its draft line
	from_sheet = set()
	for row in onboarding.opening_balances:
		if row.status != "Posted" and row.account in names:
			books[row.account] = flt(books.get(row.account)) + flt(row.debit) - flt(row.credit)
			from_sheet.add(row.account)
	currency = frappe.get_cached_value("Company", company, "default_currency")
	rows = onboarding.uncleared_cheques
	banks = []
	for ledger in ledgers:
		mine = [row for row in rows if row.bank_account == ledger.name]
		issued = [row for row in mine if row.entry_type == "Issued"]
		deposited = [row for row in mine if row.entry_type == "Deposited"]
		worked_out = flt(
			flt(books.get(ledger.name))
			+ sum(flt(row.amount) for row in issued)
			- sum(flt(row.amount) for row in deposited),
			2,
		)
		statement = statements.get(ledger.name)
		off = flt(worked_out - statement, 2) if statement is not None else None
		banks.append(
			{
				"account": ledger.name,
				"bank": saved[ledger.name].bank if ledger.name in saved else None,
				"account_currency": ledger.account_currency,
				"book": flt(books.get(ledger.name)),
				"book_from_sheet": ledger.name in from_sheet,
				"issued": sum(flt(row.amount) for row in issued),
				"issued_count": len(issued),
				"deposited": sum(flt(row.amount) for row in deposited),
				"deposited_count": len(deposited),
				"worked_out": worked_out,
				"statement": statement,
				"off": off if off is not None and abs(off) >= 1 else (0 if off is not None else None),
			}
		)
	drafts = [row for row in rows if row.status != "Posted"]
	missing_date = {}
	for row in drafts:
		if not row.reference_date:
			missing_date[row.bank_account] = missing_date.get(row.bank_account, 0) + 1
	foreign = {ledger.name for ledger in ledgers if ledger.account_currency != currency}
	stale_months = get_stale_months(company)
	stale_before = getdate(add_months(day_before, -stale_months))
	# a cheque posted on one bank, again on another: a sheet's rows put on the wrong bank
	posted_keys = {
		(row.entry_type, row.reference_no, flt(row.amount, 2)): row.bank_account
		for row in rows
		if row.status == "Posted" and row.reference_no
	}
	return {
		"day_before": day_before,
		"banks": banks,
		"drafts": len(drafts),
		"missing_date": missing_date,
		"missing_type": sum(1 for row in drafts if not row.entry_type),
		"other_currency": sum(1 for row in drafts if row.bank_account in foreign),
		"no_reference": sum(1 for row in drafts if not row.reference_no),
		"stale": sum(
			1 for row in drafts if row.reference_date and getdate(row.reference_date) < stale_before
		),
		"stale_months": stale_months,
		"posted_elsewhere": [
			{"name": row.name, "bank_account": row.bank_account, "posted_on": posted_on}
			for row in drafts
			if row.reference_no
			and (posted_on := posted_keys.get((row.entry_type, row.reference_no, flt(row.amount, 2))))
			and posted_on != row.bank_account
		],
		"post_dated": sum(
			1 for row in drafts if row.reference_date and getdate(row.reference_date) > day_before
		),
		"balances_posted": bool(onboarding.opening_balances)
		and all(row.status == "Posted" for row in onboarding.opening_balances),
		"posting_blocker": get_posting_blocker(company, day_before),
		"posting_fix": get_posting_fix(company, day_before),
	}


def get_cheque_blocker(onboarding, checks: dict) -> str | None:
	"""Why the drafts can't be posted, or None. One reason at a time, the first that applies."""
	if not checks["balances_posted"]:
		return cheque_words(onboarding.company, _("Post opening balances first to add uncleared cheques."))
	if checks["posting_blocker"]:
		return checks["posting_blocker"]
	if checks["posted_elsewhere"]:
		count = len(checks["posted_elsewhere"])
		return (
			_("1 row is posted already on another bank. Remove it, or fix its bank.")
			if count == 1
			else _("{0} rows are posted already on another bank. Remove them, or fix their bank.").format(
				count
			)
		)
	if checks["other_currency"] == 1:
		return _("Foreign currency bank accounts come later. Remove 1 row.")
	if checks["other_currency"]:
		return _("Foreign currency bank accounts come later. Remove {0} rows.").format(
			checks["other_currency"]
		)
	if checks["missing_type"] == 1:
		return _("Issued or deposited missing for 1 row.")
	if checks["missing_type"]:
		return _("Issued or deposited missing for {0} rows.").format(checks["missing_type"])
	missing = checks["missing_date"]
	if len(missing) == 1:
		account, count = next(iter(missing.items()))
		name = frappe.get_cached_value("Account", account, "account_name")
		if count == 1:
			return _("Date missing for 1 row of {0}.").format(name)
		return _("Date missing for {0} rows of {1}.").format(count, name)
	if missing:
		return _("Date missing for {0} rows of {1} banks.").format(sum(missing.values()), len(missing))
	return None


def is_posting_cheques(company: str) -> bool:
	return is_job_enqueued(JOB_ID.format(company)) or is_job_enqueued(UNDO_JOB_ID.format(company))


def post_cheques(company: str):
	"""Post the draft rows, all or none: per bank, one Bank Entry for each cheque or deposit, so Bank
	Clearance can clear it, and one opening entry for the net amount the other way. All dated the day
	before migration, against Temporary Opening, so they net to zero."""
	onboarding = frappe.get_doc("Company Setup", company)
	drafts = [row for row in onboarding.uncleared_cheques if row.status != "Posted"]
	if not drafts:
		frappe.throw(
			cheque_words(company, _("There are no cheques or deposits to post.")), title=_("Nothing to post")
		)
	validate_uncleared_cheques(onboarding)
	blocker = get_cheque_blocker(onboarding, get_cheque_checks(onboarding))
	if blocker:
		frappe.throw(blocker, title=_("Can't post yet"))
	day_before = get_day_before(onboarding)
	ensure_fiscal_year(day_before, company)
	temporary = get_temporary_account(company, create=True)
	dimensions = get_default_dimensions(company)
	cards = get_card_ledgers(company)
	drafts.sort(key=lambda row: (row.bank_account, row.idx))
	for account, rows in groupby(drafts, key=lambda row: row.bank_account):
		rows = list(rows)
		for row in rows:
			row.journal_entry = make_cheque_entry(
				company, row, day_before, temporary, dimensions, row.bank_account in cards
			)
			row.status = "Posted"
		opening_entry = make_opening_entry(company, account, rows, day_before, temporary, dimensions)
		for row in rows:
			row.opening_entry = opening_entry
	onboarding.has_uncleared_cheques = "Yes"
	onboarding.flags.posting = True
	onboarding.save()


def post_cheques_in_background(company: str):
	run_posting_job("company_setup_cheques", company, lambda onboarding: post_cheques(company))


def make_cheque_entry(
	company: str, row, posting_date, temporary: str, dimensions: dict, is_card: bool = False
) -> str:
	"""A Bank Entry for one uncleared cheque or deposit, a Credit Card Entry for a card's charge, with
	its number and date. No party moves."""
	# money out of the bank for a cheque issued, into it for a deposit
	amount = flt(row.amount) if row.entry_type == "Deposited" else -flt(row.amount)
	reference_date = row.reference_date or posting_date
	je = frappe.new_doc("Journal Entry")
	je.update(
		{
			"voucher_type": "Credit Card Entry" if is_card else "Bank Entry",
			"company": company,
			"posting_date": posting_date,
			"cheque_no": row.reference_no
			or (_("Deposit {0}") if row.entry_type == "Deposited" else _("Payment {0}")).format(
				formatdate(reference_date)
			),
			"cheque_date": reference_date,
			"user_remark": _("Uncleared on {0}, from Company Setup").format(formatdate(posting_date))
			+ (f": {row.party_name}" if row.party_name else ""),
		}
	)
	append_lines(je, row.bank_account, amount, temporary, dimensions)
	je.insert()
	je.submit()
	return je.name


def make_opening_entry(
	company: str,
	account: str,
	rows: list,
	posting_date,
	temporary: str,
	dimensions: dict,
	amended_from: str | None = None,
) -> str | None:
	"""The opening entry that adds back what these cheques and deposits take out of the bank, so the
	bank balance stays as in the old books. None when they cancel out."""
	amount = flt(sum(flt(row.amount) if row.entry_type == "Issued" else -flt(row.amount) for row in rows), 2)
	if not amount:
		return None
	je = frappe.new_doc("Journal Entry")
	je.update(
		{
			"voucher_type": "Opening Entry",
			"company": company,
			"posting_date": posting_date,
			"amended_from": amended_from,
			"user_remark": cheque_words(
				company, _("Uncleared cheques and deposits of {0}, from Company Setup").format(account)
			),
		}
	)
	append_lines(je, account, amount, temporary, dimensions)
	je.insert()
	je.submit()
	return je.name


def get_posted_cheques(onboarding) -> dict:
	"""The posted cheques and deposits by source, each with its rows, whether the bank cleared it,
	and the undone ones."""
	rows = [row for row in onboarding.uncleared_cheques if row.status == "Posted" and row.journal_entry]
	entries = {
		entry.name: entry
		for entry in frappe.get_all(
			"Journal Entry",
			filters={"name": ("in", [row.journal_entry for row in rows] or [""])},
			fields=["name", "docstatus", "creation", "clearance_date"],
		)
	}
	ledgers = [row.bank_account for row in rows]
	banks = {
		account: bank_account.bank
		for account, bank_account in get_company_bank_accounts(onboarding.company, ledgers).items()
	}
	groups = {}
	for row in rows:
		entry = entries.get(row.journal_entry, frappe._dict())
		group = groups.setdefault(
			row.source or "",
			{
				"source": row.source or "",
				"count": 0,
				"cheques": 0,
				"deposits": 0,
				"issued": 0,
				"deposited": 0,
				"rows": [],
			},
		)
		group["count"] += 1
		issued = row.entry_type == "Issued"
		group["cheques" if issued else "deposits"] += 1
		group["issued" if issued else "deposited"] += flt(row.amount)
		group["posted_on"] = min(filter(None, [group.get("posted_on"), entry.creation]), default=None)
		group["rows"].append(
			{
				"name": row.name,
				"bank_account": row.bank_account,
				"bank": banks.get(row.bank_account),
				"entry_type": row.entry_type,
				"reference_no": row.reference_no,
				"reference_date": row.reference_date,
				"party_name": row.party_name,
				"amount": flt(row.amount),
				"journal_entry": row.journal_entry,
				"clearance_date": entry.clearance_date,
			}
		)
	undone = {}
	for row in onboarding.undone_cheques:
		group = undone.setdefault(row.source or "", {"source": row.source or "", "count": 0, "amount": 0})
		group["count"] += 1
		group["amount"] += flt(row.amount)
	return {
		"groups": sorted(groups.values(), key=lambda group: cstr(group.get("posted_on"))),
		"undone": list(undone.values()),
		"cancelled": [name for name, entry in entries.items() if entry.docstatus != 1],
	}


def get_cheques_to_undo(onboarding, source: str | None, entries: list[str] | None = None) -> list:
	return [
		row
		for row in onboarding.uncleared_cheques
		if row.status == "Posted"
		and (row.source or "") == (source or "")
		and (entries is None or row.journal_entry in entries)
	]


def check_cleared(rows: list):
	"""Refuse to undo nothing, or a cheque the bank cleared since."""
	if not rows:
		frappe.throw(_("There is nothing posted to undo."), title=_("Nothing to undo"))
	cleared = frappe.get_all(
		"Journal Entry",
		filters={"name": ("in", [row.journal_entry for row in rows]), "clearance_date": ("is", "set")},
		pluck="name",
	)
	if cleared:
		frappe.throw(
			cheque_words(
				rows[0].parent,
				_("1 cheque is already cleared. Remove the clearance date first.")
				if len(cleared) == 1
				else _("{0} cheques are already cleared. Remove the clearance date first.").format(
					len(cleared)
				),
			),
			title=_("Can't undo"),
		)


def undo_cheques(onboarding, source: str | None, entries: list[str] | None = None) -> frappe._dict:
	"""Cancel the Bank Entries of this source's cheques and deposits, or only those named, and keep
	their rows as undone. Each bank touched gets its opening entries cancelled and one made again for
	all its cheques that stay. Undoing the whole source drops its drafts too. Refused when the bank
	cleared one."""
	rows = get_cheques_to_undo(onboarding, source, entries)
	check_cleared(rows)
	day_before = get_day_before(onboarding)
	temporary = get_temporary_account(onboarding.company, create=True)
	for row in rows:
		cancel_doc("Journal Entry", row.journal_entry)
	dimensions = get_default_dimensions(onboarding.company)
	staying = [row for row in onboarding.uncleared_cheques if row.status == "Posted" and row not in rows]
	for account in sorted({row.bank_account for row in rows}):
		kept = [row for row in staying if row.bank_account == account]
		cancelled = sorted({row.opening_entry for row in rows + kept if row.bank_account == account} - {None})
		for name in cancelled:
			cancel_doc("Journal Entry", name)
		opening_entry = make_opening_entry(
			onboarding.company,
			account,
			kept,
			day_before,
			temporary,
			dimensions,
			cancelled[0] if len(cancelled) == 1 and frappe.db.exists("Journal Entry", cancelled[0]) else None,
		)
		for row in kept:
			row.opening_entry = opening_entry
	unposted = (
		[]
		if entries is not None
		else [
			row
			for row in onboarding.uncleared_cheques
			if row.status != "Posted" and (row.source or "") == (source or "")
		]
	)
	for row in rows:
		onboarding.append(
			"undone_cheques",
			{
				field: row.get(field)
				for field in (
					"bank_account",
					"entry_type",
					"reference_no",
					"reference_date",
					"party_name",
					"amount",
					"source",
				)
			}
			| {"status": "Draft"},
		)
	dropped = {row.name for row in rows + unposted}
	onboarding.set(
		"uncleared_cheques",
		[row for row in onboarding.uncleared_cheques if row.name not in dropped],
	)
	onboarding.flags.posting = True
	onboarding.save()
	return frappe._dict(removed=len(rows), unposted=len(unposted), kept_cancelled=pop_kept_reasons(source))


def undo_cheques_in_background(company: str, source: str | None, entries: list[str] | None = None):
	run_undo_job(company, "cheques", source, lambda onboarding: undo_cheques(onboarding, source, entries))
