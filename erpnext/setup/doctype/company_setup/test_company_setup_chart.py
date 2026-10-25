# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.setup.doctype.company_setup.company_setup import get_chart_import_blocker
from erpnext.setup.doctype.company_setup.test_company_setup import (
	SHEET,
	make_company,
	make_customer,
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
		# the preview changes nothing; customers and suppliers are left out, and said so
		before = frappe.db.count("Account", {"company": self.company})
		preview = self.doc.preview_chart(make_sheet(SHEET))
		self.assertEqual((len(preview["accounts"]), preview["current_accounts"]), (len(SHEET) - 1, before))
		self.assertEqual(preview["left_out"], {"Receivable": 95000, "Payable": -82000})
		self.assertTrue(frappe.db.exists("Account", "Cash - _TOC"))
		# a balance on a group account is refused
		on_group = [
			row.replace(
				"Current Assets,Assets,1100,1000,1,,Asset,INR,,",
				"Current Assets,Assets,1100,1000,1,,Asset,INR,10,",
			)
			for row in SHEET
		]
		self.assertRaises(frappe.ValidationError, self.doc.preview_chart, make_sheet(on_group))

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

		big_lines = [
			*SHEET,
			"Debtors - Retainers,Current Assets,1135,1100,0,Receivable,Asset,INR,500000,",
			"Creditors - Capital Goods,Liabilities,2105,2000,0,Payable,Liability,INR,,500000",
			"Creditors - Import,Liabilities,2110,2000,0,Payable,Liability,INR,,1000",
			"Input CGST,Current Assets,1140,1100,0,,Asset,INR,,",
		]
		self.doc.import_chart(make_sheet(big_lines))
		# a GST ledger with no type is a tax account, so tax templates can pick it
		self.assertEqual(
			frappe.db.get_value(
				"Account", {"company": self.company, "account_name": "Input CGST"}, "account_type"
			),
			"Tax",
		)
		# Retainers holds the most, but Sundry Debtors has the usual name
		self.assertEqual(
			frappe.db.get_value("Company", self.company, "default_receivable_account"),
			"1130 - Sundry Debtors - _TOC",
		)

		# no usual name among the payables: left for the user to pick, not the biggest line
		self.doc.import_chart(
			make_sheet([row for row in big_lines if not row.startswith("Sundry Creditors")])
		)
		self.assertFalse(frappe.db.get_value("Company", self.company, "default_payable_account"))
		self.doc.run_method("onload")
		self.assertIn(
			"default_payable_account",
			[gap["fieldname"] for gap in self.doc.get_onload().books_reminders["default_account_gaps"]],
		)

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
