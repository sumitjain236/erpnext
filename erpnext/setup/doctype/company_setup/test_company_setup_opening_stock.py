# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.setup.doctype.company_setup.opening_invoices import add_account, get_temporary_account
from erpnext.setup.doctype.company_setup.test_company_setup import (
	get_account,
	get_balance,
	get_setup,
	make_company,
	make_stock_item,
	make_user,
	make_xlsx_file,
	step_status,
)
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.tests.utils import ERPNextTestSuite


class TestOpeningStock(ERPNextTestSuite):
	"""Items, Warehouses and Opening stock on the Stock tab."""

	def setUp(self):
		self.enterContext(self.change_settings("Accounts Settings", {"delete_linked_ledger_entries": 1}))
		self.company = make_company()
		frappe.db.set_value("Company", self.company, "enable_perpetual_inventory", 1)
		self.yarn = make_stock_item("_Test Setup Yarn", "Kg")
		self.vest = make_stock_item("_Test Setup Vest", "Nos", {"Box": 12})
		self.stock_in_hand = get_account(self.company, account_type="Stock")
		self.job_line = add_account(
			self.company,
			"Stock with Job Workers",
			"Stock",
			"Asset",
			parent=frappe.db.get_value("Account", self.stock_in_hand, "parent_account"),
		)
		self.stores = frappe.db.get_value("Warehouse", {"company": self.company, "warehouse_name": "Stores"})
		frappe.db.set_value("Warehouse", self.stores, "account", self.stock_in_hand)
		user = make_user("onboarding.stock@example.com", "Accounts Manager")
		for role in ("Stock Manager", "Purchase Master Manager", "Item Manager"):
			make_user(user, role)
		frappe.set_user(user)
		self.doc = frappe.get_doc("Company Setup", self.company)
		self.doc.migration_date = "2026-04-01"
		self.doc.sheet_accounts = {"Stock": {self.stock_in_hand: 41000, self.job_line: 5000}}
		self.doc.save()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.enable_perpetual_inventory = {}
		super().tearDown()

	def godown_summary(self) -> str:
		"""A Tally Godown Summary: items indented under their stock group, under the godown."""
		return make_xlsx_file(
			"godown_summary.xlsx",
			[
				("Kaveri Textiles", None, None, None),
				("Godown Summary", None, None, None),
				("Particulars", "Closing Balance", None, None),
				(None, "Quantity", "Rate", "Value"),
				("Stores", None, None, None, 0),
				("Yarn", None, None, None, 1),
				("_Test Setup Yarn", "100 Kg", "285.00", 28500, 2),
				("Garments", None, None, None, 1),
				("_Test Setup Vest", "10 Box", "1200", 12000, 2),
				("_Test Setup Spare", "2.5 Nos", "200", 500, 2),
				("Grand Total", None, None, 41000, 0),
			],
			indent_column=1,
		)

	def job_worker_sheet(self, challan: bool = True) -> str:
		return make_xlsx_file(
			"job_worker_stock.xlsx",
			[
				("Item", "Warehouse", "Qty", "Unit", "Value", "Challan no", "Challan date", "Return due"),
				(
					"_Test Setup Yarn",
					"Velan Job Works",
					20,
					"Kg",
					5000,
					"VJ/1" if challan else None,
					"2026-02-12",
					"2027-02-12",
				),
			],
		)

	def checks(self) -> dict:
		self.doc.reload()
		return self.doc.get_opening_stock()

	def test_opening_stock_posts_per_warehouse_against_temporary_opening(self):
		temporary = get_temporary_account(self.company, create=True)
		self.assertIn("opening_stock", step_status(get_setup(self.company)))
		frappe.db.set_value("Company", self.company, "enable_perpetual_inventory", 0)
		frappe.local.enable_perpetual_inventory = {}
		result = self.doc.import_opening_stock(self.godown_summary())
		self.assertEqual((result["read"], result["added"]), (3, 3))
		rows = {row.item_name: row for row in self.doc.opening_stock}
		self.assertEqual(
			(rows[self.yarn].warehouse, rows[self.yarn].qty, rows[self.yarn].uom, rows[self.yarn].amount),
			(self.stores, 100, "Kg", 28500),
		)
		self.assertEqual(rows["_Test Setup Vest"].stock_group, "Garments")
		# perpetual inventory is needed, and turned on from here
		self.assertIn("perpetual inventory", self.checks()["blocker"])
		self.doc.turn_on_perpetual_inventory()
		checks = self.checks()
		self.assertEqual(checks["reds"][0]["key"], "missing_items")
		self.assertEqual(checks["blocker"], "1 item is not in ERPNext. Import it in Items first.")

		make_stock_item("_Test Setup Spare", "Nos")
		self.assertEqual(self.checks()["blocker"], "1 row has a part quantity for a whole-number unit (Nos).")
		self.doc.save_opening_stock(
			[
				row.as_dict() | ({"qty": 2, "amount": 400} if row.item_name == "_Test Setup Spare" else {})
				for row in self.doc.opening_stock
			]
		)

		# the job worker's warehouse is not in ERPNext yet, nor the job worker
		self.doc.import_opening_stock(self.job_worker_sheet(challan=False))
		self.assertEqual(
			self.checks()["blocker"], "1 warehouse is not in ERPNext. Add it in Warehouses first."
		)
		window = self.doc.get_warehouses()
		new = next(row for row in window["rows"] if not row["warehouse"])
		self.assertEqual(
			(new["warehouse_name"], new["kind"], new["job_worker"], new["account"], new["new_job_worker"]),
			("Velan Job Works", "Job worker", "Velan Job Works", self.job_line, True),
		)
		self.doc.save_warehouses(window["rows"])
		self.assertTrue(frappe.db.exists("Supplier", "Velan Job Works"))
		velan = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Velan Job Works"}
		)
		self.assertEqual(frappe.db.get_value("Warehouse", velan, "account"), self.job_line)
		self.assertEqual(self.checks()["blocker"], "1 job worker row needs challan details.")
		self.doc.save_opening_stock(
			[
				row.as_dict() | ({"challan_no": "VJ/1"} if row.challan_date else {})
				for row in self.doc.opening_stock
			]
		)
		self.assertIsNone(self.checks()["blocker"])
		self.assertEqual(self.checks()["off"], 100)

		self.doc.post_opening_stock()
		self.doc.reload()
		self.assertEqual({row.status for row in self.doc.opening_stock}, {"Posted"})
		# one reconciliation per upload and warehouse; a box is 12 vests, the value kept
		entries = {row.source: row.stock_entry for row in self.doc.opening_stock}
		self.assertEqual(len(set(entries.values())), 2)
		vest = frappe.db.get_value(
			"Stock Ledger Entry",
			{"item_code": self.vest, "warehouse": self.stores, "is_cancelled": 0},
			["qty_after_transaction", "stock_value_difference", "posting_date"],
			as_dict=True,
		)
		self.assertEqual(
			(vest.qty_after_transaction, vest.stock_value_difference, str(vest.posting_date)),
			(120, 12000, "2026-03-31"),
		)
		self.assertEqual(
			(get_balance(self.stock_in_hand), get_balance(self.job_line), get_balance(temporary)),
			(40900, 5000, -45900),
		)
		self.assertEqual(step_status(get_setup(self.company))["opening_stock"], "Not Started")

		# posting the same rows again leaves them out
		result = self.doc.import_opening_stock(self.job_worker_sheet())
		self.assertEqual((result["skipped"], result["differ"]), (1, 0))

		# the stock line is off by ₹100: post the difference, with the capital
		self.doc.reload()
		self.doc.run_method("onload")
		self.assertEqual(
			self.doc.get_onload().temporary_parts,
			[{"label": "stock difference", "amount": 100, "to_come": False, "over": False}],
		)
		self.assertEqual([line["difference"] for line in self.doc.get_stock_difference()["lines"]], [100])
		entry = self.doc.post_stock_difference()
		difference = get_account(self.company, account_name="Opening Stock Difference")
		self.assertEqual(frappe.db.get_value("Account", difference, "report_type"), "Balance Sheet")
		self.assertEqual((get_balance(difference, entry), get_balance(temporary)), (100, -46000))
		doc = get_setup(self.company)
		self.assertEqual(step_status(doc)["opening_stock"], "Done")
		doc.run_method("onload")
		self.assertTrue(doc.get_onload().stock_match["matches"])

		# the job worker's warehouse has no later entries: it is undone, the difference is on another line
		self.doc.reload()
		self.doc.undo_opening_stock("job_worker_stock.xlsx", [velan])
		self.assertEqual(frappe.db.get_value("Journal Entry", entry, "docstatus"), 1)
		self.assertEqual((get_balance(self.job_line), get_balance(temporary)), (0, -41000))
		self.doc.reload()
		self.assertEqual(len(self.doc.undone_stock), 1)

		# a later issue from Stores: the godown can't be undone, its rows are corrected instead
		frappe.set_user("Administrator")
		issue = make_stock_entry(
			item_code=self.yarn,
			qty=10,
			from_warehouse=self.stores,
			company=self.company,
			posting_date="2026-04-10",
		)
		frappe.set_user("onboarding.stock@example.com")
		self.doc.reload()
		self.assertRaisesRegex(
			frappe.ValidationError, "can not be undone", self.doc.undo_opening_stock, "godown_summary.xlsx"
		)
		yarn = next(row for row in self.doc.opening_stock if row.item_code == self.yarn)
		self.assertRaisesRegex(
			frappe.ValidationError,
			"would go below zero",
			self.doc.correct_opening_stock,
			[{"name": yarn.name, "qty": 5, "amount": 1425}],
		)
		self.doc.reload()
		result = self.doc.correct_opening_stock([{"name": yarn.name, "qty": 100, "amount": 30000}])
		self.assertEqual((result.rows, result.amount), (1, 1500))
		# the correction is on Stores' line, so the posted difference is undone first
		self.assertFalse(frappe.db.exists("Journal Entry", entry))
		self.assertEqual(get_balance(temporary), -41000 + 100 - 1500)
		# against Temporary Opening, never Stock Adjustment
		correction = frappe.db.get_value("Company Setup Opening Stock", yarn.name, "correction")
		self.assertEqual(
			set(frappe.get_all("GL Entry", {"voucher_no": correction, "is_cancelled": 0}, pluck="account")),
			{self.stock_in_hand, temporary},
		)
		# the issue's cost is worked out again at the corrected rate; till then Go live points it out
		repost = frappe.get_all(
			"Repost Item Valuation",
			{"company": self.company},
			pluck="name",
			order_by="creation desc",
			limit=1,
		)[0]
		frappe.db.set_value("Repost Item Valuation", repost, "status", "Queued")
		doc = get_setup(self.company)
		doc.run_method("onload")
		self.assertEqual(doc.get_onload().go_live_reminders["stock_reposts"], {"running": 1, "failed": 0})
		frappe.set_user("Administrator")
		run_reposts()
		self.assertEqual(
			frappe.db.get_value(
				"Stock Ledger Entry", {"voucher_no": issue.name, "is_cancelled": 0}, "stock_value_difference"
			),
			-3000,
		)


def run_reposts():
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost_entries

	repost_entries()
