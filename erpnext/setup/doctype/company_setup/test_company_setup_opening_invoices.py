# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from contextlib import contextmanager

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_fiscal_year
from erpnext.setup.doctype.company_setup import company_setup_steps
from erpnext.setup.doctype.company_setup.opening_invoices import get_invoice_accounts
from erpnext.setup.doctype.company_setup.test_company_setup import (
	COMPANY,
	get_account,
	make_company,
	make_file,
	make_user,
	step_key,
)
from erpnext.tests.utils import ERPNextTestSuite


@contextmanager
def day_first_dates():
	"""Dates written day first, as Indian sites usually do."""
	frappe.local.user_date_format = "dd-mm-yyyy"
	try:
		yield
	finally:
		frappe.local.user_date_format = None


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

	def test_undo_posting_keeps_a_paid_invoice_and_is_refused_in_a_closed_year_or_once_live(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		invoices = self.post_two_sources()
		payment = get_payment_entry("Sales Invoice", invoices["delhi.xlsx"], party_amount=40)
		payment.update({"reference_no": "OB-PAY-1", "reference_date": "2026-04-02"})
		payment.insert()
		payment.submit()

		result = self.doc.undo_posting("Customer", "delhi.xlsx")
		self.assertEqual((result.removed, result.kept), (0, 1))
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoices["delhi.xlsx"], "docstatus"), 1)

		# a closed year stops posting and undo with a plain reason
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

		self.doc.db_set("status", "Live")
		self.assertRaises(frappe.ValidationError, self.doc.undo_posting, "Customer", "delhi.xlsx")

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
		# the failed row is left alone, and the step says so
		self.doc.run_method("onload")
		self.assertEqual(
			self.doc.get_onload().steps[step_key(self.doc, "opening_sales_invoices")]["summary"],
			"1 invoice posted · 1 invoice left to post: 1 failed, fix them and post again",
		)
		# an invoice after the migration date is refused
		self.assertRaises(
			frappe.ValidationError,
			self.doc.save_opening_invoices,
			"Customer",
			[{"party_name": "X", "invoice_date": "2026-04-05", "outstanding_amount": 10}],
		)
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

	def test_only_names_confirmed_as_new_are_added_once_and_complete(self):
		# a name confirmed as new needs permission to add customers
		frappe.set_user("Administrator")
		frappe.set_user(make_user("onboarding.no.customers@example.com", "Accounts Manager"))
		doc = frappe.get_doc("Company Setup", self.company)
		doc.migration_date = "2026-04-01"
		doc.save_opening_invoices(
			"Customer",
			[{"party_name": "Onboarding Not Allowed Customer", "outstanding_amount": 100, "create_party": 1}],
		)
		self.assertFalse(doc.get_invoice_names("Customer")["can_create"])
		self.assertRaises(frappe.PermissionError, doc.create_opening_invoices, "Customer")

		frappe.set_user("onboarding.accounts@example.com")
		self.allow_adding_customers()
		self.doc.reload()
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

	def test_a_missing_year_is_asked_for_and_old_invoices_keep_their_dates_only_when_asked(self):
		from erpnext.setup.doctype.company_setup.opening_invoices import get_posting_dates

		# an Accounts Manager is asked for a missing year before posting
		self.doc.migration_date = "2081-04-01"
		self.doc.save_opening_invoices(
			"Customer",
			[
				{
					"party_name": self.customer.customer_name,
					"invoice_number": "OB-FY-1",
					"outstanding_amount": 100,
				}
			],
		)
		self.doc.run_method("onload")
		self.assertIn(
			{
				"text": "Ask a System Manager to add the financial year 2081 first.",
				"fix": {"label": "New Fiscal Year", "doctype": "Fiscal Year", "name": "new"},
			},
			self.doc.get_onload().posting_reds["Customer"],
		)
		self.assertRaisesRegex(
			frappe.ValidationError, "Ask a System Manager", self.doc.create_opening_invoices, "Customer"
		)
		self.assertFalse(frappe.db.exists("Sales Invoice", "OB-FY-1"))

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
