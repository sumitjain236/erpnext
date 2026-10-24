# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""What Company Setup posted, grouped by where it came from, and undoing one group."""

import frappe
from frappe import _
from frappe.query_builder.functions import Count, Sum
from frappe.utils import cint, cstr, flt, get_fullname
from frappe.utils.scheduler import is_scheduler_inactive

from erpnext.accounts.utils import (
	_delete_accounting_ledger_entries,
	_delete_adv_pl_entries,
	delete_exchange_gain_loss_journal,
)
from erpnext.setup.doctype.company_setup.opening_invoices import (
	BACKGROUND_FROM,
	INVOICE_ROW_FIELDS,
	PARTY_ACCOUNT_FIELDS,
	TABLES,
	UNDONE_TABLES,
	get_error_text,
	get_hand_posted,
	parties_enabled,
	post_invoices,
)

INVOICE_DOCTYPES = {"Customer": "Sales Invoice", "Supplier": "Purchase Invoice"}
# by company and party type
INVOICE_JOB_ID = "company_setup_invoices::{0}::{1}"
INVOICE_UNDO_JOB_ID = "company_setup_undo::{0}::{1}"
# a background job's time: at least this, and this much per row (an invoice takes about half a second)
JOB_TIMEOUT = 1500
SECONDS_PER_ROW = 2
PARTY_FIELDS = {"Customer": "customer", "Supplier": "supplier"}
# an opening entry made outside is listed up to this many; the rest are counted
OUTSIDE_LIMIT = 500
# an undo's timeline comment lists this many reasons for what it kept; the rest are counted
COMMENT_REASONS = 20


def get_posted(onboarding, kind: str) -> dict:
	"""Posted balances, or a party type's posted invoices, by source; the opening entries made outside
	Company Setup; and the posted documents cancelled since in ERPNext."""
	if kind == "balances":
		return get_posted_balances(onboarding)
	return get_posted_invoices(onboarding, kind)


def get_posted_invoices(onboarding, party_type: str) -> dict:
	rows = [row for row in onboarding.get(TABLES[party_type]) if row.status == "Created" and row.invoice]
	doctype = INVOICE_DOCTYPES[party_type]
	docs = get_docs(rows, doctype)
	paid = get_paid(list(docs))
	groups = {}
	# a sheet's rows not posted yet, failed or draft, go with it on undo
	for row in onboarding.get(TABLES[party_type]):
		if row.status != "Created":
			group = groups.setdefault(
				row.source or "", {"source": row.source or "", "count": 0, "amount": 0, "paid": 0}
			)
			group["unposted"] = group.get("unposted", 0) + 1
	for row in rows:
		group = groups.setdefault(
			row.source or "", {"source": row.source or "", "count": 0, "amount": 0, "paid": 0}
		)
		group["count"] += 1
		group["advances"] = group.get("advances", 0) + (flt(row.outstanding_amount) < 0)
		if flt(row.outstanding_amount) >= 0:
			# a re-post's name when the undo keeps the invoices, shown in the undo confirm
			group.setdefault("example", f"{row.invoice}-1")
		group["amount"] += flt(row.outstanding_amount)
		group["paid"] += row.invoice in paid
		group["posted_on"] = min(
			filter(None, [group.get("posted_on"), docs.get(row.invoice, {}).get("creation")]), default=None
		)

	accounts = dict(
		frappe.get_all(
			doctype,
			filters={"name": ("in", list(docs) or [""])},
			fields=["name", PARTY_ACCOUNT_FIELDS[party_type]],
			as_list=True,
		)
	)
	undone_rows = onboarding.get(UNDONE_TABLES[party_type])
	# a new child row takes its parent's creation, so the cancel's own time comes from the document
	cancelled_on = {
		name: doc.modified for name, doc in get_docs(undone_rows, doctype, ["name", "modified"]).items()
	}
	undone = {}
	for row in undone_rows:
		group = undone.setdefault(row.source or "", {"source": row.source or "", "count": 0, "amount": 0})
		group["count"] += 1
		group["amount"] += flt(row.outstanding_amount)
		# a deleted invoice's time is its undone row's
		undone_on = cancelled_on.get(row.invoice) or row.modified
		group["undone_on"] = max(filter(None, [group.get("undone_on"), undone_on]))
		group.setdefault("times", set()).add(undone_on.replace(second=0, microsecond=0))
	for group in undone.values():
		# undone more than once: the list says when the last one was
		group["more_than_once"] = len(group.pop("times") - {None}) > 1
	return {
		# a sheet with nothing posted yet is only in the invoices window
		"groups": sorted(
			(group for group in groups.values() if group["count"]),
			key=lambda group: cstr(group.get("posted_on")),
		),
		"undone": sorted(undone.values(), key=lambda group: cstr(group.get("undone_on"))),
		"accounts": accounts,
		"cancelled": [name for name, doc in docs.items() if doc.docstatus != 1],
		"outside": get_outside(
			doctype, onboarding.company, set(docs), "base_grand_total", PARTY_FIELDS[party_type]
		),
	}


def get_posted_balances(onboarding) -> dict:
	rows = [row for row in onboarding.opening_balances if row.status == "Posted" and row.journal_entry]
	entries = {
		entry.name: entry
		for entry in frappe.get_all(
			"Journal Entry",
			filters={"name": ("in", list({row.journal_entry for row in rows}) or [""])},
			fields=["name", "docstatus", "creation", "voucher_type", "total_debit"],
		)
	}
	groups = {}
	for row in rows:
		group = groups.setdefault(row.source or "", {"source": row.source or "", "count": 0, "entries": []})
		group["count"] += 1
		if row.journal_entry not in group["entries"]:
			group["entries"].append(row.journal_entry)
	for group in groups.values():
		group["posted_on"] = min(
			(entries[name].creation for name in group["entries"] if name in entries), default=None
		)
		# undoing cancels whole entries, so balances of other sources in them go back to drafts too
		group["affected"] = sum(1 for row in rows if row.journal_entry in group["entries"])
		group["entries"] = [
			{
				"name": name,
				"voucher_type": entries[name].voucher_type if name in entries else None,
				"total": flt(entries[name].total_debit) if name in entries else 0,
			}
			for name in group["entries"]
		]

	# advances are opening entries too, made by the invoices step
	ours = set(entries) | {
		row.invoice for table in TABLES.values() for row in onboarding.get(table) if row.invoice
	}
	return {
		"groups": sorted(groups.values(), key=lambda group: cstr(group.get("posted_on"))),
		"cancelled": [name for name, doc in entries.items() if doc.docstatus != 1],
		"outside": get_outside("Journal Entry", onboarding.company, ours, "total_debit", "voucher_type"),
	}


def get_docs(rows, doctype: str, fields: list | None = None) -> dict:
	"""The posted documents of these rows by name: invoices, and the journal entries of advances."""
	docs = {}
	for name, names in (
		(doctype, [row.invoice for row in rows if row.invoice and flt(row.outstanding_amount) >= 0]),
		("Journal Entry", [row.invoice for row in rows if row.invoice and flt(row.outstanding_amount) < 0]),
	):
		if names:
			for doc in frappe.get_all(
				name,
				filters={"name": ("in", names)},
				fields=fields or ["name", "docstatus", "creation"],
			):
				docs[doc.name] = doc
	return docs


def get_paid(names: list[str]) -> set[str]:
	"""Those of these invoices and advances that a payment, credit note or another entry is set against."""
	if not names:
		return set()
	return {
		entry.against_voucher_no
		for entry in frappe.get_all(
			"Payment Ledger Entry",
			filters={"against_voucher_no": ("in", names), "delinked": 0},
			fields=["voucher_no", "against_voucher_no"],
		)
		if entry.voucher_no != entry.against_voucher_no
	}


def undo_invoices(
	onboarding, party_type: str, source: str, invoices: list[str] | None = None
) -> frappe._dict:
	"""Cancel the invoices and advances posted from this source, or only those of them named, and keep
	their rows as undone. Undoing the whole source drops its rows not posted too. Those with a payment
	against them stay posted."""
	table = TABLES[party_type]
	unposted = (
		[]
		if invoices is not None
		else [
			row
			for row in onboarding.get(table)
			if row.status != "Created" and (row.source or "") == (source or "")
		]
	)
	rows = get_undo_rows(onboarding, party_type, source, invoices)
	paid = get_paid([row.invoice for row in rows])
	# a disabled customer or supplier, posted anyway, is enabled for the undo
	disabled = frappe.get_all(
		party_type,
		filters={"name": ("in", list({row.party for row in rows if row.party}) or [""]), "disabled": 1},
		pluck="name",
	)
	with parties_enabled(party_type, disabled):
		removed, kept, failed = [], 0, []
		for row in rows:
			if row.invoice in paid:
				kept += 1
				continue
			doctype = INVOICE_DOCTYPES[party_type] if flt(row.outstanding_amount) >= 0 else "Journal Entry"
			account = get_posted_account(doctype, row.invoice, row.party, party_type)
			savepoint = f"undo_invoice_{row.idx}"
			frappe.db.savepoint(savepoint)
			try:
				cancel_doc(doctype, row.invoice)
				removed.append(row)
			except Exception as e:
				frappe.db.rollback(save_point=savepoint)
				frappe.clear_messages()
				failed.append(f"{row.invoice}: {get_error_text(e)}")
				continue
			onboarding.append(
				UNDONE_TABLES[party_type],
				{field: row.get(field) for field in (*INVOICE_ROW_FIELDS, "invoice")}
				| {"account": account, "status": "Draft"},
			)
	dropped = {row.name for row in removed + unposted}
	onboarding.set(table, [row for row in onboarding.get(table) if row.name not in dropped])
	onboarding.flags.posting = True
	onboarding.save()
	return frappe._dict(
		removed=len(removed),
		unposted=len(unposted),
		kept=kept,
		failed=failed,
		kept_cancelled=pop_kept_reasons(source),
	)


def get_outside(doctype: str, company: str, ours: set, amount_field: str, label_field: str) -> dict:
	"""The submitted opening documents of this doctype not in ours, made outside Company Setup: how
	many, their total, and the first OUTSIDE_LIMIT by date, each named by label_field."""
	doc = frappe.qb.DocType(doctype)
	outside = frappe.qb.from_(doc).where(
		(doc.company == company)
		& (doc.is_opening == "Yes")
		& (doc.docstatus == 1)
		& doc.name.notin(list(ours) or [""])
	)
	count, amount = outside.select(Count("*"), Sum(doc[amount_field])).run()[0]
	return {
		"count": count,
		"amount": flt(amount),
		"rows": outside.select(
			doc.name, doc[label_field].as_("party"), doc.posting_date, doc[amount_field].as_("amount")
		)
		.orderby(doc.posting_date)
		.limit(OUTSIDE_LIMIT)
		.run(as_dict=True),
	}


def get_posted_account(doctype: str, name: str, party: str, party_type: str) -> str | None:
	"""The receivable or payable account an invoice or advance was posted on."""
	if doctype == "Journal Entry":
		return frappe.db.get_value(
			"Journal Entry Account", {"parent": name, "party_type": party_type, "party": party}, "account"
		)
	return frappe.db.get_value(doctype, name, PARTY_ACCOUNT_FIELDS[party_type])


def create_invoices_in_background(
	company: str, party_type: str, keep_old_dates: bool = False, disabled_parties: list[str] | None = None
):
	try:
		run_posting_job(
			"company_setup_invoices",
			company,
			lambda onboarding: post_invoices(onboarding, party_type, keep_old_dates, True, disabled_parties),
			party_type=party_type,
		)
	except Exception as e:
		mark_drafts_failed(company, party_type, get_error_text(e))
		raise


def mark_drafts_failed(company: str, party_type: str, error: str):
	"""After a posting that failed as a whole and was rolled back: its drafts keep why, so the card
	still says so after a reload."""
	onboarding = frappe.get_doc("Company Setup", company)
	hand_posted = {row.name for row in get_hand_posted(onboarding, party_type)}
	for row in onboarding.get(TABLES[party_type]):
		if row.status != "Created" and row.name not in hand_posted:
			row.update({"status": "Failed", "error": error})
	onboarding.flags.posting = True
	onboarding.save()
	# a test runs the job inline: a commit would keep its data
	if not frappe.in_test:
		frappe.db.commit()


def undo_invoices_in_background(
	company: str, party_type: str, source: str, invoices: list[str] | None = None
):
	run_undo_job(
		company,
		party_type,
		source,
		lambda onboarding: undo_invoices(onboarding, party_type, source, invoices),
	)


def undo_balances(onboarding, source: str) -> frappe._dict:
	"""Cancel the journal entries the balances of this source were posted in. Every balance in them
	goes back to a draft."""
	entries = {
		row.journal_entry
		for row in onboarding.opening_balances
		if row.status == "Posted" and row.journal_entry and (row.source or "") == (source or "")
	}
	for name in sorted(entries):
		cancel_doc("Journal Entry", name)
	drafts = 0
	for row in onboarding.opening_balances:
		if row.journal_entry in entries:
			row.status = "Draft"
			row.journal_entry = None
			drafts += 1
	onboarding.flags.posting = True
	onboarding.save()
	return frappe._dict(entries=len(entries), drafts=drafts, kept_cancelled=pop_kept_reasons(source))


def run_or_enqueue(rows: int, run, job, job_id: str, **kwargs):
	"""run() right away for fewer than BACKGROUND_FROM rows, and what it returns, or "done" when
	nothing; else the job in the background, and "queued"."""
	if rows < BACKGROUND_FROM:
		result = run()
		return "done" if result is None else result
	enqueue_job(job, job_id, rows, **kwargs)
	return "queued"


def run_posting_job(event: str, company: str, post, **details):
	"""Run post(onboarding) as a background job, then tell the open form on event that it is done,
	with the fields post returns, or why not."""
	data = {"company": company, **details}
	try:
		lock_company_setup(company)
		data |= post(frappe.get_doc("Company Setup", company)) or {}
	except Exception as e:
		frappe.db.rollback()
		frappe.publish_realtime(
			event, {**data, "error": get_error_text(e)}, doctype="Company Setup", docname=company
		)
		raise
	frappe.publish_realtime(event, data, doctype="Company Setup", docname=company, after_commit=True)


def run_undo_job(company: str, kind: str, source: str | None, undo):
	"""Run undo(onboarding) as a background job, then tell the open form what it did, or why not."""
	result, error = None, None
	try:
		lock_company_setup(company)
		result = undo(frappe.get_doc("Company Setup", company))
		# a test runs the job inline: a commit would keep its data
		if not frappe.in_test:
			frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		error = get_error_text(e)
		frappe.log_error(f"Company Setup: undo {kind} of {company}")
	frappe.publish_realtime(
		"company_setup_undo",
		{"company": company, "party_type": kind, "source": source, "result": result, "error": error},
		doctype="Company Setup",
		docname=company,
		after_commit=True,
	)


def lock_company_setup(company: str):
	"""One posting or undo at a time: a second one waits here, then finds the rows done."""
	frappe.db.get_value("Company Setup", company, "name", for_update=True)


def enqueue_job(method, job_id: str, rows: int, **kwargs):
	"""Queue a Company Setup posting or undo on the long queue, once per job id, with time for its
	rows: a posting killed for time rolls everything back. Refused in plain words when no worker runs
	the queue, or the page would wait for it forever."""
	if is_scheduler_inactive() and not frappe.in_test:
		frappe.throw(
			_(
				"Background jobs are not running on this site, so these {0} rows can't be done in the background. Ask your administrator to turn the scheduler on, or do fewer rows at a time."
			).format(rows),
			title=_("Background jobs are off"),
		)
	choice = frappe.flags.get("company_setup_delete_entries")
	if choice is not None:
		kwargs = {"job": method, "delete_entries": choice, **kwargs}
		method = run_with_undo_choice
	frappe.enqueue(
		method,
		queue="long",
		job_id=job_id,
		deduplicate=True,
		timeout=max(JOB_TIMEOUT, rows * SECONDS_PER_ROW),
		now=frappe.in_test,
		**kwargs,
	)


def run_with_undo_choice(job, delete_entries: int, **kwargs):
	"""A background undo, with the choice made in its confirm."""
	set_undo_choice(delete_entries)
	job(**kwargs)


def set_undo_choice(delete_entries: bool | int | None):
	"""This undo's choice: delete what it cancels, with the ledger rows, or keep what has ledger rows
	cancelled. None follows Accounts Settings."""
	frappe.flags.company_setup_delete_entries = None if delete_entries is None else cint(delete_entries)


def get_undo_choice() -> bool:
	choice = frappe.flags.get("company_setup_delete_entries")
	return deletes_ledger_entries() if choice is None else bool(choice)


def cancel_doc(doctype: str, name: str, now: bool = False):
	"""Cancel a posted document, then delete it when that is safe: see delete_cancelled. With now, the
	cancel is not left to a background job, as a big Stock Reconciliation's is."""
	if frappe.db.get_value(doctype, name, "docstatus") == 1:
		doc = frappe.get_doc(doctype, name)
		if not doc.has_permission("cancel"):
			frappe.throw(
				_("You can't cancel {0} {1}, so it can't be undone. Ask someone who can.").format(
					_(doctype), name
				),
				frappe.PermissionError,
				title=_("Can't undo"),
			)
		doc._cancel() if now else doc.cancel()
	delete_cancelled(doctype, name)


def delete_cancelled(doctype: str, name: str) -> bool:
	"""Delete a cancelled document Company Setup made, with what it owns, so a posting again takes its
	name and nothing undone stays behind. One with ledger rows stays cancelled when the undo keeps them
	(get_undo_choice). Not when the user can't delete it or something else
	links to it: it stays cancelled and the reason is kept for the undo to tell (pop_kept_reasons).
	Returns whether it was deleted."""
	doc = frappe.db.get_value(doctype, name, ["docstatus", "company"], as_dict=True)
	if not doc or doc.docstatus != 2:
		return False
	undo = frappe.flags.setdefault("company_setup_undo", {"company": doc.company, "deleted": {}, "kept": {}})
	if frappe.db.get_value("Company Setup", doc.company, "status") == "Live":
		keep_reason(doctype, name, _("the company is live"))
		return False
	delete_entries = get_undo_choice()
	if not delete_entries and has_ledger_entries(doctype, name):
		undo["kept"][doctype] = undo["kept"].get(doctype, 0) + 1
		return False
	if not frappe.has_permission(doctype, "delete", name):
		keep_reason(doctype, name, _("you can't delete a {0}").format(_(doctype)))
		return False
	frappe.db.savepoint("company_setup_delete")
	try:
		forget_in_setup(doctype, name)
		delete_owned(doctype, name)
		if delete_entries and not deletes_ledger_entries():
			delete_ledger_entries(doctype, name)
		frappe.delete_doc(doctype, name)
		if doctype == "Asset":
			# ERPNext's "Asset deleted" note: the next asset posted may take this name again
			frappe.db.delete("Asset Activity", {"asset": name})
	except (frappe.ValidationError, frappe.PermissionError) as e:
		frappe.db.rollback(save_point="company_setup_delete")
		frappe.clear_messages()
		reason = (
			_("another record in ERPNext still refers to it")
			if isinstance(e, frappe.LinkExistsError)
			else get_error_text(e)
		)
		keep_reason(doctype, name, reason)
		return False
	undo["deleted"][doctype] = undo["deleted"].get(doctype, 0) + 1
	return True


def deletes_ledger_entries() -> bool:
	return bool(cint(frappe.get_single_value("Accounts Settings", "delete_linked_ledger_entries")))


def has_ledger_entries(doctype: str, name: str) -> bool:
	return any(
		frappe.db.exists(ledger, {"voucher_type": doctype, "voucher_no": name})
		for ledger in ("GL Entry", "Payment Ledger Entry", "Stock Ledger Entry")
	)


def delete_ledger_entries(doctype: str, name: str):
	"""The ledger rows ERPNext's own delete removes when Accounts Settings lets it."""
	delete_exchange_gain_loss_journal(frappe.get_doc(doctype, name))
	_delete_accounting_ledger_entries(doctype, name)
	_delete_adv_pl_entries(doctype, name)
	ple = frappe.qb.DocType("Payment Ledger Entry")
	frappe.qb.from_(ple).delete().where(
		(ple.against_voucher_type == doctype) & (ple.against_voucher_no == name) & (ple.delinked == 1)
	).run()
	sle = frappe.qb.DocType("Stock Ledger Entry")
	frappe.qb.from_(sle).delete().where((sle.voucher_type == doctype) & (sle.voucher_no == name)).run()


def get_undo_split(onboarding, kind: str, source: str | None, names: list[str] | None = None) -> dict:
	"""The documents an undo of this source (or of the names picked in it) cancels, counted by doctype
	in those with ledger rows and those without, which are deleted whatever the undo's choice."""
	split = {"with_ledger": {}, "without_ledger": {}}
	for doctype, name in get_undo_documents(onboarding, kind, source, names):
		counts = split["with_ledger" if has_ledger_entries(doctype, name) else "without_ledger"]
		counts[doctype] = counts.get(doctype, 0) + 1
	return split


def get_undo_documents(onboarding, kind: str, source: str | None, names: list[str] | None) -> list[tuple]:
	"""What each undo cancels, as it does: see undo_invoices, undo_balances, bank_accounts.undo_cheques,
	opening_stock.undo_opening_stock and fixed_assets.undo_fixed_assets."""
	from erpnext.setup.doctype.company_setup import bank_accounts, opening_stock

	if kind in TABLES:
		rows = get_undo_rows(onboarding, kind, source, names)
		paid = get_paid([row.invoice for row in rows])
		return [
			(INVOICE_DOCTYPES[kind] if flt(row.outstanding_amount) >= 0 else "Journal Entry", row.invoice)
			for row in rows
			if row.invoice not in paid
		]
	if kind == "balances":
		return [
			("Journal Entry", name)
			for name in sorted(
				{
					row.journal_entry
					for row in onboarding.opening_balances
					if row.status == "Posted" and row.journal_entry and (row.source or "") == (source or "")
				}
			)
		]
	if kind == "cheques":
		rows = bank_accounts.get_cheques_to_undo(onboarding, source, names)
		banks = {row.bank_account for row in rows}
		openings = {
			row.opening_entry
			for row in onboarding.uncleared_cheques
			if row.status == "Posted" and row.bank_account in banks and row.opening_entry
		}
		return [("Journal Entry", name) for name in [*(row.journal_entry for row in rows), *sorted(openings)]]
	if kind == "stock":
		rows = opening_stock.get_rows_to_undo(onboarding, source, names)
		entries = {
			name for row in rows for name in [row.stock_entry, *opening_stock.get_corrections(row)] if name
		}
		documents = [("Stock Reconciliation", name) for name in sorted(entries)]
		if opening_stock.has_difference_on(onboarding, {row.warehouse for row in rows}):
			documents.append(("Journal Entry", onboarding.stock_difference_entry))
		return documents
	if kind == "assets":
		rows = [
			row
			for row in onboarding.fixed_assets
			if row.status == "Posted"
			and (row.source or "") == (source or "")
			and (names is None or row.asset in names)
		]
		entries = sorted({row.net_value_entry for row in rows if row.net_value_entry})
		return [("Asset", row.asset) for row in rows] + [("Journal Entry", name) for name in entries]
	return []


def get_delete_setting() -> dict:
	"""Accounts Settings' "Delete Accounting and Stock Ledger Entries on deletion of Transaction": the
	undo confirm's default."""
	return {"delete": deletes_ledger_entries()}


def get_setup_links(doctype: str) -> list[frappe._dict]:
	"""The Link fields of Company Setup's tables to a doctype, read once per request."""
	cache = frappe.flags.setdefault("company_setup_links", {})
	if doctype not in cache:
		cache[doctype] = frappe.get_all(
			"DocField",
			filters={"parent": ("like", "Company Setup%"), "fieldtype": "Link", "options": doctype},
			fields=["parent", "fieldname"],
		)
	return cache[doctype]


def forget_in_setup(doctype: str, name: str):
	"""Clear Company Setup's own links to the document, which would stop its delete. The undo drops or
	clears the rows that held it."""
	for field in get_setup_links(doctype):
		frappe.db.set_value(
			field.parent, {field.fieldname: name}, field.fieldname, None, update_modified=False
		)
	if doctype in ("Sales Order", "Purchase Order", "Work Order", "Subcontracting Order"):
		frappe.db.set_value(
			"Company Setup Open Order", {"document": name}, "document", None, update_modified=False
		)


def delete_owned(doctype: str, name: str):
	"""What a cancelled document owns besides itself and its ledger rows (ERPNext's own delete takes
	those): the cancelled reposts, depreciation schedules and job cards made for it."""
	for repost in frappe.get_all(
		"Repost Item Valuation",
		filters={"voucher_type": doctype, "voucher_no": name},
		fields=["name", "docstatus"],
	):
		if repost.docstatus == 1:
			frappe.get_doc("Repost Item Valuation", repost.name).cancel()
		frappe.delete_doc("Repost Item Valuation", repost.name, ignore_permissions=True)
	if doctype == "Asset":
		for schedule in frappe.get_all(
			"Asset Depreciation Schedule", filters={"asset": name, "docstatus": 2}, pluck="name"
		):
			frappe.delete_doc("Asset Depreciation Schedule", schedule, ignore_permissions=True)
		# the asset's own log; the cancelled movement that placed it, when it moved nothing else
		frappe.db.delete("Asset Activity", {"asset": name})
		for movement in set(frappe.get_all("Asset Movement Item", filters={"asset": name}, pluck="parent")):
			if frappe.db.get_value("Asset Movement", movement, "docstatus") == 2 and not frappe.db.exists(
				"Asset Movement Item", {"parent": movement, "asset": ("!=", name)}
			):
				frappe.delete_doc("Asset Movement", movement, ignore_permissions=True)
	if doctype == "Work Order":
		for card in frappe.get_all(
			"Job Card", filters={"work_order": name, "docstatus": ("!=", 1)}, pluck="name"
		):
			frappe.delete_doc("Job Card", card, ignore_permissions=True)


def keep_reason(doctype: str, name: str, reason: str):
	frappe.flags.setdefault("company_setup_kept", []).append(
		_("{0} {1} is kept cancelled: {2}").format(_(doctype), name, reason)
	)


def pop_kept_reasons(source: str | None = None) -> list[str]:
	"""Why the entries an undo cancelled are kept, one line each, and forget them. Notes the undo on
	the Company Setup's timeline."""
	kept = frappe.flags.pop("company_setup_kept", None) or []
	frappe.flags.pop("company_setup_delete_entries", None)
	undo = frappe.flags.pop("company_setup_undo", None)
	if undo:
		add_undo_comment(undo["company"], undo["deleted"], undo["kept"], kept, source)
	return kept


def add_undo_comment(
	company: str,
	deleted: dict[str, int],
	kept_by_choice: dict[str, int],
	kept: list[str],
	source: str | None,
):
	def counts(by_doctype):
		return ", ".join(
			f"{frappe.format_value(count, 'Int')} {_(doctype)}" for doctype, count in by_doctype.items()
		)

	parts = []
	if deleted:
		parts.append(_("{0} deleted, with any ledger entries.").format(counts(deleted)))
	if kept_by_choice:
		parts.append(
			_("{0} kept cancelled with their ledger entries, as chosen in the undo.").format(
				counts(kept_by_choice)
			)
		)
	text = _("Undo by {0}:").format(get_fullname()) + " " + (" ".join(parts) or _("nothing deleted."))
	if source:
		text += " " + _("From upload {0}.").format(source)
	if kept:
		text += " " + _("{0} kept cancelled:").format(frappe.format_value(len(kept), "Int"))
		text += " " + "; ".join(kept[:COMMENT_REASONS])
		if len(kept) > COMMENT_REASONS:
			text += " " + _("and {0} more.").format(frappe.format_value(len(kept) - COMMENT_REASONS, "Int"))
	frappe.get_doc("Company Setup", company).add_comment("Info", text)


def needs_background(onboarding, party_type: str, source: str, invoices: list[str] | None = None) -> bool:
	return len(get_undo_rows(onboarding, party_type, source, invoices)) >= BACKGROUND_FROM


def get_undo_rows(onboarding, party_type: str, source: str, invoices: list[str] | None = None) -> list:
	"""The posted rows of this source an undo cancels: all, or those of the invoices named."""
	return [
		row
		for row in onboarding.get(TABLES[party_type])
		if row.status == "Created"
		and row.invoice
		and (row.source or "") == (source or "")
		and (invoices is None or row.invoice in invoices)
	]


def get_first_posting_date(onboarding, kind: str, source: str, invoices: list[str] | None = None):
	"""The earliest posting date of the entries an undo would cancel, or None when there are none."""
	if kind == "balances":
		vouchers = {
			row.journal_entry
			for row in onboarding.opening_balances
			if row.status == "Posted" and (row.source or "") == (source or "")
		}
	else:
		vouchers = {row.invoice for row in get_undo_rows(onboarding, kind, source, invoices)}
	vouchers.discard(None)
	if not vouchers:
		return None
	return frappe.db.get_value(
		"GL Entry", {"voucher_no": ("in", list(vouchers)), "is_cancelled": 0}, [{"MIN": "posting_date"}]
	)


def check_undo(onboarding):
	if onboarding.status == "Live":
		frappe.throw(
			_("{0} is live, so posted entries can't be undone here. Cancel them in ERPNext instead.").format(
				onboarding.company
			),
			title=_("Already live"),
		)
