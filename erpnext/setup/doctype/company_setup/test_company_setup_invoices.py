# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import patch

import frappe

from erpnext.accounts.party import get_party_account
from erpnext.controllers.sales_and_purchase_return import make_return_doc
from erpnext.setup.doctype.company_setup import (
	company_setup_steps,
	test_company_setup_bank,
	test_company_setup_opening_invoices,
)
from erpnext.setup.doctype.company_setup import test_company_setup as base
from erpnext.setup.doctype.company_setup.posted_entries import get_delete_setting
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

	def test_dr_cr_with_a_dot_and_a_year_another_company_has(self):
		from erpnext.setup.doctype.company_setup.opening_invoices import ensure_fiscal_year, get_sheet_amount

		self.assertEqual(
			[
				get_sheet_amount("30,000.00 Cr.", "Customer"),
				get_sheet_amount("30,000.00 Dr.", "Customer"),
				get_sheet_amount("500 Dr.", "Supplier"),
				get_sheet_amount("500 Cr", "Supplier"),
				get_sheet_amount("700", "Supplier"),
			],
			[-30000, 30000, -500, 500, 700],
		)

		# a year of that name kept for another company: this one gets its own, named after it
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": "2071-2072",
				"year_start_date": "2071-04-01",
				"year_end_date": "2072-03-31",
				"companies": [{"company": "_Test Company"}],
			}
		).insert()
		ensure_fiscal_year("2071-06-30", self.company)
		self.assertEqual(
			frappe.get_all("Fiscal Year Company", {"parent": "2071-2072 (_TOC)"}, pluck="company"),
			[self.company],
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

	def test_the_per_ledger_check_counts_only_the_rows_that_post(self):
		from erpnext.setup.doctype.company_setup.opening_invoices import get_ledger_overs

		default, insurers = self.add_receivable_account("Debtors - Insurance")
		usd = frappe.get_doc("Customer", base.make_customer("Onboarding Dollar Customer"))
		frappe.set_user("Administrator")
		usd.db_set("default_currency", "USD")
		frappe.set_user("onboarding.accounts@example.com")
		disabled = base.make_customer("Onboarding Off Customer", disabled=1)
		self.doc.migration_date = "2026-04-01"
		self.doc.sheet_accounts = frappe.as_json({"Receivable": {default: 100, insurers: 300}})
		self.doc.save()
		self.doc.save_opening_invoices(
			"Customer",
			[
				{"party_name": self.customer.customer_name, "outstanding_amount": 100},
				{"party_name": usd.customer_name, "outstanding_amount": 500},
				{"party_name": disabled, "outstanding_amount": 500},
			],
		)
		# the USD and disabled rows stay drafts, so they put nothing over the default ledger's line
		self.assertEqual(get_ledger_overs(self.doc, "Customer"), [])
		# the disabled customer posted anyway does
		self.assertEqual(
			[over["account"] for over in get_ledger_overs(self.doc, "Customer", [disabled])], [default]
		)

	def test_posting_writes_no_party_account_so_a_sheet_goes_back_to_its_ledger(self):
		sundry, retainers = self.add_receivable_account("Debtors - Retainers")
		self.doc.migration_date = "2026-04-01"
		self.doc.sheet_accounts = frappe.as_json({"Receivable": {sundry: 300, retainers: 200}})
		self.doc.save()
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
		sheets = {
			"3a.xlsx": (row | {"invoice_number": "OB-A-1", "outstanding_amount": 300}, sundry),
			"3b.xlsx": (row | {"invoice_number": "OB-A-2", "outstanding_amount": 200}, retainers),
		}
		for source, (sheet_row, post_to) in sheets.items():
			self.doc.save_opening_invoices(
				"Customer",
				[*[r.as_dict() for r in self.doc.opening_sales_invoices], sheet_row],
				source,
				post_to,
			)
			self.doc.create_opening_invoices("Customer")
			self.doc.reload()
		self.assertFalse(
			frappe.db.exists("Party Account", {"parent": self.customer.name, "company": self.company})
		)
		for source in sheets:
			self.doc.undo_posting("Customer", source)
			self.doc.reload()
		sheet_row, post_to = sheets["3a.xlsx"]
		self.doc.save_opening_invoices("Customer", [sheet_row], "3a.xlsx", post_to)
		self.assertEqual([r.account for r in self.doc.opening_sales_invoices], [sundry])
		self.doc.create_opening_invoices("Customer")
		self.assertEqual((get_balance(sundry, "OB-A-1"), get_balance(retainers, "OB-A-1")), (300, 0))

	def test_a_post_to_cleared_stays_cleared_when_the_sheet_comes_again(self):
		default, retainers = self.add_receivable_account("Debtors - Retainers")
		sheet = [
			"Bills Receivable,,,",
			"Ledger: Debtors - Retainers   1-Apr-2025 to 31-Mar-2026,,,",
			"Date,Ref. No.,Party's Name,Pending Amount",
			f"2026-03-01,OB-C-1,{self.customer.customer_name},300",
		]
		self.assertEqual(
			self.doc.import_opening_invoices("Customer", make_file("retainers.csv", sheet))["post_to"],
			retainers,
		)
		# Post to cleared in the window: the rows go back to the customer's own ledger
		self.doc.reload()
		rows = [row.as_dict() | {"account": default} for row in self.doc.opening_sales_invoices]
		self.doc.save_opening_invoices("Customer", rows)
		result = self.doc.import_opening_invoices("Customer", make_file("retainers.csv", sheet))
		self.doc.reload()
		self.assertEqual(
			(result["post_to"], [row.account for row in self.doc.opening_sales_invoices]), (None, [None])
		)

	def test_a_file_that_is_not_a_sheet_is_refused_in_words(self):
		self.assertRaisesRegex(
			frappe.ValidationError,
			r"Upload an Excel \(\.xlsx\) or CSV file",
			self.doc.import_opening_invoices,
			"Customer",
			make_file("bills.xlsx", ["not a sheet"]),
		)

	def test_post_to_moves_only_its_own_sheets_rows(self):
		sundry, retainers = self.add_receivable_account("Debtors - Retainers")
		row = {"party_name": self.customer.customer_name}
		self.doc.save_opening_invoices(
			"Customer", [row | {"invoice_number": "OB-S-1", "outstanding_amount": 100}], "delhi.xlsx", sundry
		)
		delhi = [r.as_dict() for r in self.doc.opening_sales_invoices]
		mumbai = row | {"invoice_number": "OB-S-2", "outstanding_amount": 200}
		self.doc.save_opening_invoices("Customer", [*delhi, mumbai], "mumbai.xlsx", retainers)
		self.assertEqual([r.account for r in self.doc.opening_sales_invoices], [sundry, retainers])

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

	def test_post_to_moves_every_row_not_set_on_purpose(self):
		_default, related = self.add_receivable_account("Debtors - Related Parties")
		_default, retainers = self.add_receivable_account("Debtors - Retainers")
		frappe.set_user("Administrator")
		group = frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": "Onboarding Related Parties",
				"parent_customer_group": "All Customer Groups",
				"accounts": [{"company": self.company, "account": related}],
			}
		).insert()
		frappe.set_user("onboarding.accounts@example.com")
		sister = base.make_customer("Onboarding Sister Co")
		frappe.db.set_value("Customer", sister, "customer_group", group.name)
		rows = [
			{"party_name": sister, "invoice_number": "OB-G-1", "outstanding_amount": 100},
			{
				"party_name": self.customer.customer_name,
				"invoice_number": "OB-G-2",
				"outstanding_amount": 200,
			},
			# a new name confirmed into the group gets the group's ledger
			{
				"party_name": "Onboarding Sister Two",
				"invoice_number": "OB-G-3",
				"outstanding_amount": 300,
				"create_party": 1,
				"party_group": group.name,
			},
		]
		self.doc.save_opening_invoices("Customer", rows)
		self.assertEqual([row.account for row in self.doc.opening_sales_invoices], [related, None, related])
		# Post to moves every row but one set on purpose, like a bulk Apply
		rows[2] |= {"account": related, "account_by_hand": 1}
		self.doc.save_opening_invoices("Customer", rows, post_to=retainers)
		self.assertEqual(
			[row.account for row in self.doc.opening_sales_invoices], [retainers, retainers, related]
		)
		# the window's Post to keeps it too
		self.assertEqual(self.doc.get_invoice_names("Customer")["own_accounts"], {sister: related})

	def test_a_sundry_debtors_title_names_no_ledger(self):
		from erpnext.setup.doctype.company_setup.opening_invoices import get_title_account

		self.add_receivable_account("Debtors - Retainers")
		self.assertIsNone(
			get_title_account(self.company, "Customer", ["Outstanding Analysis (Bill-wise) - Sundry Debtors"])
		)

	def test_an_accounts_manager_is_asked_for_a_missing_year_before_posting(self):
		# test sites have years up to 2050 only
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

	def test_invoices_made_by_hand_count_and_a_bill_with_no_due_date_follows_its_terms(self):
		frappe.set_user("Administrator")
		terms = frappe.get_doc(
			{
				"doctype": "Payment Terms Template",
				"template_name": "_Test Onboarding 30 days",
				"terms": [
					{
						"invoice_portion": 100,
						"due_date_based_on": "Day(s) after invoice date",
						"credit_days": 30,
					}
				],
			}
		)
		if not frappe.db.exists("Payment Terms Template", terms.template_name):
			terms.insert()
		frappe.db.set_value("Customer", self.customer.name, "payment_terms", terms.template_name)
		frappe.set_user("onboarding.accounts@example.com")
		self.doc.migration_date = "2026-04-01"
		receivable = get_party_account("Customer", self.customer.name, self.company)
		self.doc.sheet_accounts = frappe.as_json({"Receivable": {receivable: 500}})
		self.doc.save()
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
		self.doc.save_opening_invoices(
			"Customer",
			[
				row | {"invoice_number": "OB-H-1", "outstanding_amount": 300},
				row | {"invoice_number": "OB-H-2", "outstanding_amount": 200},
			],
		)
		self.doc.create_opening_invoices("Customer")
		self.assertEqual(str(frappe.db.get_value("Sales Invoice", "OB-H-1", "due_date")), "2026-03-31")

		# one of them as if made by hand: still counted against the trial balance
		self.doc.reload()
		self.doc.set(
			"opening_sales_invoices", [r for r in self.doc.opening_sales_invoices if r.invoice != "OB-H-2"]
		)
		self.doc.flags.posting = True
		self.doc.save()
		from erpnext.setup.doctype.company_setup.opening_invoices import get_sheet_match

		match = get_sheet_match(self.doc, "Customer")
		self.assertEqual((match["entered"], match["outside"]), (500, {"count": 1, "amount": 200}))

	def test_invoices_over_the_trial_balance_are_not_waited_for(self):
		self.doc.migration_date = "2026-04-01"
		receivable = get_party_account("Customer", self.customer.name, self.company)
		self.doc.sheet_accounts = frappe.as_json({"Receivable": {receivable: 500}})
		self.doc.save()
		self.doc.save_opening_invoices(
			"Customer",
			[
				{
					"party_name": self.customer.customer_name,
					"invoice_date": "2026-03-01",
					"invoice_number": "OB-O-1",
					"outstanding_amount": 800,
				}
			],
		)
		self.doc.create_opening_invoices("Customer")
		doc = base.get_setup(self.company)
		self.assertEqual(
			company_setup_steps.get_temporary_parts(doc),
			[{"label": "customer invoices", "amount": -300, "to_come": False, "over": True}],
		)
		self.assertTrue(
			company_setup_steps.check_books(doc)["summary"].startswith("More than your trial balance")
		)
		# all posted, but over the line: the step says so rather than only "All posted"
		step = company_setup_steps.opening_sales_invoices(doc)
		self.assertFalse(step["done"])
		self.assertIn("is ₹ 300 more than the trial balance", step["summary"])

	def test_a_posted_row_whose_ledger_was_set_is_not_moved_back(self):
		default, retainers = self.add_receivable_account("Debtors - Retainers")
		self.doc.migration_date = "2026-04-01"
		self.doc.sheet_accounts = frappe.as_json({"Receivable": {default: 100, retainers: 100}})
		self.doc.save()
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
		self.doc.save_opening_invoices(
			"Customer", [row | {"invoice_number": "OB-F-1", "outstanding_amount": 100, "account": retainers}]
		)
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()
		self.doc.sheet_accounts = frappe.as_json({"Receivable": {default: 200}})
		self.doc.save()
		self.doc.save_opening_invoices(
			"Customer",
			[
				*[r.as_dict() for r in self.doc.opening_sales_invoices],
				row | {"invoice_number": "OB-F-2", "outstanding_amount": 100, "account": default},
			],
		)
		(clash,) = self.doc.get_account_clashes("Customer")
		self.assertEqual((clash["kind"], clash["account"]), ("posted", default))

		# set on the row, by its sheet or an earlier move: it stays where it is
		posted = next(r for r in self.doc.opening_sales_invoices if r.invoice_number == "OB-F-1")
		frappe.db.set_value(posted.doctype, posted.name, "account_by_hand", 1)
		self.doc.reload()
		self.assertEqual(self.doc.get_account_clashes("Customer"), [])

	def test_zoho_bill_numbers_and_a_name_confirmed_before_an_undo(self):
		supplier = frappe.get_all("Supplier", pluck="name", limit=1)[0]
		self.doc.migration_date = "2026-04-01"
		self.doc.import_opening_invoices(
			"Supplier",
			make_file(
				"bills.csv", ["Vendor Name,Bill#,Bill Date,Balance", f"{supplier},Z-77,2026-03-02,500"]
			),
		)
		self.assertEqual(self.doc.opening_purchase_invoices[0].invoice_number, "Z-77")

		# a close spelling linked by hand stays linked once its invoice is undone and uploaded again
		row = {"party_name": "Cust Typo Name", "party": self.customer.name, "invoice_number": "OB-U-1"}
		self.doc.save_opening_invoices("Customer", [row | {"outstanding_amount": 40}], "a.xlsx")
		self.doc.create_opening_invoices("Customer")
		self.doc.reload()
		self.doc.undo_posting("Customer", "a.xlsx")
		self.doc.reload()
		self.doc.save_opening_invoices(
			"Customer", [{"party_name": "Cust Typo Name", "outstanding_amount": 40}], "a.xlsx"
		)
		self.assertEqual(self.doc.opening_sales_invoices[0].party, self.customer.name)


class TestChequeReports(ERPNextTestSuite):
	"""Uncleared cheques: the bank reconciliation reports of Tally, Busy and QuickBooks."""

	setUp = test_company_setup_bank.TestBankAccounts.setUp
	save_bank_details = test_company_setup_bank.TestBankAccounts.save_bank_details
	post_balances = test_company_setup_bank.TestBankAccounts.post_balances

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

	def test_a_title_saying_not_cleared_is_read(self):
		self.doc.mark_bank_accounts()
		self.doc.import_uncleared_cheques(
			make_file(
				"pending.csv",
				[
					"Cheques not cleared as on 31.03.2026,,,,",
					"Bank,Cheque No,Date,Party,Amount",
					"HDFC Bank A/c,4521,2026-03-29,Velan,-3500",
				],
			)
		)
		self.assertEqual(len(self.doc.uncleared_cheques), 1)

	def test_a_customer_list_loses_its_title_rows_before_data_import(self):
		from erpnext.setup.doctype.company_setup.chart_sheet import read_report, strip_title_rows

		def cleaned(rows):
			return [
				row.cells[:2] for row in read_report(strip_title_rows("Customer", make_file("c.csv", rows)))
			]

		# Zoho: title rows, its own headings, and a Total line
		self.assertEqual(
			cleaned(
				[
					"Sanjivani Pharma,,",
					"Customer Balances,,",
					"Customer Name,City,Mobile",
					"Gulf Medicare LLC,Dubai,+971",
					"Total,,",
				]
			),
			[["Customer Name", "City"], ["Gulf Medicare LLC", "Dubai"]],
		)
		# QuickBooks: "Customer" for the name, under a title and an empty row
		self.assertEqual(
			cleaned(
				[
					"Bluebonnet Software Inc,,",
					"Customer Contact List,,",
					",,",
					"Customer,Email,Full Name",
					"Travis County Title Co,ap@example.com,Maria",
				]
			),
			[["Customer Name", "Email"], ["Travis County Title Co", "ap@example.com"]],
		)

	def test_a_cheque_posted_on_one_bank_is_not_posted_again_on_another(self):
		self.doc.mark_bank_accounts()
		self.save_bank_details()
		self.post_balances()
		cheque = {"entry_type": "Issued", "reference_no": "000231", "reference_date": "2026-03-27"}
		self.doc.save_uncleared_cheques([cheque | {"bank_account": self.hdfc, "amount": 236000}], "hdfc.csv")
		self.doc.post_uncleared_cheques()
		self.doc.reload()
		self.doc.save_uncleared_cheques(
			[
				*[row.as_dict() for row in self.doc.uncleared_cheques],
				cheque | {"bank_account": self.od, "amount": 236000},
			],
			"icici.csv",
		)
		self.assertRaisesRegex(
			frappe.ValidationError, "posted already on another bank", self.doc.post_uncleared_cheques
		)
		self.assertEqual(get_balance(self.od), -50000)

	def test_a_statement_off_the_books_is_a_yellow_line_and_the_step_is_still_done(self):
		self.doc.mark_bank_accounts()
		self.post_balances()
		self.save_bank_details("No")
		step = company_setup_steps.bank_accounts(frappe.get_doc("Company Setup", self.company))
		self.assertTrue(step["done"])
		self.assertIn("HDFC Bank A/c: statement is ₹ 23,000 more than the books.", step["warnings"][0])
		# an uncleared cheque listed explains part of the gap
		self.save_bank_details("Yes")
		cheque = {"entry_type": "Issued", "reference_no": "000231", "reference_date": "2026-03-27"}
		self.doc.save_uncleared_cheques([cheque | {"bank_account": self.hdfc, "amount": 20000}], "hdfc.csv")
		step = company_setup_steps.bank_accounts(frappe.get_doc("Company Setup", self.company))
		self.assertIn("HDFC Bank A/c: statement is ₹ 3,000 more than the books.", step["warnings"][0])


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

	def test_unticked_the_undo_keeps_the_invoice_and_a_repost_takes_the_next_name(self):
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

	def test_with_no_choice_the_undo_follows_accounts_settings(self):
		self.doc.migration_date = "2026-04-01"
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
		self.post([row | {"invoice_number": "OB-DEF-1", "outstanding_amount": 400}])
		with self.change_settings("Accounts Settings", {"delete_linked_ledger_entries": 0}):
			self.assertFalse(get_delete_setting()["delete"])
			self.doc.undo_posting("Customer", "a.xlsx")
		self.assertEqual(frappe.db.get_value("Sales Invoice", "OB-DEF-1", "docstatus"), 2)
		self.assertTrue(get_delete_setting()["delete"])

	def test_undoing_an_empty_pick_undoes_nothing(self):
		self.doc.migration_date = "2026-04-01"
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
		self.post([row | {"invoice_number": "OB-NONE-1", "outstanding_amount": 400}])
		self.doc.undo_posting("Customer", "a.xlsx", [])
		self.assertEqual(frappe.db.get_value("Sales Invoice", "OB-NONE-1", "docstatus"), 1)

	def test_an_entry_something_else_links_to_is_kept_cancelled_with_the_reason(self):
		self.doc.migration_date = "2026-04-01"
		row = {"party_name": self.customer.customer_name, "invoice_date": "2026-03-01"}
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
