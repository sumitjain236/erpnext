# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe
from frappe.utils import flt, getdate

from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_fiscal_year
from erpnext.setup.doctype.company_setup import company_setup_steps
from erpnext.setup.doctype.company_setup.chart_sheet import get_draft_balances
from erpnext.setup.doctype.company_setup.opening_invoices import add_account, ensure_fiscal_year
from erpnext.setup.doctype.company_setup.test_company_setup import (
	get_account,
	has_year,
	make_company,
	make_file,
	make_user,
	make_xlsx_file,
	step_status,
)
from erpnext.tests.utils import ERPNextTestSuite


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
