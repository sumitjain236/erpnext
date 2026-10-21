# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""The steps of Company Setup.

A step is a function that gets the Company Setup document and returns its title,
description, and whether it is done, plus an optional short summary of what was done, whether it
is started but not done, and the form tab where it is done. "Done" always comes from the company's
data, never from a click.

Every app, ERPNext included, lists its steps in hooks.py. A step can say where it goes, before or
after another step by its name (the last part of its path)::

    company_setup_steps = [
        {"step": "india_compliance.onboarding.gst_settings", "after": "chart_of_accounts"},
        "hrms.onboarding.add_employees",  # no place given: just before Go Live
    ]

Two steps asking for the same place keep the order of the apps' install. A step whose place is
not found goes just before Go Live, and Go Live is always last.
"""

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import add_days, flt, fmt_money, formatdate

from erpnext.setup.doctype.company_setup.opening_invoices import get_sheet_match

HOOK_NAME = "company_setup_steps"
# Go Live always comes last, whatever the hooks say
LAST_STEP = "erpnext.setup.doctype.company_setup.company_setup_steps.go_live"


def get_step_paths() -> list[str]:
	"""Every app's steps, in the order the user sees them."""
	steps = []
	added = {}  # place -> how many steps were put after it, so the earlier app stays first
	pending = get_hooked_steps()

	# a step may point to a step that is added later, so keep going while steps find their place
	while pending:
		waiting = []
		for entry in pending:
			place = entry.get("after") or entry.get("before")
			index = find_step(steps, place) if place else None
			if index is None and place and find_step(pending, place, key="step") is not None:
				waiting.append(entry)
			elif index is None:
				steps.append(entry["step"])
			elif entry.get("after"):
				count = added.get(steps[index], 0)
				added[steps[index]] = count + 1
				steps.insert(index + 1 + count, entry["step"])
			else:
				steps.insert(index, entry["step"])
		if len(waiting) == len(pending):
			# they only point at each other, so none can be placed: put them before Go Live
			steps.extend(entry["step"] for entry in waiting)
			break
		pending = waiting

	return [*steps, LAST_STEP]


def get_hooked_steps() -> list[dict]:
	"""Steps from the hook of every installed app, in install order. Bad entries are skipped."""
	entries = []
	for app in frappe.get_installed_apps():
		for entry in frappe.get_hooks(HOOK_NAME, app_name=app) or []:
			if isinstance(entry, str):
				entry = {"step": entry}
			if not isinstance(entry, dict) or not isinstance(entry.get("step"), str):
				frappe.logger("company_setup").warning(f"Skipping step {entry!r} from {app}")
				continue
			if is_last_step(entry["step"]):
				continue
			if is_last_step(entry.get("after")):
				# nothing comes after Go Live
				entry = {"step": entry["step"], "before": "go_live"}
			if is_last_step(entry.get("before")):
				entry = {"step": entry["step"]}
			entries.append(entry)
	return entries


def find_step(steps: list, name: str, key: str | None = None) -> int | None:
	"""Where a step is in the list, by its full path or its name (the last part of the path)."""
	for index, step in enumerate(steps):
		path = step[key] if key else step
		if name in (path, path.rsplit(".", 1)[-1]):
			return index
	return None


def is_last_step(name: str | None) -> bool:
	return bool(name) and name in (LAST_STEP, LAST_STEP.rsplit(".", 1)[-1])


def migration_date(onboarding) -> dict:
	day_before = onboarding.migration_date and add_days(onboarding.migration_date, -1)
	return {
		"title": _("Migration date"),
		"description": _(
			"The day you move to ERPNext. Your old balances come in as they stood the day before."
		),
		"summary": onboarding.migration_date
		and _("{0} · balances as on {1}").format(
			formatdate(onboarding.migration_date), formatdate(day_before)
		),
		"done": bool(onboarding.migration_date),
	}


def chart_of_accounts(onboarding) -> dict:
	if onboarding.chart_of_accounts_source == "Existing":
		description = _("You kept the chart made during setup. Edit it any time.")
	elif onboarding.chart_of_accounts_source == "Imported":
		description = _("You imported your own chart. Edit it any time.")
	else:
		description = _("Keep the chart made during setup, or import your own.")
	done = bool(onboarding.chart_of_accounts_source)
	summary = None
	if done:
		count = frappe.db.count("Account", {"company": onboarding.company})
		# posting adds Temporary Opening to an imported chart: counted apart, so the number stays the sheet's
		added = onboarding.chart_of_accounts_source == "Imported" and frappe.db.count(
			"Account",
			{
				"company": onboarding.company,
				"account_type": "Temporary",
				"account_name": _("Temporary Opening"),
			},
		)
		summary = (
			_("{0} accounts · plus Temporary Opening").format(number(count - added))
			if added
			else _("{0} accounts").format(number(count))
		)
	return {
		"title": _("Chart of Accounts"),
		"description": description,
		"summary": summary,
		"done": done,
		"tab": "accounts_tab",
		"required": True,
	}


def opening_balances(onboarding) -> dict:
	rows = onboarding.opening_balances
	posted = all_opening_balances_posted(onboarding)
	if posted:
		summary = (
			_("1 account posted") if len(rows) == 1 else _("{0} accounts posted").format(number(len(rows)))
		)
	elif rows:
		currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
		difference = flt(sum(flt(row.debit) - flt(row.credit) for row in rows), 2)
		summary = (
			_("{0} ready to post · difference {1}").format(
				number(len(rows)), fmt_money(abs(difference), currency=currency)
			)
			if difference
			else _("{0} ready to post · balanced").format(number(len(rows)))
		)
	else:
		summary = None
	return {
		"title": _("Opening balances"),
		"description": _("Bank, cash, loans, capital, tax and other balances from your old books."),
		"summary": summary,
		"done": posted,
		"started": bool(rows) and not posted,
		"tab": "accounts_tab",
		"required": True,
	}


def customers(onboarding) -> dict:
	count = frappe.db.count("Customer", {"disabled": 0})
	return {
		"title": _("Customers"),
		"description": _("Import your customers with their addresses and tax IDs, as often as you like."),
		"summary": count and (_("1 customer") if count == 1 else _("{0} customers").format(count)),
		"done": bool(count),
		"tab": "selling_tab",
	}


def opening_sales_invoices(onboarding) -> dict:
	return opening_invoices(
		onboarding,
		"Sales Invoice",
		_("Opening sales invoices"),
		_("Customer invoices still unpaid on the day before migration."),
		_("All {0} invoices posted · {1}"),
		"selling_tab",
	)


def suppliers(onboarding) -> dict:
	count = frappe.db.count("Supplier", {"disabled": 0})
	return {
		"title": _("Suppliers"),
		"description": _("Import your suppliers with their addresses and tax IDs, as often as you like."),
		"summary": count and (_("1 supplier") if count == 1 else _("{0} suppliers").format(count)),
		"done": bool(count),
		"tab": "buying_tab",
	}


def opening_purchase_invoices(onboarding) -> dict:
	return opening_invoices(
		onboarding,
		"Purchase Invoice",
		_("Opening purchase invoices"),
		_("Supplier bills still unpaid on the day before migration."),
		_("All {0} bills posted · {1}"),
		"buying_tab",
	)


def opening_invoices(onboarding, doctype: str, title: str, description: str, summary: str, tab: str) -> dict:
	totals = frappe.get_all(
		doctype,
		filters={"company": onboarding.company, "is_opening": "Yes", "docstatus": 1},
		fields=[{"COUNT": "*", "as": "count"}, {"SUM": "outstanding_amount", "as": "outstanding"}],
	)[0]
	table = "opening_sales_invoices" if doctype == "Sales Invoice" else "opening_purchase_invoices"
	drafts = [row for row in onboarding.get(table) if row.status != "Created"]
	failed = sum(1 for row in drafts if row.status == "Failed")
	currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
	# counted like the Overview cards: invoices without the advances, the amount net of them
	posted = [row for row in onboarding.get(table) if row.status == "Created"]
	posted_count = number(sum(1 for row in posted if flt(row.outstanding_amount) > 0))
	if drafts and totals.count:
		text = (
			_("{0} posted · {1} left to post: {2} failed, fix them and post again").format(
				posted_count, number(len(drafts)), failed
			)
			if failed
			else _("{0} posted · {1} left to post").format(posted_count, number(len(drafts)))
		)
	elif failed:
		text = _("{0} ready to post · {1} failed, fix them and post again").format(
			number(len(drafts)), failed
		)
	elif totals.count:
		undone = onboarding.get(
			"undone_sales_invoices" if doctype == "Sales Invoice" else "undone_purchase_invoices"
		)
		is_sales = doctype == "Sales Invoice"
		if posted_count == "1":
			text = _("1 invoice posted · {1}") if is_sales else _("1 bill posted · {1}")
		elif undone:
			text = _("{0} invoices posted · {1}") if is_sales else _("{0} bills posted · {1}")
		else:
			text = summary
		text = text.format(
			posted_count,
			fmt_money(sum(flt(row.outstanding_amount) for row in posted), currency=currency),
		)
	elif drafts:
		to_review = len({row.party_name for row in drafts if not row.party and not row.create_party})
		amount = fmt_money(sum(flt(row.outstanding_amount) for row in drafts), currency=currency)
		text = _("{0} ready to post · {1}").format(number(len(drafts)), amount)
		if to_review:
			text += " · " + (
				_("1 name to review") if to_review == 1 else _("{0} names to review").format(to_review)
			)
	else:
		text = None
	# done once all is posted and adds up to what the trial balance says, when there is one
	match = get_sheet_match(onboarding, "Customer" if doctype == "Sales Invoice" else "Supplier")
	matches = not match or all(
		abs(flt(line["sheet"]) - flt(line["entered"])) < 1
		for line in match["lines"] or [{"sheet": match["sheet"], "entered": match["entered"]}]
	)
	done = bool(totals.count) and not drafts and matches
	return {
		"title": title,
		"description": description,
		"summary": text,
		"done": done,
		"started": bool(drafts or totals.count) and not done,
		"tab": tab,
	}


def check_books(onboarding) -> dict:
	temporary = get_temporary_opening_balance(onboarding.company)
	posted = all_opening_balances_posted(onboarding) or has_opening_invoices(onboarding.company)
	waiting = get_waiting(onboarding)
	if waiting:
		summary = _("{0} still to post").format(frappe.utils.comma_and(waiting, add_quotes=False))
	elif posted and temporary:
		currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
		summary = _("{0} left in Temporary Opening").format(fmt_money(abs(temporary), currency=currency))
	elif posted:
		summary = _("Your books add up")
	else:
		summary = None
	return {
		"title": _("Check your books"),
		"description": _(
			"Compare with your old books. Done when everything is posted and Temporary Opening is zero."
		),
		"summary": summary,
		"done": not waiting and posted and not temporary,
		"tab": "accounts_tab",
		"required": True,
	}


def go_live(onboarding) -> dict:
	summary = None
	if onboarding.status == "Live":
		temporary = get_temporary_opening_balance(onboarding.company)
		currency = frappe.get_cached_value("Company", onboarding.company, "default_currency")
		summary = (
			_("Live · {0} still in Temporary Opening").format(fmt_money(abs(temporary), currency=currency))
			if temporary
			else _("Live")
		)
	return {
		"title": _("Go live"),
		"description": _("Start using ERPNext. We point out anything left, but nothing stops you."),
		"summary": summary,
		"done": onboarding.status == "Live",
	}


def get_waiting(onboarding) -> list[str]:
	"""What is entered but not posted yet, as "141 balances", "625 invoices" and "40 bills"."""
	labels = {
		"opening_balances": (_("1 balance"), _("{0} balances")),
		"opening_sales_invoices": (_("1 invoice"), _("{0} invoices")),
		"opening_purchase_invoices": (_("1 bill"), _("{0} bills")),
	}
	waiting = []
	for table, (one, many) in labels.items():
		count = sum(1 for row in onboarding.get(table) if row.status not in ("Posted", "Created"))
		if count:
			waiting.append(one if count == 1 else many.format(number(count)))
	return waiting


def has_opening_invoices(company: str) -> bool:
	return any(
		frappe.db.exists(doctype, {"company": company, "is_opening": "Yes", "docstatus": 1})
		for doctype in ("Sales Invoice", "Purchase Invoice")
	)


def all_opening_balances_posted(onboarding) -> bool:
	rows = onboarding.opening_balances
	return bool(rows) and all(row.status == "Posted" for row in rows)


def get_temporary_opening_balance(company: str) -> float:
	"""Debit minus credit on the company's Temporary accounts. Zero once the opening is complete."""
	accounts = frappe.get_all(
		"Account", filters={"company": company, "account_type": "Temporary", "is_group": 0}, pluck="name"
	)
	if not accounts:
		return 0.0

	gle = frappe.qb.DocType("GL Entry")
	balance = (
		frappe.qb.from_(gle)
		.select(Sum(gle.debit) - Sum(gle.credit))
		.where(gle.account.isin(accounts) & (gle.is_cancelled == 0))
	).run()[0][0]
	return flt(balance, frappe.get_precision("GL Entry", "debit"))


def number(value: int) -> str:
	"""A count with the site's thousands separators, like 1,400."""
	return fmt_money(value, precision=0)
