# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import patch

import frappe

from erpnext.accounts.party import get_party_account
from erpnext.controllers.sales_and_purchase_return import make_return_doc
from erpnext.setup.doctype.company_setup import test_company_setup as base
from erpnext.setup.doctype.company_setup import (
	test_company_setup_bank,
	test_company_setup_opening_invoices,
)
from erpnext.setup.doctype.company_setup.test_company_setup import (
	get_account,
	get_balance,
	make_file,
	set_party_account,
)
from erpnext.tests.utils import ERPNextTestSuite


class TestInvoiceLedgers(ERPNextTestSuite):
	"""Opening invoices: the receivable ledger a sheet goes to, and the rows that wait."""

	setUp = test_company_setup_opening_invoices.TestOpeningInvoices.setUp
	add_receivable_account = test_company_setup_opening_invoices.TestOpeningInvoices.add_receivable_account

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_a_sheet_is_not_posted_on_a_ledger_its_trial_balance_line_cannot_take(self):
		default, retainers = self.add_receivable_account("Debtors - Retainers")
		set_party_account("Customer", self.customer.name, self.company, default)
		self.doc.migration_date = "2026-04-01"
		self.doc.sheet_accounts = frappe.as_json({"Receivable": {default: 100, retainers: 300}})
		self.doc.save()
		row = {"party_name": self.customer.customer_name, "invoice_number": "OB-R-1"}
		self.doc.save_opening_invoices("Customer", [row | {"outstanding_amount": 300}])
		# on the customer's own ledger it would be 200 over there, with Retainers 300 short
		self.assertRaisesRegex(
			frappe.ValidationError, "Retainers", self.doc.create_opening_invoices, "Customer"
		)
		self.assertFalse(frappe.db.exists("Sales Invoice", "OB-R-1"))

		# Tally names the ledger in the report's title: the rows go there
		result = self.doc.import_opening_invoices(
			"Customer",
			make_file(
				"retainers.csv",
				[
					"Bills Receivable,,,",
					"Ledger: Debtors - Retainers   1-Apr-2025 to 31-Mar-2026,,,",
					"Date,Ref. No.,Party's Name,Pending Amount",
					f"2026-03-01,OB-R-1,{self.customer.customer_name},300",
				],
			),
		)
		self.assertEqual(result["post_to"], retainers)
		# the title's ledger wins over the customer's own
		self.doc.reload()
		with patch("frappe.publish_realtime") as publish:
			self.assertEqual(self.doc.create_opening_invoices("Customer"), "done")
		# a posting in this request tells no open form it is posting in the background
		self.assertNotIn("company_setup_invoices_progress", [call.args[0] for call in publish.call_args_list])

		temporary = get_account(self.company, account_type="Temporary")
		self.assertEqual(
			(
				get_balance(retainers, "OB-R-1"),
				get_balance(default, "OB-R-1"),
				get_balance(temporary, "OB-R-1"),
			),
			(300, 0, -300),
		)
		# the customer's own ledger stays as it was
		self.assertEqual(
			frappe.db.get_value(
				"Party Account", {"parent": self.customer.name, "company": self.company}, "account"
			),
			default,
		)

	def test_a_usd_or_disabled_customer_stays_a_draft(self):
		usd = frappe.get_doc("Customer", base.make_customer("Onboarding Dollar Customer"))
		frappe.set_user("Administrator")
		usd.db_set("default_currency", "USD")
		frappe.set_user("onboarding.accounts@example.com")
		disabled = base.make_customer("Onboarding Off Customer", disabled=1)
		self.doc.migration_date = "2026-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{
					"party_name": usd.customer_name,
					"invoice_number": "OB-USD-1",
					"outstanding_amount": 1155000,
				},
				{"party_name": disabled, "invoice_number": "OB-OFF-1", "outstanding_amount": 100},
			],
		)
		# billed in USD though its ledger is the company's: never posted at a rate of 1
		self.assertEqual(self.doc.get_invoice_names("Customer")["other_currency"], {usd.name: "USD"})
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()
		self.assertEqual(
			[(row.status, row.error) for row in self.doc.opening_sales_invoices], [("Draft", None)] * 2
		)
		self.assertFalse(frappe.db.exists("GL Entry", {"voucher_no": ("in", ["OB-USD-1", "OB-OFF-1"])}))

		# posted anyway: enabled for the posting only
		self.doc.create_opening_invoices("Customer", disabled_parties=[disabled])
		self.assertEqual(frappe.db.get_value("Sales Invoice", "OB-OFF-1", "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Customer", disabled, "disabled"), 1)
		# and undone the same way
		self.doc.reload()
		result = self.doc.undo_posting("Customer", "", ["OB-OFF-1"])
		self.assertEqual((result.removed, result.failed), (1, []))
		self.assertEqual(frappe.db.get_value("Customer", disabled, "disabled"), 1)

	def test_a_wrong_split_is_caught(self):
		from erpnext.setup.doctype.company_setup.opening_invoices import get_ledger_overs

		default, insurers = self.add_receivable_account("Debtors - Insurance")
		insurer = base.make_customer("Onboarding Insurer")
		set_party_account("Customer", insurer, self.company, insurers)
		self.doc.migration_date = "2026-04-01"
		# Busy: the group's own line has no account beside its sub-group's
		self.doc.sheet_accounts = frappe.as_json({"Receivable": {"": 100, insurers: 300}})
		self.doc.save()
		rows = [
			{
				"party_name": self.customer.customer_name,
				"invoice_number": "OB-B-1",
				"outstanding_amount": 100,
			},
			{"party_name": insurer, "invoice_number": "OB-B-2", "outstanding_amount": 300},
		]
		# the whole sheet put on the default ledger by hand: the per-ledger check sees the wrong split
		self.doc.save_opening_invoices(
			"Customer", [row | {"account": default, "account_by_hand": 1} for row in rows]
		)
		self.assertEqual([over["account"] for over in get_ledger_overs(self.doc, "Customer")], [default])

		# no Post to: the insurer goes to its own ledger, and the lines match
		self.doc.save_opening_invoices("Customer", [row | {"account": default} for row in rows])
		self.assertEqual([row.account for row in self.doc.opening_sales_invoices], [default, insurers])
		self.assertEqual(get_ledger_overs(self.doc, "Customer"), [])

		# one insurer row moved by hand to the default ledger: red before Post
		rows = [r.as_dict() for r in self.doc.opening_sales_invoices]
		rows[1].update({"account": default, "account_by_hand": 1})
		self.doc.save_opening_invoices("Customer", rows, post_to=default)
		self.assertEqual([r.account for r in self.doc.opening_sales_invoices], [default, default])
		self.assertRaisesRegex(
			frappe.ValidationError, "Debtors - Insurance", self.doc.create_opening_invoices, "Customer"
		)
		# the gap comes from that row, not from anything posted: no move of posted invoices is offered
		self.assertFalse(
			[c for c in self.doc.get_account_clashes("Customer") if c["kind"] in ("posted", "split")]
		)

	def test_a_draft_posted_by_hand_is_flagged_counted_once_and_removed_on_request(self):
		from erpnext.setup.doctype.company_setup.opening_invoices import get_sheet_match

		self.doc.migration_date = "2026-04-01"
		receivable = get_party_account("Customer", self.customer.name, self.company)
		self.doc.sheet_accounts = frappe.as_json({"Receivable": {receivable: 500}})
		self.doc.save()
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
		self.doc.save_opening_invoices(
			"Customer",
			[
				row | {"invoice_number": "OB-HP-1", "outstanding_amount": 300},
				row | {"invoice_number": "OB-HP-2", "outstanding_amount": 200},
			],
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()
		# as if OB-HP-2 had been posted by hand while its row stayed a draft
		hand = next(r for r in self.doc.opening_sales_invoices if r.invoice_number == "OB-HP-2")
		frappe.db.set_value("Company Setup Opening Invoice", hand.name, {"status": "Draft", "invoice": None})
		self.doc.reload()
		# flagged, counted once, not posted again; opening the form changes nothing
		(flagged,) = self.doc.get_invoice_names("Customer")["hand_posted"]
		self.assertEqual((flagged["invoice_number"], flagged["invoice"]), ("OB-HP-2", "OB-HP-2"))
		self.assertEqual(get_sheet_match(self.doc, "Customer")["entered"], 500)
		self.doc.check_steps()
		self.doc.reload()
		self.assertEqual(len(self.doc.opening_sales_invoices), 2)

		# removed on the user's word
		self.assertEqual(self.doc.remove_hand_posted("Customer", [flagged]), 1)
		self.doc.reload()
		self.assertEqual([r.invoice_number for r in self.doc.opening_sales_invoices], ["OB-HP-1"])
		self.assertEqual(get_sheet_match(self.doc, "Customer")["entered"], 500)

		# an account the trial balance doesn't have; customers listed by name: only the total
		_, export = self.add_receivable_account("Debtors - Export")
		self.doc.sheet_accounts = {"Receivable": {export: 200}}
		lines = get_sheet_match(self.doc, "Customer")["lines"]
		self.assertEqual(
			{line["account"]: (line["sheet"], line["entered"]) for line in lines}[receivable], (None, 500)
		)
		self.doc.sheet_accounts = {"Receivable": {"": 500}}
		match = get_sheet_match(self.doc, "Customer")
		self.assertEqual(
			(match["by_account"], match["sheet"], match["entered"], match["lines"]), (False, 500, 500, [])
		)

	def test_an_earlier_sheets_strays_move_to_its_ledger(self):
		default, retainers = self.add_receivable_account("Debtors - Retainers")
		other = base.make_customer("Onboarding Stray Customer")
		self.doc.migration_date = "2026-04-01"
		self.doc.sheet_accounts = frappe.as_json({"Receivable": {default: 300, retainers: 200}})
		self.doc.save()
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
		# a sheet for the default ledger, one name of which went to Retainers
		self.doc.save_opening_invoices(
			"Customer",
			[
				row | {"invoice_number": "OB-S-1", "outstanding_amount": 100},
				row | {"invoice_number": "OB-S-2", "outstanding_amount": 150},
				{
					"party_name": other,
					"invoice_number": "OB-S-3",
					"outstanding_amount": 50,
					"account": retainers,
				},
			],
			"a.xlsx",
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()
		self.doc.save_opening_invoices(
			"Customer",
			[
				*[r.as_dict() for r in self.doc.opening_sales_invoices],
				row | {"invoice_number": "OB-R-1", "outstanding_amount": 200, "account": retainers},
			],
			"b.xlsx",
		)
		# the earlier sheet's stray is pointed at, not this sheet's rows
		(clash,) = self.doc.get_account_clashes("Customer")
		self.assertEqual(
			(clash["kind"], clash["account"], clash["from_account"], clash["parties"][0]["invoices"]),
			("split", default, retainers, ["OB-S-3"]),
		)
		self.assertRaisesRegex(
			frappe.ValidationError, "Retainers", self.doc.create_opening_invoices, "Customer"
		)
		self.doc.create_opening_invoices(
			"Customer", [{"kind": "split", "account": default, "from_account": retainers}]
		)
		self.assertEqual((get_balance(default), get_balance(retainers)), (300, 200))
		# deleted and posted again under its own number
		self.assertEqual(frappe.db.get_value("Sales Invoice", "OB-S-3", "debit_to"), default)


class TestChequeReports(ERPNextTestSuite):
	"""Uncleared cheques: the bank reconciliation reports of Tally, Busy and QuickBooks."""

	setUp = test_company_setup_bank.TestBankAccounts.setUp

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def rows(self):
		return [
			(
				row.bank_account,
				row.entry_type,
				row.reference_no,
				str(row.reference_date),
				row.party_name,
				row.amount,
			)
			for row in self.doc.uncleared_cheques
		]

	def test_reports_read_only_their_uncleared_rows_on_the_bank_they_name(self):
		self.doc.mark_bank_accounts()
		# Busy: no bank column, the title names it, and a blank cheque date takes the voucher's
		result = self.doc.import_uncleared_cheques(
			make_file(
				"busy.csv",
				[
					"Bank Reconciliation - ICICI Bank OD,,,,,,,",
					"Date,Vch Type,Particulars,Chq. No.,Chq. Date,Debit (Rs.),Credit (Rs.),Clearing Date",
					"2026-03-27,Payment,Siemens Healthcare,000231,,,236000,",
				],
			),
			self.hdfc,
		)
		self.assertEqual(result["title_bank"], self.od)
		self.assertEqual(
			self.rows(), [(self.od, "Issued", "000231", "2026-03-27", "Siemens Healthcare", 236000)]
		)

		# Tally: the balance lines under the rows are no cheques
		self.doc.import_uncleared_cheques(
			make_file(
				"tally.csv",
				[
					"Bank,Date,Particulars,Vch Type,Instrument No,Instrument Date,Debit,Credit",
					"HDFC Bank A/c,2026-03-28,Regus,Payment,000418,2026-03-28,,125000",
					"Balance as per Company Books,,,,,,2348760,",
					"Amounts not reflected in Bank,,,,,,236000,125000",
				],
			)
		)
		self.assertEqual(len(self.doc.uncleared_cheques), 2)

		# QuickBooks: only the uncleared sections, the sign or the type says which way
		self.doc.import_uncleared_cheques(
			make_file(
				"qbo.csv",
				[
					"HDFC Bank A/c, Period Ending 03/31/2026,,,",
					"Checks and payments cleared (1),,,,",
					"DATE,TYPE,REF NO.,PAYEE,AMOUNT (INR)",
					"2026-03-02,Check,4501,Cleared Landlord,-12000",
					"Total,,,,-12000",
					"Uncleared checks and payments as of 03/31/2026,,,,",
					"DATE,TYPE,REF NO.,PAYEE,AMOUNT (INR)",
					"2026-03-28,Check,4518,City Utilities,-1842.37",
					"2026-03-30,Expense,,Delta Air Lines,-642.3",
					"Uncleared deposits and other credits as of 03/31/2026,,,,",
					"DATE,TYPE,REF NO.,PAYEE,AMOUNT (INR)",
					"2026-03-31,Deposit,,Credit Union,18250",
				],
			),
			self.od,
		)
		self.assertEqual(
			self.rows()[1:],
			[
				(self.hdfc, "Issued", "4518", "2026-03-28", "City Utilities", 1842.37),
				(self.hdfc, "Issued", "", "2026-03-30", "Delta Air Lines", 642.3),
				(self.hdfc, "Deposited", "", "2026-03-31", "Credit Union", 18250),
			],
		)

		# no bank column, no bank in the title and none picked: the user says which
		self.assertRaisesRegex(
			frappe.ValidationError,
			"no bank column",
			self.doc.import_uncleared_cheques,
			make_file("plain.csv", ["Cheque No,Date,Party,Amount", "9,2026-03-29,Velan,-100"]),
		)


class TestUndoDeletes(ERPNextTestSuite):
	"""Undo deletes what Company Setup posted, when nothing else holds it, so posting again keeps names."""

	setUp = test_company_setup_opening_invoices.TestOpeningInvoices.setUp

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def post(self, rows: list[dict]):
		self.doc.save_opening_invoices("Customer", rows, "a.xlsx")
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()
		return {row.invoice_number or "advance": row.invoice for row in self.doc.opening_sales_invoices}

	def test_an_undone_invoice_is_deleted_and_posts_again_under_its_own_number(self):
		self.doc.migration_date = "2026-04-01"
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
		posted = self.post(
			[
				row | {"invoice_number": "OB-DEL-1", "outstanding_amount": 400},
				row | {"outstanding_amount": -50},
			]
		)
		self.assertEqual(posted["OB-DEL-1"], "OB-DEL-1")
		# ticked in the undo: deleted with the ledger rows, though Accounts Settings keeps them
		with self.change_settings("Accounts Settings", {"delete_linked_ledger_entries": 0}):
			result = self.doc.undo_posting("Customer", "a.xlsx", delete_entries=1)
		self.assertEqual(result.kept_cancelled, [])
		(comment,) = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Company Setup", "reference_name": self.doc.name},
			pluck="content",
		)
		self.assertIn("1 Sales Invoice, 1 Journal Entry", comment)
		self.assertIn("a.xlsx", comment)
		for doctype, name in (("Sales Invoice", "OB-DEL-1"), ("Journal Entry", posted["advance"])):
			self.assertFalse(frappe.db.exists(doctype, name))
			for ledger in ("GL Entry", "Payment Ledger Entry"):
				self.assertFalse(frappe.db.exists(ledger, {"voucher_no": name}), (ledger, name))

		self.doc.reload()
		self.post([row | {"invoice_number": "OB-DEL-1", "outstanding_amount": 400}])
		temporary = get_account(self.company, account_type="Temporary")
		receivable = get_party_account("Customer", self.customer.name, self.company)
		self.assertEqual(
			(get_balance(receivable, "OB-DEL-1"), get_balance(temporary, "OB-DEL-1")), (400, -400)
		)

	def test_an_undo_that_keeps_the_invoice_cancelled_and_a_repost_takes_the_next_name(self):
		self.doc.migration_date = "2026-04-01"
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
		self.post([row | {"invoice_number": "OB-AUD-1", "outstanding_amount": 400}])
		# Accounts Settings deletes ledger rows, but the undo keeps them
		self.assertEqual(self.doc.undo_posting("Customer", "a.xlsx", delete_entries=0).kept_cancelled, [])
		self.assertEqual(frappe.db.get_value("Sales Invoice", "OB-AUD-1", "docstatus"), 2)
		self.assertTrue(frappe.db.exists("GL Entry", {"voucher_no": "OB-AUD-1", "is_cancelled": 1}))
		(comment,) = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Company Setup", "reference_name": self.doc.name},
			pluck="content",
		)
		self.assertIn("1 Sales Invoice kept cancelled", comment)
		for name, amends in (("OB-AUD-1-1", "OB-AUD-1"), ("OB-AUD-1-2", "OB-AUD-1-1")):
			self.doc.reload()
			self.post([row | {"invoice_number": "OB-AUD-1", "outstanding_amount": 400}])
			self.assertEqual(
				frappe.db.get_value("Sales Invoice", name, ["docstatus", "amended_from"]), (1, amends)
			)
			self.doc.reload()
			if name == "OB-AUD-1-1":
				self.doc.undo_posting("Customer", "a.xlsx", delete_entries=0)

		# an entry something else links to is kept cancelled, with the reason
		self.doc.reload()
		self.post([row | {"invoice_number": "OB-LNK-1", "outstanding_amount": 400}])
		receivable = get_party_account("Customer", self.customer.name, self.company)
		# a credit note made against the invoice, still a draft: a cancel allows it, a delete doesn't
		draft = make_return_doc("Sales Invoice", "OB-LNK-1")
		draft.insert()

		result = self.doc.undo_posting("Customer", "a.xlsx")
		self.assertEqual(frappe.db.get_value("Sales Invoice", "OB-LNK-1", "docstatus"), 2, result)
		(reason,) = result.kept_cancelled
		self.assertIn("OB-LNK-1", reason)
		# its ledger rows stay with it, cancelled, and net to nothing
		self.assertEqual(get_balance(receivable, "OB-LNK-1"), 0)
		self.assertTrue(frappe.db.exists("GL Entry", {"voucher_no": "OB-LNK-1", "is_cancelled": 1}))
		# posted again beside it, under the next amended name
		self.doc.reload()
		self.post([row | {"invoice_number": "OB-LNK-1", "outstanding_amount": 400}])
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", "OB-LNK-1-1", ["docstatus", "amended_from"]), (1, "OB-LNK-1")
		)
