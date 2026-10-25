# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe
from frappe.utils import getdate

from erpnext.assets.doctype.asset.depreciation import post_depreciation_entries
from erpnext.setup.doctype.company_setup.opening_invoices import add_account
from erpnext.setup.doctype.company_setup.test_company_setup import (
	get_account,
	get_balance,
	get_setup,
	make_company,
	make_user,
	make_xlsx_file,
	step_key,
)
from erpnext.tests.utils import ERPNextTestSuite


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
