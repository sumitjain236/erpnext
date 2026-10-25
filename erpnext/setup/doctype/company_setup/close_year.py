# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import escape_html, formatdate, get_datetime, now_datetime, time_diff_in_seconds
from frappe.utils.background_jobs import is_job_enqueued

from erpnext.setup.doctype.company_setup import opening_stock as stock
from erpnext.setup.doctype.company_setup.company_setup_steps import get_drafts_by, get_unclosed_years
from erpnext.setup.doctype.company_setup.opening_invoices import get_error_text, normalize_account_name


def make_closing_voucher(company: str, year, closing_account: str) -> str:
	"""Submit the Period Closing Voucher of a year, dated its end, or the draft one left for it."""
	voucher = frappe.db.get_value(
		"Period Closing Voucher", {"company": company, "fiscal_year": year.fiscal_year, "docstatus": 0}
	)
	doc = (
		frappe.get_doc("Period Closing Voucher", voucher)
		if voucher
		else frappe.new_doc("Period Closing Voucher")
	)
	doc.update(
		{
			"company": company,
			"fiscal_year": year.fiscal_year,
			"transaction_date": year.year_end_date,
			"period_start_date": year.year_start_date,
			"period_end_date": year.year_end_date,
			"closing_account_head": closing_account,
			"remarks": _("Closing {0} from Company Setup").format(year.fiscal_year),
		}
	)
	doc.save()
	doc.submit()
	return doc.name


YEAR_CLOSE_JOB_ID = "company_setup_year_close::{0}"
# a stock close still running after this long is taken as stuck: ERPNext's own job gives up at 25
STOCK_CLOSE_SECONDS = 40 * 60


def get_year_close_steps(onboarding, fiscal_year: str) -> list[dict]:
	"""What closing fiscal_year takes, in order: for it and each year before it still open, the stock
	to the year end when it isn't closed yet, then the year."""
	years = get_unclosed_years(onboarding)
	index = next((index for index, row in enumerate(years) if row.fiscal_year == fiscal_year), None)
	if index is None:
		frappe.throw(
			_("{0} has no profit or loss left to close.").format(fiscal_year), title=_("Nothing to close")
		)
	drafts = get_drafts_by(onboarding, years[index].year_end_date)
	if drafts:
		frappe.throw(
			_("Post or remove these opening drafts first: {0}. After the close they can't be posted.").format(
				" · ".join(escape_html(line) for line in drafts)
			),
			title=_("Can't close yet"),
		)
	steps = []
	for year in years[: index + 1]:
		closing = get_year_stock_closing(onboarding.company, year.year_end_date)
		if closing.needed and not closing.done:
			if closing.blocker:
				frappe.throw(closing.blocker, title=_("Not yet"))
			steps.append({"stock": str(year.year_end_date)})
		steps.append({"fiscal_year": year.fiscal_year})
	return steps


def get_year_close(onboarding) -> dict | None:
	"""The year being closed in the background, for the page: its steps, how many are done, and why
	it stopped. One no job carries on any more stopped too."""
	state = frappe.parse_json(onboarding.year_close)
	if not state:
		return None
	if not state.get("error") and not is_closing_year(onboarding.company):
		state["error"] = _("The close stopped before it was done.")
	return state


def is_closing_year(company: str) -> bool:
	if is_job_enqueued(YEAR_CLOSE_JOB_ID.format(company)):
		return True
	state = frappe.parse_json(frappe.db.get_value("Company Setup", company, "year_close"))
	return bool(state and state.get("waiting"))


def enqueue_year_close(company: str):
	frappe.enqueue(
		run_year_close,
		queue="long",
		job_id=YEAR_CLOSE_JOB_ID.format(company),
		deduplicate=True,
		timeout=1800,
		enqueue_after_commit=True,
		now=frappe.in_test,
		company=company,
	)


def resume_year_closes():
	"""Scheduled each minute: carry on the year closes waiting for a stock close to build."""
	for company in frappe.get_all(
		"Company Setup", filters={"year_close": ("like", '%"waiting": 1%')}, pluck="name"
	):
		enqueue_year_close(company)


def resume_year_close_on_stock_close(doc, method=None):
	"""Carry on a year close waiting for this stock close as soon as it is built."""
	if doc.status == "Completed" and frappe.db.get_value(
		"Company Setup", {"name": doc.company, "year_close": ("like", '%"waiting": 1%')}
	):
		enqueue_year_close(doc.company)


def run_year_close(company: str):
	"""The steps of the year close in turn, each kept once done. A stock close builds in ERPNext's
	own job: this one ends then as waiting, and starts again once it is built, or from
	resume_year_closes until STOCK_CLOSE_SECONDS pass. ERPNext enqueues that job before the commit,
	so a closing whose job lost its entry stays In Progress: the wait ends at that limit. A step that
	fails stops the close, with its error."""
	state = frappe._dict(frappe.parse_json(frappe.db.get_value("Company Setup", company, "year_close")))
	state.pop("waiting", None)
	while state.steps and state.done < len(state.steps):
		step = frappe._dict(state.steps[state.done])
		frappe.db.savepoint("year_close")
		try:
			if step.stock and not close_stock_step(company, step, state):
				save_year_close(company, state | {"waiting": 1})
				return
			if step.fiscal_year:
				close_year_step(company, step.fiscal_year, state.closing_account)
		except Exception as e:
			frappe.db.rollback(save_point="year_close")
			save_year_close(company, state | {"error": get_error_text(e)})
			return
		state.done += 1
		state.since = None
		save_year_close(company, state)
	save_year_close(company, None)


def close_stock_step(company: str, step, state) -> bool:
	"""Whether the stock is closed to the step's date: started here when not, and then waited for."""
	closing = get_year_stock_closing(company, step.stock)
	if closing.done or not closing.needed:
		return True
	if closing.status in ("Queued", "In Progress"):
		if not state.since:
			state.since = str(now_datetime())
		elif time_diff_in_seconds(now_datetime(), get_datetime(state.since)) > STOCK_CLOSE_SECONDS:
			frappe.throw(
				_("Closing stock to {0} is taking too long. See Stock Closing Entry {1}.").format(
					formatdate(step.stock), closing.name
				),
				title=_("Stock close stuck"),
			)
		return False
	if state.since:
		frappe.throw(
			_("Closing stock to {0} failed. See Stock Closing Entry {1} and the Error Log.").format(
				formatdate(step.stock), closing.name
			),
			title=_("Stock close failed"),
		)
	stock.make_stock_closing(company, step.stock)
	state.since = str(now_datetime())
	return False


def close_year_step(company: str, fiscal_year: str, closing_account: str):
	if frappe.db.exists(
		"Period Closing Voucher", {"company": company, "fiscal_year": fiscal_year, "docstatus": 1}
	):
		return
	year = frappe.db.get_value(
		"Fiscal Year",
		fiscal_year,
		["name as fiscal_year", "year_start_date", "year_end_date"],
		as_dict=True,
	)
	make_closing_voucher(company, year, closing_account)


def save_year_close(company: str, state: dict | None):
	"""Keep how far the close got, and tell the open page."""
	frappe.db.set_value(
		"Company Setup", company, "year_close", state and frappe.as_json(state), update_modified=False
	)
	# each step done stays done when a later one fails; a test runs the job inline and keeps no data
	if not frappe.in_test:
		frappe.db.commit()
	frappe.publish_realtime(
		"company_setup_year_close",
		{"company": company, "state": state},
		doctype="Company Setup",
		docname=company,
	)


def get_year_stock_closing(company: str, year_end) -> frappe._dict:
	"""The Stock Closing Entry a year's close needs, as opening_stock.get_stock_closing gives it;
	ERPNext asks for one only with perpetual inventory."""
	closing = stock.get_stock_closing(company, year_end)
	if not frappe.get_cached_value("Company", company, "enable_perpetual_inventory"):
		closing.needed = False
	return closing


# the names the account a year's profit is closed into usually has, the first found is suggested
CLOSING_ACCOUNT_NAMES = ("retained earnings", "reserves and surplus", "surplus", "profit and loss")


def get_closing_account(company: str) -> str | None:
	"""The account to suggest for a year's profit: a ledger under equity or liabilities named like
	Retained Earnings or Reserves & Surplus."""
	accounts = frappe.get_all(
		"Account",
		filters={
			"company": company,
			"is_group": 0,
			"disabled": 0,
			"root_type": ("in", ("Equity", "Liability")),
		},
		fields=["name", "account_name", "root_type"],
		order_by="lft",
	)
	names = {normalize_account_name(account.account_name): account.name for account in reversed(accounts)}
	equity = [account.name for account in accounts if account.root_type == "Equity"]
	return next((names[name] for name in CLOSING_ACCOUNT_NAMES if name in names), None) or (
		equity[0] if len(equity) == 1 else None
	)
