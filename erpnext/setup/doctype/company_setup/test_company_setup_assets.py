# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.utils import flt, getdate

from erpnext.setup.doctype.company_setup import fixed_assets, posted_entries
from erpnext.setup.doctype.company_setup import test_company_setup as base
from erpnext.setup.doctype.company_setup.opening_invoices import add_account
from erpnext.setup.doctype.company_setup.test_company_setup import (
	get_account,
	get_balance,
	get_setup,
	make_user,
)
from erpnext.tests.utils import ERPNextTestSuite

USER = "onboarding.assets@example.com"
MOVED = "Accumulated Depreciation - Plant and Machinery"


class TestFixedAssetMoney(ERPNextTestSuite):
	"""Fixed assets: the ledger after each post and undo, and the schedule ERPNext makes from them.
	Same company as TestFixedAssets: Furniture ₹1,20,000 less ₹30,000 depreciation, Plant and
	Machinery at net value ₹31,000, migrated on 1 Jan 2026."""

	setUp = base.TestFixedAssets.setUp
	checks = base.TestFixedAssets.checks

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def book_by_hand(self, amount: float, date: str, credit: str):
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
					{"account": credit, "credit_in_account_currency": amount},
				],
			}
		).insert()
		je.submit()
		return je.name

	def upload(self, rows: list[tuple], name: str = "fixed_asset_schedule.xlsx"):
		self.doc.reload()
		self.doc.import_fixed_assets(
			base.make_xlsx_file(
				name,
				[
					(
						"Asset Name",
						"Asset Account",
						"Purchase Date",
						"Cost",
						"Depreciation till date",
						"Method",
						"Life",
						"Rate",
					),
					*rows,
				],
			)
		)

	def post(self):
		self.doc.reload()
		self.doc.post_fixed_assets()
		self.doc.reload()

	def match(self) -> dict:
		doc = get_setup(self.company)
		doc.run_method("onload")
		return doc.get_onload().asset_match

	def asset_step(self) -> dict:
		from erpnext.setup.doctype.company_setup.company_setup_steps import fixed_assets as step

		return step(get_setup(self.company))

	def schedule(self, asset: str) -> list[tuple]:
		return [
			(str(row.schedule_date), flt(row.depreciation_amount))
			for row in frappe.get_all(
				"Depreciation Schedule",
				filters={
					"parent": (
						"in",
						frappe.get_all("Asset Depreciation Schedule", {"asset": asset}, pluck="name"),
					)
				},
				fields=["schedule_date", "depreciation_amount"],
				order_by="schedule_date",
			)
		]

	def set_migration_date(self, date: str):
		# only the asset schedule is checked, so the posted balances may stay on the old date
		frappe.db.set_value("Company Setup", self.company, "migration_date", date)
		self.doc.reload()

	def test_part_undo_then_post_again_moves_the_rest_of_the_net_value_line(self):
		self.upload(
			[
				("Loom A", "Plant and Machinery", "2022-07-15", 60000, 44500, "SLM", 5),
				("Loom B", "Plant and Machinery", "2022-07-15", 60000, 44500, "SLM", 5),
				("Office Desk", "Furniture and Fixtures", "2024-04-01", 120000, 30000, "SLM", 10),
			]
		)
		self.doc.mark_asset_accounts()
		checks = self.checks()
		first = checks["first_depreciation"]
		self.post()
		moved = get_account(self.company, account_name=MOVED)
		# the window shows the line the post makes, not the company's accumulated depreciation
		self.assertEqual(checks["net_value_accounts"], {self.plant: moved})
		rows = {row.asset_name: row.asset for row in self.doc.fixed_assets}
		# the confirm's first depreciation is what ERPNext books on that date
		self.assertEqual(
			flt(first["amount"], 0),
			flt(
				sum(
					amount
					for asset in rows.values()
					for date, amount in self.schedule(asset)
					if date == str(first["date"])
				),
				0,
			),
		)

		self.doc.undo_fixed_assets("fixed_asset_schedule.xlsx", [rows["Loom B"]])
		self.assertEqual((get_balance(self.plant), get_balance(moved)), (75500, -44500))
		self.doc.reload()
		self.doc.save_fixed_assets([self.doc.undone_assets[0].as_dict()], "fixed_asset_schedule.xlsx")
		self.assertEqual(self.checks()["net_value"], {self.plant: 44500})
		self.post()
		self.assertEqual((get_balance(self.plant), get_balance(moved)), (120000, -89000))
		self.assertTrue(self.match()["matches"])

		# undo all and post again: the same made account takes it
		self.doc.undo_fixed_assets("fixed_asset_schedule.xlsx")
		self.assertEqual((get_balance(self.plant), get_balance(moved)), (31000, 0))
		self.doc.reload()
		self.doc.save_fixed_assets([row.as_dict() for row in self.doc.undone_assets[-3:]])
		self.post()
		self.assertEqual((get_balance(self.plant), get_balance(moved)), (120000, -89000))
		self.assertEqual(
			frappe.db.count("Account", {"company": self.company, "account_name": ("like", f"{MOVED}%")}), 1
		)

	def test_a_second_upload_counts_only_the_hand_depreciation_left(self):
		self.book_by_hand(5000, "2026-02-28", self.accumulated)
		self.upload([("Desk 1", "Furniture and Fixtures", "2024-01-01", 60000, 15000, "SLM", 10)], "one.xlsx")
		self.doc.spread_hand_booked()
		self.post()
		self.upload([("Desk 2", "Furniture and Fixtures", "2024-01-01", 60000, 15000, "SLM", 10)], "two.xlsx")
		checks = self.checks()
		self.assertEqual(checks["hand"]["amount"], 0)
		self.assertIsNone(checks["blocker"])
		self.post()
		self.assertEqual(
			[(row.asset_name, row.hand_booked) for row in self.doc.fixed_assets],
			[("Desk 1", 5000), ("Desk 2", 0)],
		)
		self.assertEqual(get_balance(self.accumulated), -35000)
		self.assertEqual(self.match()["books_depreciation"], 35000)

	def test_hand_depreciation_credited_to_a_net_value_line_stays_with_its_category(self):
		self.book_by_hand(4000, "2026-02-28", self.plant)
		self.upload(
			[
				("Rapier Loom", "Plant and Machinery", "2022-07-15", 120000, 89000, "SLM", 5),
				("Office Desk", "Furniture and Fixtures", "2024-04-01", 120000, 30000, "SLM", 10),
			]
		)
		self.doc.mark_asset_accounts()
		self.doc.spread_hand_booked()
		self.post()
		self.assertEqual(
			[(row.asset_name, row.hand_booked, row.moved) for row in self.doc.fixed_assets],
			[("Rapier Loom", 4000, 93000), ("Office Desk", 0, 0)],
		)
		moved = get_account(self.company, account_name=MOVED)
		self.assertEqual(
			(
				get_balance(self.plant),
				get_balance(moved),
				get_balance(self.furniture),
				get_balance(self.accumulated),
				get_balance(get_account(self.company, account_type="Temporary")),
			),
			(120000, -93000, 120000, -30000, 0),
		)
		self.assertTrue(self.match()["matches"])

	def test_mid_year_every_month_written_down_value(self):
		self.set_migration_date("2026-07-01")
		# ₹1,00,000 on 1 Jan 2026, ₹20,000 booked Jan-Jun at 40 %
		self.upload([("Lathe", "Furniture and Fixtures", "2020-01-01", 150000, 70000, "WDV", 10, 40)])
		checks = self.checks()
		self.assertEqual(
			(checks["blocker"], checks["reds"][0]["key"], str(checks["year_start"])),
			("Answer the question to post.", "answer", "2026-01-01"),
		)
		self.doc.save_fixed_assets(
			[row.as_dict() for row in self.doc.fixed_assets], depreciation_booked="Every month"
		)
		checks = self.checks()
		self.assertIsNone(checks["blocker"])
		self.assertEqual(
			(
				checks["wdv_gap"],
				checks["first_depreciation"]["amount"],
				str(checks["first_depreciation"]["date"]),
			),
			(4000, 2666.67, "2026-07-31"),
		)
		self.post()
		asset = frappe.get_doc("Asset", self.doc.fixed_assets[0].asset)
		self.assertEqual(
			(asset.finance_books[0].daily_prorata_based, asset.finance_books[0].frequency_of_depreciation),
			(0, 1),
		)
		rows = self.schedule(asset.name)
		self.assertEqual(rows[0], ("2026-07-31", 2666.67))
		self.assertEqual(flt(sum(amount for date, amount in rows if date < "2027-01-01"), 0), 16000)
		# next year starts from ₹64,000: ₹25,600 a year
		self.assertEqual(flt(sum(amount for date, amount in rows if date[:4] == "2027"), 0), 25600)
		self.assertEqual(self.match()["wdv_gap"], 4000)

	def test_mid_year_only_at_year_end(self):
		self.set_migration_date("2026-07-01")
		self.upload(
			[
				("Old Desk", "Furniture and Fixtures", "2022-01-01", 120000, 48000, "SLM", 10),
				("New Desk", "Furniture and Fixtures", "2026-03-01", 12000, 500, "SLM", 10),
			]
		)
		self.doc.save_fixed_assets(
			[row.as_dict() for row in self.doc.fixed_assets], depreciation_booked="Only at year end"
		)
		self.assertEqual(
			self.checks()["blocker"],
			"1 asset bought this year has depreciation, but your old books booked none this year.",
		)
		self.doc.save_fixed_assets([self.doc.fixed_assets[0].as_dict()])
		self.post()
		asset = frappe.get_doc("Asset", self.doc.fixed_assets[0].asset)
		self.assertEqual(
			(
				asset.opening_number_of_booked_depreciations,
				asset.finance_books[0].frequency_of_depreciation,
				str(asset.finance_books[0].depreciation_start_date),
			),
			(4, 12, "2026-12-31"),
		)
		self.assertEqual(self.schedule(asset.name)[0], ("2026-12-31", 12000))

	def test_no_depreciation_on_an_old_asset_is_pointed_out(self):
		self.upload([("Old Desk", "Furniture and Fixtures", "2020-01-01", 120000, 0, "SLM", 10)])
		checks = self.checks()
		self.assertEqual(checks["yellows"]["no_depreciation"], [1])
		self.assertEqual(checks["first_depreciation"]["amount"], 12000)
		self.post()
		# ERPNext's own note per asset is not shown
		self.assertFalse(
			[m for m in frappe.local.message_log if "Depreciation Schedule" in str(m.get("message"))]
		)
		self.assertEqual(self.schedule(self.doc.fixed_assets[0].asset)[0], ("2026-12-31", 12000))

	def test_a_closed_period_dates_the_net_value_entry_after_it(self):
		self.book_by_hand(4000, "2026-02-28", self.plant)
		self.upload([("Rapier Loom", "Plant and Machinery", "2022-07-15", 120000, 89000, "SLM", 5)])
		self.doc.mark_asset_accounts()
		self.doc.spread_hand_booked()
		frappe.set_user("Administrator")
		period = frappe.get_doc(
			{
				"doctype": "Accounting Period",
				"period_name": "Assets H1 2026",
				"start_date": "2026-01-01",
				"end_date": "2026-06-30",
				"company": self.company,
				"closed_documents": [{"document_type": "Journal Entry", "closed": 1}],
			}
		).insert()
		frappe.set_user(USER)
		# assets go monthly from 31 Mar 2026, inside the closed period
		self.assertIn("Accounting Period Assets H1 2026", self.checks()["blocker"])
		# unless the user has the role the period lets through
		frappe.db.set_value("Accounting Period", period.name, "exempted_role", "Accounts Manager")
		self.assertNotIn("Assets H1 2026", self.checks()["blocker"] or "")
		frappe.db.set_value("Accounting Period", period.name, "exempted_role", None)

		frappe.db.set_value("Accounting Period", period.name, "end_date", "2026-02-28")
		checks = self.checks()
		self.assertIsNone(checks["blocker"])
		self.assertEqual(checks["net_value_date"], getdate("2026-03-01"))
		self.post()
		self.assertEqual(
			str(
				frappe.db.get_value("Journal Entry", self.doc.fixed_assets[0].net_value_entry, "posting_date")
			),
			"2026-03-01",
		)
		self.assertEqual(get_balance(self.plant), 120000)

	def test_a_frozen_date_moves_the_net_value_entry_and_stops_undo_with_a_plain_reason(self):
		self.upload([("Rapier Loom", "Plant and Machinery", "2022-07-15", 120000, 89000, "SLM", 5)])
		self.doc.mark_asset_accounts()
		# frozen up to the migration date: the move is dated the day after, as after a closed year
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", "2026-01-01")
		checks = self.checks()
		self.assertEqual((checks["blocker"], checks["net_value_date"]), (None, getdate("2026-01-02")))
		self.post()
		self.assertEqual(get_balance(self.plant), 120000)

		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", "2026-01-31")
		self.assertRaisesRegex(
			frappe.ValidationError,
			"Accounts are frozen up to",
			self.doc.undo_fixed_assets,
			"fixed_asset_schedule.xlsx",
		)

	def test_a_category_with_posted_assets_keeps_its_accounts(self):
		self.upload([("Office Desk", "Furniture and Fixtures", "2024-04-01", 120000, 30000, "SLM", 10)])
		self.post()
		other = base.add_account(self.company, "Old Depreciation", "Accumulated Depreciation", "Asset")
		self.assertRaisesRegex(
			frappe.ValidationError,
			"has posted assets",
			self.doc.save_asset_categories,
			[{"asset_account": self.furniture, "accumulated_account": other}],
		)
		doc = frappe.get_doc("Company Setup", self.company)
		doc.asset_categories[0].accumulated_account = other
		doc.save()
		doc.reload()
		self.assertEqual(doc.asset_categories[0].accumulated_account, self.accumulated)

	def test_a_live_asset_purchase_keeps_the_register_matched(self):
		self.upload([("Office Desk", "Furniture and Fixtures", "2024-04-01", 120000, 30000, "SLM", 10)])
		self.post()
		self.assertEqual(self.match()["books_cost"], 120000)
		frappe.set_user("Administrator")
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": self.company,
				"posting_date": "2026-02-01",
				"accounts": [
					{"account": self.furniture, "debit_in_account_currency": 25000},
					{
						"account": get_account(self.company, account_type="Cash"),
						"credit_in_account_currency": 25000,
					},
				],
			}
		).insert()
		je.submit()
		frappe.set_user(USER)
		self.assertEqual(self.match()["books_cost"], 120000)

	def test_rows_with_their_own_life_keep_it_and_the_category_fills_the_rest(self):
		self.upload(
			[
				("Desk", "Furniture and Fixtures", "2024-04-01", 60000, 15000, "SLM", 10),
				("Chair", "Furniture and Fixtures", "2024-04-01", 30000, 7500, "SLM", 5),
				("Shelf", "Furniture and Fixtures", "2024-04-01", 30000, 7500),
			]
		)
		(red,) = self.checks()["reds"]
		self.assertEqual((red["key"], red["rows"]), ("categories", [3]))
		self.doc.save_asset_categories(
			[{"asset_account": self.furniture, "method": "Straight Line", "life": 8}]
		)
		# set again: the rows it filled change with it
		self.doc.save_asset_categories([{"asset_account": self.furniture, "life": 6}])
		self.assertIsNone(self.checks()["blocker"])
		self.post()
		# a year's depreciation is the cost over each one's life
		self.assertEqual(
			[(row.asset_name, self.schedule(row.asset)[0]) for row in self.doc.fixed_assets],
			[
				("Desk", ("2026-12-31", 6000)),
				("Chair", ("2026-12-31", 6000)),
				("Shelf", ("2026-12-31", 5000)),
			],
		)
		self.assertEqual((get_balance(self.furniture), get_balance(self.accumulated)), (120000, -30000))

	@patch.object(fixed_assets, "HSN_FIELD", "customs_tariff_number")
	def test_a_code_the_items_need_is_asked_per_category_and_checked_before_posting(self):
		self.upload([("Office Desk", "Furniture and Fixtures", "2024-04-01", 120000, 30000, "SLM", 10)])
		self.assertEqual([red["key"] for red in self.checks()["reds"]], ["hsn_code"])
		self.doc.save_asset_categories([{"asset_account": self.furniture, "hsn_code": "94033010"}])
		# a code ERPNext does not know is a red in the window, before posting; checking makes nothing
		self.assertEqual(
			self.checks()["blocker"],
			"Furniture and Fixtures: Could not find Customs Tariff Number: 94033010",
		)
		self.assertFalse(frappe.db.exists("Asset Category", "Furniture and Fixtures"))
		self.assertFalse(frappe.db.exists("Item", "Furniture and Fixtures - Asset"))

		frappe.set_user("Administrator")
		frappe.get_doc({"doctype": "Customs Tariff Number", "tariff_number": "94033010"}).insert()
		frappe.set_user(USER)
		self.post()
		self.assertEqual(
			frappe.db.get_value("Item", "Furniture and Fixtures - Asset", "customs_tariff_number"),
			"94033010",
		)
		self.assertEqual((get_balance(self.furniture), get_balance(self.accumulated)), (120000, -30000))

	def test_posting_without_item_rights_names_the_role(self):
		frappe.set_user("Administrator")
		frappe.get_doc("User", USER).remove_roles("Item Manager")
		frappe.set_user(USER)
		self.upload([("Office Desk", "Furniture and Fixtures", "2024-04-01", 120000, 30000, "SLM", 10)])
		self.assertEqual(
			self.checks()["blocker"],
			"Posting makes Item records, which needs the Item Manager role. Ask an Administrator to add it in User.",
		)
		frappe.set_user("Administrator")
		make_user(USER, "Item Manager")
		frappe.set_user(USER)

	def test_hand_depreciation_moves_from_the_line_it_was_credited_to(self):
		# credited to the company's line for a category at net value
		self.book_by_hand(4000, "2026-02-28", self.accumulated)
		# credited to the asset line of a category kept at cost
		self.book_by_hand(3000, "2026-02-27", self.furniture)
		self.upload(
			[
				("Rapier Loom", "Plant and Machinery", "2022-07-15", 120000, 89000, "SLM", 5),
				("Office Desk", "Furniture and Fixtures", "2024-04-01", 120000, 30000, "SLM", 10),
			]
		)
		self.doc.mark_asset_accounts()
		self.doc.spread_hand_booked()
		self.post()
		self.assertEqual(
			[(row.asset_name, row.hand_booked) for row in self.doc.fixed_assets],
			[("Rapier Loom", 4000), ("Office Desk", 3000)],
		)
		moved = get_account(self.company, account_name=MOVED)
		self.assertEqual(
			(
				get_balance(self.plant),
				get_balance(moved),
				get_balance(self.furniture),
				get_balance(self.accumulated),
				get_balance(get_account(self.company, account_type="Temporary")),
			),
			(120000, -93000, 120000, -33000, 0),
		)
		self.assertTrue(self.match()["matches"])

		# undo the loom: the desk's move is made again on its own
		self.doc.undo_fixed_assets("fixed_asset_schedule.xlsx", [self.doc.fixed_assets[0].asset])
		self.assertEqual(
			(
				get_balance(self.plant),
				get_balance(moved),
				get_balance(self.furniture),
				get_balance(self.accumulated),
			),
			(31000, 0, 120000, -37000),
		)

	def test_hand_depreciation_after_the_first_post_is_not_counted_again(self):
		self.upload([("Desk 1", "Furniture and Fixtures", "2024-01-01", 60000, 15000, "SLM", 10)], "one.xlsx")
		self.post()
		entry = self.book_by_hand(1000, "2026-02-28", self.accumulated)
		self.upload([("Desk 2", "Furniture and Fixtures", "2024-01-01", 60000, 15000, "SLM", 10)], "two.xlsx")
		checks = self.checks()
		self.assertEqual((checks["hand"]["amount"], checks["blocker"]), (0, None))
		self.post()
		self.assertEqual([row.hand_booked for row in self.doc.fixed_assets], [0, 0])
		match = self.match()
		self.assertEqual((match["hand_after"], match["books_depreciation"]), (1000, 30000))
		# named to the user to cancel; the step waits for it
		self.assertEqual(
			match["hand_after_entries"],
			[
				{
					"name": entry,
					"posting_date": getdate("2026-02-28"),
					"amount": 1000,
					"accounts": [self.depreciation, self.accumulated],
				}
			],
		)
		self.assertFalse(self.asset_step()["done"])

		frappe.get_doc("Journal Entry", entry).cancel()
		self.assertEqual(self.match()["hand_after_entries"], [])
		self.assertTrue(self.asset_step()["done"])

	def test_hand_depreciation_counted_and_then_cancelled_asks_to_undo(self):
		entry = self.book_by_hand(5000, "2026-02-28", self.accumulated)
		self.upload([("Desk 1", "Furniture and Fixtures", "2024-01-01", 60000, 15000, "SLM", 10)], "one.xlsx")
		self.doc.spread_hand_booked()
		self.post()
		frappe.get_doc("Journal Entry", entry).cancel()
		self.upload([("Desk 2", "Furniture and Fixtures", "2024-01-01", 60000, 15000, "SLM", 10)], "two.xlsx")
		checks = self.checks()
		self.assertEqual((checks["reds"][0]["key"], checks["reds"][0]["rows"]), ("overcounted", [1]))
		self.assertRaisesRegex(
			frappe.ValidationError, "Nothing to spread|left to share", self.doc.spread_hand_booked
		)

	def test_a_category_checks_against_its_own_draft_lines_before_they_are_posted(self):
		group = frappe.get_cached_value("Account", self.furniture, "parent_account")
		computers = add_account(self.company, "Computers", "Fixed Asset", "Asset", parent=group)
		own_line = add_account(
			self.company,
			"Acc. Depreciation - Computers",
			"Accumulated Depreciation",
			"Asset",
			parent=frappe.get_cached_value("Account", self.accumulated, "parent_account"),
		)
		capital = get_account(self.company, root_type="Equity", account_type=("!=", "Temporary"))
		self.doc.save_opening_balances(
			[
				{"account": computers, "debit": 50000},
				{"account": own_line, "credit": 10000},
				{"account": capital, "credit": 40000},
			]
		)
		self.upload([("Laptop", "Computers", "2025-04-01", 50000, 10000, "SLM", 3)])
		self.assertEqual(self.doc.asset_categories[0].accumulated_account, own_line)
		checks = self.checks()
		self.assertEqual((checks["off"], checks["depreciation_off"]), ({}, {}))

	def test_posted_assets_off_from_their_line_finish_the_step(self):
		self.upload([("Office Desk", "Furniture and Fixtures", "2024-04-01", 100000, 25000, "SLM", 10)])
		self.assertEqual(self.checks()["off"], {self.furniture: 20000})
		self.post()
		self.assertFalse(self.match()["matches"])
		self.assertTrue(self.asset_step()["done"])

	def test_untyped_depreciation_ledgers_are_guessed_and_marked(self):
		frappe.db.set_value(
			"Company",
			self.company,
			{"accumulated_depreciation_account": None, "depreciation_expense_account": None},
		)
		for account in (self.accumulated, self.depreciation):
			frappe.db.set_value("Account", account, "account_type", "")
		frappe.clear_cache()
		self.upload([("Office Desk", "Furniture and Fixtures", "2024-04-01", 120000, 30000, "SLM", 10)])
		self.doc.reload()
		category = self.doc.asset_categories[0]
		self.assertEqual(
			(category.accumulated_account, category.depreciation_account),
			(self.accumulated, self.depreciation),
		)
		self.assertEqual(self.checks()["blocker"], "Mark the type of 2 accounts first.")
		self.assertEqual(
			sorted(self.doc.mark_asset_accounts()), sorted([self.accumulated, self.depreciation])
		)
		self.assertEqual(
			frappe.get_all(
				"Account",
				filters={"name": ("in", [self.accumulated, self.depreciation])},
				pluck="account_type",
				order_by="name",
			),
			sorted(
				["Accumulated Depreciation", "Depreciation"],
				key=lambda account_type: [self.accumulated, self.depreciation][
					["Accumulated Depreciation", "Depreciation"].index(account_type)
				],
			),
		)
		self.assertIsNone(self.checks()["blocker"])

	def test_a_big_undo_runs_in_the_background(self):
		self.upload(
			[
				("Desk 1", "Furniture and Fixtures", "2024-04-01", 60000, 15000, "SLM", 10),
				("Desk 2", "Furniture and Fixtures", "2024-04-01", 60000, 15000, "SLM", 10),
			]
		)
		self.post()
		with patch.object(posted_entries, "BACKGROUND_FROM", 2):
			self.assertEqual(self.doc.undo_fixed_assets("fixed_asset_schedule.xlsx"), "queued")
		self.doc.reload()
		self.assertEqual((len(self.doc.fixed_assets), len(self.doc.undone_assets)), (0, 2))

	def test_schedules_end_on_the_value_left_and_a_row_takes_only_its_own_method(self):
		self.set_migration_date("2026-07-01")
		frappe.set_user("Administrator")
		computers = add_account(
			self.company,
			"Computers",
			"Fixed Asset",
			"Asset",
			parent=frappe.db.get_value("Account", self.furniture, "parent_account"),
		)
		frappe.set_user(USER)
		self.upload(
			[
				# the old books counted the month bought in: 17 months of ₹1,500 till June
				("Racks", "Furniture and Fixtures", "2025-02-27", 180000, 25500, "SLM", 10),
				("Lathe", "Furniture and Fixtures", "2022-01-01", 100000, 60000, "WDV", None, 40),
				("Drill", "Furniture and Fixtures", "2022-01-01", 10000, 6000, "WDV", None, 40),
				("Router", "Computers", "2023-07-18", 36000, 35000, "SLM", 3),
			]
		)
		# nothing about periods booked this year until the question is answered
		self.assertEqual([red["key"] for red in self.checks()["reds"]], ["answer"])
		# the category takes the method most rows have, and only their life and rate
		self.assertEqual(
			[
				(row.method, row.life, row.rate)
				for row in self.doc.asset_categories
				if row.asset_account == self.furniture
			],
			[("Written Down Value", 0, 40)],
		)
		row = self.doc.append(
			"opening_balances", {"account": self.depreciation, "debit": 9000, "status": "Posted"}
		)
		row.db_insert()
		self.doc.save_fixed_assets(
			[row.as_dict() for row in self.doc.fixed_assets], depreciation_booked="Only at year end"
		)
		checks = self.checks()
		self.assertEqual(checks["reds"][0]["key"], "booked_this_year")
		# the category's life is the straight line row's; the written down value row runs by its rate
		self.assertEqual(checks["greys"]["life_from_rate"], 2)

		self.doc.save_fixed_assets(
			[row.as_dict() for row in self.doc.fixed_assets], depreciation_booked="Every month"
		)
		self.assertIsNone(self.checks()["blocker"])
		self.post()
		rows = {row.asset_name: row.asset for row in self.doc.fixed_assets}
		self.assertEqual(flt(sum(amount for date, amount in self.schedule(rows["Racks"])), 2), 154500)
		# its life ends mid-month: the last part ends there
		self.assertEqual(self.schedule(rows["Router"]), [("2026-07-17", 1000)])
		self.assertEqual(
			(get_balance(self.furniture), get_balance(computers), get_balance(self.accumulated)),
			(120000, 0, -30000),
		)

	def test_an_upload_replaces_drafts_of_the_same_asset_and_leaves_out_posted_ones(self):
		self.upload([("Desk 1", "Furniture and Fixtures", "2024-01-01", 60000, 15000, "SLM", 10)], "one.xlsx")
		self.post()
		self.upload(
			[
				("Desk 1", "Furniture and Fixtures", "2024-01-01", 60000, 15000, "SLM", 10),
				("Desk 2", "Furniture and Fixtures", "2024-01-01", 60000, 15000, "SLM", 10),
			],
			"two.xlsx",
		)
		self.doc.reload()
		self.assertEqual([row.asset_name for row in fixed_assets.get_drafts(self.doc)], ["Desk 2"])
		self.doc.save_fixed_assets([{**fixed_assets.get_drafts(self.doc)[0].as_dict(), "hand_booked": 700}])

		# the step's own sheet, back under the name a second upload of it gets
		fixed_assets.download_fixed_assets(self.company)
		file_url = (
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "two (1).xlsx",
					"content": frappe.response["filecontent"],
					"is_private": 1,
				}
			)
			.insert()
			.file_url
		)
		self.doc.reload()
		self.assertEqual(self.doc.import_fixed_assets(file_url), {"added": 1, "replaced": 1, "posted": 0})
		self.doc.reload()
		self.assertEqual(
			[(row.asset_name, row.hand_booked) for row in fixed_assets.get_drafts(self.doc)],
			[("Desk 2", 700)],
		)

	def test_hand_depreciation_on_a_category_s_own_line_stays_with_that_category(self):
		frappe.set_user("Administrator")
		group = frappe.db.get_value("Account", self.furniture, "parent_account")
		add_account(self.company, "Computers", "Fixed Asset", "Asset", parent=group)
		lines = [
			add_account(
				self.company,
				f"Accumulated Depreciation - {name}",
				"Accumulated Depreciation",
				"Asset",
				parent=group,
			)
			for name in ("Furniture and Fixtures", "Computers")
		]
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Depreciation Entry",
				"company": self.company,
				"posting_date": "2026-01-31",
				"accounts": [
					{
						"account": self.depreciation,
						"debit_in_account_currency": 5000,
						"cost_center": frappe.get_cached_value("Company", self.company, "cost_center"),
					},
					{"account": lines[0], "credit_in_account_currency": 3000},
					{"account": lines[1], "credit_in_account_currency": 2000},
				],
			}
		).insert()
		je.submit()
		frappe.set_user(USER)
		self.upload(
			[
				("Desk", "Furniture and Fixtures", "2024-04-01", 120000, 30000, "SLM", 10),
				("PC", "Computers", "2024-04-01", 60000, 35000, "SLM", 3),
			]
		)
		# an old guess stored the computers' line on the furniture: a save puts its own back
		self.doc.reload()
		furniture = next(row for row in self.doc.asset_categories if row.asset_account == self.furniture)
		furniture.accumulated_account = lines[1]
		self.doc.save()
		self.assertEqual(furniture.accumulated_account, lines[0])
		# the trial balance put the furniture's depreciation on the company's line, not its own
		self.assertEqual(self.checks()["depreciation_off"], {lines[0]: -30000, lines[1]: -35000})
		self.doc.spread_hand_booked()
		self.post()
		self.assertEqual(
			[(row.asset_name, row.hand_booked, row.net_value_entry) for row in self.doc.fixed_assets],
			[("Desk", 3000, None), ("PC", 2000, None)],
		)
		self.assertEqual(
			(get_balance(lines[0]), get_balance(lines[1]), get_balance(self.accumulated)),
			(-3000, -2000, -30000),
		)

	def test_straight_line_runs_at_cost_over_life_and_ends_with_the_life(self):
		self.upload(
			[
				# ₹30,000 a year: part of 2024 and all of 2025 booked
				("Dell", "Furniture and Fixtures", "2024-07-12", 90000, 44180.33, "SLM", 3),
				# ₹1,00,000 a year: its life ends in September
				("Server", "Furniture and Fixtures", "2023-10-01", 300000, 225205.48, "SLM", 3),
			]
		)
		self.assertIsNone(self.checks()["blocker"])
		self.post()
		rows = {row.asset_name: row.asset for row in self.doc.fixed_assets}
		self.assertEqual(self.schedule(rows["Dell"]), [("2026-12-31", 30000), ("2027-07-11", 15819.67)])
		self.assertEqual(self.schedule(rows["Server"]), [("2026-09-30", 74794.52)])
		# the life as ERPNext counts it, so a sale or repair that remakes the schedule keeps it
		self.assertEqual(
			frappe.db.get_value(
				"Asset Finance Book", {"parent": rows["Dell"]}, "total_number_of_depreciations"
			),
			3,
		)
		self.assertEqual((get_balance(self.furniture), get_balance(self.accumulated)), (120000, -30000))

	def test_sheet_account_names_match_the_chart_and_picks_stay_on_upload_again(self):
		frappe.set_user("Administrator")
		group = frappe.db.get_value("Account", self.furniture, "parent_account")
		# a QuickBooks chart import names its repeated "Original Cost" after the parent
		computers = add_account(
			self.company, "Original Cost (Computer Equipment)", "Fixed Asset", "Asset", parent=group
		)
		frappe.set_user(USER)
		rows = [
			("Laptop", "Computer Equipment:Original Cost", "2024-04-01", 60000, 15000, "SLM", 4),
			("Desk", "Furniture & Fixture", "2024-04-01", 60000, 15000, "SLM", 10),
			("Lathe", "Workshop Gear", "2024-04-01", 60000, 15000, "SLM", 10),
		]
		self.upload(rows, "one.xlsx")
		self.doc.reload()
		drafts = [row.as_dict() for row in self.doc.fixed_assets]
		self.assertEqual([row["asset_account"] for row in drafts], [computers, self.furniture, None])
		self.doc.save_fixed_assets([*drafts[:2], {**drafts[2], "asset_account": self.furniture}])
		self.upload(rows, "one (1).xlsx")
		self.doc.reload()
		self.assertEqual(
			[row.asset_account for row in self.doc.fixed_assets], [computers, self.furniture, self.furniture]
		)

	def test_undone_assets_go_back_to_drafts_and_leave_the_undone_list_when_posted_again(self):
		self.upload(
			[
				("Desk 1", "Furniture and Fixtures", "2024-04-01", 60000, 15000, "SLM", 10),
				("Desk 2", "Furniture and Fixtures", "2024-04-01", 60000, 15000, "SLM", 10),
			]
		)
		self.post()
		assets = [row.asset for row in self.doc.fixed_assets]
		self.doc.undo_fixed_assets("fixed_asset_schedule.xlsx")
		# gone with their log, so an asset posted again under a name starts its own
		self.assertFalse(frappe.get_all("Asset", filters={"name": ("in", assets)}))
		self.assertFalse(frappe.get_all("Asset Activity", filters={"asset": ("in", assets)}))
		self.assertEqual(get_balance(self.accumulated), -30000)

		self.doc.reload()
		self.doc.restore_undone_assets("fixed_asset_schedule.xlsx")
		self.doc.reload()
		self.assertEqual(
			([row.asset_name for row in fixed_assets.get_drafts(self.doc)], len(self.doc.undone_assets)),
			(["Desk 1", "Desk 2"], 0),
		)
		self.post()
		self.assertEqual(
			sorted(
				frappe.get_all(
					"Asset Activity",
					filters={"asset": ("in", [row.asset for row in self.doc.fixed_assets])},
					pluck="subject",
				)
			),
			["Asset created", "Asset created", "Asset submitted", "Asset submitted"],
		)
		self.assertEqual((get_balance(self.furniture), get_balance(self.accumulated)), (120000, -30000))

		# undone again, uploaded again under another name and posted: not undone any more
		self.doc.undo_fixed_assets("fixed_asset_schedule.xlsx", [self.doc.fixed_assets[0].asset])
		self.upload(
			[("Desk 1", "Furniture and Fixtures", "2024-04-01", 60000, 15000, "SLM", 10)], "again.xlsx"
		)
		self.post()
		self.assertEqual(len(self.doc.undone_assets), 0)

	def test_sheet_checks_catch_a_bad_rate_a_shared_name_and_a_wrong_account_type(self):
		frappe.set_user("Administrator")
		group = frappe.db.get_value("Account", self.furniture, "parent_account")
		for name in ("Lab Gear", "Lab Gear A/c"):
			add_account(self.company, name, "Fixed Asset", "Asset", parent=group)
		frappe.set_user(USER)
		self.upload(
			[
				("Desk", "Furniture & Fixtures A/c", "2024-04-01", 60000, 15000, "WDV", None, 150),
				("Printer", "Lab Gear Account", "2024-04-01", 20000, 5000, "SLM", 5),
			]
		)
		self.doc.reload()
		# "A/c" is not part of the name; two ledgers that mean the same leave the pick to the user
		self.assertEqual([row.asset_account for row in self.doc.fixed_assets], [self.furniture, None])
		self.doc.save_fixed_assets(
			[
				self.doc.fixed_assets[0].as_dict(),
				{**self.doc.fixed_assets[1].as_dict(), "asset_account": self.accumulated},
			]
		)
		reds = {red["key"]: red["rows"] for red in self.checks()["reds"]}
		self.assertEqual((reds["rate"], reds["wrong_type"]), ([1], [self.accumulated]))

	def test_undoing_one_upload_keeps_another_uploads_undone_asset_of_the_same_name(self):
		self.upload([("Desk", "Furniture and Fixtures", "2024-04-01", 60000, 15000, "SLM", 10)], "one.xlsx")
		self.post()
		self.doc.undo_fixed_assets("one.xlsx")
		self.upload([("Desk", "Furniture and Fixtures", "2025-04-01", 60000, 6000, "SLM", 10)], "two.xlsx")
		self.post()
		self.doc.undo_fixed_assets("two.xlsx")
		self.doc.reload()
		self.assertEqual(
			sorted((row.source, str(row.purchase_date)) for row in self.doc.undone_assets),
			[("one.xlsx", "2024-04-01"), ("two.xlsx", "2025-04-01")],
		)

	def test_purchase_dates_are_read_the_way_their_column_writes_them(self):
		# 03/25 can only be month first, so 01/04 is 4 January, whatever the site's format
		self.upload(
			[
				("Desk", "Furniture and Fixtures", "03/25/2024", 60000, 15000, "SLM", 10),
				("Chair", "Furniture and Fixtures", "01/04/2024", 60000, 15000, "SLM", 10),
			]
		)
		self.doc.reload()
		self.assertEqual(
			[str(row.purchase_date) for row in self.doc.fixed_assets], ["2024-03-25", "2024-01-04"]
		)
