# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cstr, flt, fmt_money

from erpnext.accounts.party import get_party_account
from erpnext.setup.doctype.company_setup.invoice_sheet import (
	INVOICE_ACCOUNT_TYPES,
	INVOICE_ROW_FIELDS,
	PARTY_ACCOUNT_FIELDS,
	PARTY_DEFAULTS,
	PARTY_GROUP_TITLES,
	PARTY_NAME_FIELDS,
	UNDONE_TABLES,
	find_party,
	get_similar_key,
	get_table,
)


def get_sheet_match(onboarding, party_type: str) -> dict | None:
	"""What the trial balance says customers owe or suppliers are owed, by account when it says so,
	set against the invoices or bills entered so far, posted or not. None without a trial balance."""
	account_type = "Receivable" if party_type == "Customer" else "Payable"
	sheet = (frappe.parse_json(onboarding.sheet_accounts or "{}") or {}).get(account_type)
	if not sheet:
		return None
	# the sheet keeps the ledger's side: a supplier's balance is a credit
	sign = 1 if party_type == "Customer" else -1
	default = get_party_account(party_type, None, onboarding.company)
	# a draft an invoice made by hand stands for counts once, as that invoice
	hand = get_hand_posted(onboarding, party_type)
	hand_posted = {row.name for row in hand}
	rows = [row for row in onboarding.get(get_table(party_type)) if row.name not in hand_posted]
	accounts = get_row_accounts(onboarding, party_type, rows)
	waiting = get_waiting_parties(onboarding, party_type, rows)
	entered = {}
	# the drafts that can't post as they are: in another currency, by currency, or of a disabled party
	left_out = {"currencies": {}, "disabled": {"amount": 0, "count": 0}}
	for row in rows:
		account = accounts[row.name]
		line = entered.setdefault(
			account, {"amount": 0, "count": 0, "draft": 0, "waiting": 0, "disabled": {}, "left_out": 0}
		)
		amount = flt(row.outstanding_amount)
		line["amount"] += amount
		line["count"] += 1
		if row.status != "Created":
			line["draft"] += amount
			if (not row.party and not row.create_party) or row.party in waiting.other_currency:
				line["waiting"] += amount
			elif row.party in waiting.disabled:
				line["disabled"][row.party] = line["disabled"].get(row.party, 0) + amount
			if row.party in waiting.other_currency:
				bucket = left_out["currencies"].setdefault(
					waiting.other_currency[row.party], {"amount": 0, "count": 0}
				)
			elif row.party in waiting.disabled:
				bucket = left_out["disabled"]
			else:
				continue
			bucket["amount"] += amount
			bucket["count"] += 1
			line["left_out"] += amount
	outside = get_outside_invoices(onboarding, party_type)
	for invoice in outside:
		line = entered.setdefault(
			invoice.account,
			{"amount": 0, "count": 0, "draft": 0, "waiting": 0, "disabled": {}, "left_out": 0},
		)
		line["amount"] += flt(invoice.amount)
		line["count"] += 1
	# a line no account stands for, like Busy's Sundry Debtors beside its sub-groups, is the only other
	# ledger's, else the default ledger's when that has no line; else it can only match in total
	if "" in sheet and len(sheet) > 1:
		others = [a for a in get_invoice_accounts(onboarding.company, party_type) if a not in sheet]
		line = others[0] if len(others) == 1 else default if default not in sheet else None
		if line:
			sheet = {(line if account == "" else account): amount for account, amount in sheet.items()}
	by_account = "" not in sheet
	accounts = sorted(set(sheet) | set(entered)) if by_account else [None]
	return {
		"by_account": by_account,
		"sheet": sign * sum(flt(amount) for amount in sheet.values()),
		"entered": sum(line["amount"] for line in entered.values()),
		"lines": [
			{
				"account": account,
				"sheet": sign * flt(sheet[account]) if account in sheet else None,
				"entered": entered.get(account, {}).get("amount", 0),
				"count": entered.get(account, {}).get("count", 0),
				"draft": entered.get(account, {}).get("draft", 0),
				# drafts that stay drafts when posting: unconfirmed or in another currency, and the
				# disabled customers' or suppliers' unless they are posted anyway
				"waiting": entered.get(account, {}).get("waiting", 0),
				"disabled": entered.get(account, {}).get("disabled", {}),
				# of those, the ones in another currency or of a disabled party, as the card leaves out
				"left_out": entered.get(account, {}).get("left_out", 0),
			}
			for account in accounts
		]
		if by_account
		else [],
		"outside": {"count": len(outside), "amount": sum(flt(invoice.amount) for invoice in outside)},
		"hand_posted": {"count": len(hand), "amount": sum(flt(row.outstanding_amount) for row in hand)},
		"left_out": left_out,
	}


def get_waiting_parties(onboarding, party_type: str, rows) -> frappe._dict:
	"""The draft rows' customers or suppliers that keep them drafts when posting: disabled, or in
	another currency."""
	from erpnext.setup.doctype.company_setup.company_setup import get_other_currency_parties

	parties = {row.party for row in rows if row.status != "Created" and row.party}
	return frappe._dict(
		other_currency=get_other_currency_parties(onboarding.company, party_type, parties),
		disabled=set(
			frappe.get_all(
				party_type, filters={"name": ("in", list(parties) or [""]), "disabled": 1}, pluck="name"
			)
		),
	)


def get_hand_posted(onboarding, party_type: str) -> list:
	"""The draft rows an opening invoice made by hand stands for: same customer or supplier, and its
	number, or else its amount in the company's currency when only one invoice of theirs has it."""
	drafts = [row for row in onboarding.get(get_table(party_type)) if row.status != "Created" and row.party]
	if not drafts:
		return []
	parties = {row.party for row in drafts}
	outside = [doc for doc in get_outside_invoices(onboarding, party_type) if doc.party in parties]
	matched, used = [], set()
	for row in drafts:
		theirs = [doc for doc in outside if doc.party == row.party and doc.name not in used]
		number = cstr(row.invoice_number).strip()
		found = [doc for doc in theirs if number and number in (doc.name, doc.get("bill_no"))]
		if not found:
			found = [doc for doc in theirs if abs(flt(doc.amount) - flt(row.outstanding_amount)) < 1]
		if len(found) == 1:
			used.add(found[0].name)
			row.hand_invoice = found[0].name
			matched.append(row)
	return matched


def get_cached(onboarding, key: str, method):
	"""What method gives for the onboarding as last saved or loaded: the steps, onload and checks ask
	for it several times in one request."""
	stored = onboarding.flags.get(key)
	if not stored or stored[0] != onboarding.modified:
		stored = onboarding.flags[key] = (onboarding.modified, method(onboarding))
	return stored[1]


def get_outside_invoices(onboarding, party_type: str) -> list[frappe._dict]:
	"""The opening invoices made outside Company Setup, by hand or Data Import, with their party,
	account and amount in the company's currency, whatever currency they were billed in. Read once
	per save of the setup: the form asks several times on each open."""
	party = "customer" if party_type == "Customer" else "supplier"
	invoices = get_cached(
		onboarding,
		f"outside_invoices_{party_type}",
		lambda onboarding: frappe.get_all(
			"Sales Invoice" if party_type == "Customer" else "Purchase Invoice",
			filters={"company": onboarding.company, "is_opening": "Yes", "docstatus": 1},
			fields=[
				"name",
				f"{party} as party",
				f"{PARTY_ACCOUNT_FIELDS[party_type]} as account",
				"base_grand_total as amount",
				*(["bill_no"] if party_type == "Supplier" else []),
			],
		),
	)
	own = {row.invoice for row in onboarding.get(get_table(party_type)) if row.invoice}
	return [invoice for invoice in invoices if invoice.name not in own]


def get_ledger_overs(onboarding, party_type: str, disabled_parties: list[str] | None = None) -> list[dict]:
	"""The receivable or payable ledgers the drafts that post would put above their trial balance
	line while another ledger is short of its own: money going to the wrong ledger. Empty when the
	trial balance doesn't say by ledger, or none is short. disabled_parties: those posted anyway."""
	match = get_sheet_match(onboarding, party_type)
	if not match or not match["by_account"]:
		return []
	lines = []
	for line in match["lines"]:
		stays = line["waiting"] + sum(
			amount for party, amount in line["disabled"].items() if party not in (disabled_parties or [])
		)
		lines.append(line | {"entered": line["entered"] - stays, "draft": line["draft"] - stays})
	short = [line for line in lines if line["sheet"] is not None and line["sheet"] - line["entered"] >= 1]
	if not short:
		return []
	return [
		{
			"account": line["account"],
			"account_name": get_account_name(line["account"]),
			"sheet": flt(line["sheet"]),
			"entered": flt(line["entered"], 2),
			"over": flt(line["entered"] - flt(line["sheet"]), 2),
			"short": [
				{
					"account": other["account"],
					"account_name": get_account_name(other["account"]),
					"short": flt(other["sheet"] - other["entered"], 2),
				}
				for other in short
			],
		}
		for line in lines
		if abs(flt(line["draft"])) >= 0.005 and line["entered"] - flt(line["sheet"]) >= 1
	]


def get_ledger_over_message(over: dict, currency: str) -> str:
	short = over["short"][0]
	return _(
		"These rows would put {0} on {1}, {2} more than its trial balance line, while {3} is {4} short. Pick the ledger this sheet is for in Post to, or fix the rows."
	).format(
		fmt_money(over["entered"], currency=currency),
		over["account_name"],
		fmt_money(over["over"], currency=currency),
		short["account_name"],
		fmt_money(short["short"], currency=currency),
	)


def get_account_name(account: str) -> str:
	return frappe.get_cached_value("Account", account, "account_name") or account


def get_title_account(company: str, party_type: str, titles: list[str]) -> str | None:
	"""The receivable or payable ledger a report's title rows name, like Tally's "Ledger: Debtors -
	Retainers", when the company has several and one name is found whole: the longest, when only one
	is that long. Busy's and Tally's "Sundry Debtors" or "Sundry Creditors" is the group of every
	party ledger, so it names none of them."""
	accounts = get_invoice_accounts(company, party_type)
	if len(accounts) < 2 or not titles:
		return None
	group = f" {PARTY_GROUP_TITLES[party_type]} "
	keys = [f" {get_similar_key(title)} ".replace(group, " ") for title in titles]
	found = sorted(
		(
			(len(key), account.name)
			for account in frappe.get_all(
				"Account", filters={"name": ("in", accounts)}, fields=["name", "account_name"]
			)
			if (key := get_similar_key(account.account_name)) and any(f" {key} " in title for title in keys)
		),
		reverse=True,
	)
	if not found or (len(found) > 1 and found[0][0] == found[1][0]):
		return None
	return found[0][1]


def get_party_accounts(party_type: str, parties, company: str) -> tuple[dict, dict, dict]:
	"""get_party_account for many parties in a few queries: the account each one's invoices go to,
	the account saved on the party itself, and the one saved on the party or its group (neither is
	the company's default)."""
	parties = list(set(filter(None, parties)))
	if not parties:
		return {}, {}, {}
	group_field = "customer_group" if party_type == "Customer" else "supplier_group"
	saved = dict(
		frappe.get_all(
			"Party Account",
			filters={"parenttype": party_type, "parent": ("in", parties), "company": company},
			fields=["parent", "account"],
			as_list=True,
		)
	)
	groups = dict(
		frappe.get_all(
			party_type, filters={"name": ("in", parties)}, fields=["name", group_field], as_list=True
		)
	)
	group_accounts = dict(
		frappe.get_all(
			"Party Account",
			filters={
				"parenttype": f"{party_type} Group",
				"parent": ("in", list(set(filter(None, groups.values()))) or [""]),
				"company": company,
			},
			fields=["parent", "account"],
			as_list=True,
		)
	)
	kept = {
		party: account
		for party in parties
		if (account := saved.get(party) or group_accounts.get(groups.get(party)))
	}
	default = get_party_account(party_type, None, company)
	return {party: kept.get(party) or default for party in parties}, saved, kept


def get_new_party_accounts(party_type: str, company: str, rows) -> dict[str, str]:
	"""For rows whose customer or supplier is to be added, by name: the ledger saved on the group
	the new one gets."""
	field = PARTY_DEFAULTS[party_type][0]
	groups = {
		row.party_name: make_party(party_type, row.party_name, company, row.party_group).get(field)
		for row in rows
		if row.create_party and not row.party
	}
	group_accounts = dict(
		frappe.get_all(
			"Party Account",
			filters={
				"parenttype": f"{party_type} Group",
				"parent": ("in", list(set(filter(None, groups.values()))) or [""]),
				"company": company,
			},
			fields=["parent", "account"],
			as_list=True,
		)
	)
	return {name: group_accounts[group] for name, group in groups.items() if group in group_accounts}


def get_posted_accounts(party_type: str, rows) -> dict[str, str]:
	"""The receivable or payable account each posted row's invoice or advance is on, by its name."""
	posted = [row for row in rows if row.status == "Created" and row.invoice]
	invoices = [row.invoice for row in posted if flt(row.outstanding_amount) >= 0]
	advances = [row.invoice for row in posted if flt(row.outstanding_amount) < 0]
	accounts = {}
	if invoices:
		accounts |= dict(
			frappe.get_all(
				"Sales Invoice" if party_type == "Customer" else "Purchase Invoice",
				filters={"name": ("in", invoices)},
				fields=["name", PARTY_ACCOUNT_FIELDS[party_type]],
				as_list=True,
			)
		)
	if advances:
		accounts |= dict(
			frappe.get_all(
				"Journal Entry Account",
				filters={"parent": ("in", advances), "party_type": party_type},
				fields=["parent", "account"],
				as_list=True,
			)
		)
	return accounts


def get_account_clashes(onboarding, party_type: str) -> list[dict]:
	"""Customers or suppliers whose invoices about to be posted and whose earlier ones, or the account
	saved on them, are on different accounts, where something the user said picks one (a sheet's or a
	typed account, or the saved account) and the trial balance is short on that one and over on the
	other. "posted" when the earlier invoices are the odd ones, "draft" when these are, and "split"
	for an earlier sheet's invoices posted apart from the rest of that sheet (get_split_clashes)."""
	match = get_sheet_match(onboarding, party_type)
	if not match or not match["by_account"]:
		return []
	gap = {line["account"]: flt(line["sheet"]) - flt(line["entered"]) for line in match["lines"]}
	in_sheet = {line["account"] for line in match["lines"] if line["sheet"] is not None}
	stray_drafts = get_stray_drafts(
		onboarding.get(get_table(party_type)),
		get_row_accounts(onboarding, party_type, onboarding.get(get_table(party_type))),
	)

	def caused_by_posted(account, posted_rows, target):
		"""Whether the posted rows, not this upload's strays, put the account over, and moving them
		keeps the target within its line."""
		if account in in_sheet and stray_drafts.get(account, 0) >= -gap.get(account, 0) - 0.005:
			return False
		return sum(flt(row.outstanding_amount) for row in posted_rows) <= gap.get(target, 0) + 0.005

	def is_short(account):
		return gap.get(account, 0) > 0.005

	def is_over(account):
		return gap.get(account, 0) < -0.005 or account not in in_sheet

	rows = onboarding.get(get_table(party_type))
	posted_accounts = get_posted_accounts(party_type, rows)
	party_accounts, saved_accounts, _kept = get_party_accounts(
		party_type, [row.party for row in rows], onboarding.company
	)
	by_party = {}
	for row in rows:
		if row.party:
			by_party.setdefault(row.party, []).append(row)
	clashes = {}
	for party, party_rows in by_party.items():
		drafts = [row for row in party_rows if row.status != "Created"]
		if not drafts:
			continue
		saved = saved_accounts.get(party)
		posted = {}
		for row in party_rows:
			if row.status == "Created" and posted_accounts.get(row.invoice):
				posted.setdefault(posted_accounts[row.invoice], []).append(row)
		going = {}
		for row in drafts:
			going.setdefault(row.account or party_accounts[party], []).append(row)
		for account, these in going.items():
			said = any(row.account for row in these)
			for other in (set(posted) | {saved}) - {account, None}:
				if not (said or other == saved or any(row.account for row in posted.get(other, []))):
					continue
				# posted rows whose ledger was set on them, by their sheet or an earlier move, stay
				movable = [row for row in posted.get(other, []) if not row.account_by_hand]
				if (
					is_short(account)
					and is_over(other)
					and movable
					and caused_by_posted(other, movable, account)
				):
					kind, target, moving = "posted", account, movable
				elif is_short(other) and is_over(account):
					kind, target, moving = "draft", other, these
				else:
					continue
				clash = clashes.setdefault(
					(kind, target, other if kind == "posted" else account),
					{
						"kind": kind,
						"account": target,
						"from_account": other if kind == "posted" else account,
						"short": gap[target],
						"said": False,
						"saved": False,
						"earlier": False,
						"parties": [],
					},
				)
				# for the wording: whether this sheet says its account, the saved account is the one
				# to use, and earlier invoices are on the other account
				clash["said"] = clash["said"] or said
				clash["saved"] = clash["saved"] or saved == target
				clash["earlier"] = clash["earlier"] or bool(posted.get(other))
				clash["parties"].append(
					{
						"party": party,
						"sources": sorted({row.source or "" for row in moving}),
						"count": len(moving),
						"amount": sum(flt(row.outstanding_amount) for row in moving),
					}
				)
	splits = get_split_clashes(onboarding, party_type, match)
	# an over ledger an earlier sheet's strays explain: those move, not this sheet's rows
	split_from = {clash["from_account"] for clash in splits}
	clashes = [
		clash
		for clash in clashes.values()
		if not (clash["kind"] == "draft" and clash["from_account"] in split_from)
	]
	for clash in clashes + splits:
		for key in ("account", "from_account"):
			clash[f"{key}_name"] = frappe.get_cached_value("Account", clash[key], "account_name")
	return clashes + splits


def get_split_clashes(onboarding, party_type: str, match: dict) -> list[dict]:
	"""Posted invoices on a ledger over its trial balance line, from a sheet whose other rows are on a
	ledger short of its line: that sheet's strays, like names confirmed in Review names that went to
	the default ledger. One clash per sheet ledger and stray ledger."""
	gap = {line["account"]: flt(line["sheet"]) - flt(line["entered"]) for line in match["lines"]}
	in_sheet = {line["account"] for line in match["lines"] if line["sheet"] is not None}
	rows = onboarding.get(get_table(party_type))
	accounts = get_row_accounts(onboarding, party_type, rows)
	by_source = {}
	for row in rows:
		by_source.setdefault(row.source or "", {}).setdefault(accounts[row.name], []).append(row)
	stray_drafts = get_stray_drafts(rows, accounts)
	clashes = {}
	for source, on in by_source.items():
		if not source or len(on) < 2:
			continue
		# the sheet's ledger is where most of its rows are
		home = max(on, key=lambda account: len(on[account]))
		if gap.get(home, 0) <= 0.005:
			continue
		for account, these in on.items():
			strays = [row for row in these if row.status == "Created" and not row.account_by_hand]
			if account == home or not strays or (account in in_sheet and gap[account] >= -0.005):
				continue
			# this upload's own strays there explain the gap: those are the rows to look at
			if account in in_sheet and stray_drafts.get(account, 0) >= -gap[account] - 0.005:
				continue
			# moving them must not put the sheet's ledger over its own line
			if sum(flt(row.outstanding_amount) for row in strays) > gap[home] + 0.005:
				continue
			clash = clashes.setdefault(
				(home, account),
				{
					"kind": "split",
					"account": home,
					"from_account": account,
					"short": gap[home],
					"parties": [],
				},
			)
			for party in sorted({row.party for row in strays}):
				mine = [row for row in strays if row.party == party]
				clash["parties"].append(
					{
						"party": party,
						"sources": [source],
						"count": len(mine),
						"amount": sum(flt(row.outstanding_amount) for row in mine),
						"invoices": [row.invoice for row in mine],
					}
				)
	return list(clashes.values())


def get_stray_drafts(rows, accounts: dict[str, str]) -> dict[str, float]:
	"""By account, the draft amount going there from sheets whose rows mostly go elsewhere, like a row
	moved by hand: the ones that can put a ledger over, rather than what was posted."""
	by_source = {}
	for row in rows:
		on = by_source.setdefault(row.source or "", {})
		on[accounts[row.name]] = on.get(accounts[row.name], 0) + 1
	homes = {source: max(on, key=on.get) for source, on in by_source.items()}
	stray = {}
	for row in rows:
		account = accounts[row.name]
		if row.status != "Created" and account != homes[row.source or ""]:
			stray[account] = stray.get(account, 0) + flt(row.outstanding_amount)
	return stray


def get_row_accounts(onboarding, party_type: str, rows) -> dict[str, str]:
	"""The account each row is on, or will go to, by row name, as get_sheet_match counts it."""
	default = get_party_account(party_type, None, onboarding.company)
	posted_accounts = get_posted_accounts(party_type, rows)
	party_accounts = get_party_accounts(party_type, [row.party for row in rows], onboarding.company)[0]
	return {
		row.name: posted_accounts.get(row.invoice) or row.account or party_accounts.get(row.party) or default
		for row in rows
	}


def fix_account_clashes(onboarding, party_type: str, fixes: list[dict]):
	"""Do what the user ticked on the clashes found now, and nothing else: put these drafts on the
	other account, or cancel the earlier invoices and add them back as drafts on this sheet's account."""
	from erpnext.setup.doctype.company_setup.posted_entries import check_undo

	wanted = {(fix.get("kind"), fix.get("account"), fix.get("from_account")) for fix in fixes}
	rows = onboarding.get(get_table(party_type))
	posted_accounts = get_posted_accounts(party_type, rows)
	party_accounts = get_party_accounts(party_type, [row.party for row in rows], onboarding.company)[0]
	for clash in get_account_clashes(onboarding, party_type):
		if (clash["kind"], clash["account"], clash["from_account"]) not in wanted:
			continue
		parties = {party["party"] for party in clash["parties"]}
		if clash["kind"] == "split":
			check_undo(onboarding)
			by_source = {}
			for party in clash["parties"]:
				by_source.setdefault(party["sources"][0], []).extend(party["invoices"])
			move_posted(onboarding, party_type, by_source, clash["account"])
			rows = onboarding.get(get_table(party_type))
			continue
		if clash["kind"] == "draft":
			for row in rows:
				if (
					row.party in parties
					and row.status != "Created"
					and (row.account or party_accounts[row.party]) == clash["from_account"]
				):
					row.update({"account": clash["account"], "account_by_hand": 1})
			continue
		check_undo(onboarding)
		by_source = {}
		for row in rows:
			if (
				row.party in parties
				and row.status == "Created"
				and not row.account_by_hand
				and posted_accounts.get(row.invoice) == clash["from_account"]
			):
				by_source.setdefault(row.source or "", []).append(row.invoice)
		move_posted(onboarding, party_type, by_source, clash["account"])
		rows = onboarding.get(get_table(party_type))
	onboarding.flags.posting = True
	onboarding.save()


def move_posted(onboarding, party_type: str, by_source: dict[str, list[str]], account: str):
	"""Undo these posted invoices and add them back as drafts on the account, to post again."""
	from erpnext.setup.doctype.company_setup.posted_entries import undo_invoices

	for source, invoices in by_source.items():
		undo_invoices(onboarding, party_type, source, invoices)
		for undone in onboarding.get(UNDONE_TABLES[party_type]):
			if undone.invoice in invoices:
				onboarding.append(
					get_table(party_type),
					{field: undone.get(field) for field in INVOICE_ROW_FIELDS}
					| {"account": account, "account_by_hand": 1, "status": "Draft"},
				)


def set_ledger_accounts(company: str, party_type: str, invoices: list[dict]) -> list[str]:
	"""Put each invoice on the account its sheet's ledger column names, when that is one of the
	company's receivable or payable accounts, by name with or without the company's abbreviation.
	Returns the ledger names that are not."""
	accounts = get_invoice_accounts(company, party_type)
	by_name = {account.lower(): account for account in accounts}
	by_name |= {
		name.lower(): account
		for account, name in frappe.get_all(
			"Account",
			filters={"name": ("in", accounts or [""])},
			fields=["name", "account_name"],
			as_list=True,
		)
	}
	unknown = set()
	for invoice in invoices:
		ledger = invoice.pop("ledger", None)
		if not ledger:
			continue
		if ledger.lower() in by_name:
			# the sheet names the ledger: as if picked on the row
			invoice.update({"account": by_name[ledger.lower()], "account_by_hand": 1})
		else:
			unknown.add(ledger)
	return sorted(unknown)


def get_invoice_accounts(company: str, party_type: str) -> list[str]:
	"""The accounts a customer's or supplier's opening invoices can go to, in the company's currency.
	An employee advance is a payable account too, but an asset, so it isn't one of them."""
	account_type, root_type = INVOICE_ACCOUNT_TYPES[party_type]
	return frappe.get_all(
		"Account",
		filters={
			"company": company,
			"account_type": account_type,
			"root_type": root_type,
			"is_group": 0,
			"disabled": 0,
			"account_currency": frappe.get_cached_value("Company", company, "default_currency"),
		},
		pluck="name",
		order_by="lft",
	)


def get_missing_default_account(onboarding, party_type: str) -> str | None:
	"""Why the draft rows can't post: the company has no default receivable or payable account and a
	row needs it, having no account of its own. A chart with such accounts only in another currency
	leaves the default empty."""
	if get_party_account(party_type, None, onboarding.company):
		return None
	with_account = set(
		frappe.get_all(
			"Party Account",
			filters={"company": onboarding.company, "parenttype": party_type},
			pluck="parent",
		)
	)
	if not any(
		not row.account and row.party not in with_account
		for row in onboarding.get(get_table(party_type))
		if row.status != "Created"
	):
		return None
	currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
	if party_type == "Customer":
		return _(
			"{0} has no default receivable account in {1}. Set Default Receivable Account on the company first."
		).format(onboarding.company, currency)
	return _(
		"{0} has no default payable account in {1}. Set Default Payable Account on the company first."
	).format(onboarding.company, currency)


def get_party_currency(party_type: str, party: str, company: str, account: str | None = None) -> str:
	"""The currency of the receivable or payable account a customer's or supplier's invoices go to,
	or else the one it is billed in, like a USD customer on the company's own receivable account."""
	account = account or get_party_account(party_type, party, company)
	currency = frappe.get_cached_value("Account", account, "account_currency")
	if currency != frappe.get_cached_value("Company", company, "default_currency"):
		return currency
	return frappe.db.get_value(party_type, party, "default_currency") or currency


def get_new_names(onboarding, party_type: str, parties: frappe._dict, confirmed: bool = False) -> list[str]:
	"""The names on the draft rows that are no customer or supplier yet; with confirmed, only those the
	user confirmed as new."""
	return sorted(
		{
			row.party_name
			for row in onboarding.get(get_table(party_type))
			if row.status != "Created"
			and row.party_name
			and row.party not in parties.names
			and not find_party(parties, row.party_name)
			and (row.create_party or not confirmed)
		}
	)


def make_party(party_type: str, name: str, company: str | None = None, group: str | None = None):
	"""A new, unsaved customer or supplier with this name and the defaults a new one gets. Where the
	settings have none, as after a setup wizard that skipped them, the company's country as the
	territory, and the only group there is; else none, as a group can't be picked."""
	party = frappe.new_doc(party_type)
	party.set(PARTY_NAME_FIELDS[party_type], name)
	# the group picked when the name was confirmed as new
	party.set(PARTY_DEFAULTS[party_type][0], group or None)
	settings = "Selling Settings" if party_type == "Customer" else "Buying Settings"
	for field in PARTY_DEFAULTS[party_type]:
		if not party.get(field):
			party.set(field, frappe.db.get_single_value(settings, field) or get_default_node(field, company))
	return party


def get_default_node(field: str, company: str | None) -> str | None:
	doctype = {
		"customer_group": "Customer Group",
		"territory": "Territory",
		"supplier_group": "Supplier Group",
	}[field]
	if field == "territory" and company:
		country = frappe.get_cached_value("Company", company, "country")
		if country and frappe.db.get_value("Territory", country, "is_group") == 0:
			return country
	leaves = frappe.get_all(doctype, filters={"is_group": 0}, pluck="name", limit=2)
	return leaves[0] if len(leaves) == 1 else None
