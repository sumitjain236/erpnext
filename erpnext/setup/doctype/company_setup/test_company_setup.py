# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from contextlib import contextmanager
from io import BytesIO
from unittest.mock import patch

import frappe
from frappe.contacts.doctype.address.address import get_default_address
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.utils import flt, formatdate, getdate
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment

from erpnext.accounts.doctype.bank_clearance.bank_clearance import get_payment_entries_for_bank_clearance
from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_fiscal_year
from erpnext.assets.doctype.asset.depreciation import post_depreciation_entries
from erpnext.setup.doctype.company_setup import company_setup_steps
from erpnext.setup.doctype.company_setup.bank_accounts import get_statement_offs, guess_bank, guess_new_bank
from erpnext.setup.doctype.company_setup.chart_sheet import download_chart_template, get_draft_balances
from erpnext.setup.doctype.company_setup.company_setup import (
	create_company_setup,
	get_chart_import_blocker,
)
from erpnext.setup.doctype.company_setup.company_setup_steps import HOOK_NAME, get_step_paths
from erpnext.setup.doctype.company_setup.company_setup_steps import warehouses as warehouses_step
from erpnext.setup.doctype.company_setup.opening_invoices import (
	BACKGROUND_FROM,
	add_account,
	download_new_parties,
	ensure_fiscal_year,
	get_invoice_accounts,
	get_temporary_account,
)
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
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


def hidden_step(onboarding) -> None:
	"""A step of another app that doesn't apply to this company."""
	return None


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
	"taxes",
	"opening_balances",
	"bank_accounts",
	"customers",
	"opening_sales_invoices",
	"open_sales_orders",
	"suppliers",
	"opening_purchase_invoices",
	"open_purchase_orders",
	"items",
	"warehouses",
	"opening_stock",
	"fixed_assets",
	"boms",
	"open_work_orders",
	"job_workers",
	"open_subcontracting_orders",
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
	def test_onboarding_is_made_renamed_and_deleted_with_the_company(self):
		company = make_company()
		doc = frappe.get_doc("Company Setup", company)
		doc.check_steps()
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
		# every step shows, with or without a bank ledger or a trial balance
		self.assertEqual(list(step_status(doc)), [*ERPNEXT_STEPS, "go_live"])

		renamed = "_Test Renamed Onboarding Company"
		frappe.rename_doc("Company", company, renamed, force=True)
		self.assertEqual(frappe.db.get_value("Company Setup", renamed, "company"), renamed)

		frappe.delete_doc("Company", renamed)
		self.assertFalse(frappe.db.exists("Company Setup", renamed))

	def test_a_hooked_step_shows_and_a_failing_one_does_not_stop_the_setup(self):
		extra = f"{__name__}.extra_step"
		with hooked_steps({"app_one": [{"step": extra, "before": ERPNEXT_STEPS[2]}]}):
			doc = get_onboarding()
			doc.save()
		self.assertEqual(doc.steps[2].step_key, extra)
		self.assertEqual(doc.steps[2].step, "Extra step")

		# a step no app adds any more is dropped
		doc.save()
		self.assertNotIn(extra, [row.step_key for row in doc.steps])

		broken = f"{__name__}.broken_step"
		with hooked_steps({"app_one": [broken]}):
			doc = get_onboarding()
			doc.save()
			company = make_company()
		self.assertNotIn(broken, [row.step_key for row in doc.steps])
		self.assertTrue(frappe.db.exists("Company Setup", company))

	def test_a_step_that_returns_nothing_is_hidden(self):
		hidden = f"{__name__}.hidden_step"
		with hooked_steps({"app_one": [hidden]}):
			doc = get_onboarding()
			doc.run_method("onload")
		self.assertNotIn(hidden, [row.step_key for row in doc.steps])
		self.assertNotIn(hidden, doc.get_onload().steps)

	def test_progress_counts_only_needed_steps_and_go_live_lists_whats_left(self):
		doc = get_onboarding()
		sales = step_key(doc, "opening_sales_invoices")
		purchases = step_key(doc, "opening_purchase_invoices")
		doc.append("opening_purchase_invoices", {"party_name": "Y", "outstanding_amount": 10})
		doc.save()
		doc.run_method("onload")
		steps = doc.get_onload().steps
		self.assertTrue(steps[step_key(doc, "migration_date")]["needed"])
		self.assertTrue(steps[step_key(doc, "go_live")]["needed"])
		# optional: counted once started, or skipped
		self.assertFalse(steps[sales]["needed"])
		self.assertTrue(steps[purchases]["needed"])

		doc.skip_step(sales)
		doc.run_method("onload")
		self.assertTrue(doc.get_onload().steps[sales]["needed"])
		reminders = doc.get_onload().go_live_reminders
		self.assertEqual(reminders["skipped"], [sales])
		# only required steps not done; an optional one is left out even when started
		self.assertIn(step_key(doc, "migration_date"), reminders["steps"])
		self.assertNotIn(purchases, reminders["steps"])
		self.assertNotIn(step_key(doc, "go_live"), reminders["steps"])

	def test_chart_step_is_done_from_the_choice(self):
		doc = get_onboarding()
		self.assertEqual(step_status(doc)["chart_of_accounts"], "Not Started")

		doc.use_existing_chart()
		self.assertEqual(doc.chart_of_accounts_source, "Existing")
		self.assertEqual(step_status(doc)["chart_of_accounts"], "Done")

	def test_skipping_steps(self):
		doc = get_onboarding()
		for name in ("migration_date", "chart_of_accounts", "opening_balances", "check_books", "go_live"):
			self.assertRaises(frappe.ValidationError, doc.skip_step, step_key(doc, name))
			self.assertEqual(step_status(doc)[name], "Not Started")

		key = step_key(doc, "opening_sales_invoices")
		doc.skip_step(key)
		self.assertEqual(step_status(doc)["opening_sales_invoices"], "Skipped")

		# a skipped step stays skipped when the steps are checked again
		doc.check_steps()
		self.assertEqual(step_status(doc)["opening_sales_invoices"], "Skipped")

		doc.skip_step(key, skip=0)
		self.assertEqual(step_status(doc)["opening_sales_invoices"], "Not Started")

		# a done step stays done
		make_customer("_Test Setup Skip Customer")
		doc.check_steps()
		doc.skip_step(step_key(doc, "customers"))
		self.assertEqual(step_status(doc)["customers"], "Done")

	def test_steps_are_checked_after_a_save_from_elsewhere(self):
		opened = get_onboarding()
		# like a background posting saving the document while the form was open
		other = get_onboarding()
		other.migration_date = "2026-04-01"
		other.save()

		opened.check_steps()
		self.assertEqual(step_status(opened)["migration_date"], "Done")

	def test_nothing_is_changed_while_invoices_post_in_the_background(self):
		doc = get_onboarding()
		with patch("erpnext.setup.doctype.company_setup.company_setup.is_job_enqueued", return_value=True):
			self.assertRaises(
				frappe.ValidationError,
				doc.save_opening_invoices,
				"Customer",
				[{"party_name": "Onboarding Busy Customer", "outstanding_amount": 100}],
			)
			# opening the form while posting changes nothing either
			self.assertFalse(doc.check_steps())

	def test_opening_balances_done_only_when_all_posted(self):
		doc = self.posting_setup()
		cash = get_account(self.company, account_type="Cash")
		doc.save_opening_balances([{"account": cash, "debit": 100}, {"account": self.capital, "credit": 100}])
		self.assertEqual(step_status(get_setup(self.company))["opening_balances"], "Not Started")

		doc.post_opening_balances()
		self.assertEqual(step_status(get_setup(self.company))["opening_balances"], "Done")

	def test_check_books_needs_temporary_opening_at_zero_and_nothing_left_to_post(self):
		doc = self.posting_setup()
		cash = get_account(self.company, account_type="Cash")
		# a balance missing from the sheet: Temporary Opening takes the 500
		doc.save_opening_balances([{"account": cash, "debit": 500}])
		doc.post_opening_balances()
		self.assertEqual(step_status(get_setup(self.company))["check_books"], "Not Started")

		doc.save_opening_balances([{"account": self.capital, "credit": 500}])
		doc.post_opening_balances()
		self.assertEqual(step_status(get_setup(self.company))["check_books"], "Done")

		doc.reload()
		doc.save_opening_invoices("Customer", [{"party_name": "X", "outstanding_amount": 10}])
		doc.save_opening_invoices(
			"Supplier",
			[{"party_name": "Y", "outstanding_amount": 10}, {"party_name": "Z", "outstanding_amount": 10}],
		)
		doc = get_setup(self.company)
		self.assertEqual(step_status(doc)["check_books"], "Not Started")
		self.assertEqual(
			company_setup_steps.check_books(doc)["summary"], "1 invoice and 2 bills still to post"
		)

	def posting_setup(self):
		"""A new company's setup, worked on as an Accounts Manager, migrating on 01-04-2026."""
		self.company = make_company()
		self.capital = get_account(self.company, root_type="Equity", account_type=("!=", "Temporary"))
		frappe.set_user(make_user("onboarding.accounts@example.com", "Accounts Manager"))
		self.addCleanup(frappe.set_user, "Administrator")
		doc = frappe.get_doc("Company Setup", self.company)
		doc.migration_date = "2026-04-01"
		doc.save()
		return doc

	def test_go_live(self):
		doc = get_onboarding()
		doc.go_live()
		self.assertEqual(doc.status, "Live")
		self.assertEqual(step_status(doc)["go_live"], "Done")

		# with money in Temporary Opening, only on a confirmation
		doc = frappe.get_doc("Company Setup", make_company())
		doc.migration_date = "2026-04-01"
		customer = frappe.get_all("Customer", pluck="customer_name", limit=1)[0]
		doc.save_opening_invoices("Customer", [{"party_name": customer, "outstanding_amount": 500}])
		doc.create_opening_invoices("Customer")
		doc.reload()

		self.assertRaises(frappe.ValidationError, doc.go_live)
		doc.reload()
		self.assertEqual(doc.status, "In Progress")

		doc.go_live(accept_temporary_opening=True)
		self.assertEqual(doc.status, "Live")
		doc.run_method("onload")
		summary = doc.get_onload().steps[step_key(doc, "go_live")]["summary"]
		self.assertIn("still in Temporary Opening", summary)

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

	def test_erpnext_steps_and_steps_without_a_known_place(self):
		self.assertEqual(
			[path.rsplit(".", 1)[-1] for path in frappe.get_hooks(HOOK_NAME, app_name="erpnext")],
			[*ERPNEXT_STEPS, "go_live"],
		)
		self.assertEqual(step_names(), [*ERPNEXT_STEPS, "go_live"])
		# they go before Go Live
		for step in ("app_one.steps.gst", {"step": "app_one.steps.gst", "before": "no_such_step"}):
			with self.subTest(step=step):
				self.assertEqual(step_names(app_one=[step]), [*ERPNEXT_STEPS, "gst", "go_live"])

	def test_steps_go_before_the_step_they_name(self):
		self.assertEqual(
			step_names(
				app_one=[
					{"step": "app_one.steps.gst", "before": "taxes"},
					{"step": "app_one.steps.bank", "before": "check_books"},
				]
			),
			steps_with(before={"taxes": ["gst"], "check_books": ["bank"]}),
		)

		# at the same place, in install order
		self.assertEqual(
			step_names(
				app_one=[{"step": "app_one.steps.gst", "before": "check_books"}],
				app_two=[{"step": "app_two.steps.employees", "before": "check_books"}],
			),
			steps_with(before={"check_books": ["gst", "employees"]}),
		)

		# a step of an app installed before can be named too
		self.assertEqual(
			step_names(
				app_one=["app_one.steps.employees"],
				app_two=[{"step": "app_two.steps.payroll", "before": "employees"}],
			),
			[*ERPNEXT_STEPS, "payroll", "employees", "go_live"],
		)

	def test_a_step_takes_the_place_of_the_step_it_replaces(self):
		self.assertEqual(
			step_names(app_one=[{"step": "app_one.steps.gst", "replaces": "opening_balances"}]),
			[step if step != "opening_balances" else "gst" for step in [*ERPNEXT_STEPS, "go_live"]],
		)
		# with the same name, and replacing a step of the same app
		with hooked_steps(
			{
				"app_one": [{"step": "app_one.steps.customers", "replaces": "customers"}],
				"app_two": [
					{"step": "app_two.steps.employees", "before": "suppliers"},
					{"step": "app_two.steps.payroll", "replaces": "employees"},
				],
			}
		):
			paths = get_step_paths()
		self.assertEqual(
			[path.rsplit(".", 1)[-1] for path in paths], steps_with(before={"suppliers": ["payroll"]})
		)
		self.assertIn("app_one.steps.customers", paths)
		# only the first step of that name is left out, not another app's step named the same
		with hooked_steps(
			{
				"app_one": [{"step": "app_one.steps.gst", "replaces": "opening_balances"}],
				"app_two": ["app_two.steps.opening_balances"],
			}
		):
			paths = get_step_paths()
		self.assertIn("app_two.steps.opening_balances", paths)
		self.assertNotIn("erpnext.setup.doctype.company_setup.company_setup_steps.opening_balances", paths)
		# Go Live is never replaced
		self.assertEqual(
			step_names(app_one=[{"step": "app_one.steps.gst", "replaces": "go_live"}])[-2:],
			["gst", "go_live"],
		)

	def test_go_live_is_always_last(self):
		go_live = "erpnext.setup.doctype.company_setup.company_setup_steps.go_live"
		self.assertEqual(
			step_names(app_one=[go_live, "app_one.steps.gst"])[-2:],
			["gst", "go_live"],
		)
		self.assertEqual(
			step_names(app_one=[{"step": "app_one.steps.gst", "before": "go_live"}]),
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
		self.accounts_manager = make_user("onboarding.accounts@example.com", "Accounts Manager")
		frappe.set_user(self.accounts_manager)
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
		self.assertEqual(
			self.doc.preview_chart(make_sheet(sheet))["added_accounts"], ["Stock Received But Not Billed"]
		)
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

	def test_import_keeps_the_default_and_party_accounts_by_name(self):
		# a big company's chart: several receivable accounts, the usual one first
		sheet = [
			*SHEET,
			"Debtors,Current Assets,1131,1100,0,Receivable,Asset,INR,,",
			"Retention Money Receivable,Current Assets,1132,1100,0,Receivable,Asset,INR,,",
			"Debtors - Export,Current Assets,1134,1100,0,Receivable,Asset,INR,,",
		]
		self.doc.import_chart(make_sheet(sheet))
		receivable = frappe.db.get_value("Company", self.company, "default_receivable_account")
		self.assertEqual(frappe.db.get_value("Account", receivable, "account_name"), "Debtors")

		customer = make_customer("_Test Setup Export Customer")
		group = frappe.db.get_value("Customer Group", {"is_group": 0})
		set_party_account("Customer", customer, self.company, "1134 - Debtors - Export - _TOC")
		set_party_account("Customer Group", group, self.company, "1134 - Debtors - Export - _TOC")

		self.doc.import_chart(
			make_sheet([*sheet, "Debtors - Old,Current Assets,1133,1100,0,Receivable,Asset,INR,,"])
		)

		for doctype, name in (("Customer", customer), ("Customer Group", group)):
			self.assertEqual(
				frappe.get_all(
					"Party Account",
					{"parenttype": doctype, "parent": name, "company": self.company},
					pluck="account",
				),
				["1134 - Debtors - Export - _TOC"],
			)

	def test_a_foreign_currency_receivable_is_not_made_the_default(self):
		sheet = [
			*SHEET,
			"Debtors - USD,Current Assets,1135,1100,0,Receivable,Asset,USD,,",
			"Creditors - USD,Liabilities,2150,2000,0,Payable,Liability,USD,,",
		]
		self.doc.import_chart(make_sheet(sheet))
		self.assertEqual(
			frappe.db.get_value(
				"Company", self.company, ["default_receivable_account", "default_payable_account"]
			),
			("1130 - Sundry Debtors - _TOC", "2100 - Sundry Creditors - _TOC"),
		)

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

	def test_import_is_only_for_an_accounts_manager_and_before_any_entries(self):
		self.assertIsNone(get_chart_import_blocker(self.company))

		frappe.set_user(self.system_manager)
		self.assertTrue(get_chart_import_blocker(self.company))
		self.assertRaises(frappe.PermissionError, self.doc.import_chart, make_sheet(SHEET))

		frappe.set_user(self.accounts_manager)
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


def set_party_account(doctype: str, name: str, company: str, account: str):
	user = frappe.session.user
	frappe.set_user("Administrator")
	doc = frappe.get_doc(doctype, name)
	doc.set("accounts", [{"company": company, "account": account}])
	doc.save()
	frappe.set_user(user)


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


@contextmanager
def day_first_dates():
	"""Dates written day first, as Indian sites usually do."""
	frappe.local.user_date_format = "dd-mm-yyyy"
	try:
		yield
	finally:
		frappe.local.user_date_format = None


def make_file(name: str, rows: list[str]) -> str:
	return (
		frappe.get_doc({"doctype": "File", "file_name": name, "content": "\n".join(rows), "is_private": 1})
		.insert()
		.file_url
	)


class TestOpeningBalances(ERPNextTestSuite):
	"""Opening balances on the Accounts tab: everything but customers, suppliers and stock."""

	def setUp(self):
		self.enterContext(self.change_settings("Accounts Settings", {"delete_linked_ledger_entries": 1}))
		self.company = make_company()
		frappe.set_user(make_user("onboarding.accounts@example.com", "Accounts Manager"))
		self.doc = frappe.get_doc("Company Setup", self.company)
		self.cash = get_account(self.company, account_type="Cash")
		self.capital = get_account(self.company, root_type="Equity", account_type=("!=", "Temporary"))

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_undo_posting_turns_balances_back_into_drafts(self):
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_balances(
			[{"account": self.cash, "debit": 1000}, {"account": self.capital, "credit": 1000}], "tb.xlsx"
		)
		entries = self.doc.post_opening_balances()
		(group,) = self.doc.get_posted_entries("balances")["groups"]
		self.assertEqual(
			(group["source"], group["count"], [entry["name"] for entry in group["entries"]]),
			("tb.xlsx", 2, entries),
		)

		# the same sheet again leaves out what is posted: nothing counts twice
		names = [
			frappe.db.get_value("Account", account, "account_name") for account in (self.cash, self.capital)
		]
		again = self.doc.import_opening_balances(
			make_file("tb_again.csv", ["Particulars,Debit,Credit", f"{names[0]},1000,", f"{names[1]},,1000"])
		)
		self.assertEqual((again["added"], again["skipped"]), (0, 2))
		self.assertEqual(len(self.doc.opening_balances), 2)

		result = self.doc.undo_posting("balances", "tb.xlsx")
		self.doc.reload()
		self.assertEqual((result.entries, result.drafts), (len(entries), 2))
		self.assertEqual(
			{(row.status, row.journal_entry) for row in self.doc.opening_balances}, {("Draft", None)}
		)
		# undone entries are deleted, with their ledger rows
		self.assertFalse(frappe.db.exists("Journal Entry", {"name": ("in", list(entries))}))
		self.assertFalse(frappe.db.exists("GL Entry", {"voucher_no": ("in", list(entries))}))

	def test_save_replaces_drafts_nets_each_row_and_keeps_posted_rows(self):
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

		# a row with both sides keeps the difference, on one side
		self.doc.save_opening_balances(
			[{"account": self.cash, "debit": 100, "credit": 30}, {"account": self.capital, "debit": -50}]
		)
		self.assertEqual(
			[(flt(row.debit), flt(row.credit), row.status) for row in self.doc.opening_balances],
			[(10, 0, "Posted"), (70, 0, "Draft"), (0, 50, "Draft")],
		)

	def test_a_form_save_cannot_mark_a_row_posted(self):
		self.doc.save_opening_balances([{"account": self.cash, "debit": 100}])
		self.doc.opening_balances[0].status = "Posted"
		self.doc.save()
		self.assertEqual(self.doc.opening_balances[0].status, "Draft")

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

	def test_customer_supplier_stock_and_other_companies_accounts_are_refused(self):
		accounts = [
			get_account(self.company, account_type=account_type)
			for account_type in ("Receivable", "Payable", "Stock", "Temporary")
		]
		for account in [*accounts, get_account("_Test Company", account_type="Cash")]:
			with self.subTest(account=account):
				self.assertRaises(
					frappe.ValidationError,
					self.doc.save_opening_balances,
					[{"account": account, "debit": 100}],
				)

	def test_trial_balance_sheets_with_one_heading_row(self):
		cash_name = frappe.db.get_value("Account", self.cash, "account_name")
		with self.subTest("closing balances are taken"):
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
				[
					(row.original_account, row.account, row.debit, row.credit)
					for row in self.doc.opening_balances
				],
				[(cash_name, self.cash, 5000, 0), ("Old Ledger", None, 0, 300)],
			)
			self.assertEqual(result["unmatched"], ["Old Ledger"])
			self.assertEqual(result["left_out"], {"Receivable": 10000})
			self.assertEqual(result["groups"], 1)
			self.assertEqual(
				frappe.db.get_value(
					"Company Setup", self.doc.name, ["sheet_receivables", "sheet_payables", "sheet_stock"]
				),
				(10000, 0, 0),
			)
			# the debtors account's line by account, the customer's apart: it has none
			sheet = frappe.parse_json(frappe.db.get_value("Company Setup", self.doc.name, "sheet_accounts"))
			self.assertEqual(sorted(sheet["Receivable"].values()), [1000, 9000])
			self.assertEqual(sheet["Receivable"][""], 1000)

		with self.subTest("Busy, with a group column"):
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
			self.assertEqual(
				[(row.account, row.debit) for row in self.doc.opening_balances], [(self.cash, 300)]
			)
			self.assertEqual(result["left_out"], {"Stock": 1500})
			self.assertEqual(frappe.db.get_value("Company Setup", self.doc.name, "sheet_stock"), 1500)

		with self.subTest("a receivables total line is left out with customers"):
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

	def test_trial_balance_sheets_with_headings_over_rows_or_indented_names(self):
		cash_name = frappe.db.get_value("Account", self.cash, "account_name")
		capital_name = frappe.db.get_value("Account", self.capital, "account_name")
		with self.subTest("Tally, as exported"):
			# title rows, headings over a few rows, ledgers indented under groups
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

		with self.subTest("the closing columns are named in the row above"):
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

		with self.subTest("the indent is read from the name column"):
			rows = [
				("Account Number", "Account Name", "Debit", "Credit"),
				(None, "Current Assets", 300, None, 0),
				("A100", cash_name, 300, None, 2),
			]
			result = self.doc.import_opening_balances(make_xlsx_file("tb.xlsx", rows, indent_column=2))
			self.assertEqual([row.debit for row in self.doc.opening_balances], [300])
			self.assertEqual(result["groups"], 1)

	def test_an_account_of_the_chart_under_the_debtors_group_keeps_its_balance(self):
		user = frappe.session.user
		frappe.set_user("Administrator")
		receivable = get_account(self.company, account_type="Receivable")
		provision = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "Provision for Doubtful Debts",
				"company": self.company,
				"parent_account": frappe.db.get_value("Account", receivable, "parent_account"),
			}
		).insert()
		frappe.set_user(user)
		result = self.doc.import_opening_balances(
			make_file(
				"busy_tb.csv",
				[
					"Particulars,Group,Debit,Credit",
					"Onboarding Customer A,Sundry Debtors,900,",
					"Provision for Doubtful Debts,Sundry Debtors,,200",
					"Total,,900,200",
				],
			)
		)
		self.assertEqual(
			[(row.account, row.credit) for row in self.doc.opening_balances], [(provision.name, 200)]
		)
		self.assertEqual(result["left_out"], {"Receivable": 900})

	def test_downloaded_sheet_adds_up_the_rows_of_an_account(self):
		self.doc.save_opening_balances(
			[{"account": self.cash, "debit": 100}, {"account": self.cash, "debit": 50}]
		)
		self.assertEqual(get_draft_balances(self.doc), {self.cash: (150, 0)})

	def test_post_balances_against_temporary_opening(self):
		sales = get_account(self.company, root_type="Income")
		rows = [
			{"account": self.cash, "debit": 1000},
			{"account": self.capital, "credit": 600},
			{"account": sales, "credit": 100},
		]
		self.doc.save_opening_balances(rows)
		# not before the migration date is set
		self.assertRaises(frappe.ValidationError, self.doc.post_opening_balances)

		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_balances(rows)
		opening, so_far = self.doc.post_opening_balances()

		opening = frappe.get_doc("Journal Entry", opening)
		self.assertEqual((opening.is_opening, str(opening.posting_date)), ("Yes", "2026-03-31"))
		self.assertEqual(opening.total_debit, 1000)
		so_far = frappe.get_doc("Journal Entry", so_far)
		self.assertEqual(so_far.is_opening, "No")
		self.assertEqual(company_setup_steps.get_temporary_opening_balance(self.company), -400 + 100)
		self.assertEqual({row.status for row in self.doc.opening_balances}, {"Posted"})
		self.assertEqual(step_status(self.doc)["opening_balances"], "Done")

	def test_a_mandatory_dimension_needs_a_default_and_frozen_accounts_stop_the_undo(self):
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_balances(
			[{"account": self.cash, "debit": 1000}, {"account": self.capital, "credit": 1000}]
		)
		detail = frappe.get_doc(
			{
				"doctype": "Accounting Dimension Detail",
				"parent": "Location",
				"parenttype": "Accounting Dimension",
				"parentfield": "dimension_defaults",
				"company": self.company,
				"mandatory_for_bs": 1,
			}
		)
		detail.db_insert()
		with self.assertRaisesRegex(frappe.ValidationError, "has no default Location"):
			self.doc.post_opening_balances()

		detail.db_set("default_dimension", "Block 1")
		(entry,) = self.doc.post_opening_balances()
		self.assertEqual(
			set(frappe.get_all("GL Entry", {"voucher_no": entry}, pluck="location")), {"Block 1"}
		)

		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", "2026-03-31")
		with self.assertRaisesRegex(frappe.ValidationError, "can't be undone"):
			self.doc.undo_posting("balances", None)
		self.assertEqual(frappe.db.get_value("Journal Entry", entry, "docstatus"), 1)

	def test_add_account_goes_under_the_group_given(self):
		group = get_account(self.company, is_group=1, root_type="Liability", parent_account=("is", "set"))
		account = add_account(self.company, "Setup Loan", "", "Liability", parent=group)
		self.assertEqual(frappe.db.get_value("Account", account, "parent_account"), group)

		root = frappe.db.get_value(
			"Account", add_account(self.company, "Setup Loan 2", "", "Liability"), "parent_account"
		)
		self.assertFalse(frappe.db.get_value("Account", root, "parent_account"))

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
		# test sites have years up to 2050 only
		self.doc.migration_date = "2073-04-01"
		self.doc.run_method("onload")
		self.assertEqual(
			self.doc.get_onload().missing_fiscal_year, {"year": "2073", "date": getdate("2073-03-31")}
		)

		frappe.set_user("Administrator")
		self.doc.migration_date = "2072-04-01"
		self.doc.save_opening_balances([{"account": self.cash, "debit": 100}])
		# a first post that adds the year, then fails and is rolled back with the year still cached
		frappe.db.savepoint("failed_post")
		ensure_fiscal_year("2072-03-31", self.company)
		get_fiscal_year("2072-03-31", company=self.company, boolean=True)
		frappe.db.rollback(save_point="failed_post")
		self.assertFalse(has_year("2072-03-31"))

		self.doc.post_opening_balances()
		self.assertTrue(has_year("2072-03-31"))


class TestOpeningInvoices(ERPNextTestSuite):
	"""Unpaid customer and supplier invoices on the Selling and Buying tabs."""

	def setUp(self):
		self.enterContext(self.change_settings("Accounts Settings", {"delete_linked_ledger_entries": 1}))
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

	def post_two_sources(self):
		"""One invoice from a sheet and one typed in, posted; their invoice names by source."""
		self.doc.migration_date = "2026-04-01"
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
		self.doc.save_opening_invoices(
			"Customer", [row | {"invoice_number": "OB-SHEET-1", "outstanding_amount": 100}], "delhi.xlsx"
		)
		self.doc.save_opening_invoices(
			"Customer",
			[
				*[row.as_dict() for row in self.doc.opening_sales_invoices],
				row | {"invoice_number": "OB-TYPED-1", "outstanding_amount": 50},
			],
		)
		self.assertEqual([row.source for row in self.doc.opening_sales_invoices], ["delhi.xlsx", None])
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()
		return {row.source or "": row.invoice for row in self.doc.opening_sales_invoices}

	def test_undo_posting_takes_back_one_source_only(self):
		invoices = self.post_two_sources()
		posted = self.doc.get_posted_entries("Customer")
		self.assertEqual(
			sorted((group["source"], group["count"], group["amount"]) for group in posted["groups"]),
			[("", 1, 50), ("delhi.xlsx", 1, 100)],
		)

		# a row of the same sheet that isn't posted goes with it
		self.doc.save_opening_invoices(
			"Customer",
			[
				{
					"party_name": self.customer.customer_name,
					"invoice_number": "OB-SHEET-2",
					"outstanding_amount": 70,
					"source": "delhi.xlsx",
				}
			],
		)
		result = self.doc.undo_posting("Customer", "delhi.xlsx")
		self.doc.reload()
		self.assertEqual((result.removed, result.unposted, result.kept), (1, 1, 0))
		self.assertFalse(frappe.db.exists("Sales Invoice", invoices["delhi.xlsx"]))
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoices[""], "docstatus"), 1)
		self.assertEqual([row.invoice_number for row in self.doc.opening_sales_invoices], ["OB-TYPED-1"])
		# the undone invoice is kept as an undone row, with its account, to download and upload again
		self.assertEqual(
			[(row.invoice_number, row.account, row.source) for row in self.doc.undone_sales_invoices],
			[("OB-SHEET-1", get_party_account("Customer", self.customer.name, self.company), "delhi.xlsx")],
		)
		self.assertEqual(
			[
				(group["source"], group["count"])
				for group in self.doc.get_posted_entries("Customer")["undone"]
			],
			[("delhi.xlsx", 1)],
		)

		# the right sheet posts again under the same number
		self.doc.save_opening_invoices(
			"Customer",
			[
				{
					"party_name": self.customer.customer_name,
					"invoice_number": "OB-SHEET-1",
					"invoice_date": "2026-03-01",
					"outstanding_amount": 120,
				}
			],
			"delhi-fixed.xlsx",
		)
		self.doc.create_opening_invoices("Customer")
		self.assertEqual(frappe.db.get_value("Sales Invoice", "OB-SHEET-1", "docstatus"), 1)
		self.doc.reload()
		self.assertEqual(self.doc.undone_sales_invoices, [])

	def test_undo_posting_of_picked_invoices_keeps_the_rest_of_the_sheet(self):
		self.doc.migration_date = "2026-04-01"
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
		self.doc.save_opening_invoices(
			"Customer",
			[
				row | {"invoice_number": "OB-PICK-1", "outstanding_amount": 100},
				row | {"invoice_number": "OB-PICK-2", "outstanding_amount": 60},
			],
			"delhi.xlsx",
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		result = self.doc.undo_posting("Customer", "delhi.xlsx", ["OB-PICK-2"])
		self.doc.reload()
		self.assertEqual((result.removed, result.unposted), (1, 0))
		self.assertFalse(frappe.db.exists("Sales Invoice", "OB-PICK-2"))
		self.assertEqual(
			[(row.invoice_number, row.status) for row in self.doc.opening_sales_invoices],
			[("OB-PICK-1", "Created")],
		)
		self.assertEqual([row.invoice_number for row in self.doc.undone_sales_invoices], ["OB-PICK-2"])

		# a form save from a page opened before the undo can't bring back or drop undone rows
		self.doc.flags.posting = False
		self.doc.undone_sales_invoices = []
		self.doc.save()
		self.doc.reload()
		self.assertEqual([row.invoice_number for row in self.doc.undone_sales_invoices], ["OB-PICK-2"])

	def test_undo_posting_keeps_an_invoice_with_a_payment_and_is_refused_once_live(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		invoices = self.post_two_sources()
		payment = get_payment_entry("Sales Invoice", invoices["delhi.xlsx"], party_amount=40)
		payment.update({"reference_no": "OB-PAY-1", "reference_date": "2026-04-02"})
		payment.insert()
		payment.submit()

		result = self.doc.undo_posting("Customer", "delhi.xlsx")
		self.assertEqual((result.removed, result.kept), (0, 1))
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoices["delhi.xlsx"], "docstatus"), 1)

		self.doc.db_set("status", "Live")
		self.assertRaises(frappe.ValidationError, self.doc.undo_posting, "Customer", "delhi.xlsx")

	def test_a_closed_year_stops_posting_and_undo_with_a_plain_reason(self):
		invoices = self.post_two_sources()
		frappe.get_doc(
			{
				"doctype": "Period Closing Voucher",
				"name": "_Test Setup PCV",
				"company": self.company,
				"period_end_date": "2026-03-31",
				"docstatus": 1,
			}
		).db_insert()
		closed = "Last year is closed in ERPNext, so opening entries can't"
		with self.assertRaisesRegex(frappe.ValidationError, closed + " be undone"):
			self.doc.undo_posting("Customer", "delhi.xlsx")
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoices["delhi.xlsx"], "docstatus"), 1)

		self.doc.save_opening_invoices(
			"Customer",
			[
				*[row.as_dict() for row in self.doc.opening_sales_invoices],
				{"party_name": self.customer.customer_name, "outstanding_amount": 70},
			],
		)
		with self.assertRaisesRegex(frappe.ValidationError, closed + " be posted"):
			self.doc.create_opening_invoices("Customer")

	def test_opening_invoices_made_outside_are_listed_apart(self):
		self.post_two_sources()
		tool = frappe.new_doc("Opening Invoice Creation Tool")
		tool.update({"company": self.company, "invoice_type": "Sales"})
		tool.append(
			"invoices",
			{
				"party": self.customer.name,
				"posting_date": "2026-03-31",
				"due_date": "2026-03-31",
				"outstanding_amount": 70,
				"temporary_opening_account": get_account(self.company, account_type="Temporary"),
			},
		)
		tool.make_invoices()

		outside = self.doc.get_posted_entries("Customer")["outside"]
		self.assertEqual((outside["count"], outside["amount"]), (1, 70))
		self.assertEqual(outside["rows"][0].party, self.customer.name)

	def test_save_links_customers_by_name_or_as_picked(self):
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

		self.doc.save_opening_invoices(
			"Customer",
			[{"party_name": "Sheet Name Only", "party": self.customer.name, "outstanding_amount": 100}],
		)
		row = self.doc.opening_sales_invoices[0]
		self.assertEqual((row.party_name, row.party), ("Sheet Name Only", self.customer.name))
		self.assertEqual(self.doc.get_invoice_names("Customer")["new"], [])

	def test_names_almost_like_a_customer_or_each_other_are_pointed_out(self):
		with self.subTest("one letter off a customer"):
			almost = self.customer.customer_name[:-1] + "x"
			self.doc.save_opening_invoices("Customer", [{"party_name": almost, "outstanding_amount": 10}])
			self.assertEqual(
				self.doc.get_invoice_names("Customer")["suggestions"], {almost: self.customer.name}
			)

		with self.subTest("only a typo, not another name sharing a word"):
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

		with self.subTest("Indian name styles"):
			icici = make_customer("ICICI Bank Ltd")
			godrej = make_customer("Godrej & Boyce Mfg Co Ltd")
			adani = make_customer("Adani Realty Pvt Ltd")
			mahindra = make_customer("Mahindra Lifespace Developers Ltd")
			self.doc.save_opening_invoices(
				"Customer",
				[
					{"party_name": "M/s I.C.I.C.I. BANK", "outstanding_amount": 10},
					{"party_name": "Godrej and Boyce Mfg. Co. Ltd.", "outstanding_amount": 20},
					{"party_name": "M/s ADANI REALTY", "outstanding_amount": 30},
					{"party_name": "M/s MAHINDRA LIFESPACE", "outstanding_amount": 40},
				],
			)
			self.assertEqual(
				[row.party for row in self.doc.opening_sales_invoices], [icici, godrej, adani, None]
			)
			self.assertEqual(
				self.doc.get_invoice_names("Customer")["suggestions"], {"M/s MAHINDRA LIFESPACE": mahindra}
			)

		with self.subTest("two spellings of one new name"):
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

	def test_a_disabled_or_foreign_currency_customer_is_pointed_out_and_not_posted(self):
		disabled = make_customer("Onboarding Disabled Customer", disabled=1)
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer", [{"party_name": "Onboarding Disabled Customer", "outstanding_amount": 100}]
		)
		self.assertEqual(self.doc.get_invoice_names("Customer")["disabled"], [disabled])
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		row = self.doc.opening_sales_invoices[0]
		self.assertEqual((row.party, row.status, row.error), (disabled, "Draft", None))
		self.assertEqual(frappe.db.count("Customer", {"customer_name": "Onboarding Disabled Customer"}), 1)

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
		self.doc.save_opening_invoices(
			"Customer", [{"party_name": "Onboarding Export Customer", "outstanding_amount": 100}]
		)
		names = self.doc.get_invoice_names("Customer")
		self.assertEqual(names["other_currency"], {customer.name: "USD"})
		# its USD account is no account to pick, so saving the rows as shown doesn't stop on it
		self.add_receivable_account("Debtors - Domestic")
		self.assertNotIn(customer.name, self.doc.get_invoice_names("Customer")["party_accounts"])
		# nor does the books' preview count it
		self.assertEqual(self.doc.preview_opening_balances()["left_out_invoices"], 1)
		# not even on a ledger in the company's currency, as the Selling card leaves it out
		row = self.doc.opening_sales_invoices[0]
		frappe.db.set_value(row.doctype, row.name, "account", receivable)
		self.doc.reload()
		self.assertEqual(self.doc.preview_opening_balances()["left_out"]["currencies"], {"USD": 1})
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		row = self.doc.opening_sales_invoices[0]
		self.assertEqual((row.status, row.error), ("Draft", None))

	def test_invoice_after_the_migration_date_is_refused(self):
		self.doc.migration_date = "2026-04-01"
		self.assertRaises(
			frappe.ValidationError,
			self.doc.save_opening_invoices,
			"Customer",
			[{"party_name": "X", "invoice_date": "2026-04-05", "outstanding_amount": 10}],
		)

	def test_bills_sheets_from_tally_and_excel(self):
		name = self.customer.customer_name
		with self.subTest("a receivables report"):
			result = self.doc.import_opening_invoices(
				"Customer",
				make_file(
					"receivables.csv",
					[
						"Customer,Invoice No,Invoice Date,Due Date,Outstanding",
						f"{name},OB-INV-1,2026-02-01,2026-03-03,1200",
						"Onboarding New Customer,OB-INV-2,,,800",
						f"{name},,,,-300",
					],
				),
			)
			self.assertEqual(
				result,
				{
					"added": 3,
					"advances": 1,
					"skipped": 0,
					"repeated": 0,
					"unused_columns": [],
					"bad_dates": [],
					"unknown_ledgers": [],
					"post_to": None,
					"source": "receivables.csv",
				},
			)
			self.assertEqual(
				[(row.invoice_number, row.outstanding_amount) for row in self.doc.opening_sales_invoices],
				[("OB-INV-1", 1200), ("OB-INV-2", 800), (None, -300)],
			)

		with self.subTest("a tracker whose Ledger column names the account"):
			_, export = self.add_receivable_account("Debtors - Export")
			result = self.doc.import_opening_invoices(
				"Customer",
				make_file(
					"tracker.csv",
					[
						"Customer Name,Ledger,Invoice No,Outstanding",
						f"{name},Debtors - Export,OB-LDG-1,700",
						f"{name},Old Ledger,OB-LDG-2,300",
					],
				),
			)
			self.assertEqual(
				[(row.invoice_number, row.account) for row in self.doc.opening_sales_invoices],
				[("OB-LDG-1", export), ("OB-LDG-2", None)],
			)
			self.assertEqual(result["unknown_ledgers"], ["Old Ledger"])

		with self.subTest("Tally's bills report, as exported"), day_first_dates():
			report = [
				"Kapoor Interiors",
				"Office 7, Baner Road, Pune",
				"Bills Receivable",
				"1-Apr-2025 to 31-Mar-2026",
				"Date,Ref. No.,Party's Name,Pending Amount,Due on,Overdue by days",
				f'18-01-2026,KI/141,{name},"2,36,000.00 Dr",17-02-2026,42',
				f'25-03-2026,On Account,{name},"30,000.00 Cr",,',
				f",On Account,{name},12500,,",
				"Total,,,,,",
			]
			result = self.doc.import_opening_invoices("Customer", make_file("bills.csv", report))
			self.assertEqual(
				result,
				{
					"added": 3,
					"advances": 1,
					"skipped": 0,
					"repeated": 0,
					"unused_columns": ["Overdue by days"],
					"bad_dates": [],
					"unknown_ledgers": [],
					"post_to": None,
					"source": "bills.csv",
				},
			)
			self.assertEqual(
				[(row.invoice_number, row.outstanding_amount) for row in self.doc.opening_sales_invoices],
				[("KI/141", 236000), (None, -30000), (None, 12500)],
			)
			self.assertEqual(str(self.doc.opening_sales_invoices[0].invoice_date), "2026-01-18")

			self.doc.import_opening_invoices(
				"Supplier",
				make_file(
					"payables.csv",
					["Bills Payable", "Party's Name,Pending Amount", 'Sai Marble,"20,000.00 Dr"'],
				),
			)
			self.assertEqual(self.doc.opening_purchase_invoices[0].outstanding_amount, -20000)

		with self.subTest("Tally's bills with Dr and Cr columns"):
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

		with self.subTest("a total in another column is not an invoice"):
			report = [
				"Debtors outstanding - 31 Mar 2026",
				"Customer,Bill No,Bill Date,Amount (Rs),Their Bill No",
				f"{name},FM/311,2026-01-31,212400,PO-9",
				",,Total,212400,",
			]
			result = self.doc.import_opening_invoices("Customer", make_file("excel.csv", report))
			self.assertEqual(
				[(row.invoice_number, row.outstanding_amount) for row in self.doc.opening_sales_invoices],
				[("FM/311", 212400)],
			)
			self.assertEqual(result["unused_columns"], ["Their Bill No"])

		with self.subTest("amounts with a currency and a name that starts with Total"):
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

	def test_bills_sheets_from_busy_and_quickbooks(self):
		name = self.customer.customer_name
		with self.subTest("Busy, with a Dr/Cr column and a named total"), day_first_dates():
			report = [
				"Bill-wise Outstanding (Receivable) as on 31-03-2026",
				"Party Name,Bill No.,Bill Date,Bill Amt.,Pending Amt.,Dr/Cr,Their Ref",
				f"{name},SI/1012,12-02-2026,145600,98750,Dr,PO-7",
				f"{name},,22-03-2026,,50000,Cr,",
				"Total,,,,48750,Dr,",
			]
			result = self.doc.import_opening_invoices("Customer", make_file("busy.csv", report))
			self.assertEqual(
				[(row.invoice_number, row.outstanding_amount) for row in self.doc.opening_sales_invoices],
				[("SI/1012", 98750), (None, -50000)],
			)
			self.assertEqual(result["unused_columns"], ["Bill Amt.", "Their Ref"])

		with self.subTest("Busy's bill-wise report, grouped by party"):
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

		with self.subTest("Busy's columns with day-first dates"), day_first_dates():
			self.doc.import_opening_invoices(
				"Customer",
				make_file(
					"busy.csv",
					[
						"Party Name,Bill No.,Bill Date,Due Date,Pending Amount",
						f"{name},B-1,10-05-2026,09-06-2026,500",
					],
				),
			)
			row = self.doc.opening_sales_invoices[0]
			self.assertEqual((str(row.invoice_date), str(row.due_date)), ("2026-05-10", "2026-06-09"))
			self.assertEqual((row.invoice_number, row.outstanding_amount), ("B-1", 500))

		with self.subTest("QuickBooks open invoices, grouped by customer"):
			report = [
				"Maple Street Goods",
				"Open Invoices",
				",Date,Transaction Type,Num,Due Date,Amount,Open Balance",
				f"{name},,,,,,",
				',2026-05-12,Invoice,1042,2026-06-11,"1,250.00","1,250.00"',
				",2026-06-20,Invoice,1058,2026-07-20,845.50,600.00",
				f'Total for {name},,,,,,"$1,850.00"',
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
					(name, "1042", 1250),
					(name, "1058", 600),
					("Quechee Gorge Village Inc.", None, -1500),
				],
			)

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

	def test_a_missing_default_receivable_account_is_named_before_posting(self):
		# a chart whose only receivable ledger is in another currency leaves the default empty
		frappe.db.set_value("Company", self.company, "default_receivable_account", None)
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer", [{"party_name": self.customer.customer_name, "outstanding_amount": 100}]
		)
		self.assertRaisesRegex(
			frappe.ValidationError,
			"has no default receivable account in INR",
			self.doc.create_opening_invoices,
			"Customer",
		)
		self.assertFalse(frappe.db.exists("Sales Invoice", {"company": self.company}))

	def test_sheet_names_match_an_account_only_when_sure(self):
		from erpnext.setup.doctype.company_setup.company_setup import (
			get_sheet_root_type,
			match_account_name,
			suggest_account,
		)

		def account(name, root_type, **values):
			return frappe._dict(
				{
					"name": f"{name} - X",
					"account_name": name,
					"root_type": root_type,
					"is_group": 0,
					"disabled": 0,
				}
				| values
			)

		accounts = [
			account("Furniture and Fixtures", "Asset"),
			account("Salary", "Expense"),
			account("Office Rent", "Expense"),
			account("Rent Deposit", "Asset"),
			account("Cash", "Asset"),
			account("Petty Cash", "Asset"),
		]
		expense = get_sheet_root_type(["Indirect Expenses"])
		self.assertEqual(expense, "Expense")
		# the same name, tidied: used
		self.assertEqual(
			match_account_name("Furniture & Fixtures A/c", accounts), "Furniture and Fixtures - X"
		)
		# close, not the same: only suggested, and only when one account fits
		self.assertIsNone(match_account_name("Salaries", accounts, expense))
		self.assertEqual(suggest_account("Salaries", accounts, expense), "Salary - X")
		# a name inside another is suggested only within the kind the sheet says: "Rent" may be the
		# rent paid or the deposit
		self.assertEqual(suggest_account("Rent", accounts, expense), "Office Rent - X")
		self.assertIsNone(suggest_account("Rent", accounts))
		self.assertIsNone(
			suggest_account("Cash", [*accounts, frappe._dict(account("Cash", "Asset"), name="Cash 2 - X")])
		)
		# an account of another kind is never matched, even by the same name
		self.assertIsNone(match_account_name("Salary", accounts, "Income"))

	def test_old_invoices_keep_their_dates_only_when_asked(self):
		from erpnext.setup.doctype.company_setup.opening_invoices import get_posting_dates

		# test sites have years up to 2050 only
		frappe.set_user("Administrator")
		self.doc.migration_date = "2074-01-01"
		row = {"party_name": self.customer.customer_name, "outstanding_amount": 100}
		self.doc.save_opening_invoices(
			"Customer",
			[row | {"invoice_number": "OB-OLD-1", "invoice_date": "2072-05-10", "due_date": "2072-06-09"}],
		)
		dates = get_posting_dates(self.doc, "Customer")
		self.assertEqual((dates["no_year"], dates["undated"], dates["kept"]), (1, 0, 0))

		self.doc.create_opening_invoices("Customer", keep_old_dates=1)
		self.assertEqual(
			[
				str(date)
				for date in frappe.db.get_value("Sales Invoice", "OB-OLD-1", ["posting_date", "due_date"])
			],
			["2072-05-10", "2072-06-09"],
		)
		# the years between it and those ERPNext had are added too, so none is missing
		self.assertTrue(get_fiscal_year("2061-05-10", company=self.company, boolean=True))

		# not asked: the day before migration, with the due date moved there
		self.doc.reload()
		self.doc.save_opening_invoices(
			"Customer",
			[row | {"invoice_number": "OB-OLD-2", "invoice_date": "2005-05-10", "due_date": "2005-06-09"}],
		)
		self.doc.create_opening_invoices("Customer")
		self.assertEqual(
			[
				str(date)
				for date in frappe.db.get_value("Sales Invoice", "OB-OLD-2", ["posting_date", "due_date"])
			],
			["2073-12-31", "2073-12-31"],
		)
		self.assertFalse(get_fiscal_year("2005-05-10", company=self.company, boolean=True))

	def allow_adding_customers(self):
		user = frappe.session.user
		frappe.set_user("Administrator")
		make_user(user, "Sales User")
		frappe.set_user(user)

	def test_a_new_customer_gets_the_group_picked_for_it(self):
		self.allow_adding_customers()
		group = frappe.get_all("Customer Group", filters={"is_group": 0}, pluck="name", limit=1)[0]
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{
					"party_name": "Onboarding Grouped Customer",
					"outstanding_amount": 100,
					"create_party": 1,
					"party_group": group,
				}
			],
		)
		self.doc.create_opening_invoices("Customer")
		self.assertEqual(
			frappe.db.get_value(
				"Customer", {"customer_name": "Onboarding Grouped Customer"}, "customer_group"
			),
			group,
		)

	def test_only_names_confirmed_as_new_are_added_once_and_complete(self):
		self.allow_adding_customers()
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": "Onboarding Twice Customer", "outstanding_amount": 100, "create_party": 1},
				{"party_name": "Onboarding Twice Customer", "outstanding_amount": 200, "create_party": 1},
			],
		)
		self.doc.create_opening_invoices("Customer")
		# posting again from the same, older copy finds the rows posted
		self.assertRaises(frappe.ValidationError, self.doc.create_opening_invoices, "Customer")
		self.doc.reload()

		rows = self.doc.opening_sales_invoices
		self.assertEqual({row.status for row in rows}, {"Created"}, [row.error for row in rows])
		self.assertEqual(frappe.db.count("Customer", {"customer_name": "Onboarding Twice Customer"}), 1)
		self.assertEqual(rows[0].party, rows[1].party)

		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": "Onboarding Confirmed Customer", "outstanding_amount": 100, "create_party": 1},
				{"party_name": "Onboarding Unconfirmed Customer", "outstanding_amount": 200},
			],
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		rows = {row.party_name: row for row in self.doc.opening_sales_invoices}
		self.assertEqual(
			(rows["Onboarding Confirmed Customer"].status, rows["Onboarding Unconfirmed Customer"].status),
			("Created", "Failed"),
		)
		self.assertFalse(frappe.db.exists("Customer", {"customer_name": "Onboarding Unconfirmed Customer"}))

		# like a custom app making a field mandatory, with no default to fill it
		make_property_setter("Customer", "tax_id", "reqd", 1, "Check")
		self.addCleanup(frappe.clear_cache, doctype="Customer")
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": self.customer.customer_name, "outstanding_amount": 100},
				{
					"party_name": "Onboarding Incomplete Customer",
					"outstanding_amount": 200,
					"create_party": 1,
				},
			],
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		rows = {row.party_name: row for row in self.doc.opening_sales_invoices}
		known, new = rows[self.customer.customer_name], rows["Onboarding Incomplete Customer"]
		self.assertEqual((known.status, new.status), ("Created", "Failed"))
		self.assertIn("Tax ID", new.error)
		self.assertFalse(frappe.db.exists("Customer", {"customer_name": "Onboarding Incomplete Customer"}))

	def test_a_name_confirmed_as_new_needs_permission_to_add_customers(self):
		frappe.set_user("Administrator")
		frappe.set_user(make_user("onboarding.no.customers@example.com", "Accounts Manager"))
		self.doc = frappe.get_doc("Company Setup", self.company)
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[{"party_name": "Onboarding Not Allowed Customer", "outstanding_amount": 100, "create_party": 1}],
		)
		self.assertFalse(self.doc.get_invoice_names("Customer")["can_create"])
		self.assertRaises(frappe.PermissionError, self.doc.create_opening_invoices, "Customer")

	def test_a_name_posted_on_an_earlier_sheet_is_linked_on_the_next(self):
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{
					"party_name": "ONBOARDING DELHI SPELLING",
					"party": self.customer.name,
					"outstanding_amount": 100,
				}
			],
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()
		self.assertEqual(self.doc.opening_sales_invoices[0].status, "Created")

		# the next office's sheet spells the name the same way
		self.doc.save_opening_invoices(
			"Customer", [{"party_name": "Onboarding Delhi Spelling", "outstanding_amount": 200}]
		)
		self.assertEqual(self.doc.opening_sales_invoices[-1].party, self.customer.name)
		self.assertEqual(self.doc.get_invoice_names("Customer")["new"], [])

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

	def add_receivable_account(self, account_name):
		user = frappe.session.user
		frappe.set_user("Administrator")
		receivable = get_account(self.company, account_type="Receivable")
		account = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"company": self.company,
				"parent_account": frappe.db.get_value("Account", receivable, "parent_account"),
				"account_type": "Receivable",
			}
		).insert()
		frappe.set_user(user)
		return receivable, account.name

	def test_invoices_are_set_against_the_trial_balance_by_account(self):
		from erpnext.setup.doctype.company_setup.opening_invoices import get_sheet_match

		_, export = self.add_receivable_account("Debtors - Export")
		default = frappe.get_cached_value("Company", self.company, "default_receivable_account")
		row = {"party_name": self.customer.customer_name, "party": self.customer.name}
		self.doc.save_opening_invoices(
			"Customer",
			[
				row | {"invoice_number": "OB-M-1", "outstanding_amount": 300},
				row | {"invoice_number": "OB-M-2", "outstanding_amount": 150, "account": export},
			],
		)

		def lines(match):
			return {line["account"]: (line["sheet"], line["entered"]) for line in match["lines"]}

		self.doc.sheet_accounts = {"Receivable": {default: 300, export: 200}}
		self.assertEqual(
			lines(get_sheet_match(self.doc, "Customer")), {default: (300, 300), export: (200, 150)}
		)

		# an account the trial balance doesn't have
		self.doc.sheet_accounts = {"Receivable": {export: 200}}
		self.assertEqual(lines(get_sheet_match(self.doc, "Customer"))[default], (None, 300))

		# customers listed by name: only the total can be set against the invoices
		self.doc.sheet_accounts = {"Receivable": {"": 500}}
		match = get_sheet_match(self.doc, "Customer")
		self.assertEqual(
			(match["by_account"], match["sheet"], match["entered"], match["lines"]), (False, 500, 450, [])
		)

	def test_posting_fixes_only_the_account_clashes_ticked(self):
		from erpnext.setup.doctype.company_setup.opening_invoices import get_account_clashes

		_, export = self.add_receivable_account("Debtors - Export")
		default = frappe.get_cached_value("Company", self.company, "default_receivable_account")
		self.doc.migration_date = "2026-04-01"
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
		self.doc.save_opening_invoices(
			"Customer", [row | {"invoice_number": "OB-C-1", "outstanding_amount": 300}], "a.xlsx"
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()
		self.doc.db_set("sheet_accounts", frappe.as_json({"Receivable": {export: 600}}))

		# a later sheet's ledger puts the customer on Export, where the trial balance is short
		self.doc.save_opening_invoices(
			"Customer",
			[row | {"invoice_number": "OB-C-2", "outstanding_amount": 200, "account": export}],
			"b.xlsx",
		)
		(clash,) = get_account_clashes(self.doc, "Customer")
		self.assertEqual(
			(clash["kind"], clash["account"], clash["from_account"], clash["said"], clash["parties"]),
			(
				"posted",
				export,
				default,
				True,
				[{"party": self.customer.name, "sources": ["a.xlsx"], "count": 1, "amount": 300}],
			),
		)
		# a fix the server did not find is ignored
		self.doc.create_opening_invoices(
			"Customer", [{"kind": "draft", "account": default, "from_account": export}]
		)
		self.doc.reload()
		self.assertEqual(frappe.db.get_value("Sales Invoice", "OB-C-1", "docstatus"), 1)

		self.doc.save_opening_invoices(
			"Customer",
			[row | {"invoice_number": "OB-C-3", "outstanding_amount": 100, "account": export}],
			"b.xlsx",
		)
		self.doc.create_opening_invoices(
			"Customer", [{"kind": "posted", "account": export, "from_account": default}]
		)
		self.doc.reload()
		# moved: deleted and posted again on Export under its own number
		self.assertEqual(
			{
				name: frappe.db.get_value("Sales Invoice", name, ["docstatus", "debit_to"])
				for name in ("OB-C-1", "OB-C-2", "OB-C-3")
			},
			{name: (1, export) for name in ("OB-C-1", "OB-C-2", "OB-C-3")},
		)
		self.assertEqual(self.doc.undone_sales_invoices, [])

		# now this sheet is the odd one: it says the default account, the earlier invoices are on Export
		self.doc.db_set("sheet_accounts", frappe.as_json({"Receivable": {export: 650}}))
		self.doc.save_opening_invoices(
			"Customer",
			[row | {"invoice_number": "OB-C-4", "outstanding_amount": 50, "account": default}],
			"c.xlsx",
		)
		(clash,) = get_account_clashes(self.doc, "Customer")
		self.assertEqual(
			(clash["kind"], clash["account"], clash["from_account"], clash["saved"], clash["earlier"]),
			("draft", export, default, False, True),
		)
		self.doc.create_opening_invoices(
			"Customer", [{"kind": "draft", "account": export, "from_account": default}]
		)
		self.assertEqual(frappe.db.get_value("Sales Invoice", "OB-C-4", "debit_to"), export)
		self.assertEqual(get_account_clashes(frappe.get_doc("Company Setup", self.company), "Customer"), [])

	def test_an_invoice_number_another_company_used_gets_this_companys_abbreviation(self):
		from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice

		user = frappe.session.user
		frappe.set_user("Administrator")
		other = create_sales_invoice(company="_Test Company", do_not_save=1)
		other.insert(set_name="OB-SHARED-1")
		frappe.set_user(user)
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{
					"party_name": self.customer.customer_name,
					"invoice_number": "OB-SHARED-1",
					"outstanding_amount": 100,
				}
			],
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()
		abbr = frappe.get_cached_value("Company", self.company, "abbr")
		row = self.doc.opening_sales_invoices[0]
		self.assertEqual((row.status, row.invoice), ("Created", f"OB-SHARED-1-{abbr}"))

	def test_invoices_post_to_their_customers_account_and_keep_it(self):
		self.assertEqual(self.doc.get_invoice_names("Customer")["accounts"], [])
		receivable, export = self.add_receivable_account("Debtors - Export")
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{
					"party_name": self.customer.customer_name,
					"account": export,
					"invoice_number": "OB-SINV-EXP-1",
					"outstanding_amount": 500,
				},
				{"party_name": self.customer.customer_name, "account": export, "outstanding_amount": -100},
			],
		)
		names = self.doc.get_invoice_names("Customer")
		self.assertEqual(set(names["accounts"]), {receivable, export})
		self.assertEqual(names["party_accounts"], {self.customer.name: receivable})
		self.assertEqual(names["default_account"], receivable)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		rows = self.doc.opening_sales_invoices
		self.assertEqual([row.status for row in rows], ["Created"] * 2, [row.error for row in rows])
		self.assertEqual(frappe.db.get_value("Sales Invoice", "OB-SINV-EXP-1", "debit_to"), export)
		advance = frappe.get_doc("Journal Entry", rows[1].invoice)
		self.assertEqual(next(line.account for line in advance.accounts if line.party), export)
		# posting leaves the customer's own account alone: only the user or an import sets it
		self.assertEqual(get_party_account("Customer", self.customer.name, self.company), receivable)

		# only the company's receivable or payable accounts can be picked
		income = get_account(self.company, root_type="Income")
		for account in (income, get_account(COMPANY, account_type="Receivable")):
			with self.assertRaises(frappe.ValidationError):
				self.doc.save_opening_invoices(
					"Customer",
					[
						{
							"party_name": self.customer.customer_name,
							"account": account,
							"outstanding_amount": 1,
						}
					],
				)
		# an employee advance is payable too, but an asset: never a supplier's account
		for account in get_invoice_accounts(self.company, "Supplier"):
			self.assertEqual(frappe.db.get_value("Account", account, "root_type"), "Liability")
		self.assertIn(export, get_invoice_accounts(self.company, "Customer"))

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
		self.assertEqual(
			step["summary"], "1 invoice posted · 1 invoice left to post: 1 failed, fix them and post again"
		)

	def test_posted_and_repeated_invoices_are_kept_once(self):
		result = self.doc.import_opening_invoices(
			"Customer",
			make_file(
				"twice.csv",
				[
					"Customer,Invoice No,Invoice Date,Outstanding",
					f"{self.customer.customer_name},OB-TWICE-1,2026-01-01,700",
					f"{self.customer.customer_name},OB-TWICE-1,2026-01-01,700",
				],
			),
		)
		self.assertEqual((result["added"], result["repeated"]), (1, 1))
		self.assertEqual(len(self.doc.opening_sales_invoices), 1)

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
		self.doc.reload()

		# uploading a sheet again leaves out what it posted
		sheet = make_file(
			"receivables.csv",
			[
				"Customer,Invoice No,Invoice Date,Outstanding",
				f"{self.customer.customer_name},OB-AGAIN-1,2026-02-01,1200",
				"Onboarding New Customer,OB-AGAIN-2,2026-02-03,800",
			],
		)
		self.doc.import_opening_invoices("Customer", sheet)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()

		result = self.doc.import_opening_invoices("Customer", sheet)
		self.assertEqual(
			result,
			{
				"added": 1,
				"advances": 0,
				"skipped": 1,
				"repeated": 0,
				"unused_columns": [],
				"bad_dates": [],
				"unknown_ledgers": [],
				"post_to": None,
				"source": "receivables.csv",
			},
		)
		self.assertEqual(
			[(row.invoice_number, row.status) for row in self.doc.opening_sales_invoices],
			[("OB-KEEP-1", "Created"), ("OB-AGAIN-1", "Created"), ("OB-AGAIN-2", "Draft")],
		)

	def test_posting_adds_a_temporary_account_when_the_chart_has_none(self):
		# like an imported chart: no temporary account at all
		frappe.set_user("Administrator")
		frappe.delete_doc("Account", get_account(self.company, account_type="Temporary"))
		frappe.set_user("onboarding.accounts@example.com")
		self.doc.migration_date = "2026-04-01"
		self.doc.save()
		# invoices and balances each add it: undo the first so the second starts without one too
		frappe.db.savepoint("no_temporary_account")

		self.doc.save_opening_invoices(
			"Customer", [{"party_name": self.customer.customer_name, "outstanding_amount": 100}]
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()
		self.assertEqual(
			self.doc.opening_sales_invoices[0].status, "Created", self.doc.opening_sales_invoices[0].error
		)
		frappe.db.rollback(save_point="no_temporary_account")

		self.doc.reload()
		self.doc.save_opening_balances(
			[{"account": get_account(self.company, account_type="Cash"), "debit": 1000}]
		)
		self.doc.post_opening_balances()
		account = frappe.db.get_value(
			"Account", {"company": self.company, "account_type": "Temporary", "disabled": 0}
		)
		self.assertEqual(company_setup_steps.get_temporary_opening_balance(self.company), -1000)
		self.assertEqual(frappe.db.get_value("Account", account, "root_type"), "Asset")

	def test_advances_and_supplier_bills_post_as_opening_entries(self):
		supplier = frappe.get_all("Supplier", pluck="supplier_name", limit=1)[0]
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": self.customer.customer_name, "outstanding_amount": 1000},
				{"party_name": self.customer.customer_name, "outstanding_amount": -300},
			],
		)
		self.doc.save_opening_invoices(
			"Supplier",
			[
				{"party_name": supplier, "outstanding_amount": -200},
				{"party_name": supplier, "invoice_number": "BILL/77", "outstanding_amount": 500},
			],
		)
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

		# a supplier's bill number is kept as the bill's
		bill = self.doc.opening_purchase_invoices[1]
		self.assertEqual(bill.status, "Created", bill.error)
		self.assertEqual(frappe.db.get_value("Purchase Invoice", bill.invoice, "bill_no"), "BILL/77")

		# 1000 owed by a customer, less 300 paid in advance, plus 200 paid to a supplier, less 500 owed to it
		self.assertEqual(company_setup_steps.get_temporary_opening_balance(self.company), -400)


def get_balance(account: str, voucher_no: str | None = None) -> float:
	filters = {"account": account, "is_cancelled": 0}
	if voucher_no:
		filters["voucher_no"] = voucher_no
	rows = frappe.get_all("GL Entry", filters=filters, fields=["debit", "credit"])
	return flt(sum(flt(row.debit) - flt(row.credit) for row in rows), 2)


class TestBankAccounts(ERPNextTestSuite):
	"""Bank accounts on the Accounts tab: bank details, and the cheques the bank had not cleared."""

	def setUp(self):
		self.enterContext(self.change_settings("Accounts Settings", {"delete_linked_ledger_entries": 1}))
		self.company = make_company()
		self.hdfc = add_account(
			self.company,
			"HDFC Bank A/c",
			"Bank",
			"Asset",
			parent=get_account(self.company, is_group=1, account_type="Bank"),
		)
		loans = get_account(
			self.company, is_group=1, root_type="Liability", account_name="Loans (Liabilities)"
		)
		od_group = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "Bank OD A/c",
				"is_group": 1,
				"company": self.company,
				"parent_account": loans,
			}
		).insert()
		self.od = add_account(self.company, "ICICI Bank OD", "", "Liability", parent=od_group.name)
		for bank in ("_Test Setup HDFC", "_Test Setup ICICI"):
			if not frappe.db.exists("Bank", bank):
				frappe.get_doc({"doctype": "Bank", "bank_name": bank}).insert()
		frappe.set_user(make_user("onboarding.accounts@example.com", "Accounts Manager"))
		self.doc = frappe.get_doc("Company Setup", self.company)
		self.doc.migration_date = "2026-04-01"
		self.doc.save()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def allow_new_banks(self):
		user = frappe.session.user
		frappe.set_user("Administrator")
		make_user(user, "System Manager")
		frappe.set_user(user)

	def save_bank_details(self, answer: str = "Yes", draft: bool = False, od_bank: str = "_Test Setup ICICI"):
		self.doc.save_bank_accounts(
			[
				{
					"account": self.hdfc,
					"bank": "_Test Setup HDFC",
					"bank_account_no": "50200012345678",
					"statement_balance": 123000,
					"is_default": 1,
				},
				{"account": self.od, "bank": od_bank, "statement_balance": 0},
			],
			answer,
			draft,
		)

	def make_group(self, name: str, root_type: str, leaf: str):
		"""A bank loan or deposit group with an untyped ledger: not a bank account."""
		group = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": name,
				"is_group": 1,
				"company": self.company,
				"parent_account": get_account(
					self.company, is_group=1, root_type=root_type, parent_account=("is", "set")
				),
			}
		).insert()
		add_account(self.company, leaf, "", root_type, parent=group.name)

	def post_cheques(self, rows: list[dict], source: str):
		self.doc.save_uncleared_cheques(
			[row.as_dict() for row in self.doc.uncleared_cheques if row.status != "Posted"] + rows, source
		)
		self.doc.post_uncleared_cheques()
		self.doc.reload()

	def post_balances(self):
		capital = get_account(self.company, root_type="Equity", account_type=("!=", "Temporary"))
		self.doc.save_opening_balances(
			[
				{"account": self.hdfc, "debit": 100000},
				{"account": self.od, "credit": 50000},
				{"account": capital, "credit": 50000},
			]
		)
		self.doc.post_opening_balances()

	def test_bank_details_make_the_banks_bank_accounts_and_default(self):
		key = step_key(self.doc, "bank_accounts")
		self.make_group("Bank Loans", "Liability", "HDFC Term Loan")
		self.make_group("Bank Deposits", "Asset", "SBI Fixed Deposit")
		self.assertEqual(self.doc.get_bank_accounts()["unmarked"], [self.od])
		self.assertEqual(self.doc.mark_bank_accounts(), [self.od])
		# an overdraft stays a liability under its group
		self.assertEqual(
			frappe.db.get_value("Account", self.od, ["account_type", "root_type"]), ("Bank", "Liability")
		)
		self.doc.skip_step(key)
		self.assertEqual(step_status(self.doc)["bank_accounts"], "Skipped")
		self.doc.skip_step(key, skip=0)

		# a draft makes nothing, but the step has rows now, so it can't be skipped
		self.save_bank_details(draft=True)
		self.assertFalse(frappe.db.exists("Bank Account", {"account": self.hdfc}))
		self.assertRaisesRegex(frappe.ValidationError, "can't be skipped", self.doc.skip_step, key)
		self.assertRaisesRegex(frappe.ValidationError, "Say whether", self.save_bank_details, "")
		# only a System Manager can make a Bank
		self.assertRaises(frappe.PermissionError, self.save_bank_details, "No", od_bank="_Test Setup Kotak")
		self.assertFalse(frappe.db.exists("Bank", "_Test Setup Kotak"))
		self.allow_new_banks()
		self.save_bank_details("No", od_bank="_Test Setup Kotak")
		self.assertTrue(frappe.db.exists("Bank Account", {"account": self.od, "bank": "_Test Setup Kotak"}))

		self.save_bank_details("No")
		hdfc = frappe.get_doc("Bank Account", {"account": self.hdfc})
		self.assertEqual(
			(hdfc.bank, hdfc.bank_account_no, hdfc.is_company_account, hdfc.is_default),
			("_Test Setup HDFC", "50200012345678", 1, 1),
		)
		od = frappe.db.get_value("Bank Account", {"account": self.od}, ["name", "bank"], as_dict=True)
		self.assertEqual(od.bank, "_Test Setup ICICI")
		self.assertEqual(frappe.db.get_value("Company", self.company, "default_bank_account"), self.hdfc)
		statements = {
			row.bank_account: row.balance
			for row in frappe.get_all(
				"Bank Account Balance", filters={"date": "2026-03-31"}, fields=["bank_account", "balance"]
			)
		}
		# a statement balance of zero is kept too
		self.assertEqual((statements.get(hdfc.name), statements.get(od.name)), (123000, 0))
		self.assertEqual(
			frappe.db.get_value(
				"Mode of Payment Account", {"parent": "Cheque", "company": self.company}, "default_account"
			),
			self.hdfc,
		)
		self.assertEqual(step_status(self.doc)["bank_accounts"], "Done")
		self.doc.run_method("onload")
		self.assertEqual(
			self.doc.get_onload().steps[key]["summary"], "2 bank accounts · No uncleared cheques"
		)
		self.assertEqual(
			guess_bank("State Bank of India A/c", ["Bank of India", "State Bank of India"]),
			"State Bank of India",
		)

	def test_an_overdraft_statement_typed_as_the_bank_shows_it_is_overdrawn(self):
		self.doc.mark_bank_accounts()
		self.doc.save_opening_balances([{"account": self.od, "credit": 50000}])
		self.doc.save_bank_accounts(
			[
				{"account": self.hdfc, "bank": "_Test Setup HDFC", "statement_balance": 123000},
				{"account": self.od, "bank": "_Test Setup ICICI", "statement_balance": 50000},
			],
			"No",
			True,
		)
		statements = {row.account: row.statement_balance for row in self.doc.bank_accounts}
		self.assertEqual((statements[self.hdfc], statements[self.od]), (123000, -50000))
		self.assertNotIn(self.od, get_statement_offs(self.doc))

	def test_uncleared_cheques_net_to_zero_and_undo_amends_the_bank_entry(self):
		self.doc.mark_bank_accounts()
		self.save_bank_details()
		cheques = [
			{
				"bank_account": self.hdfc,
				"entry_type": "Issued",
				"reference_no": "001",
				"reference_date": "2026-03-20",
				"party_name": "Velan Packaging",
				"amount": 30000,
			},
			{
				"bank_account": self.hdfc,
				"entry_type": "Issued",
				"reference_no": "002",
				"reference_date": "2026-03-28",
				"amount": 5000,
			},
			{
				"bank_account": self.hdfc,
				"entry_type": "Deposited",
				"reference_date": "2026-03-28",
				"amount": 12000,
			},
			{"bank_account": self.od, "entry_type": "Issued", "reference_no": "118803", "amount": 7000},
		]
		self.doc.save_uncleared_cheques(cheques, "uncleared.xlsx")
		self.assertRaisesRegex(
			frappe.ValidationError, "Post opening balances first", self.doc.post_uncleared_cheques
		)
		self.post_balances()
		temporary = get_account(self.company, account_type="Temporary")
		self.assertRaisesRegex(
			frappe.ValidationError,
			"Date missing for 1 row of ICICI Bank OD",
			self.doc.post_uncleared_cheques,
		)
		cheques[3]["reference_date"] = "2026-03-29"
		self.doc.save_uncleared_cheques(cheques, "uncleared.xlsx")
		checks = self.doc.get_uncleared_cheques()
		hdfc = next(bank for bank in checks["banks"] if bank["account"] == self.hdfc)
		# 1,00,000 in the books + 35,000 issued - 12,000 deposited is what the bank says
		self.assertEqual((hdfc["worked_out"], hdfc["statement"], hdfc["off"]), (123000, 123000, 0))

		self.assertEqual(self.doc.post_uncleared_cheques(), "done")
		self.doc.reload()
		rows = {row.reference_no or row.entry_type: row for row in self.doc.uncleared_cheques}
		cheque = frappe.get_doc("Journal Entry", rows["001"].journal_entry)
		self.assertEqual(
			(cheque.voucher_type, cheque.is_opening, str(cheque.posting_date), cheque.cheque_no),
			("Bank Entry", "No", "2026-03-31", "001"),
		)
		self.assertEqual(
			(get_balance(self.hdfc, cheque.name), get_balance(temporary, cheque.name)), (-30000, 30000)
		)
		self.assertTrue(frappe.db.get_value("Journal Entry", rows["Deposited"].journal_entry, "cheque_no"))
		opening = rows["001"].opening_entry
		self.assertEqual(frappe.db.get_value("Journal Entry", opening, "is_opening"), "Yes")
		self.assertEqual((get_balance(self.hdfc, opening), get_balance(temporary, opening)), (23000, -23000))
		self.assertEqual(get_balance(self.od, rows["118803"].opening_entry), 7000)
		# the bank balances and Temporary Opening stay as posted with the opening balances
		self.assertEqual(
			(get_balance(self.hdfc), get_balance(self.od), get_balance(temporary)), (100000, -50000, 0)
		)
		self.assertEqual(step_status(self.doc)["bank_accounts"], "Done")
		self.doc.run_method("onload")
		self.assertEqual(self.doc.get_onload().bank_match["matched"], 2)
		self.assertEqual(
			self.doc.get_onload().steps[step_key(self.doc, "bank_accounts")]["summary"],
			"2 bank accounts · 3 cheques and 1 deposit posted. When the bank clears them, mark them in"
			f" Bank Clearance, from {formatdate('2026-03-31')} on.",
		)

		# Bank Clearance from the day before lists each cheque, never the opening entry
		listed = {
			entry.payment_entry
			for entry in get_payment_entries_for_bank_clearance(
				"2026-03-31", "2026-04-30", self.hdfc, None, False, False
			)
		}
		self.assertEqual(listed, {rows[key].journal_entry for key in ("001", "002", "Deposited")})

		self.assertRaisesRegex(
			frappe.ValidationError, "Undo uncleared cheques first", self.doc.undo_posting, "balances", None
		)
		self.assertRaisesRegex(
			frappe.ValidationError, "Pick the cheques", self.doc.undo_uncleared_cheques, "uncleared.xlsx", []
		)
		frappe.db.set_value("Journal Entry", rows["002"].journal_entry, "clearance_date", "2026-04-04")
		picked = [rows["001"].journal_entry, rows["002"].journal_entry]
		self.assertRaisesRegex(
			frappe.ValidationError,
			"1 cheque is already cleared",
			self.doc.undo_uncleared_cheques,
			"uncleared.xlsx",
			picked,
		)

		self.doc.undo_uncleared_cheques("uncleared.xlsx", picked[:1])
		self.doc.reload()
		# the undone cheque's entry and the bank's old opening entry are deleted, a new one made
		self.assertFalse(frappe.db.exists("Journal Entry", rows["001"].journal_entry))
		self.assertFalse(frappe.db.exists("Journal Entry", opening))
		amended = next(row.opening_entry for row in self.doc.uncleared_cheques if row.reference_no == "002")
		# 5,000 issued less 12,000 deposited: the bank is credited 7,000 now
		self.assertEqual(get_balance(self.hdfc, amended), -7000)
		self.assertEqual((get_balance(self.hdfc), get_balance(temporary)), (100000, 0))
		self.assertEqual([row.reference_no for row in self.doc.undone_cheques], ["001"])
		# the same sheet again brings back only the undone cheque, not the one dated before posting
		cheques[3]["reference_date"] = None
		self.assertEqual(self.doc.save_uncleared_cheques(cheques, "uncleared.xlsx"), {"skipped": 3})
		self.assertEqual(
			[row.reference_no for row in self.doc.uncleared_cheques if row.status != "Posted"], ["001"]
		)
		self.doc.save_uncleared_cheques([], "uncleared.xlsx")
		(group,) = self.doc.get_posted_entries("cheques")["groups"]
		self.assertEqual((group["count"], group["cheques"], group["deposits"]), (3, 2, 1))

		frappe.db.set_value("Journal Entry", rows["002"].journal_entry, "clearance_date", None)
		self.doc.undo_uncleared_cheques("uncleared.xlsx")
		self.doc.reload()
		self.assertFalse(self.doc.uncleared_cheques)
		self.assertEqual(
			(get_balance(self.hdfc), get_balance(self.od), get_balance(temporary)), (100000, -50000, 0)
		)

	def test_cheque_sheets_with_a_bank_column_or_for_one_bank(self):
		self.doc.mark_bank_accounts()
		result = self.doc.import_uncleared_cheques(
			make_file(
				"uncleared.csv",
				[
					"Bank Account,Issued/Deposited,Cheque or Reference No,Date,Party,Amount",
					"HDFC Bank A/c,Issued,004512,2026-03-26,Lakshmi Spinning Mills,85000",
					"HDFC Bank A/c,Deposited,,2026-03-28,Ramesh Garments,120000",
					"Kotak Current,Issued,9,2026-03-28,,100",
					"HDFC Bank A/c,,77,2026-03-26,Velan Packaging,500",
				],
			)
		)
		self.assertEqual((result["added"], result["unknown_banks"]), (3, ["Kotak Current"]))
		# a blank Issued/Deposited is never guessed: posting waits for it
		self.assertFalse(self.doc.uncleared_cheques[2].entry_type)
		self.assertEqual(self.doc.get_uncleared_cheques()["missing_type"], 1)
		self.doc.save_uncleared_cheques(
			[row.as_dict() for row in self.doc.uncleared_cheques if row.entry_type], "uncleared.csv"
		)
		# Tally's bank reconciliation: money into the bank is a deposit
		result = self.doc.import_uncleared_cheques(
			make_file(
				"brs.csv",
				[
					"Date,Particulars,Vch Type,Instrument No,Instrument Date,Debit,Credit",
					"2026-03-29,Coimbatore Dyeing Works,Payment,118803,2026-03-29,,96500",
					",Total,,,,,96500",
				],
			),
			self.od,
		)
		self.assertEqual(result["added"], 1)
		self.assertEqual(
			[
				(
					row.bank_account,
					row.entry_type,
					row.reference_no,
					str(row.reference_date),
					row.party_name,
					row.amount,
				)
				for row in self.doc.uncleared_cheques
			],
			[
				(self.hdfc, "Issued", "004512", "2026-03-26", "Lakshmi Spinning Mills", 85000),
				(self.hdfc, "Deposited", "", "2026-03-28", "Ramesh Garments", 120000),
				(self.od, "Issued", "118803", "2026-03-29", "Coimbatore Dyeing Works", 96500),
			],
		)

	def test_two_posts_a_zero_net_bank_and_undo_of_the_first(self):
		self.doc.mark_bank_accounts()
		self.save_bank_details()
		self.post_balances()
		temporary = get_account(self.company, account_type="Temporary")
		# issued and deposited cancel out: no opening entry is needed
		self.post_cheques(
			[
				{
					"bank_account": self.hdfc,
					"entry_type": "Issued",
					"reference_no": "A1",
					"reference_date": "2026-03-20",
					"amount": 10000,
				},
				{
					"bank_account": self.hdfc,
					"entry_type": "Deposited",
					"reference_no": "D1",
					"reference_date": "2026-03-21",
					"amount": 10000,
				},
			],
			"first.xlsx",
		)
		self.assertEqual({row.opening_entry for row in self.doc.uncleared_cheques}, {None})
		self.post_cheques(
			[
				{
					"bank_account": self.hdfc,
					"entry_type": "Issued",
					"reference_no": "B1",
					"reference_date": "2026-03-25",
					"amount": 4000,
				}
			],
			"second.xlsx",
		)
		self.assertEqual((get_balance(self.hdfc), get_balance(temporary)), (100000, 0))

		# a closed Accounting Period stops the undo with a plain reason
		frappe.set_user("Administrator")
		period = frappe.get_doc(
			{
				"doctype": "Accounting Period",
				"period_name": "_Test Setup March",
				"company": self.company,
				"start_date": "2026-03-01",
				"end_date": "2026-03-31",
				"closed_documents": [{"document_type": "Journal Entry", "closed": 1}],
			}
		).insert()
		frappe.set_user("onboarding.accounts@example.com")
		deposit = next(row.journal_entry for row in self.doc.uncleared_cheques if row.reference_no == "D1")
		self.assertRaisesRegex(
			frappe.ValidationError,
			"Accounting Period .* is closed for Journal Entry, so these entries can't be undone",
			self.doc.undo_uncleared_cheques,
			"first.xlsx",
			[deposit],
		)
		frappe.set_user("Administrator")
		period.delete()
		frappe.set_user("onboarding.accounts@example.com")

		# undo the deposit of the first post: the issued cheques that stay are added back
		self.doc.undo_uncleared_cheques("first.xlsx", [deposit])
		self.doc.reload()
		self.assertEqual((get_balance(self.hdfc), get_balance(temporary)), (100000, 0))
		(opening,) = {row.opening_entry for row in self.doc.uncleared_cheques}
		self.assertEqual(get_balance(self.hdfc, opening), 14000)

		self.doc.undo_uncleared_cheques("first.xlsx")
		self.doc.reload()
		self.assertEqual([row.reference_no for row in self.doc.uncleared_cheques], ["B1"])
		self.assertEqual((get_balance(self.hdfc), get_balance(temporary)), (100000, 0))
		self.assertEqual(get_balance(self.hdfc, self.doc.uncleared_cheques[0].opening_entry), 4000)

		# Check your books is not done while a bank is off its trial balance line
		self.doc.check_steps()
		self.assertEqual(step_status(self.doc)["check_books"], "Done")
		capital = get_account(self.company, root_type="Equity", account_type=("!=", "Temporary"))
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": self.company,
				"posting_date": "2026-03-30",
				"accounts": [
					{"account": self.hdfc, "debit_in_account_currency": 5000},
					{"account": capital, "credit_in_account_currency": 5000},
				],
			}
		).insert()
		je.submit()
		# as the form opens again
		doc = frappe.get_doc("Company Setup", self.company)
		doc.check_steps()
		self.assertEqual(step_status(doc)["check_books"], "Not Started")

	def test_many_cheques_post_and_undo_in_the_background(self):
		self.doc.mark_bank_accounts()
		self.save_bank_details()
		self.post_balances()
		temporary = get_account(self.company, account_type="Temporary")
		rows = [
			{
				"bank_account": self.hdfc,
				"entry_type": "Issued" if number % 2 else "Deposited",
				"reference_no": f"C{number}",
				"reference_date": "2026-03-30",
				"amount": 100 + number,
			}
			for number in range(BACKGROUND_FROM)
		]
		self.doc.save_uncleared_cheques(rows, "many.xlsx")
		self.assertEqual(self.doc.post_uncleared_cheques(), "queued")
		self.doc.reload()
		self.assertEqual({row.status for row in self.doc.uncleared_cheques}, {"Posted"})
		self.assertEqual((get_balance(self.hdfc), get_balance(temporary)), (100000, 0))

		# the job commits its work; the test keeps it to roll back
		with patch.object(frappe.db, "commit"):
			self.assertEqual(self.doc.undo_uncleared_cheques("many.xlsx"), "queued")
		self.doc.reload()
		self.assertFalse(self.doc.uncleared_cheques)
		self.assertEqual(len(self.doc.undone_cheques), BACKGROUND_FROM)
		self.assertEqual((get_balance(self.hdfc), get_balance(temporary)), (100000, 0))


class TestEveryTab(ERPNextTestSuite):
	"""Every tab and step shows; a step the trial balance has nothing for says so and is optional."""

	def setUp(self):
		self.company = make_company()
		frappe.set_user(make_user("onboarding.accounts@example.com", "Accounts Manager"))
		self.doc = frappe.get_doc("Company Setup", self.company)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def get_steps(self) -> dict:
		doc = get_setup(self.company)
		doc.run_method("onload")
		steps = doc.get_onload().steps
		return {key.rsplit(".", 1)[-1]: step for key, step in steps.items()}

	def test_steps_with_nothing_in_the_trial_balance_show_and_are_optional(self):
		# before the trial balance is in, nothing is called not needed yet
		steps = self.get_steps()
		waiting = "Optional · we'll tell you once your trial balance is in"
		self.assertEqual(
			{
				name: steps[name]["summary"]
				for name in ("items", "opening_stock", "fixed_assets", "bank_accounts")
			},
			dict.fromkeys(("items", "opening_stock", "fixed_assets", "bank_accounts"), waiting),
		)

		self.doc.reload()
		self.doc.append(
			"opening_balances", {"account": get_account(self.company, account_type="Cash"), "debit": 10}
		)
		self.doc.save()
		steps = self.get_steps()
		self.assertEqual(
			{
				name: (steps[name]["summary"], bool(steps[name].get("required")))
				for name in ("opening_stock", "fixed_assets", "bank_accounts")
			},
			{
				"opening_stock": (
					"Not needed · no stock in your opening balances · add stock if you keep any",
					False,
				),
				"fixed_assets": (
					"Optional · no fixed assets in your opening balances · add the asset list if you have any",
					False,
				),
				"bank_accounts": (
					"No bank lines in your opening balances · add bank details if you have a bank account",
					False,
				),
			},
		)
		self.assertFalse(
			any(steps[name].get("required") for name in ("items", "warehouses", "boms", "job_workers"))
		)

		# a stock line makes Items, Warehouses and Opening stock needed
		stock = get_account(self.company, account_type="Stock")
		self.doc.reload()
		self.doc.sheet_accounts = {"Stock": {stock: 1000}}
		self.doc.save()
		steps = self.get_steps()
		self.assertTrue(all(steps[name]["required"] for name in ("items", "warehouses", "opening_stock")))


def make_stock_item(code: str, uom: str, conversions: dict | None = None) -> str:
	if not frappe.db.exists("Item", code):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": code,
				"item_group": "All Item Groups",
				"stock_uom": uom,
				"is_stock_item": 1,
				"uoms": [{"uom": uom, "conversion_factor": 1}]
				+ [
					{"uom": other, "conversion_factor": factor}
					for other, factor in (conversions or {}).items()
				],
			}
		).insert()
	return code


class TestOpeningStock(ERPNextTestSuite):
	"""Items, Warehouses and Opening stock on the Stock tab."""

	def setUp(self):
		self.enterContext(self.change_settings("Accounts Settings", {"delete_linked_ledger_entries": 1}))
		self.company = make_company()
		frappe.db.set_value("Company", self.company, "enable_perpetual_inventory", 1)
		self.yarn = make_stock_item("_Test Setup Yarn", "Kg")
		self.vest = make_stock_item("_Test Setup Vest", "Nos", {"Box": 12})
		self.stock_in_hand = get_account(self.company, account_type="Stock")
		self.job_line = add_account(
			self.company,
			"Stock with Job Workers",
			"Stock",
			"Asset",
			parent=frappe.db.get_value("Account", self.stock_in_hand, "parent_account"),
		)
		self.stores = frappe.db.get_value("Warehouse", {"company": self.company, "warehouse_name": "Stores"})
		frappe.db.set_value("Warehouse", self.stores, "account", self.stock_in_hand)
		user = make_user("onboarding.stock@example.com", "Accounts Manager")
		for role in ("Stock Manager", "Purchase Master Manager", "Item Manager"):
			make_user(user, role)
		frappe.set_user(user)
		self.doc = frappe.get_doc("Company Setup", self.company)
		self.doc.migration_date = "2026-04-01"
		self.doc.sheet_accounts = {"Stock": {self.stock_in_hand: 41000, self.job_line: 5000}}
		self.doc.save()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.enable_perpetual_inventory = {}
		super().tearDown()

	def godown_summary(self) -> str:
		"""A Tally Godown Summary: items indented under their stock group, under the godown."""
		return make_xlsx_file(
			"godown_summary.xlsx",
			[
				("Kaveri Textiles", None, None, None),
				("Godown Summary", None, None, None),
				("Particulars", "Closing Balance", None, None),
				(None, "Quantity", "Rate", "Value"),
				("Stores", None, None, None, 0),
				("Yarn", None, None, None, 1),
				("_Test Setup Yarn", "100 Kg", "285.00", 28500, 2),
				("Garments", None, None, None, 1),
				("_Test Setup Vest", "10 Box", "1200", 12000, 2),
				("_Test Setup Spare", "2.5 Nos", "200", 500, 2),
				("Grand Total", None, None, 41000, 0),
			],
			indent_column=1,
		)

	def job_worker_sheet(self, challan: bool = True) -> str:
		return make_xlsx_file(
			"job_worker_stock.xlsx",
			[
				("Item", "Warehouse", "Qty", "Unit", "Value", "Challan no", "Challan date", "Return due"),
				(
					"_Test Setup Yarn",
					"Velan Job Works",
					20,
					"Kg",
					5000,
					"VJ/1" if challan else None,
					"2026-02-12",
					"2027-02-12",
				),
			],
		)

	def checks(self) -> dict:
		self.doc.reload()
		return self.doc.get_opening_stock()

	def test_warehouses_are_done_once_reviewed(self):
		self.assertNotEqual(step_status(get_setup(self.company))["warehouses"], "Done")
		self.assertTrue(warehouses_step(self.doc)["summary"].endswith("· review them"))
		self.doc.save_warehouses(self.doc.get_warehouses()["rows"])
		self.assertEqual(step_status(get_setup(self.company))["warehouses"], "Done")

	def test_opening_stock_posts_per_warehouse_against_temporary_opening(self):
		temporary = get_temporary_account(self.company, create=True)
		self.assertIn("opening_stock", step_status(get_setup(self.company)))
		result = self.doc.import_opening_stock(self.godown_summary())
		self.assertEqual((result["read"], result["added"]), (3, 3))
		rows = {row.item_name: row for row in self.doc.opening_stock}
		self.assertEqual(
			(rows[self.yarn].warehouse, rows[self.yarn].qty, rows[self.yarn].uom, rows[self.yarn].amount),
			(self.stores, 100, "Kg", 28500),
		)
		self.assertEqual(rows["_Test Setup Vest"].stock_group, "Garments")
		checks = self.checks()
		self.assertEqual(checks["reds"][0]["key"], "missing_items")
		self.assertEqual(checks["blocker"], "1 item is not in ERPNext. Import it in Items first.")

		make_stock_item("_Test Setup Spare", "Nos")
		self.assertEqual(self.checks()["blocker"], "1 row has a part quantity for a whole-number unit (Nos).")
		self.doc.save_opening_stock(
			[
				row.as_dict() | ({"qty": 2, "amount": 400} if row.item_name == "_Test Setup Spare" else {})
				for row in self.doc.opening_stock
			]
		)

		# the job worker's warehouse is not in ERPNext yet, nor the job worker
		self.doc.import_opening_stock(self.job_worker_sheet(challan=False))
		self.assertEqual(
			self.checks()["blocker"], "1 warehouse is not in ERPNext. Add it in Warehouses first."
		)
		window = self.doc.get_warehouses()
		new = next(row for row in window["rows"] if not row["warehouse"])
		self.assertEqual(
			(new["warehouse_name"], new["kind"], new["job_worker"], new["account"], new["new_job_worker"]),
			("Velan Job Works", "Job worker", "Velan Job Works", self.job_line, True),
		)
		self.doc.save_warehouses(window["rows"])
		self.assertTrue(frappe.db.exists("Supplier", "Velan Job Works"))
		velan = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Velan Job Works"}
		)
		self.assertEqual(frappe.db.get_value("Warehouse", velan, "account"), self.job_line)
		self.assertEqual(self.checks()["blocker"], "1 job worker row needs challan details.")
		self.doc.save_opening_stock(
			[
				row.as_dict() | ({"challan_no": "VJ/1"} if row.challan_date else {})
				for row in self.doc.opening_stock
			]
		)
		self.assertIsNone(self.checks()["blocker"])
		self.assertEqual(self.checks()["off"], 100)

		self.doc.post_opening_stock()
		self.doc.reload()
		self.assertEqual({row.status for row in self.doc.opening_stock}, {"Posted"})
		# one reconciliation per upload and warehouse; a box is 12 vests, the value kept
		entries = {row.source: row.stock_entry for row in self.doc.opening_stock}
		self.assertEqual(len(set(entries.values())), 2)
		vest = frappe.db.get_value(
			"Stock Ledger Entry",
			{"item_code": self.vest, "warehouse": self.stores, "is_cancelled": 0},
			["qty_after_transaction", "stock_value_difference", "posting_date"],
			as_dict=True,
		)
		self.assertEqual(
			(vest.qty_after_transaction, vest.stock_value_difference, str(vest.posting_date)),
			(120, 12000, "2026-03-31"),
		)
		self.assertEqual(
			(get_balance(self.stock_in_hand), get_balance(self.job_line), get_balance(temporary)),
			(40900, 5000, -45900),
		)
		self.assertEqual(step_status(get_setup(self.company))["opening_stock"], "Not Started")

		# posting the same rows again leaves them out
		result = self.doc.import_opening_stock(self.job_worker_sheet())
		self.assertEqual((result["skipped"], result["differ"]), (1, 0))

		# the stock line is off by ₹100: post the difference, with the capital
		self.doc.reload()
		self.doc.run_method("onload")
		self.assertEqual(
			self.doc.get_onload().temporary_parts,
			[{"label": "stock difference", "amount": 100, "to_come": False, "over": False}],
		)
		self.assertEqual([line["difference"] for line in self.doc.get_stock_difference()["lines"]], [100])
		entry = self.doc.post_stock_difference()
		difference = get_account(self.company, account_name="Opening Stock Difference")
		self.assertEqual(frappe.db.get_value("Account", difference, "report_type"), "Balance Sheet")
		self.assertEqual((get_balance(difference, entry), get_balance(temporary)), (100, -46000))
		doc = get_setup(self.company)
		self.assertEqual(step_status(doc)["opening_stock"], "Done")
		doc.run_method("onload")
		self.assertTrue(doc.get_onload().stock_match["matches"])

		# the job worker's warehouse has no later entries: it is undone, the difference is on another line
		self.doc.reload()
		self.doc.undo_opening_stock("job_worker_stock.xlsx", [velan])
		self.assertEqual(frappe.db.get_value("Journal Entry", entry, "docstatus"), 1)
		self.assertEqual((get_balance(self.job_line), get_balance(temporary)), (0, -41000))
		self.doc.reload()
		self.assertEqual(len(self.doc.undone_stock), 1)

		# a later issue from Stores: the godown can't be undone, its rows are corrected instead
		frappe.set_user("Administrator")
		issue = make_stock_entry(
			item_code=self.yarn,
			qty=10,
			from_warehouse=self.stores,
			company=self.company,
			posting_date="2026-04-10",
		)
		frappe.set_user("onboarding.stock@example.com")
		self.doc.reload()
		self.assertRaisesRegex(
			frappe.ValidationError, "can not be undone", self.doc.undo_opening_stock, "godown_summary.xlsx"
		)
		yarn = next(row for row in self.doc.opening_stock if row.item_code == self.yarn)
		self.assertRaisesRegex(
			frappe.ValidationError,
			"would go below zero",
			self.doc.correct_opening_stock,
			[{"name": yarn.name, "qty": 5, "amount": 1425}],
		)
		self.doc.reload()
		result = self.doc.correct_opening_stock([{"name": yarn.name, "qty": 100, "amount": 30000}])
		self.assertEqual((result.rows, result.amount), (1, 1500))
		# the correction is on Stores' line, so the posted difference is undone first
		self.assertFalse(frappe.db.exists("Journal Entry", entry))
		self.assertEqual(get_balance(temporary), -41000 + 100 - 1500)
		# against Temporary Opening, never Stock Adjustment
		correction = frappe.db.get_value("Company Setup Opening Stock", yarn.name, "correction")
		self.assertEqual(
			set(frappe.get_all("GL Entry", {"voucher_no": correction, "is_cancelled": 0}, pluck="account")),
			{self.stock_in_hand, temporary},
		)
		# the issue's cost is worked out again at the corrected rate; till then Go live points it out
		repost = frappe.get_all(
			"Repost Item Valuation",
			{"company": self.company},
			pluck="name",
			order_by="creation desc",
			limit=1,
		)[0]
		frappe.db.set_value("Repost Item Valuation", repost, "status", "Queued")
		doc = get_setup(self.company)
		doc.run_method("onload")
		self.assertEqual(doc.get_onload().go_live_reminders["stock_reposts"], {"running": 1, "failed": 0})
		frappe.set_user("Administrator")
		run_reposts()
		self.assertEqual(
			frappe.db.get_value(
				"Stock Ledger Entry", {"voucher_no": issue.name, "is_cancelled": 0}, "stock_value_difference"
			),
			-3000,
		)

	def test_perpetual_inventory_is_needed_and_wip_kept_as_a_balance_is_not_stock(self):
		frappe.db.set_value("Company", self.company, "enable_perpetual_inventory", 0)
		frappe.local.enable_perpetual_inventory = {}
		self.doc.import_opening_stock(self.godown_summary())
		self.assertIn("perpetual inventory", self.checks()["blocker"])
		self.doc.turn_on_perpetual_inventory()
		self.assertNotIn("perpetual inventory", self.checks()["blocker"])

		wip = add_account(
			self.company,
			"Work in Progress",
			"",
			"Asset",
			parent=frappe.db.get_value("Account", self.stock_in_hand, "parent_account"),
		)
		capital = get_account(self.company, root_type="Equity", account_type=("!=", "Temporary"))
		self.doc.save_opening_balances(
			[{"account": wip, "debit": 1000}, {"account": capital, "credit": 1000}]
		)
		self.doc.run_method("onload")
		self.assertEqual(self.doc.get_onload().stock_like_lines, [{"account": wip, "amount": 1000}])
		# kept as a balance: not a stock line, so not in the stock total
		self.assertEqual(self.checks()["trial_balance"]["total"], 46000)
		self.doc.move_to_opening_stock(wip)
		self.doc.reload()
		self.assertEqual(frappe.db.get_value("Account", wip, "account_type"), "Stock")
		self.assertEqual(self.checks()["trial_balance"]["total"], 47000)
		self.assertEqual([row.account for row in self.doc.opening_balances], [capital])

	def test_progress_counts_party_steps_and_temporary_opening_names_its_parts(self):
		receivable = frappe.db.get_value("Company", self.company, "default_receivable_account")
		self.doc.sheet_accounts = {
			"Stock": {self.stock_in_hand: 41000, self.job_line: 5000},
			"Receivable": {receivable: 10000},
		}
		self.doc.save()
		cash = get_account(self.company, account_type="Cash")
		capital = get_account(self.company, root_type="Equity", account_type=("!=", "Temporary"))
		self.doc.save_opening_balances(
			[{"account": cash, "debit": 1000}, {"account": capital, "credit": 5000}]
		)
		doc = get_setup(self.company)
		doc.run_method("onload")
		steps = doc.get_onload().steps
		# customers are in the trial balance, suppliers are not
		self.assertTrue(steps[step_key(doc, "customers")]["needed"])
		self.assertTrue(steps[step_key(doc, "opening_sales_invoices")]["needed"])
		self.assertFalse(steps[step_key(doc, "opening_purchase_invoices")]["needed"])
		self.assertEqual(
			doc.get_onload().temporary_parts,
			[
				{"label": "opening balances", "amount": -4000},
				{"label": "stock", "amount": 46000, "to_come": True, "over": False},
				{"label": "customer invoices", "amount": 10000, "to_come": True, "over": False},
			],
		)
		self.assertEqual(
			[guess_new_bank(name) for name in ("Axis Bank Current A/c", "Bank of Baroda OD", "Bank OD A/c")],
			["Axis Bank", "Bank of Baroda", None],
		)


def get_setup(company: str):
	doc = frappe.get_doc("Company Setup", company)
	doc.check_steps()
	return doc


def run_reposts():
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost_entries

	repost_entries()


def make_bom(item: str, raw_material: str, company: str) -> str:
	bom = frappe.get_doc(
		{
			"doctype": "BOM",
			"item": item,
			"company": company,
			"quantity": 1,
			"items": [{"item_code": raw_material, "qty": 2, "rate": 10}],
		}
	).insert()
	bom.submit()
	return bom.name


class TestOpenOrders(ERPNextTestSuite):
	"""Open sales, purchase, work and subcontracting orders: made for the qty left, no money moves."""

	def setUp(self):
		self.company = make_company()
		self.yarn = make_stock_item("_Test Setup Yarn", "Kg")
		self.fabric = make_stock_item("_Test Setup Fabric", "Nos")
		self.dyed = make_stock_item("_Test Setup Dyed Fabric", "Nos")
		frappe.db.set_value("Item", self.dyed, "is_sub_contracted_item", 1)
		if not frappe.db.exists("Item", "_Test Setup Dyeing"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "_Test Setup Dyeing",
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()
		self.bom = make_bom(self.fabric, self.yarn, self.company)
		make_bom(self.dyed, self.fabric, self.company)
		self.customer = make_customer("_Test Setup Order Customer")
		self.wip = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Work In Progress"}
		)
		user = make_user("onboarding.order.maker@example.com", "Accounts Manager")
		for role in (
			"Sales Manager",
			"Purchase Manager",
			"Manufacturing User",
			"Stock Manager",
			"Item Manager",
			"Purchase Master Manager",
		):
			make_user(user, role)
		frappe.set_user(user)
		self.doc = frappe.get_doc("Company Setup", self.company)
		self.doc.migration_date = "2026-04-01"
		self.doc.save()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_open_orders_are_made_for_the_qty_left_and_undone(self):
		sheet = make_file(
			"open_sales_orders.csv",
			[
				"Order No,Customer,Item,Qty left,Unit,Rate,Delivery date",
				f"SO-1,{self.customer},_Test Setup Fabric,30,Nos,100,2026-04-20",
				f"SO-1,{self.customer},_Test Setup Yarn,5,Kg,250,2026-04-20",
				"SO-2,_Test Setup Nobody,_Test Setup Fabric,5,Nos,100,",
			],
		)
		self.doc.import_open_orders("Sales Order", sheet)
		self.assertEqual(
			self.doc.get_open_orders("Sales Order")["blocker"],
			"1 customer is not in ERPNext. Import it in Customers first.",
		)
		self.doc.save_open_orders(
			"Sales Order",
			[row.as_dict() for row in self.doc.open_orders if row.party_name == self.customer],
			"open_sales_orders.csv",
		)
		[name] = self.doc.make_open_orders("Sales Order")
		order = frappe.get_doc("Sales Order", name)
		self.assertEqual(
			(order.docstatus, order.customer, [(row.item_code, row.qty) for row in order.items]),
			(1, self.customer, [(self.fabric, 30), (self.yarn, 5)]),
		)
		# nothing posted to the books
		self.assertFalse(frappe.db.exists("GL Entry", {"voucher_no": name}))
		self.doc.undo_open_orders("Sales Order", "open_sales_orders.csv")
		self.assertFalse(frappe.db.exists("Sales Order", name))
		self.doc.reload()
		self.assertEqual(len(self.doc.undone_orders), 2)

		# a work order for the qty left, its material not in the WIP warehouse yet
		self.doc.save_open_orders(
			"Work Order",
			[{"item_name": self.fabric, "qty": 40, "wip_warehouse": self.wip}],
		)
		[name] = self.doc.make_open_orders("Work Order")
		order = frappe.get_doc("Work Order", name)
		self.assertEqual(
			(order.docstatus, order.qty, order.bom_no, order.skip_transfer, order.wip_warehouse),
			(1, 40, self.bom, 0, self.wip),
		)
		self.assertEqual(self.doc.get_boms().boms, 2)
		frappe.db.set_value("Work Order", name, "produced_qty", 5)
		self.doc.reload()
		self.assertRaisesRegex(
			frappe.ValidationError, "already has deliveries", self.doc.undo_open_orders, "Work Order", None
		)

		# a subcontracting order at the job worker's warehouse
		self.doc.save_warehouses(
			[{"warehouse_name": "Velan Job Works", "kind": "Job worker", "job_worker": "_Test Setup Velan"}]
		)
		velan = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Velan Job Works"}
		)
		frappe.set_user("Administrator")
		ensure_fiscal_year("2026-03-31", self.company)
		make_stock_entry(
			item_code=self.fabric,
			qty=24,
			to_warehouse=velan,
			rate=10,
			company=self.company,
			posting_date="2026-03-31",
		)
		frappe.set_user("onboarding.order.maker@example.com")
		self.doc.save_open_orders(
			"Subcontracting Order",
			[
				{
					"order_no": "PO-7",
					"party_name": "_Test Setup Velan",
					"item_name": self.dyed,
					"qty": 12,
					"service_item": "_Test Setup Dyeing",
				}
			],
			"open_subcontracting_orders.xlsx",
		)
		[name] = self.doc.make_open_orders("Subcontracting Order")
		order = frappe.get_doc("Subcontracting Order", name)
		self.assertEqual(
			(
				order.docstatus,
				order.supplier,
				order.supplier_warehouse,
				order.items[0].item_code,
				order.items[0].qty,
			),
			(1, "_Test Setup Velan", velan, self.dyed, 12),
		)
		self.doc.reload()
		self.doc.undo_open_orders("Subcontracting Order", "open_subcontracting_orders.xlsx")
		self.assertFalse(frappe.db.exists("Subcontracting Order", name))
		self.assertFalse(frappe.db.exists("Purchase Order", order.purchase_order))


class TestTaxes(ERPNextTestSuite):
	"""ERPNext's own Taxes step, for countries with no compliance app."""

	def setUp(self):
		self.company = make_company()
		user = make_user("onboarding.accounts@example.com", "Accounts Manager")
		frappe.set_user(make_user(user, "Purchase Master Manager"))
		self.doc = frappe.get_doc("Company Setup", self.company)
		group = get_account(self.company, is_group=1, account_type="Tax")
		self.sales_tax = add_account(self.company, "CA Sales Tax Payable", "Tax", "Liability", parent=group)
		self.input_tax = add_account(self.company, "Input Tax", "Tax", "Liability", parent=group)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_save_makes_the_tax_templates_and_no_rows_is_done_too(self):
		key = step_key(get_setup(self.company), "taxes")
		self.assertEqual(step_status(get_setup(self.company))["taxes"], "Not Started")
		cash = get_account(self.company, account_type="Cash")
		self.assertRaisesRegex(
			frappe.ValidationError,
			"not a tax account",
			self.doc.save_taxes,
			[{"tax_type": "Sales", "title": "Wrong", "account": cash, "rate": 5}],
		)
		self.doc.save_taxes(
			[
				{"tax_type": "Sales", "title": "CA Sales Tax", "account": self.sales_tax, "rate": 7.25},
				{"tax_type": "Purchase", "title": "Input Tax", "account": self.input_tax, "rate": 5},
			],
			{"Sales": "CA Sales Tax"},
		)
		sales = frappe.get_doc("Sales Taxes and Charges Template", self.doc.tax_templates[0].template)
		self.assertEqual(
			(sales.company, sales.is_default, sales.taxes[0].account_head, sales.taxes[0].rate),
			(self.company, 1, self.sales_tax, 7.25),
		)
		# a template is the default only when picked: a company may sell exempt services too
		self.assertFalse(
			frappe.db.get_value(
				"Purchase Taxes and Charges Template",
				{"title": "Input Tax", "company": self.company},
				"is_default",
			)
		)
		self.assertTrue(
			frappe.db.exists(
				"Purchase Taxes and Charges Template", {"title": "Input Tax", "company": self.company}
			)
		)
		doc = get_setup(self.company)
		doc.run_method("onload")
		self.assertEqual(doc.get_onload().steps[key]["summary"], "2 tax templates")
		self.assertEqual(step_status(doc)["taxes"], "Done")

		# saved again with no rows: the company charges no tax; the templates stay
		self.doc.save_taxes([])
		doc = get_setup(self.company)
		doc.run_method("onload")
		self.assertEqual(
			(step_status(doc)["taxes"], doc.get_onload().steps[key]["summary"]), ("Done", "No taxes charged")
		)
		# a compliance app takes its place
		self.assertEqual(
			step_names(app_one=[{"step": "app_one.steps.gst", "replaces": "taxes"}]),
			[step if step != "taxes" else "gst" for step in [*ERPNEXT_STEPS, "go_live"]],
		)


class TestFixedAssets(ERPNextTestSuite):
	"""Fixed assets on the Assets tab: the register behind the asset lines, checked as on today."""

	def setUp(self):
		self.company = make_company()
		fixed_group = get_account(self.company, is_group=1, account_name="Fixed Assets")
		self.furniture = get_account(self.company, account_name="Furniture and Fixtures")
		# kept at net value, untyped, as in Tally
		self.plant = add_account(self.company, "Plant and Machinery", "", "Asset", parent=fixed_group)
		self.accumulated = get_account(self.company, account_type="Accumulated Depreciation")
		self.depreciation = get_account(self.company, account_type="Depreciation")
		frappe.db.set_value(
			"Company",
			self.company,
			{
				"accumulated_depreciation_account": self.accumulated,
				"depreciation_expense_account": self.depreciation,
			},
		)
		user = make_user("onboarding.assets@example.com", "Accounts Manager")
		for role in ("Item Manager", "Quality Manager", "Stock Manager"):
			make_user(user, role)
		frappe.set_user(user)
		self.doc = frappe.get_doc("Company Setup", self.company)
		self.doc.migration_date = "2026-01-01"
		self.doc.save()
		capital = get_account(self.company, root_type="Equity", account_type=("!=", "Temporary"))
		self.doc.save_opening_balances(
			[
				{"account": self.furniture, "debit": 120000},
				{"account": self.accumulated, "credit": 30000},
				{"account": self.plant, "debit": 31000},
				{"account": capital, "credit": 121000},
			]
		)
		self.doc.post_opening_balances()
		self.doc.reload()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def asset_sheet(self, rows: list[tuple]) -> str:
		return make_xlsx_file(
			"fixed_asset_schedule.xlsx",
			[
				(
					"Asset Name",
					"Asset Account",
					"Purchase Date",
					"Cost",
					"Depreciation till date",
					"Method",
					"Life",
				),
				*rows,
			],
		)

	def checks(self) -> dict:
		self.doc.reload()
		return self.doc.get_fixed_assets()

	def test_assets_post_as_existing_assets_and_net_value_moves_to_its_own_line(self):
		self.doc.import_fixed_assets(
			self.asset_sheet(
				[
					("Rapier Loom", "Plant and Machinery", "2022-07-15", 120000, 89000, "SLM", 5),
					("Office Desk", "Furniture and Fixtures", "2024-04-01", 120000, 30000, "SLM", 10),
				]
			)
		)
		self.assertEqual(self.checks()["blocker"], "Mark the type of 1 account first.")
		self.assertEqual(self.doc.mark_asset_accounts(), [self.plant])
		checks = self.checks()
		self.assertIsNone(checks["blocker"])
		self.assertEqual(checks["net_value"], {self.plant: 89000})
		self.assertEqual(checks["first_depreciation"]["date"], getdate("2026-12-31"))

		self.doc.post_fixed_assets()
		self.doc.reload()
		loom, desk = (frappe.get_doc("Asset", row.asset) for row in self.doc.fixed_assets)
		book = loom.finance_books[0]
		# no depreciation twice: four years are booked, the first from here is 31-12-2026
		self.assertEqual(
			(
				loom.asset_type,
				loom.opening_accumulated_depreciation,
				loom.opening_number_of_booked_depreciations,
			),
			("Existing Asset", 89000, 4),
		)
		self.assertEqual(
			(
				book.total_number_of_depreciations,
				book.frequency_of_depreciation,
				str(book.depreciation_start_date),
			),
			(5, 12, "2026-12-31"),
		)
		self.assertEqual(desk.opening_number_of_booked_depreciations, 2)
		# the loom's depreciation moves from the asset line to its own line; nothing else moves
		moved = get_account(self.company, account_name="Accumulated Depreciation - Plant and Machinery")
		temporary = get_account(self.company, account_type="Temporary")
		self.assertEqual(
			(
				get_balance(self.plant),
				get_balance(moved),
				get_balance(self.furniture),
				get_balance(temporary),
			),
			(120000, -89000, 120000, 0),
		)
		self.assertEqual(
			str(
				frappe.db.get_value("Journal Entry", self.doc.fixed_assets[0].net_value_entry, "posting_date")
			),
			"2026-01-01",
		)
		doc = get_setup(self.company)
		doc.run_method("onload")
		self.assertTrue(doc.get_onload().asset_match["matches"])

		# undo the loom: its line is at net value again, the account stays
		self.doc.undo_fixed_assets("fixed_asset_schedule.xlsx", [loom.name])
		self.assertEqual((get_balance(self.plant), get_balance(moved)), (31000, 0))
		# deleted, not kept cancelled: nothing else refers to it
		self.assertFalse(frappe.db.exists("Asset", loom.name))

	def test_depreciation_booked_by_hand_is_counted_and_later_hand_entries_are_pointed_out(self):
		def book_by_hand(amount: float, date: str) -> str:
			je = frappe.get_doc(
				{
					"doctype": "Journal Entry",
					"voucher_type": "Depreciation Entry",
					"company": self.company,
					"posting_date": date,
					"accounts": [
						{
							"account": self.depreciation,
							"debit_in_account_currency": amount,
							"cost_center": frappe.get_cached_value("Company", self.company, "cost_center"),
						},
						{"account": self.accumulated, "credit_in_account_currency": amount},
					],
				}
			).insert()
			je.submit()
			return je.name

		book_by_hand(5000, "2026-02-28")
		self.doc.import_fixed_assets(
			self.asset_sheet(
				[("Office Desk", "Furniture and Fixtures", "2024-01-01", 120000, 30000, "SLM", 10)]
			)
		)
		checks = self.checks()
		# the schedule gives 2 months of ₹1,000: the cost over the 120 months of its life
		self.assertEqual((checks["hand"]["amount"], checks["hand"]["schedule"]), (5000, 2000))
		self.assertIn("Booked by hand", checks["blocker"])
		self.doc.spread_hand_booked()
		self.assertIsNone(self.checks()["blocker"])
		self.doc.post_fixed_assets()
		self.doc.reload()
		desk = frappe.get_doc("Asset", self.doc.fixed_assets[0].asset)
		# counted as booked: monthly from the month after the last hand entry
		self.assertEqual(
			(desk.opening_accumulated_depreciation, desk.opening_number_of_booked_depreciations),
			(35000, 26),
		)
		self.assertEqual(
			(
				desk.finance_books[0].frequency_of_depreciation,
				str(desk.finance_books[0].depreciation_start_date),
			),
			(1, "2026-03-31"),
		)
		doc = get_setup(self.company)
		doc.run_method("onload")
		match = doc.get_onload().asset_match
		self.assertEqual((match["books_cost"], match["books_depreciation"]), (120000, 35000))
		# Plant and Machinery's line has no asset posted yet: the register doesn't match the lines
		self.assertFalse(match["matches"])

		# booked by hand again after the assets were added: pointed out, left out of the match
		book_by_hand(1000, "2026-03-31")
		doc = get_setup(self.company)
		doc.run_method("onload")
		match = doc.get_onload().asset_match
		self.assertEqual((match["hand_after"], match["books_depreciation"]), (1000, 35000))
		self.assertIn(
			"booked by hand after they were added",
			doc.get_onload().steps[step_key(doc, "fixed_assets")]["summary"],
		)

		# once ERPNext posts its depreciation, the asset can't be undone here
		frappe.set_user("Administrator")
		post_depreciation_entries(date="2026-03-31")
		frappe.set_user("onboarding.assets@example.com")
		self.doc.reload()
		self.assertRaisesRegex(
			frappe.ValidationError,
			"already have depreciation posted",
			self.doc.undo_fixed_assets,
			"fixed_asset_schedule.xlsx",
		)
