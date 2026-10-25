# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.setup.doctype.company_setup.chart_sheet import download_chart_template
from erpnext.setup.doctype.company_setup.company_setup import get_chart_import_blocker
from erpnext.setup.doctype.company_setup.test_company_setup import (
	SHEET,
	make_company,
	make_customer,
	make_file,
	make_sheet,
	make_user,
	set_party_account,
	step_status,
)
from erpnext.tests.utils import ERPNextTestSuite


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
