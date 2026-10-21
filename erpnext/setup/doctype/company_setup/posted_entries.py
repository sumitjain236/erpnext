# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""What Company Setup posted, grouped by where it came from, and undoing one group."""

import frappe
from frappe import _
from frappe.query_builder.functions import Count, Sum
from frappe.utils import cstr, flt, strip_html

from erpnext.setup.doctype.company_setup.opening_invoices import (
	BACKGROUND_FROM,
	PARTY_ACCOUNT_FIELDS,
	TABLES,
	UNDONE_TABLES,
)

INVOICE_DOCTYPES = {"Customer": "Sales Invoice", "Supplier": "Purchase Invoice"}
PARTY_FIELDS = {"Customer": "customer", "Supplier": "supplier"}
# an opening entry made outside is listed up to this many; the rest are counted
OUTSIDE_LIMIT = 500


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
		group["amount"] += flt(row.outstanding_amount)
		group["paid"] += row.invoice in paid
		group["posted_on"] = min(
			filter(None, [group.get("posted_on"), docs.get(row.invoice, {}).get("creation")]), default=None
		)

	invoice = frappe.qb.DocType(doctype)
	outside = frappe.qb.from_(invoice).where(
		(invoice.company == onboarding.company)
		& (invoice.is_opening == "Yes")
		& (invoice.docstatus == 1)
		& invoice.name.notin(list(docs) or [""])
	)
	totals = outside.select(Count("*"), Sum(invoice.grand_total)).run()[0]
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
		group["undone_on"] = max(filter(None, [group.get("undone_on"), cancelled_on.get(row.invoice)]))
		group.setdefault("times", set()).add(
			cancelled_on[row.invoice].replace(second=0, microsecond=0)
			if row.invoice in cancelled_on
			else None
		)
	for group in undone.values():
		# undone more than once: the list says when the last one was
		group["more_than_once"] = len(group.pop("times") - {None}) > 1
	return {
		"groups": sorted(groups.values(), key=lambda group: cstr(group.get("posted_on"))),
		"undone": sorted(undone.values(), key=lambda group: cstr(group.get("undone_on"))),
		"accounts": accounts,
		"cancelled": [name for name, doc in docs.items() if doc.docstatus != 1],
		"outside": {
			"count": totals[0],
			"amount": flt(totals[1]),
			"rows": outside.select(
				invoice.name,
				invoice[PARTY_FIELDS[party_type]].as_("party"),
				invoice.posting_date,
				invoice.grand_total.as_("amount"),
			)
			.orderby(invoice.posting_date)
			.limit(OUTSIDE_LIMIT)
			.run(as_dict=True),
		},
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
	entry = frappe.qb.DocType("Journal Entry")
	outside = frappe.qb.from_(entry).where(
		(entry.company == onboarding.company)
		& (entry.is_opening == "Yes")
		& (entry.docstatus == 1)
		& entry.name.notin(list(ours) or [""])
	)
	totals = outside.select(Count("*"), Sum(entry.total_debit)).run()[0]
	return {
		"groups": sorted(groups.values(), key=lambda group: cstr(group.get("posted_on"))),
		"cancelled": [name for name, doc in entries.items() if doc.docstatus != 1],
		"outside": {
			"count": totals[0],
			"amount": flt(totals[1]),
			"rows": outside.select(
				entry.name,
				entry.voucher_type.as_("party"),
				entry.posting_date,
				entry.total_debit.as_("amount"),
			)
			.orderby(entry.posting_date)
			.limit(OUTSIDE_LIMIT)
			.run(as_dict=True),
		},
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
		if invoices
		else [
			row
			for row in onboarding.get(table)
			if row.status != "Created" and (row.source or "") == (source or "")
		]
	)
	rows = [
		row
		for row in onboarding.get(table)
		if row.status == "Created"
		and row.invoice
		and (row.source or "") == (source or "")
		and (not invoices or row.invoice in invoices)
	]
	paid = get_paid([row.invoice for row in rows])
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
			failed.append(f"{row.invoice}: {strip_html(cstr(e)) or e.__class__.__name__}")
			continue
		onboarding.append(
			UNDONE_TABLES[party_type],
			{
				field: row.get(field)
				for field in (
					"party_type",
					"party",
					"party_name",
					"invoice_number",
					"invoice_date",
					"due_date",
					"outstanding_amount",
					"source",
					"invoice",
				)
			}
			| {"account": account, "status": "Draft"},
		)
	onboarding.set(table, [row for row in onboarding.get(table) if row not in removed + unposted])
	onboarding.flags.posting = True
	onboarding.save()
	return frappe._dict(removed=len(removed), unposted=len(unposted), kept=kept, failed=failed)


def get_posted_account(doctype: str, name: str, party: str, party_type: str) -> str | None:
	"""The receivable or payable account an invoice or advance was posted on."""
	if doctype == "Journal Entry":
		return frappe.db.get_value(
			"Journal Entry Account", {"parent": name, "party_type": party_type, "party": party}, "account"
		)
	return frappe.db.get_value(doctype, name, PARTY_ACCOUNT_FIELDS[party_type])


def undo_invoices_in_background(
	company: str, party_type: str, source: str, invoices: list[str] | None = None
):
	"""undo_invoices for many rows; the open form hears when it is done."""
	result, error = None, None
	try:
		result = undo_invoices(frappe.get_doc("Company Setup", company), party_type, source, invoices)
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		error = strip_html(cstr(e)) or e.__class__.__name__
		frappe.log_error(f"Company Setup: undo {party_type} invoices of {company}")
	frappe.publish_realtime(
		"company_setup_undo",
		{"company": company, "party_type": party_type, "source": source, "result": result, "error": error},
		doctype="Company Setup",
		docname=company,
		after_commit=True,
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
	return frappe._dict(entries=len(entries), drafts=drafts)


def cancel_doc(doctype: str, name: str):
	"""Cancel a posted document. ERPNext keeps a cancelled one with its ledger lines."""
	if frappe.db.get_value(doctype, name, "docstatus") == 1:
		frappe.get_doc(doctype, name).cancel()


def needs_background(onboarding, party_type: str, source: str, invoices: list[str] | None = None) -> bool:
	return (
		sum(
			1
			for row in onboarding.get(TABLES[party_type])
			if row.status == "Created"
			and (row.source or "") == (source or "")
			and (not invoices or row.invoice in invoices)
		)
		>= BACKGROUND_FROM
	)


def check_undo(onboarding):
	if onboarding.status == "Live":
		frappe.throw(
			_("{0} is live, so posted entries can't be undone here. Cancel them in ERPNext instead.").format(
				onboarding.company
			),
			title=_("Already live"),
		)
