# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from contextlib import contextmanager
from io import BytesIO
from unittest.mock import patch

import frappe
from frappe.contacts.doctype.address.address import get_default_address
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.utils import flt, formatdate
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment

from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_fiscal_year
from erpnext.setup.doctype.company_setup import company_setup_steps
from erpnext.setup.doctype.company_setup.chart_sheet import download_chart_template, get_draft_balances
from erpnext.setup.doctype.company_setup.company_setup import (
	create_company_setup,
	get_chart_import_blocker,
)
from erpnext.setup.doctype.company_setup.company_setup_steps import HOOK_NAME, get_step_paths
from erpnext.setup.doctype.company_setup.opening_invoices import download_new_parties, ensure_fiscal_year
from erpnext.tests.utils import ERPNextTestSuite

COMPANY = "_Test Company"
NEW_COMPANY = "_Test Onboarding Company"

# a small chart from an old system, with the balances it closed with
SHEET = [
	"Account Name,Parent Account,Account Number,Parent Account Number,Is Group,Account Type,Root Type,Account Currency,Opening Debit,Opening Credit",
	"Assets,,1000,,1,,Asset,INR,,",
	"Current Assets,Assets,1100,1000,1,,Asset,INR,,",
	"Cash in Hand,Current Assets,1110,1100,0,Cash,Asset,INR,25000,",
	"HDFC Bank,Current Assets,1120,1100,0,Bank,Asset,INR,180000,",
	"Sundry Debtors,Current Assets,1130,1100,0,Receivable,Asset,INR,95000,",
	"Fixed Assets,Assets,1200,1000,1,,Asset,INR,,",
	"Furniture,Fixed Assets,1210,1200,0,Fixed Asset,Asset,INR,200000,",
	"Liabilities,,2000,,1,,Liability,INR,,",
	"Sundry Creditors,Liabilities,2100,2000,0,Payable,Liability,INR,,82000",
	"Owner Capital,Liabilities,2200,2000,0,,Liability,INR,,418000",
	"Income,,4000,,1,,Income,INR,,",
	"Sales,Income,4100,4000,0,Income Account,Income,INR,,",
	"Expenses,,5000,,1,,Expense,INR,,",
	"Purchases,Expenses,5100,5000,0,Cost of Goods Sold,Expense,INR,,",
	"Rent,Expenses,5200,5000,0,,Expense,INR,,",
]


def get_onboarding():
	create_company_setup(COMPANY)
	doc = frappe.get_doc("Company Setup", COMPANY)
	# as when the form opens, so steps added since the record was made are there
	doc.check_steps()
	return doc


def make_company() -> str:
	return (
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": NEW_COMPANY,
				"abbr": "_TOC",
				"default_currency": "INR",
				"country": "India",
				"chart_of_accounts": "Standard",
			}
		)
		.insert()
		.name
	)


def make_user(email: str, role: str) -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": role, "send_welcome_email": 0}
		).insert()
	frappe.get_doc("User", email).add_roles(role)
	return email


def make_sheet(rows: list[str], balances: bool = True) -> str:
	"""Upload the sheet as a CSV file. Without balances, the two balance columns are left empty."""
	if not balances:
		rows = [rows[0], *(",".join([*row.split(",")[:8], "", ""]) for row in rows[1:])]
	return (
		frappe.get_doc(
			{"doctype": "File", "file_name": "chart.csv", "content": "\n".join(rows), "is_private": 1}
		)
		.insert()
		.file_url
	)


def step_status(doc) -> dict:
	return {row.step_key.split(".")[-1]: row.status for row in doc.steps}


def extra_step(onboarding) -> dict:
	"""A step another app could add through the hook."""
	return {"title": "Extra step", "description": "", "done": False}


def broken_step(onboarding) -> dict:
	"""A step of another app with a bug in it."""
	raise ZeroDivisionError


@contextmanager
def hooked_steps(steps_by_app: dict[str, list]):
	"""Fake the step hook of these apps; the others keep their real hook. New apps come last."""
	real_get_hooks = frappe.get_hooks
	installed = frappe.get_installed_apps()
	apps = [*installed, *(app for app in steps_by_app if app not in installed)]

	def get_hooks(hook=None, *args, **kwargs):
		if hook == HOOK_NAME and kwargs.get("app_name") in steps_by_app:
			return list(steps_by_app[kwargs["app_name"]])
		return real_get_hooks(hook, *args, **kwargs)

	with (
		patch.object(frappe, "get_installed_apps", return_value=apps),
		patch.object(frappe, "get_hooks", side_effect=get_hooks),
	):
		yield


def step_names(**steps_by_app) -> list[str]:
	"""The order of the steps, by name, with these apps' hooks."""
	with hooked_steps(steps_by_app):
		return [path.rsplit(".", 1)[-1] for path in get_step_paths()]


# ERPNext's own steps before Go Live, in order
ERPNEXT_STEPS = [
	"migration_date",
	"chart_of_accounts",
	"opening_balances",
	"customers",
	"opening_sales_invoices",
	"suppliers",
	"opening_purchase_invoices",
	"check_books",
]
# steps whose status comes only from this company's onboarding, not from shared records
OWN_STEPS = ("migration_date", "chart_of_accounts", "opening_balances", "check_books", "go_live")


def steps_with(after: dict | None = None, before: dict | None = None) -> list[str]:
	"""ERPNext's steps with other steps put after or before some of them."""
	steps = []
	for step in [*ERPNEXT_STEPS, "go_live"]:
		steps += (before or {}).get(step, [])
		steps.append(step)
		steps += (after or {}).get(step, [])
	return steps


def step_key(doc, name: str) -> str:
	return next(row.step_key for row in doc.steps if row.step_key.endswith(f".{name}"))


class TestCompanySetup(ERPNextTestSuite):
	def test_new_company_gets_its_onboarding(self):
		doc = frappe.get_doc("Company Setup", make_company())
		self.assertEqual(doc.status, "In Progress")
		self.assertEqual(
			step_status(doc),
			{
				step: "Not Started"
				for step in (
					"migration_date",
					"chart_of_accounts",
					"opening_balances",
					"check_books",
					"go_live",
				)
			}
			| {step: status for step, status in step_status(doc).items() if step not in OWN_STEPS},
		)
		self.assertEqual(list(step_status(doc)), [*ERPNEXT_STEPS, "go_live"])

	def test_deleting_the_company_deletes_its_onboarding(self):
		company = make_company()
		self.assertTrue(frappe.db.exists("Company Setup", company))

		frappe.delete_doc("Company", company)
		self.assertFalse(frappe.db.exists("Company Setup", company))

	def test_a_hooked_step_shows_on_the_onboarding(self):
		extra = f"{__name__}.extra_step"
		with hooked_steps({"app_one": [{"step": extra, "after": "chart_of_accounts"}]}):
			doc = get_onboarding()
			doc.save()
		self.assertEqual(doc.steps[2].step_key, extra)
		self.assertEqual(doc.steps[2].step, "Extra step")

		# a step no app adds any more is dropped
		doc.save()
		self.assertNotIn(extra, [row.step_key for row in doc.steps])

	def test_a_failing_hooked_step_does_not_stop_the_setup(self):
		broken = f"{__name__}.broken_step"
		with hooked_steps({"app_one": [broken]}):
			doc = get_onboarding()
			doc.save()
			company = make_company()
		self.assertNotIn(broken, [row.step_key for row in doc.steps])
		self.assertTrue(frappe.db.exists("Company Setup", company))

	def test_chart_step_is_done_from_the_choice(self):
		doc = get_onboarding()
		self.assertEqual(step_status(doc)["chart_of_accounts"], "Not Started")

		doc.use_existing_chart()
		self.assertEqual(doc.chart_of_accounts_source, "Existing")
		self.assertEqual(step_status(doc)["chart_of_accounts"], "Done")

		# changing the choice asks again
		doc.change_chart_choice()
		self.assertFalse(doc.chart_of_accounts_source)
		self.assertEqual(step_status(doc)["chart_of_accounts"], "Not Started")

	def test_skip_and_bring_back(self):
		doc = get_onboarding()
		key = next(row.step_key for row in doc.steps if row.step_key.endswith("opening_balances"))

		doc.skip_step(key)
		self.assertEqual(step_status(doc)["opening_balances"], "Skipped")

		# a skipped step stays skipped when the steps are checked again
		doc.check_steps()
		self.assertEqual(step_status(doc)["opening_balances"], "Skipped")

		doc.skip_step(key, skip=0)
		self.assertEqual(step_status(doc)["opening_balances"], "Not Started")

	def test_done_step_stays_done_when_skipped(self):
		doc = get_onboarding()
		doc.use_existing_chart()
		key = next(row.step_key for row in doc.steps if row.step_key.endswith("chart_of_accounts"))

		doc.skip_step(key)
		self.assertEqual(step_status(doc)["chart_of_accounts"], "Done")

	def test_opening_balances_done_only_when_all_posted(self):
		doc = get_onboarding()
		doc.flags.posting = True
		doc.append("opening_balances", {"original_account": "Cash", "debit": 100, "status": "Posted"})
		doc.append("opening_balances", {"original_account": "Capital", "credit": 100, "status": "Draft"})
		doc.save()
		self.assertEqual(step_status(doc)["opening_balances"], "Not Started")

		doc.opening_balances[1].status = "Posted"
		doc.save()
		self.assertEqual(step_status(doc)["opening_balances"], "Done")

	def test_migration_date_step(self):
		doc = get_onboarding()
		self.assertEqual(step_status(doc)["migration_date"], "Not Started")

		doc.migration_date = "2026-04-01"
		doc.save()
		self.assertEqual(step_status(doc)["migration_date"], "Done")
		doc.run_method("onload")
		self.assertIn(
			formatdate("2026-03-31"), doc.get_onload().steps[step_key(doc, "migration_date")]["summary"]
		)

	def test_nothing_to_bring_finishes_the_money_steps(self):
		doc = get_onboarding()
		doc.no_opening_data = 1
		doc.save()
		status = step_status(doc)
		for step in (
			"opening_balances",
			"opening_sales_invoices",
			"opening_purchase_invoices",
			"check_books",
		):
			self.assertEqual(status[step], "Done", step)

	def test_steps_say_which_tab_they_are_done_on(self):
		doc = get_onboarding()
		doc.run_method("onload")
		tabs = {key.rsplit(".", 1)[-1]: step["tab"] for key, step in doc.get_onload().steps.items()}
		self.assertEqual(tabs["chart_of_accounts"], "accounts_tab")
		self.assertEqual(tabs["opening_sales_invoices"], "selling_tab")
		self.assertEqual(tabs["suppliers"], "buying_tab")
		self.assertIsNone(tabs["go_live"])
		meta = frappe.get_meta("Company Setup")
		for tab in set(tabs.values()) - {None}:
			self.assertEqual(meta.get_field(tab).fieldtype, "Tab Break")

	def test_check_books_needs_temporary_opening_at_zero(self):
		doc = get_onboarding()
		doc.flags.posting = True
		doc.append("opening_balances", {"original_account": "Cash", "debit": 100, "status": "Posted"})

		with patch.object(company_setup_steps, "get_temporary_opening_balance", return_value=500):
			doc.save()
		self.assertEqual(step_status(doc)["check_books"], "Not Started")

		with patch.object(company_setup_steps, "get_temporary_opening_balance", return_value=0):
			doc.save()
		self.assertEqual(step_status(doc)["check_books"], "Done")

	def test_check_books_waits_for_drafts(self):
		doc = get_onboarding()
		doc.flags.posting = True
		doc.append("opening_balances", {"original_account": "Cash", "debit": 100, "status": "Posted"})
		doc.append("opening_sales_invoices", {"party_name": "X", "outstanding_amount": 10})
		with patch.object(company_setup_steps, "get_temporary_opening_balance", return_value=0):
			doc.save()
		self.assertEqual(step_status(doc)["check_books"], "Not Started")

	def test_go_live(self):
		doc = get_onboarding()
		doc.go_live()
		self.assertEqual(doc.status, "Live")
		self.assertEqual(step_status(doc)["go_live"], "Done")

	def test_update_company_details(self):
		doc = get_onboarding()
		doc.update_company_details(
			{
				"tax_id": "27ABCDE1234F1Z5",
				"email": "accounts@example.com",
				"address_line1": "12 MG Road",
				"city": "Pune",
				"country": "India",
			}
		)
		self.assertEqual(frappe.db.get_value("Company", COMPANY, "tax_id"), "27ABCDE1234F1Z5")
		address = get_default_address("Company", COMPANY)
		self.assertEqual(frappe.db.get_value("Address", address, "city"), "Pune")
		self.assertTrue(frappe.db.get_value("Address", address, "is_your_company_address"))

		# the same address is updated, not a second one added
		doc.update_company_details({"address_line1": "12 MG Road", "city": "Mumbai", "country": "India"})
		self.assertEqual(get_default_address("Company", COMPANY), address)
		self.assertEqual(frappe.db.get_value("Address", address, "city"), "Mumbai")


class TestStepOrder(ERPNextTestSuite):
	"""Where steps from other apps go."""

	def test_erpnext_lists_its_steps_in_the_hook_like_any_app(self):
		self.assertEqual(
			[path.rsplit(".", 1)[-1] for path in frappe.get_hooks(HOOK_NAME, app_name="erpnext")],
			[*ERPNEXT_STEPS, "go_live"],
		)

	def test_go_live_is_last_even_if_listed_early(self):
		go_live = "erpnext.setup.doctype.company_setup.company_setup_steps.go_live"
		self.assertEqual(
			step_names(app_one=[go_live, "app_one.steps.gst"])[-2:],
			["gst", "go_live"],
		)

	def test_erpnext_steps_alone(self):
		self.assertEqual(step_names(), [*ERPNEXT_STEPS, "go_live"])

	def test_step_without_a_place_goes_before_go_live(self):
		self.assertEqual(
			step_names(app_one=["app_one.steps.gst"]),
			[*ERPNEXT_STEPS, "gst", "go_live"],
		)

	def test_before_and_after(self):
		self.assertEqual(
			step_names(
				app_one=[
					{"step": "app_one.steps.gst", "after": "chart_of_accounts"},
					{"step": "app_one.steps.bank", "before": "check_books"},
				]
			),
			steps_with({"chart_of_accounts": ["gst"]}, before={"check_books": ["bank"]}),
		)

	def test_same_place_keeps_install_order(self):
		self.assertEqual(
			step_names(
				app_one=[{"step": "app_one.steps.gst", "after": "chart_of_accounts"}],
				app_two=[{"step": "app_two.steps.employees", "after": "chart_of_accounts"}],
			),
			steps_with({"chart_of_accounts": ["gst", "employees"]}),
		)
		self.assertEqual(
			step_names(
				app_one=[{"step": "app_one.steps.gst", "before": "check_books"}],
				app_two=[{"step": "app_two.steps.employees", "before": "check_books"}],
			),
			steps_with(before={"check_books": ["gst", "employees"]}),
		)

	def test_step_can_point_to_a_step_of_a_later_app(self):
		self.assertEqual(
			step_names(
				app_one=[{"step": "app_one.steps.payroll", "after": "employees"}],
				app_two=[{"step": "app_two.steps.employees", "after": "opening_balances"}],
			),
			steps_with({"opening_balances": ["employees", "payroll"]}),
		)

	def test_unknown_place_goes_before_go_live(self):
		self.assertEqual(
			step_names(app_one=[{"step": "app_one.steps.gst", "after": "no_such_step"}]),
			[*ERPNEXT_STEPS, "gst", "go_live"],
		)

	def test_go_live_stays_last(self):
		self.assertEqual(
			step_names(app_one=[{"step": "app_one.steps.gst", "after": "go_live"}]),
			[*ERPNEXT_STEPS, "gst", "go_live"],
		)

	def test_bad_entries_are_skipped(self):
		self.assertEqual(
			step_names(app_one=[42, {"after": "review"}, "app_one.steps.gst"]),
			[*ERPNEXT_STEPS, "gst", "go_live"],
		)


class TestChartImport(ERPNextTestSuite):
	"""Importing a chart of accounts sheet from the Chart of Accounts step."""

	def setUp(self):
		self.company = make_company()
		self.system_manager = make_user("onboarding.system@example.com", "System Manager")
		frappe.set_user(make_user("onboarding.accounts@example.com", "Accounts Manager"))
		self.doc = frappe.get_doc("Company Setup", self.company)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_import_replaces_the_chart_and_keeps_balances_as_drafts(self):
		self.doc.import_chart(make_sheet(SHEET))

		self.assertEqual(self.doc.chart_of_accounts_source, "Imported")
		self.assertEqual(step_status(self.doc)["chart_of_accounts"], "Done")
		self.assertTrue(frappe.db.exists("Account", "1110 - Cash in Hand - _TOC"))
		self.assertFalse(frappe.db.exists("Account", "Cash - _TOC"))
		self.assertEqual(
			frappe.db.get_value(
				"Company",
				self.company,
				["default_receivable_account", "default_cash_account", "default_bank_account"],
			),
			("1130 - Sundry Debtors - _TOC", "1110 - Cash in Hand - _TOC", "1120 - HDFC Bank - _TOC"),
		)

		# customers and suppliers come in as opening invoices, not as balances
		rows = {row.original_account: row for row in self.doc.opening_balances}
		self.assertEqual(len(rows), 4)
		self.assertNotIn("1130 - Sundry Debtors", rows)
		self.assertEqual(rows["1110 - Cash in Hand"].account, "1110 - Cash in Hand - _TOC")
		self.assertEqual(rows["1110 - Cash in Hand"].debit, 25000)
		self.assertEqual(rows["2200 - Owner Capital"].credit, 418000)
		self.assertEqual({row.status for row in self.doc.opening_balances}, {"Draft"})

	def test_import_adds_the_accounts_erpnext_posts_to(self):
		# a chart from other software has a round off account at most, and no stock-not-billed one
		sheet = [*SHEET, "Round Off,Expenses,5300,5000,0,Round Off,Expense,INR,,"]
		result = self.doc.import_chart(make_sheet(sheet))

		srbnb = frappe.db.get_value("Company", self.company, "stock_received_but_not_billed")
		self.assertEqual(result, {"added_accounts": [srbnb]})
		self.assertEqual(
			frappe.db.get_value("Account", srbnb, ["account_type", "root_type"]),
			("Stock Received But Not Billed", "Liability"),
		)
		self.assertEqual(
			frappe.db.get_value("Company", self.company, "round_off_account"), "5300 - Round Off - _TOC"
		)

	def test_import_keeps_the_default_accounts_by_name(self):
		# a big company's chart: several receivable accounts, the usual one first
		sheet = [
			*SHEET,
			"Debtors,Current Assets,1131,1100,0,Receivable,Asset,INR,,",
			"Retention Money Receivable,Current Assets,1132,1100,0,Receivable,Asset,INR,,",
		]
		self.doc.import_chart(make_sheet(sheet))

		receivable = frappe.db.get_value("Company", self.company, "default_receivable_account")
		self.assertEqual(frappe.db.get_value("Account", receivable, "account_name"), "Debtors")

	def test_preview_changes_nothing(self):
		before = frappe.db.count("Account", {"company": self.company})
		preview = self.doc.preview_chart(make_sheet(SHEET))

		self.assertEqual(len(preview["accounts"]), len(SHEET) - 1)
		self.assertEqual(preview["current_accounts"], before)
		# customers and suppliers are left out, and said so
		self.assertEqual(preview["balances"], 4)
		self.assertEqual(preview["debit"], 405000)
		self.assertEqual(preview["credit"], 418000)
		self.assertEqual(preview["left_out"], {"Receivable": 95000, "Payable": -82000})
		self.assertTrue(frappe.db.exists("Account", "Cash - _TOC"))

	def test_a_trial_balance_is_not_taken_for_a_chart(self):
		sheet = make_file("trial_balance.csv", ["Particulars,Debit,Credit", "Cash,100,", "Capital,,100"])
		with self.assertRaisesRegex(frappe.ValidationError, "isn't a chart of accounts"):
			self.doc.preview_chart(sheet)

	def test_balance_on_a_group_account_is_refused(self):
		sheet = [
			row.replace(
				"Current Assets,Assets,1100,1000,1,,Asset,INR,,",
				"Current Assets,Assets,1100,1000,1,,Asset,INR,10,",
			)
			for row in SHEET
		]
		self.assertRaises(frappe.ValidationError, self.doc.preview_chart, make_sheet(sheet))

	def test_draft_balances_move_to_the_new_account_with_the_same_name(self):
		self.doc.append(
			"opening_balances", {"original_account": "Cash", "account": "Cash - _TOC", "debit": 100}
		)
		self.doc.append(
			"opening_balances",
			{"original_account": "Earnest Money", "account": "Earnest Money - _TOC", "debit": 50},
		)
		self.doc.save()

		sheet = [row.replace("Cash in Hand,", "Cash,") for row in SHEET]
		self.doc.import_chart(make_sheet(sheet, balances=False))

		self.assertEqual(
			[(row.original_account, row.account) for row in self.doc.opening_balances],
			[("Cash", "1110 - Cash - _TOC"), ("Earnest Money", None)],
		)

	def test_downloaded_chart_can_be_imported_again(self):
		frappe.local.response = frappe._dict()
		download_chart_template(self.company, "Current Chart")
		file_url = (
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "chart.xlsx",
					"content": frappe.response.filecontent,
					"is_private": 1,
				}
			)
			.insert()
			.file_url
		)
		before = frappe.db.count("Account", {"company": self.company})

		self.doc.import_chart(file_url)
		self.assertEqual(frappe.db.count("Account", {"company": self.company}), before)
		self.assertTrue(frappe.db.exists("Account", "Cash - _TOC"))

	def test_only_an_accounts_manager_can_import(self):
		frappe.set_user(self.system_manager)
		self.assertTrue(get_chart_import_blocker(self.company))
		self.assertRaises(frappe.PermissionError, self.doc.import_chart, make_sheet(SHEET))

	def test_no_import_once_there_are_entries(self):
		self.assertIsNone(get_chart_import_blocker(self.company))
		frappe.get_doc(
			{
				"doctype": "GL Entry",
				"name": "_Test Onboarding GL Entry",
				"company": self.company,
				"account": "Cash - _TOC",
				"debit": 100,
			}
		).db_insert()

		self.assertTrue(get_chart_import_blocker(self.company))
		self.assertRaises(frappe.ValidationError, self.doc.import_chart, make_sheet(SHEET))


def get_account(company: str, **filters) -> str:
	return frappe.get_all(
		"Account", filters={"company": company, "is_group": 0, **filters}, pluck="name", limit=1
	)[0]


def make_customer(name: str, disabled: int = 0) -> str:
	user = frappe.session.user
	frappe.set_user("Administrator")
	customer = frappe.get_doc(
		{"doctype": "Customer", "customer_name": name, "customer_type": "Company", "disabled": disabled}
	).insert()
	frappe.set_user(user)
	return customer.name


def has_year(date: str) -> bool:
	return bool(
		frappe.db.exists(
			"Fiscal Year", {"year_start_date": ("<=", date), "year_end_date": (">=", date), "disabled": 0}
		)
	)


def make_xlsx_file(name: str, rows: list[tuple], indent_column: int | None = None) -> str:
	"""An Excel file of these rows; with indent_column, a row's extra last value is that cell's indent."""
	workbook = Workbook()
	width = max(len(row) for row in rows) - (1 if indent_column else 0)
	for row in rows:
		workbook.active.append(row[:width])
		if indent_column and len(row) > width:
			workbook.active.cell(workbook.active.max_row, indent_column).alignment = Alignment(indent=row[-1])
	content = BytesIO()
	workbook.save(content)
	return (
		frappe.get_doc({"doctype": "File", "file_name": name, "content": content.getvalue(), "is_private": 1})
		.insert()
		.file_url
	)


def make_file(name: str, rows: list[str]) -> str:
	return (
		frappe.get_doc({"doctype": "File", "file_name": name, "content": "\n".join(rows), "is_private": 1})
		.insert()
		.file_url
	)


class TestOpeningBalances(ERPNextTestSuite):
	"""Opening balances on the Accounts tab: everything but customers, suppliers and stock."""

	def setUp(self):
		self.company = make_company()
		frappe.set_user(make_user("onboarding.accounts@example.com", "Accounts Manager"))
		self.doc = frappe.get_doc("Company Setup", self.company)
		self.cash = get_account(self.company, account_type="Cash")
		self.capital = get_account(self.company, root_type="Equity", account_type=("!=", "Temporary"))

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_save_replaces_drafts_and_keeps_posted_rows(self):
		self.doc.append("opening_balances", {"account": self.cash, "debit": 10, "status": "Posted"})
		self.doc.flags.posting = True
		self.doc.save()
		self.doc.flags.posting = False

		self.doc.save_opening_balances(
			[
				{"account": self.capital, "credit": 500},
				{"account": self.cash, "debit": 0, "credit": 0},
			]
		)
		self.assertEqual(
			[(row.account, flt(row.debit), flt(row.credit), row.status) for row in self.doc.opening_balances],
			[(self.cash, 10, 0, "Posted"), (self.capital, 0, 500, "Draft")],
		)
		self.assertEqual(self.doc.opening_balances[1].root_type, "Equity")

	def test_a_form_save_cannot_mark_a_row_posted(self):
		self.doc.save_opening_balances([{"account": self.cash, "debit": 100}])
		self.doc.opening_balances[0].status = "Posted"
		self.doc.save()
		self.assertEqual(self.doc.opening_balances[0].status, "Draft")

	def test_debit_and_credit_on_one_row_are_netted(self):
		self.doc.save_opening_balances(
			[{"account": self.cash, "debit": 100, "credit": 30}, {"account": self.capital, "debit": -50}]
		)
		self.assertEqual(
			[(flt(row.debit), flt(row.credit)) for row in self.doc.opening_balances], [(70, 0), (0, 50)]
		)

	def test_post_uses_what_is_saved(self):
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_balances([{"account": self.cash, "debit": 100}])
		# a change that was never saved is not posted
		self.doc.opening_balances[0].debit = 999
		self.doc.post_opening_balances()
		self.assertEqual(self.doc.opening_balances[0].debit, 100)

	def test_migration_date_is_fixed_once_posted(self):
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_balances([{"account": self.cash, "debit": 100}])
		self.doc.post_opening_balances()
		self.doc.migration_date = "2026-05-01"
		self.assertRaises(frappe.ValidationError, self.doc.save)

	def test_customer_supplier_and_stock_accounts_are_refused(self):
		for account_type in ("Receivable", "Payable", "Stock", "Temporary"):
			account = get_account(self.company, account_type=account_type)
			self.assertRaises(
				frappe.ValidationError,
				self.doc.save_opening_balances,
				[{"account": account, "debit": 100}],
			)

	def test_another_companys_account_is_refused(self):
		other = get_account("_Test Company", account_type="Cash")
		self.assertRaises(
			frappe.ValidationError, self.doc.save_opening_balances, [{"account": other, "debit": 100}]
		)

	def test_trial_balance_upload_uses_closing_balances(self):
		cash_name = frappe.db.get_value("Account", self.cash, "account_name")
		customer = frappe.get_all("Customer", pluck="customer_name", limit=1)[0]
		debtors = frappe.db.get_value(
			"Account", get_account(self.company, account_type="Receivable"), "account_name"
		)
		group = frappe.db.get_value(
			"Account", {"company": self.company, "is_group": 1, "root_type": "Asset"}, "account_name"
		)
		result = self.doc.import_opening_balances(
			make_file(
				"trial_balance.csv",
				[
					"Particulars,Opening Debit,Opening Credit,Closing Debit,Closing Credit",
					f"{cash_name},100,,5000,",
					f"{group},100,,5000,",
					f"{debtors},,,9000,",
					f"{customer},,,1000,",
					"Old Ledger,,,,300",
				],
			)
		)
		self.assertEqual(
			[(row.original_account, row.account, row.debit, row.credit) for row in self.doc.opening_balances],
			[(cash_name, self.cash, 5000, 0), ("Old Ledger", None, 0, 300)],
		)
		self.assertEqual(result["unmatched"], ["Old Ledger"])
		self.assertEqual(result["left_out"], {"Receivable": 10000})
		self.assertEqual(result["groups"], 1)

	def test_tally_trial_balance_as_exported(self):
		cash_name = frappe.db.get_value("Account", self.cash, "account_name")
		capital_name = frappe.db.get_value("Account", self.capital, "account_name")
		# Tally's Excel export: title rows, headings over a few rows, ledgers indented under groups
		rows = [
			("Kapoor Interiors",),
			("Trial Balance",),
			("Particulars", "Kapoor Interiors"),
			(None, "Closing Balance"),
			(None, "Debit", "Credit"),
			("Capital Account", None, 1000, 0),
			(capital_name, None, 1000, 2),
			("Current Assets", 1000, None, 0),
			("Sundry Debtors", 700, None, 2),
			("Mehta Residency", 700, None, 4),
			("Cash-in-Hand", 300, None, 2),
			(cash_name, 300, None, 4),
			("Grand Total", 1000, 1000, 0),
		]
		result = self.doc.import_opening_balances(make_xlsx_file("tally_tb.xlsx", rows, indent_column=1))
		self.assertEqual(
			[(row.account, row.debit, row.credit) for row in self.doc.opening_balances],
			[(self.capital, 0, 1000), (self.cash, 300, 0)],
		)
		self.assertEqual((result["groups"], result["left_out"]), (4, {"Receivable": 700}))

	def test_trial_balance_takes_the_closing_columns_named_in_the_row_above(self):
		cash_name = frappe.db.get_value("Account", self.cash, "account_name")
		self.doc.import_opening_balances(
			make_file(
				"tb.csv",
				[
					",Opening Balance,,Transactions,,Closing Balance,,",
					"Particulars,Debit,Credit,Debit,Credit,Debit,Credit,Dr/Cr",
					f"{cash_name},100,,900,,1000,,Dr",
				],
			)
		)
		self.assertEqual(
			[(row.account, row.debit, row.credit) for row in self.doc.opening_balances],
			[(self.cash, 1000, 0)],
		)

	def test_trial_balance_indent_is_read_from_the_name_column(self):
		cash_name = frappe.db.get_value("Account", self.cash, "account_name")
		rows = [
			("Account Number", "Account Name", "Debit", "Credit"),
			(None, "Current Assets", 300, None, 0),
			("A100", cash_name, 300, None, 2),
		]
		result = self.doc.import_opening_balances(make_xlsx_file("tb.xlsx", rows, indent_column=2))
		self.assertEqual([row.debit for row in self.doc.opening_balances], [300])
		self.assertEqual(result["groups"], 1)

	def test_busy_trial_balance_with_a_group_column(self):
		cash_name = frappe.db.get_value("Account", self.cash, "account_name")
		result = self.doc.import_opening_balances(
			make_file(
				"busy_tb.csv",
				[
					"Trial Balance as on 31-03-2026",
					"Particulars,Group,Dr. Amount (Rs.),Cr. Amount (Rs.)",
					f"{cash_name},Cash-in-hand,300,",
					"Opening Stock,Stock-in-hand,1500,",
					"Total,,1800,",
				],
			)
		)
		self.assertEqual([(row.account, row.debit) for row in self.doc.opening_balances], [(self.cash, 300)])
		self.assertEqual(result["left_out"], {"Stock": 1500})

	def test_a_receivables_total_line_is_left_out_with_customers(self):
		group = frappe.db.get_value(
			"Account",
			{
				"company": self.company,
				"is_group": 1,
				"account_type": ("is", "not set"),
				"account_name": "Accounts Receivable",
			},
			"account_name",
		)
		result = self.doc.import_opening_balances(
			make_file("tb.csv", ["Account,Debit,Credit", f"{group},1500,"])
		)
		self.assertEqual(result["left_out"], {"Receivable": 1500})
		self.assertEqual(result["groups"], 0)

	def test_downloaded_sheet_adds_up_the_rows_of_an_account(self):
		self.doc.save_opening_balances(
			[{"account": self.cash, "debit": 100}, {"account": self.cash, "debit": 50}]
		)
		self.assertEqual(get_draft_balances(self.doc), {self.cash: (150, 0)})

	def test_post_needs_the_migration_date(self):
		self.doc.save_opening_balances([{"account": self.cash, "debit": 100}])
		self.assertRaises(frappe.ValidationError, self.doc.post_opening_balances)

	def test_cancelling_a_posted_entry_lets_it_be_posted_again(self):
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_balances(
			[{"account": self.cash, "debit": 1000}, {"account": self.capital, "credit": 1000}]
		)
		(entry,) = self.doc.post_opening_balances()
		frappe.get_doc("Journal Entry", entry).cancel()

		self.doc.reload()
		self.assertEqual(
			{(row.status, row.journal_entry) for row in self.doc.opening_balances}, {("Draft", None)}
		)
		self.doc.post_opening_balances()
		self.doc.reload()
		self.assertEqual({row.status for row in self.doc.opening_balances}, {"Posted"})

	def test_post_balances_against_temporary_opening(self):
		sales = get_account(self.company, root_type="Income")
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_balances(
			[
				{"account": self.cash, "debit": 1000},
				{"account": self.capital, "credit": 600},
				{"account": sales, "credit": 100},
			]
		)
		opening, so_far = self.doc.post_opening_balances()

		opening = frappe.get_doc("Journal Entry", opening)
		self.assertEqual((opening.is_opening, str(opening.posting_date)), ("Yes", "2026-03-31"))
		self.assertEqual(opening.total_debit, 1000)
		so_far = frappe.get_doc("Journal Entry", so_far)
		self.assertEqual(so_far.is_opening, "No")
		self.assertEqual(company_setup_steps.get_temporary_opening_balance(self.company), -400 + 100)
		self.assertEqual({row.status for row in self.doc.opening_balances}, {"Posted"})
		self.assertEqual(step_status(self.doc)["opening_balances"], "Done")

	def test_check_your_books_follows_balances_posted_after_nothing_to_bring(self):
		self.doc.migration_date = "2026-04-01"
		self.doc.no_opening_data = 1
		self.doc.save_opening_balances([{"account": self.cash, "debit": 1000}])
		self.doc.post_opening_balances()
		self.doc.run_method("onload")
		step = self.doc.get_onload().steps[step_key(self.doc, "check_books")]
		self.assertIn("left in Temporary Opening", step["summary"])
		self.assertNotEqual(step_status(self.doc)["check_books"], "Done")

	def test_last_years_depreciation_posts_in_a_depreciation_entry(self):
		depreciation = get_account(self.company, account_type="Depreciation")
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_balances(
			[{"account": self.cash, "debit": 1000}, {"account": depreciation, "debit": 200}]
		)
		entries = self.doc.post_opening_balances()

		voucher_types = {frappe.db.get_value("Journal Entry", name, "voucher_type") for name in entries}
		self.assertEqual(voucher_types, {"Opening Entry", "Depreciation Entry"})

	def test_preview_shows_the_books_after_posting(self):
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_balances(
			[{"account": self.cash, "debit": 1000}, {"account": self.capital, "credit": 600}]
		)
		preview = self.doc.preview_opening_balances()

		balances = {row["account"]: row["balance"] for row in preview["rows"]}
		self.assertEqual(str(preview["posting_date"]), "2026-03-31")
		self.assertEqual(balances[self.cash], 1000)
		self.assertEqual(balances[self.capital], -600)
		self.assertEqual(balances[preview["temporary_account"]], -400)
		self.assertEqual(sum(balances.values()), 0)
		# a preview posts nothing
		self.assertFalse(frappe.db.exists("GL Entry", {"company": self.company}))

	def test_preview_counts_only_invoices_that_can_post(self):
		customer = frappe.get_all("Customer", pluck="name", limit=1)[0]
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": customer, "outstanding_amount": 700},
				{"party_name": "Onboarding Nobody Yet", "outstanding_amount": 300},
			],
		)
		preview = self.doc.preview_opening_balances()

		balances = {row["account"]: row["balance"] for row in preview["rows"]}
		receivable = get_party_account("Customer", customer, self.company)
		self.assertEqual(balances[receivable], 700)
		self.assertEqual(balances[preview["temporary_account"]], -700)
		self.assertEqual((preview["draft_invoices"], preview["left_out_invoices"]), (1, 1))

	def test_post_adds_the_financial_year_it_falls_in(self):
		frappe.set_user("Administrator")
		# test sites have years up to 2050 only
		self.doc.migration_date = "2071-04-01"
		self.doc.save_opening_balances([{"account": self.cash, "debit": 100}])
		self.doc.post_opening_balances()
		self.assertTrue(has_year("2071-03-31"))

	def test_a_missing_financial_year_is_pointed_out_before_posting(self):
		self.doc.migration_date = "2073-04-01"
		self.doc.run_method("onload")
		self.assertEqual(self.doc.get_onload().missing_fiscal_year, {"year": "2073", "date": "2073-03-31"})

	def test_post_adds_the_financial_year_again_after_a_failed_post(self):
		frappe.set_user("Administrator")
		self.doc.migration_date = "2072-04-01"
		self.doc.save_opening_balances([{"account": self.cash, "debit": 100}])
		# a first post that adds the year, then fails and is rolled back with the year still cached
		frappe.db.savepoint("failed_post")
		ensure_fiscal_year("2072-03-31", self.company)
		get_fiscal_year("2072-03-31", company=self.company, boolean=True)
		frappe.db.rollback(save_point="failed_post")

		self.doc.post_opening_balances()
		self.assertTrue(has_year("2072-03-31"))

	def test_post_adds_a_temporary_account_when_the_chart_has_none(self):
		# like an imported chart: no temporary account at all
		frappe.set_user("Administrator")
		frappe.delete_doc("Account", get_account(self.company, account_type="Temporary"))
		frappe.set_user("onboarding.accounts@example.com")
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_balances([{"account": self.cash, "debit": 1000}])

		self.doc.post_opening_balances()
		account = frappe.db.get_value(
			"Account", {"company": self.company, "account_type": "Temporary", "disabled": 0}
		)
		self.assertEqual(company_setup_steps.get_temporary_opening_balance(self.company), -1000)
		self.assertEqual(frappe.db.get_value("Account", account, "root_type"), "Asset")


class TestOpeningInvoices(ERPNextTestSuite):
	"""Unpaid customer and supplier invoices on the Selling and Buying tabs."""

	def setUp(self):
		user = make_user("onboarding.accounts@example.com", "Accounts Manager")
		for role in ("Sales Manager", "Purchase Manager"):
			make_user(user, role)
		self.company = make_company()
		frappe.set_user(user)
		self.doc = frappe.get_doc("Company Setup", self.company)
		self.customer = frappe.get_all("Customer", fields=["name", "customer_name"], limit=1)[0]

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_save_finds_existing_customers_by_name(self):
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": f"  {self.customer.customer_name.upper()} ", "outstanding_amount": 100},
				{"party_name": "Onboarding New Customer", "outstanding_amount": 50},
			],
		)
		rows = self.doc.opening_sales_invoices
		self.assertEqual([row.party for row in rows], [self.customer.name, None])
		self.assertEqual(self.doc.get_invoice_names("Customer")["new"], ["Onboarding New Customer"])
		self.assertEqual(step_status(self.doc)["opening_sales_invoices"], "Not Started")
		self.doc.run_method("onload")
		step = self.doc.get_onload().steps[step_key(self.doc, "opening_sales_invoices")]
		self.assertTrue(step["started"])

	def test_a_name_almost_like_a_customer_is_suggested(self):
		almost = self.customer.customer_name[:-1] + "x"
		self.doc.save_opening_invoices("Customer", [{"party_name": almost, "outstanding_amount": 10}])
		self.assertEqual(self.doc.get_invoice_names("Customer")["suggestions"], {almost: self.customer.name})

	def test_only_a_typo_of_a_customer_is_suggested(self):
		homes = make_customer("Patil Homes Pvt Ltd")
		make_customer("Joshi Developers")
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": "Pátil Homs", "outstanding_amount": 10},
				{"party_name": "Joshi Builders", "outstanding_amount": 20},
			],
		)
		self.assertEqual(self.doc.get_invoice_names("Customer")["suggestions"], {"Pátil Homs": homes})

	def test_two_spellings_of_one_new_name_are_pointed_out(self):
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": "Ramesh Kumar Agarwal", "outstanding_amount": 10},
				{"party_name": "Ramesh Kumar Agrawal", "outstanding_amount": 20},
				{"party_name": "Global Hospitals Hyderabad", "outstanding_amount": 30},
			],
		)
		self.assertEqual(
			self.doc.get_invoice_names("Customer")["look_alikes"],
			[["Ramesh Kumar Agarwal", "Ramesh Kumar Agrawal"]],
		)

	def test_a_similar_name_links_only_when_one_party_matches(self):
		cafe = make_customer("Café Brew Lane Pvt. Ltd.")
		twin = make_customer("Twin Traders")
		make_customer("Twin Traders Ltd")
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": "cafe brew-lane", "outstanding_amount": 100},
				{"party_name": "TWIN TRADERS LLP", "outstanding_amount": 50},
				{"party_name": "Twin Traders", "outstanding_amount": 70},
			],
		)
		self.assertEqual([row.party for row in self.doc.opening_sales_invoices], [cafe, None, twin])
		names = self.doc.get_invoice_names("Customer")
		self.assertEqual(names["similar"], {"cafe brew-lane": cafe})
		self.assertEqual(names["new"], ["TWIN TRADERS LLP"])

	def test_a_picked_customer_is_kept_on_save(self):
		self.doc.save_opening_invoices(
			"Customer",
			[{"party_name": "Sheet Name Only", "party": self.customer.name, "outstanding_amount": 100}],
		)
		row = self.doc.opening_sales_invoices[0]
		self.assertEqual((row.party_name, row.party), ("Sheet Name Only", self.customer.name))
		self.assertEqual(self.doc.get_invoice_names("Customer")["new"], [])

	def test_a_disabled_customer_is_linked_but_not_posted(self):
		disabled = make_customer("Onboarding Disabled Customer", disabled=1)
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer", [{"party_name": "Onboarding Disabled Customer", "outstanding_amount": 100}]
		)
		self.assertEqual(self.doc.get_invoice_names("Customer")["disabled"], [disabled])
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		row = self.doc.opening_sales_invoices[0]
		self.assertEqual((row.party, row.status), (disabled, "Failed"))
		self.assertIn("disabled", row.error)
		self.assertEqual(frappe.db.count("Customer", {"customer_name": "Onboarding Disabled Customer"}), 1)

	def test_invoice_after_the_migration_date_is_refused(self):
		self.doc.migration_date = "2026-04-01"
		self.assertRaises(
			frappe.ValidationError,
			self.doc.save_opening_invoices,
			"Customer",
			[{"party_name": "X", "invoice_date": "2026-04-05", "outstanding_amount": 10}],
		)

	def test_receivables_report_upload(self):
		result = self.doc.import_opening_invoices(
			"Customer",
			make_file(
				"receivables.csv",
				[
					"Customer,Invoice No,Invoice Date,Due Date,Outstanding",
					f"{self.customer.customer_name},OB-INV-1,2026-02-01,2026-03-03,1200",
					"Onboarding New Customer,OB-INV-2,,,800",
					f"{self.customer.customer_name},,,,-300",
				],
			),
		)
		self.assertEqual(
			result, {"added": 3, "advances": 1, "skipped": 0, "repeated": 0, "unused_columns": []}
		)
		self.assertEqual(
			[(row.invoice_number, row.outstanding_amount) for row in self.doc.opening_sales_invoices],
			[("OB-INV-1", 1200), ("OB-INV-2", 800), (None, -300)],
		)

	def test_tally_bills_report_as_exported(self):
		frappe.local.user_date_format = "dd-mm-yyyy"
		self.addCleanup(setattr, frappe.local, "user_date_format", None)
		report = [
			"Kapoor Interiors",
			"Office 7, Baner Road, Pune",
			"Bills Receivable",
			"1-Apr-2025 to 31-Mar-2026",
			"Date,Ref. No.,Party's Name,Pending Amount,Due on,Overdue by days",
			f'18-01-2026,KI/141,{self.customer.customer_name},"2,36,000.00 Dr",17-02-2026,42',
			f'25-03-2026,On Account,{self.customer.customer_name},"30,000.00 Cr",,',
			f",On Account,{self.customer.customer_name},12500,,",
			"Total,,,,,",
		]
		result = self.doc.import_opening_invoices("Customer", make_file("bills.csv", report))
		self.assertEqual(
			result,
			{"added": 3, "advances": 1, "skipped": 0, "repeated": 0, "unused_columns": ["Overdue by days"]},
		)
		self.assertEqual(
			[(row.invoice_number, row.outstanding_amount) for row in self.doc.opening_sales_invoices],
			[("KI/141", 236000), (None, -30000), (None, 12500)],
		)
		self.assertEqual(str(self.doc.opening_sales_invoices[0].invoice_date), "2026-01-18")

		self.doc.import_opening_invoices(
			"Supplier",
			make_file(
				"payables.csv", ["Bills Payable", "Party's Name,Pending Amount", 'Sai Marble,"20,000.00 Dr"']
			),
		)
		self.assertEqual(self.doc.opening_purchase_invoices[0].outstanding_amount, -20000)

	def test_busy_bills_with_a_dr_cr_column_and_a_named_total(self):
		frappe.local.user_date_format = "dd-mm-yyyy"
		self.addCleanup(setattr, frappe.local, "user_date_format", None)
		report = [
			"Bill-wise Outstanding (Receivable) as on 31-03-2026",
			"Party Name,Bill No.,Bill Date,Bill Amt.,Pending Amt.,Dr/Cr,Their Ref",
			f"{self.customer.customer_name},SI/1012,12-02-2026,145600,98750,Dr,PO-7",
			f"{self.customer.customer_name},,22-03-2026,,50000,Cr,",
			"Total,,,,48750,Dr,",
		]
		result = self.doc.import_opening_invoices("Customer", make_file("busy.csv", report))
		self.assertEqual(
			[(row.invoice_number, row.outstanding_amount) for row in self.doc.opening_sales_invoices],
			[("SI/1012", 98750), (None, -50000)],
		)
		self.assertEqual(result["unused_columns"], ["Bill Amt.", "Their Ref"])

	def test_tally_bills_with_dr_and_cr_columns(self):
		name = self.customer.customer_name
		report = [
			"Bills Receivable",
			"Date,Ref. No.,Party's Name,Pending Amount,,Due on",
			",,,Dr,Cr,",
			f"2026-01-10,KI/1,{name},5000,,2026-02-09",
			f"2026-03-01,On Account,{name},,800,",
			",,Grand Total,5000,800,",
		]
		result = self.doc.import_opening_invoices("Customer", make_file("tally_drcr.csv", report))
		self.assertEqual(
			[(row.invoice_number, row.outstanding_amount) for row in self.doc.opening_sales_invoices],
			[("KI/1", 5000), (None, -800)],
		)
		self.assertEqual(result["advances"], 1)

	def test_busy_bill_wise_report_grouped_by_party(self):
		name = self.customer.customer_name
		report = [
			"Outstanding Analysis (Bill-wise) - Sundry Debtors",
			"Party / Bill No.,Bill Date,Bill Amt.,Pending Amt.,Dr/Cr",
			f"M/s {name},,,,",
			'BLR-1306,25/11/2025,"2,23,430.00","2,23,430.00",Dr',
			'BLR-1336,07/02/2026,"2,57,912.00","77,373.60",Dr',
			'Total,,,"3,00,803.60",Dr',
			'Grand Total,,,"3,00,803.60",Dr',
		]
		self.doc.import_opening_invoices("Customer", make_file("busy_billwise.csv", report))
		self.assertEqual(
			[
				(row.party_name, row.invoice_number, row.outstanding_amount)
				for row in self.doc.opening_sales_invoices
			],
			[(f"M/s {name}", "BLR-1306", 223430), (f"M/s {name}", "BLR-1336", 77373.6)],
		)

	def test_a_total_in_another_column_is_not_an_invoice(self):
		report = [
			"Debtors outstanding - 31 Mar 2026",
			"Customer,Bill No,Bill Date,Amount (Rs),Their Bill No",
			f"{self.customer.customer_name},FM/311,2026-01-31,212400,PO-9",
			",,Total,212400,",
		]
		result = self.doc.import_opening_invoices("Customer", make_file("excel.csv", report))
		self.assertEqual(
			[(row.invoice_number, row.outstanding_amount) for row in self.doc.opening_sales_invoices],
			[("FM/311", 212400)],
		)
		self.assertEqual(result["unused_columns"], ["Their Bill No"])

	def test_amounts_with_a_currency_and_a_name_that_starts_with_total(self):
		report = [
			"Customer,Invoice No,Outstanding",
			'Total Environment Systems,T-1,"Rs. 1,250.00"',
			"Total,,1250",
		]
		self.doc.import_opening_invoices("Customer", make_file("rs.csv", report))
		self.assertEqual(
			[(row.party_name, row.outstanding_amount) for row in self.doc.opening_sales_invoices],
			[("Total Environment Systems", 1250)],
		)

	def test_quickbooks_open_invoices_grouped_by_customer(self):
		report = [
			"Maple Street Goods",
			"Open Invoices",
			",Date,Transaction Type,Num,Due Date,Amount,Open Balance",
			f"{self.customer.customer_name},,,,,,",
			',2026-05-12,Invoice,1042,2026-06-11,"1,250.00","1,250.00"',
			",2026-06-20,Invoice,1058,2026-07-20,845.50,600.00",
			f'Total for {self.customer.customer_name},,,,,,"$1,850.00"',
			"Quechee Gorge Village Inc.,,,,,,",
			",2026-06-18,Payment,,,,(1500.00)",
		]
		self.doc.import_opening_invoices("Customer", make_file("qbo.csv", report))
		self.assertEqual(
			[
				(row.party_name, row.invoice_number, row.outstanding_amount)
				for row in self.doc.opening_sales_invoices
			],
			[
				(self.customer.customer_name, "1042", 1250),
				(self.customer.customer_name, "1058", 600),
				("Quechee Gorge Village Inc.", None, -1500),
			],
		)

	def test_busy_columns_and_day_first_dates(self):
		# the format the site writes dates in, as Indian sites usually do
		frappe.local.user_date_format = "dd-mm-yyyy"
		self.addCleanup(setattr, frappe.local, "user_date_format", None)
		self.doc.import_opening_invoices(
			"Customer",
			make_file(
				"busy.csv",
				[
					"Party Name,Bill No.,Bill Date,Due Date,Pending Amount",
					f"{self.customer.customer_name},B-1,10-05-2026,09-06-2026,500",
				],
			),
		)
		row = self.doc.opening_sales_invoices[0]
		self.assertEqual((str(row.invoice_date), str(row.due_date)), ("2026-05-10", "2026-06-09"))
		self.assertEqual((row.invoice_number, row.outstanding_amount), ("B-1", 500))

	def test_create_posts_known_customers_and_never_adds_one(self):
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{
					"party_name": self.customer.customer_name,
					"invoice_number": "OB-SINV-0001",
					"due_date": "2026-01-01",
					"outstanding_amount": 1200,
				},
				{"party_name": "Onboarding New Customer", "outstanding_amount": 800},
			],
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		rows = self.doc.opening_sales_invoices
		self.assertEqual([row.status for row in rows], ["Created", "Failed"], [row.error for row in rows])
		self.assertEqual(rows[1].error, "Onboarding New Customer is not in ERPNext yet.")
		invoice = frappe.get_doc("Sales Invoice", "OB-SINV-0001")
		self.assertEqual(
			(invoice.is_opening, invoice.docstatus, invoice.outstanding_amount), ("Yes", 1, 1200)
		)
		# no date, and no year in the books for one: dated the day before the start
		self.assertEqual(str(invoice.posting_date), "2026-03-31")
		self.assertEqual(str(invoice.due_date), "2026-03-31")
		self.assertFalse(frappe.db.exists("Customer", {"customer_name": "Onboarding New Customer"}))
		self.assertEqual(company_setup_steps.get_temporary_opening_balance(self.company), -1200)

	def allow_adding_customers(self):
		user = frappe.session.user
		frappe.set_user("Administrator")
		make_user(user, "Sales User")
		frappe.set_user(user)

	def test_add_missing_makes_one_customer_for_a_name_on_two_rows(self):
		self.allow_adding_customers()
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": "Onboarding Twice Customer", "outstanding_amount": 100},
				{"party_name": "Onboarding Twice Customer", "outstanding_amount": 200},
			],
		)
		self.doc.create_opening_invoices("Customer", add_missing=True)
		self.doc.create_opening_invoices("Customer", add_missing=True)
		self.doc.reload()

		rows = self.doc.opening_sales_invoices
		self.assertEqual({row.status for row in rows}, {"Created"}, [row.error for row in rows])
		self.assertEqual(frappe.db.count("Customer", {"customer_name": "Onboarding Twice Customer"}), 1)
		self.assertEqual(rows[0].party, rows[1].party)

	def test_add_missing_keeps_mandatory_fields(self):
		self.allow_adding_customers()
		# like a custom app making a field mandatory, with no default to fill it
		make_property_setter("Customer", "tax_id", "reqd", 1, "Check")
		self.addCleanup(frappe.clear_cache, doctype="Customer")
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": self.customer.customer_name, "outstanding_amount": 100},
				{"party_name": "Onboarding Incomplete Customer", "outstanding_amount": 200},
			],
		)
		self.doc.create_opening_invoices("Customer", add_missing=True)
		self.doc.reload()

		known, new = self.doc.opening_sales_invoices
		self.assertEqual((known.status, new.status), ("Created", "Failed"))
		self.assertIn("Tax ID", new.error)
		self.assertFalse(frappe.db.exists("Customer", {"customer_name": "Onboarding Incomplete Customer"}))

	def test_add_missing_needs_permission_to_add_customers(self):
		frappe.set_user("Administrator")
		frappe.set_user(make_user("onboarding.no.customers@example.com", "Accounts Manager"))
		self.doc = frappe.get_doc("Company Setup", self.company)
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer", [{"party_name": "Onboarding Not Allowed Customer", "outstanding_amount": 100}]
		)
		self.assertRaises(
			frappe.PermissionError, self.doc.create_opening_invoices, "Customer", add_missing=True
		)

	def test_sheet_for_new_customers_has_the_new_names_and_mandatory_columns(self):
		self.allow_adding_customers()
		make_property_setter("Customer", "tax_id", "reqd", 1, "Check")
		self.addCleanup(frappe.clear_cache, doctype="Customer")
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": self.customer.customer_name, "outstanding_amount": 100},
				{"party_name": "Onboarding Sheet Customer", "outstanding_amount": 200},
				{"party_name": "Onboarding Sheet Customer", "outstanding_amount": 300},
			],
		)
		download_new_parties(self.company, "Customer")

		rows = list(load_workbook(BytesIO(frappe.response["filecontent"])).active.iter_rows(values_only=True))
		header = rows[0]
		self.assertEqual(header[:2], ("Customer Name", "Customer Type"))
		self.assertIn("Tax ID", header)
		self.assertIn("Mobile No (Contact)", header)
		self.assertEqual([row[0] for row in rows[1:]], ["Onboarding Sheet Customer"])

	def test_a_customer_billed_in_another_currency_is_pointed_out_and_not_posted(self):
		frappe.set_user("Administrator")
		receivable = get_account(self.company, account_type="Receivable")
		export = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "Debtors - Export",
				"company": self.company,
				"parent_account": frappe.db.get_value("Account", receivable, "parent_account"),
				"account_type": "Receivable",
				"account_currency": "USD",
			}
		).insert()
		customer = frappe.get_doc("Customer", make_customer("Onboarding Export Customer"))
		customer.default_currency = "USD"
		customer.append("accounts", {"company": self.company, "account": export.name})
		customer.save()
		frappe.set_user("onboarding.accounts@example.com")
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer", [{"party_name": "Onboarding Export Customer", "outstanding_amount": 100}]
		)
		self.assertEqual(self.doc.get_invoice_names("Customer")["other_currency"], {customer.name: "USD"})
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		row = self.doc.opening_sales_invoices[0]
		self.assertEqual(row.status, "Failed")
		self.assertIn("in USD", row.error)

	def test_a_failed_invoice_is_undone_alone(self):
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{
					"party_name": self.customer.customer_name,
					"invoice_number": "OB-DUP-1",
					"outstanding_amount": 100,
				},
				{
					"party_name": self.customer.customer_name,
					"invoice_number": "OB-DUP-1",
					"outstanding_amount": 200,
				},
			],
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		first, second = self.doc.opening_sales_invoices
		self.assertEqual((first.status, second.status), ("Created", "Failed"))
		self.assertTrue(second.error)
		self.assertNotIn("<", second.error)
		self.doc.run_method("onload")
		step = self.doc.get_onload().steps[step_key(self.doc, "opening_sales_invoices")]
		self.assertEqual(step["summary"], "1 could not be posted. Open the table to see why.")

	def test_a_save_keeps_posted_invoices(self):
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{
					"party_name": self.customer.customer_name,
					"invoice_number": "OB-KEEP-1",
					"outstanding_amount": 100,
				}
			],
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		# a client sends the form without the posted row
		self.doc.opening_sales_invoices = []
		self.doc.save()
		self.doc.reload()
		self.assertEqual(
			[(row.invoice_number, row.status) for row in self.doc.opening_sales_invoices],
			[("OB-KEEP-1", "Created")],
		)
		self.doc.migration_date = "2026-03-01"
		self.assertRaises(frappe.ValidationError, self.doc.save)

	def test_a_repeated_row_in_one_sheet_is_posted_once(self):
		row = {
			"party_name": self.customer.customer_name,
			"invoice_number": "OB-TWICE-1",
			"invoice_date": "2026-01-01",
			"outstanding_amount": 700,
		}
		result = self.doc.import_opening_invoices(
			"Customer",
			make_file(
				"twice.csv",
				[
					"Customer,Invoice No,Invoice Date,Outstanding",
					f"{row['party_name']},OB-TWICE-1,2026-01-01,700",
					f"{row['party_name']},OB-TWICE-1,2026-01-01,700",
				],
			),
		)
		self.assertEqual((result["added"], result["repeated"]), (1, 1))
		self.assertEqual(len(self.doc.opening_sales_invoices), 1)

	def test_upload_again_leaves_out_posted_invoices(self):
		sheet = make_file(
			"receivables.csv",
			[
				"Customer,Invoice No,Invoice Date,Outstanding",
				f"{self.customer.customer_name},OB-AGAIN-1,2026-02-01,1200",
				"Onboarding New Customer,OB-AGAIN-2,2026-02-03,800",
			],
		)
		self.doc.migration_date = "2026-04-01"
		self.doc.import_opening_invoices("Customer", sheet)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		result = self.doc.import_opening_invoices("Customer", sheet)
		self.assertEqual(
			result, {"added": 1, "advances": 0, "skipped": 1, "repeated": 0, "unused_columns": []}
		)
		self.assertEqual(
			[(row.invoice_number, row.status) for row in self.doc.opening_sales_invoices],
			[("OB-AGAIN-1", "Created"), ("OB-AGAIN-2", "Draft")],
		)

	def test_invoices_add_a_temporary_account_when_the_chart_has_none(self):
		frappe.set_user("Administrator")
		frappe.delete_doc("Account", get_account(self.company, account_type="Temporary"))
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer", [{"party_name": self.customer.customer_name, "outstanding_amount": 100}]
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()
		self.assertEqual(
			self.doc.opening_sales_invoices[0].status, "Created", self.doc.opening_sales_invoices[0].error
		)

	def test_supplier_bill_number_is_kept_as_bill_no(self):
		supplier = frappe.get_all("Supplier", pluck="supplier_name", limit=1)[0]
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Supplier", [{"party_name": supplier, "invoice_number": "BILL/77", "outstanding_amount": 500}]
		)
		self.doc.create_opening_invoices("Supplier")
		self.doc.reload()

		row = self.doc.opening_purchase_invoices[0]
		self.assertEqual(row.status, "Created", row.error)
		self.assertEqual(frappe.db.get_value("Purchase Invoice", row.invoice, "bill_no"), "BILL/77")

	def test_advances_become_opening_advance_entries(self):
		supplier = frappe.get_all("Supplier", pluck="supplier_name", limit=1)[0]
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": self.customer.customer_name, "outstanding_amount": 1000},
				{"party_name": self.customer.customer_name, "outstanding_amount": -300},
			],
		)
		self.doc.save_opening_invoices("Supplier", [{"party_name": supplier, "outstanding_amount": -200}])
		self.doc.create_opening_invoices("Customer")
		self.doc.create_opening_invoices("Supplier")
		self.doc.reload()

		advances = (self.doc.opening_sales_invoices[1], self.doc.opening_purchase_invoices[0])
		self.assertEqual(
			[row.status for row in advances], ["Created", "Created"], [row.error for row in advances]
		)
		for row, debit, credit in ((advances[0], 0, 300), (advances[1], 200, 0)):
			je = frappe.get_doc("Journal Entry", row.invoice)
			self.assertEqual((je.voucher_type, je.is_opening, je.docstatus), ("Opening Entry", "Yes", 1))
			party_line = next(line for line in je.accounts if line.party)
			self.assertEqual(
				(party_line.party, party_line.is_advance, party_line.debit, party_line.credit),
				(row.party, "Yes", debit, credit),
			)
		# 1000 owed by a customer, less 300 paid in advance, plus 200 paid to a supplier
		self.assertEqual(company_setup_steps.get_temporary_opening_balance(self.company), -900)

	def test_going_live_with_money_in_temporary_opening_needs_a_confirmation(self):
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer", [{"party_name": self.customer.customer_name, "outstanding_amount": 500}]
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		self.assertRaises(frappe.ValidationError, self.doc.go_live)
		self.doc.reload()
		self.assertEqual(self.doc.status, "In Progress")

		self.doc.go_live(accept_temporary_opening=True)
		self.assertEqual(self.doc.status, "Live")
		self.doc.run_method("onload")
		summary = self.doc.get_onload().steps[step_key(self.doc, "go_live")]["summary"]
		self.assertIn("still in Temporary Opening", summary)

	def test_renaming_the_company_keeps_its_setup(self):
		frappe.set_user("Administrator")
		frappe.rename_doc("Company", self.company, "_Test Renamed Onboarding Company", force=True)
		self.assertTrue(frappe.db.exists("Company Setup", "_Test Renamed Onboarding Company"))
		self.assertEqual(
			frappe.db.get_value("Company Setup", "_Test Renamed Onboarding Company", "company"),
			"_Test Renamed Onboarding Company",
		)
