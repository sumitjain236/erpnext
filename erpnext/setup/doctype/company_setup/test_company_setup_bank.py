# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.utils import formatdate

from erpnext.accounts.doctype.bank_clearance.bank_clearance import get_payment_entries_for_bank_clearance
from erpnext.setup.doctype.company_setup.bank_accounts import get_statement_offs, guess_bank
from erpnext.setup.doctype.company_setup.opening_invoices import BACKGROUND_FROM, add_account
from erpnext.setup.doctype.company_setup.test_company_setup import (
	get_account,
	get_balance,
	make_company,
	make_file,
	make_user,
	step_key,
	step_status,
)
from erpnext.tests.utils import ERPNextTestSuite


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
