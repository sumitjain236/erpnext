# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt


from unittest.mock import patch

import frappe
from frappe.utils import flt, getdate

from erpnext.setup.doctype.company_setup import (
	fixed_assets,
	posted_entries,
	test_company_setup_fixed_assets,
)
from erpnext.setup.doctype.company_setup import test_company_setup as base
from erpnext.setup.doctype.company_setup.opening_invoices import add_account
from erpnext.setup.doctype.company_setup.test_company_setup import (
	get_account,
	get_balance,
	get_setup,
)
from erpnext.tests.utils import ERPNextTestSuite

USER = "onboarding.assets@example.com"
MOVED = "Accumulated Depreciation - Plant and Machinery"


class TestFixedAssetMoney(ERPNextTestSuite):
	"""Fixed assets: the ledger after each post and undo, and the schedule ERPNext makes from them.
	Same company as TestFixedAssets: Furniture ₹1,20,000 less ₹30,000 depreciation, Plant and
	Machinery at net value ₹31,000, migrated on 1 Jan 2026."""

	setUp = test_company_setup_fixed_assets.TestFixedAssets.setUp
	checks = test_company_setup_fixed_assets.TestFixedAssets.checks

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
		# monthly from 31 Mar 2026: a closed Accounting Period stops posting, unless its role is exempted
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
		self.assertIn("Accounting Period Assets H1 2026", self.checks()["blocker"])
		frappe.db.set_value("Accounting Period", period.name, "exempted_role", "Accounts Manager")
		self.assertNotIn("Assets H1 2026", self.checks()["blocker"] or "")
		frappe.db.delete("Accounting Period", period.name)
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

	def test_asset_category_accounts_hold_after_posting_and_frozen_accounts_stop_undo(self):
		self.upload(
			[
				("Rapier Loom", "Plant and Machinery", "2022-07-15", 120000, 89000, "SLM", 5),
				("Office Desk", "Furniture and Fixtures", "2024-04-01", 120000, 30000, "SLM", 10),
			]
		)
		self.doc.mark_asset_accounts()
		# frozen up to the migration date: the move is dated the day after, as after a closed year
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", "2026-01-01")
		checks = self.checks()
		self.assertEqual((checks["blocker"], checks["net_value_date"]), (None, getdate("2026-01-02")))
		self.post()
		self.assertEqual(get_balance(self.plant), 120000)
		categories = {row.asset_category: row for row in self.doc.asset_categories}
		self.assertEqual(categories["Furniture and Fixtures"].accumulated_account, self.accumulated)

		other = add_account(self.company, "Old Depreciation", "Accumulated Depreciation", "Asset")
		self.assertRaisesRegex(
			frappe.ValidationError,
			"has posted assets",
			self.doc.save_asset_categories,
			[{"asset_account": self.furniture, "accumulated_account": other}],
		)

		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", "2026-01-31")
		self.assertRaisesRegex(
			frappe.ValidationError,
			"Accounts are frozen up to",
			self.doc.undo_fixed_assets,
			"fixed_asset_schedule.xlsx",
		)

		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", None)
		with patch.object(posted_entries, "BACKGROUND_FROM", 2):
			self.assertEqual(self.doc.undo_fixed_assets("fixed_asset_schedule.xlsx"), "queued")
		self.doc.reload()
		self.assertEqual((len(self.doc.fixed_assets), len(self.doc.undone_assets)), (0, 2))
