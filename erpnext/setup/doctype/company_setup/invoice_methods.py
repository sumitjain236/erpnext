# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, sbool

from erpnext.accounts.party import get_party_account
from erpnext.setup.doctype.company_setup import bank_accounts as banks
from erpnext.setup.doctype.company_setup import fixed_assets as assets
from erpnext.setup.doctype.company_setup import opening_stock as stock
from erpnext.setup.doctype.company_setup.chart_sheet import get_file_label
from erpnext.setup.doctype.company_setup.company_setup_steps import get_draft_rows_by
from erpnext.setup.doctype.company_setup.opening_invoices import (
	TABLES,
	fix_account_clashes,
	get_account_clashes,
	get_day_before,
	get_exact_key,
	get_hand_posted,
	get_invoice_accounts,
	get_ledger_over_message,
	get_ledger_overs,
	get_look_alikes,
	get_missing_default_account,
	get_new_party_accounts,
	get_parties,
	get_party_accounts,
	get_posted_names,
	get_posting_blocker,
	get_posting_dates,
	get_suggestions,
	get_table,
	get_title_account,
	match_party,
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
	run_or_enqueue,
	set_undo_choice,
	undo_balances,
	undo_invoices,
	undo_invoices_in_background,
)


class InvoicesMixin:
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
		one set on it (account_by_hand: by the sheet's ledger column or by the user), else post_to, the
		ledger picked for the whole sheet, else its customer's or supplier's own or its group's. post_to
		moves only this source's rows: another sheet's rows keep the ledger they have."""
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
		own = get_party_accounts(party_type, [row.party for row in drafts], self.company)[2]
		new = get_new_party_accounts(party_type, self.company, drafts)
		for row in drafts:
			if (row.source or None) == (source or None):
				account = post_to or own.get(row.party) or new.get(row.party_name)
			elif row.account in accounts:
				continue
			else:
				account = own.get(row.party) or new.get(row.party_name)
			if account in accounts:
				row.account = account
		self.save()
		return frappe._dict(skipped=skipped, repeated=repeated)

	@frappe.whitelist()
	def import_opening_invoices(self, party_type: str, file_url: str) -> dict:
		"""Replace the draft opening invoices with a receivables or payables report's."""
		self.check_permission("write")
		sheet = read_invoice_sheet(file_url, party_type)
		unknown_ledgers = set_ledger_accounts(self.company, party_type, sheet.invoices)
		source = get_file_label(file_url)
		post_to = get_sheet_post_to(self, party_type, sheet.titles)
		# the sheet uploaded again: its ledger only while its drafts still sit on it
		earlier = [
			row
			for row in self.get(get_table(party_type))
			if row.status != "Created" and row.source == source and not row.account_by_hand
		]
		if post_to and earlier and any(row.account != post_to for row in earlier):
			post_to = None
		left_out = self.save_opening_invoices(party_type, sheet.invoices, source, post_to)
		return {
			"added": len(sheet.invoices) - left_out.skipped - left_out.repeated,
			"advances": sheet.advances,
			"skipped": left_out.skipped,
			"repeated": left_out.repeated,
			"unused_columns": sheet.unused_columns,
			"bad_dates": sheet.bad_dates,
			"unknown_ledgers": unknown_ledgers,
			"post_to": post_to,
			"source": source,
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

	@frappe.whitelist()
	def remove_closed_drafts(self) -> int:
		"""Remove the opening drafts dated in a closed year: they can never be posted. Returns how
		many were removed."""
		self.check_permission("write")
		self.start_change()
		closed_to = assets.get_closed_to(self.company)
		if not closed_to:
			return 0
		removed = 0
		for table, rows in get_draft_rows_by(self, closed_to).items():
			names = {row.name for row in rows}
			self.set(table, [row for row in self.get(table) if row.name not in names])
			removed += len(names)
		self.save()
		return removed

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
			# where a row with no ledger of its own goes, as the sheet match counts it
			"default_account": get_party_account(party_type, None, self.company)
			if len(accounts) > 1
			else None,
			# a ledger saved on the customer or supplier or its group, which "Post to" doesn't change
			"own_accounts": {
				party: account
				for party, account in get_party_accounts(
					party_type, (drafts | set(found.values())) - {None}, self.company
				)[2].items()
				if account in accounts
			}
			if len(accounts) > 1
			else {},
			# the ledger saved on each group, for names confirmed as new
			"group_accounts": {
				group: account
				for group, account in frappe.get_all(
					"Party Account",
					filters={"parenttype": f"{party_type} Group", "company": self.company},
					fields=["parent", "account"],
					as_list=True,
				)
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
	) -> str:
		"""Post the draft rows as opening invoices: right away, or in the background when many. The
		names confirmed as new are added first, and the account clashes ticked are fixed first. With
		keep_old_dates, invoices dated in a year ERPNext doesn't have add that year and keep their
		dates, instead of going on the day before migration. The disabled customers or suppliers
		named in disabled_parties post too, and stay disabled."""
		disabled_parties = (
			frappe.parse_json(disabled_parties) if isinstance(disabled_parties, str) else disabled_parties
		)
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


def get_sheet_post_to(onboarding, party_type: str, titles: list[str]) -> str | None:
	"""The ledger an uploaded sheet's rows go to: the trial balance's only receivable or payable line,
	else the one the sheet's title names among its lines. Never a ledger with no line, like the
	company's default."""
	accounts = get_invoice_accounts(onboarding.company, party_type)
	if len(accounts) < 2:
		return None
	account_type = "Receivable" if party_type == "Customer" else "Payable"
	sheet = (frappe.parse_json(onboarding.sheet_accounts or "{}") or {}).get(account_type) or {}
	lines = [account for account in sheet if account in accounts]
	if len(lines) == 1:
		return lines[0]
	title = get_title_account(onboarding.company, party_type, titles)
	return title if title in lines or not lines else None


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


def get_invoice_key(row) -> tuple:
	"""What makes two opening invoice rows the same invoice."""
	return (
		get_exact_key(row.get("party_name")),
		cstr(row.get("invoice_number")).strip(),
		cstr(getdate(row.get("invoice_date")) if row.get("invoice_date") else ""),
		flt(row.get("outstanding_amount"), 2),
	)
