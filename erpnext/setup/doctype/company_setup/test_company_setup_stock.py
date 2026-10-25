# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.setup.doctype.company_setup import opening_stock as stock
from erpnext.setup.doctype.company_setup.opening_invoices import add_account, get_temporary_account
from erpnext.setup.doctype.company_setup.test_company_setup import (
	get_account,
	get_balance,
	make_company,
	make_stock_item,
	make_user,
)
from erpnext.stock.doctype.batch.batch import get_batch_qty
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.tests.utils import ERPNextTestSuite

USER = "onboarding.stock@example.com"


def make_tracked_item(code: str, field: str) -> str:
	"""A stock item in Nos that tracks batches or serial numbers."""
	if not frappe.db.exists("Item", code):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				field: 1,
			}
		).insert()
	return code


class TestOpeningStockCases(ERPNextTestSuite):
	"""Batches, serial numbers, corrections and the checks before posting opening stock."""

	def setUp(self):
		self.company = make_company()
		frappe.db.set_value("Company", self.company, "enable_perpetual_inventory", 1)
		self.yarn = make_stock_item("_Test Setup Yarn", "Kg")
		self.stock_in_hand = get_account(self.company, account_type="Stock")
		self.stores = frappe.db.get_value("Warehouse", {"company": self.company, "warehouse_name": "Stores"})
		frappe.db.set_value("Warehouse", self.stores, "account", self.stock_in_hand)
		make_user(USER, "Accounts Manager")
		for role in ("Stock Manager", "Item Manager"):
			make_user(USER, role)
		frappe.set_user(USER)
		self.doc = frappe.get_doc("Company Setup", self.company)
		self.doc.migration_date = "2026-04-01"
		self.doc.sheet_accounts = {"Stock": {self.stock_in_hand: 1000}}
		self.doc.save()
		self.temporary = get_temporary_account(self.company, create=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.enable_perpetual_inventory = {}
		super().tearDown()

	def save_rows(self, rows: list[dict], source: str = "stock.xlsx"):
		self.doc.reload()
		self.doc.save_opening_stock([{"warehouse_name": "Stores", "source": source} | row for row in rows])
		self.doc.reload()

	def reds(self) -> dict:
		"""The rows of each red, by its key."""
		self.doc.reload()
		return {red["key"]: red["rows"] for red in self.doc.get_opening_stock()["reds"]}

	def row_of(self, **filters):
		self.doc.reload()
		return next(row for row in self.doc.opening_stock if all(row.get(k) == v for k, v in filters.items()))

	def test_batch_and_serial_rows_correct_on_their_own_and_every_correction_is_undone(self):
		self.enterContext(self.change_settings("Accounts Settings", {"delete_linked_ledger_entries": 1}))
		batch_item = make_tracked_item("_Test Setup Batch Item", "has_batch_no")
		serial_item = make_tracked_item("_Test Setup Serial Item", "has_serial_no")
		self.save_rows(
			[
				{"item_name": batch_item, "qty": 10, "amount": 100, "batch_no": "B-1"},
				{"item_name": batch_item, "qty": 20, "amount": 200, "batch_no": "B-2"},
				{"item_name": self.yarn, "qty": 10, "uom": "Kg", "amount": 100},
				{"item_name": serial_item, "qty": 2, "amount": 100, "serial_no": "SR-1, SR-2"},
			]
		)
		self.doc.post_opening_stock()
		self.assertEqual((get_balance(self.stock_in_hand), get_balance(self.temporary)), (500, -500))

		row = self.row_of(batch_no="B-1")
		self.assertRaisesRegex(
			frappe.ValidationError,
			"has a value but no qty",
			self.doc.correct_opening_stock,
			[{"name": row.name, "qty": 0, "amount": 100}],
		)
		self.assertRaisesRegex(
			frappe.ValidationError,
			"can not be below zero",
			self.doc.correct_opening_stock,
			[{"name": row.name, "qty": -1, "amount": 0}],
		)
		# the correction sets that batch only
		self.doc.correct_opening_stock([{"name": row.name, "qty": 12, "amount": 120}])
		batch = frappe.db.get_value("Batch", {"item": batch_item, "batch_id": "B-1"})
		other = frappe.db.get_value("Batch", {"item": batch_item, "batch_id": "B-2"})
		self.assertEqual((get_batch_qty(batch, self.stores), get_batch_qty(other, self.stores)), (12, 20))
		self.assertEqual((get_balance(self.stock_in_hand), get_balance(self.temporary)), (520, -520))

		# a serial row corrects by value only
		serial = self.row_of(item_code=serial_item)
		self.doc.correct_opening_stock([{"name": serial.name, "qty": 2, "amount": 150}])
		self.assertEqual((get_balance(self.stock_in_hand), get_balance(self.temporary)), (570, -570))
		self.doc.reload()
		self.assertRaisesRegex(
			frappe.ValidationError,
			"Change the serial numbers of 1 row",
			self.doc.correct_opening_stock,
			[{"name": serial.name, "qty": 1, "amount": 75}],
		)

		# a wrong correction is fixed by another; undo then cancels both
		yarn = self.row_of(item_code=self.yarn)
		self.doc.correct_opening_stock([{"name": yarn.name, "qty": 12, "amount": 120}])
		self.doc.reload()
		self.doc.correct_opening_stock([{"name": yarn.name, "qty": 15, "amount": 150}])
		corrections = stock.get_corrections(self.row_of(item_code=self.yarn))
		self.assertEqual(len(corrections), 2)
		self.assertEqual((get_balance(self.stock_in_hand), get_balance(self.temporary)), (620, -620))

		self.doc.reload()
		self.doc.undo_opening_stock("stock.xlsx")
		# deleted, with their stock ledger rows
		self.assertFalse(frappe.db.exists("Stock Reconciliation", {"name": ("in", corrections)}))
		self.assertFalse(frappe.db.exists("Stock Ledger Entry", {"voucher_no": ("in", corrections)}))
		self.assertEqual((get_balance(self.stock_in_hand), get_balance(self.temporary)), (0, 0))

	def test_one_item_with_many_batches_posts_in_the_background_and_undoes(self):
		batch_item = make_tracked_item("_Test Setup Batch Item", "has_batch_no")
		self.save_rows(
			[
				{"item_name": batch_item, "qty": 1, "amount": 10, "batch_no": f"B-{i:03}"}
				| ({"expiry_date": "2027-03-31"} if i == 0 else {})
				for i in range(101)
			],
			source="batches.xlsx",
		)
		self.assertEqual(self.doc.post_opening_stock(), "queued")
		self.doc.reload()
		entries = {row.stock_entry for row in self.doc.opening_stock}
		self.assertEqual(len(entries), 1)
		self.assertEqual(frappe.db.get_value("Stock Reconciliation", entries.pop(), "docstatus"), 1)
		self.assertEqual(
			str(frappe.db.get_value("Batch", {"item": batch_item, "batch_id": "B-000"}, "expiry_date")),
			"2027-03-31",
		)
		self.assertEqual((get_balance(self.stock_in_hand), get_balance(self.temporary)), (1010, -1010))

		# as many corrections go to the background too
		self.doc.reload()
		changes = [{"name": row.name, "qty": 1, "amount": 20} for row in self.doc.opening_stock]
		self.assertEqual(self.doc.correct_opening_stock(changes), "queued")
		self.assertEqual((get_balance(self.stock_in_hand), get_balance(self.temporary)), (2020, -2020))

		# undoing the whole upload drops its drafts too
		self.save_rows([{"item_name": self.yarn, "qty": 1, "uom": "Kg", "amount": 10}], source="batches.xlsx")
		result = stock.undo_opening_stock(self.doc, "batches.xlsx")
		self.assertEqual((result.removed, result.unposted), (101, 1))
		self.assertEqual((get_balance(self.stock_in_hand), get_balance(self.temporary)), (0, 0))
		self.doc.reload()
		self.assertEqual((len(self.doc.opening_stock), len(self.doc.undone_stock)), (0, 101))

	def test_rows_that_would_not_reach_the_books_are_red(self):
		vest = make_stock_item("_Test Setup Vest", "Nos")
		serial_item = make_tracked_item("_Test Setup Serial Item", "has_serial_no")
		self.save_rows(
			[
				{"item_name": serial_item, "qty": 3, "amount": 150, "serial_no": "SR-1, SR-2"},
				{"item_name": serial_item, "qty": 1, "amount": 50, "warehouse_name": "Finished Goods"},
				{"item_name": self.yarn, "qty": 0, "uom": "Kg", "amount": 500},
				{
					"item_name": self.yarn,
					"qty": 2,
					"uom": "Box",
					"amount": 100,
					"warehouse_name": "Finished Goods",
				},
				{"item_name": vest, "qty": 5, "amount": 100, "source": "first.xlsx"},
				{"item_name": vest, "qty": 5, "amount": 100, "source": "second.xlsx"},
			]
		)
		reds = self.reds()
		self.assertEqual((len(reds["no_qty"]), len(reds["unknown_units"]), len(reds["repeated"])), (1, 1, 2))
		self.assertEqual((len(reds["serial_count"]), len(reds["no_serials"])), (1, 1))
		self.assertEqual(self.doc.get_opening_stock()["blocker"], "1 row has a value but no qty.")

		# work in progress kept as a balance is not stock too
		wip = add_account(
			self.company,
			"Work in Progress",
			"",
			"Asset",
			parent=frappe.db.get_value("Account", self.stock_in_hand, "parent_account"),
		)
		# capital work in progress is a fixed asset, not stock
		cwip = add_account(
			self.company,
			"Capital Work in Progress",
			"",
			"Asset",
			parent=frappe.db.get_value("Account", self.stock_in_hand, "parent_account"),
		)
		capital = get_account(self.company, root_type="Equity", account_type=("!=", "Temporary"))
		self.doc.save_opening_balances(
			[
				{"account": wip, "debit": 1000},
				{"account": cwip, "debit": 500},
				{"account": capital, "credit": 1500},
			]
		)
		# keeping it as a balance is remembered, so the question is not asked again
		self.doc.run_method("onload")
		self.assertEqual(self.doc.get_onload().stock_like_lines, [{"account": wip, "amount": 1000}])
		self.doc.keep_as_balance(wip)
		self.doc.reload()
		self.doc.run_method("onload")
		self.assertEqual(self.doc.get_onload().stock_like_lines, [])
		self.save_rows([{"item_name": vest, "qty": 5, "amount": 1000, "warehouse_name": "Work In Progress"}])
		self.assertEqual(
			self.doc.get_opening_stock()["blocker"],
			"Work in Progress (₹ 1,000.00) is already in your opening balances, so its row can not post as stock.",
		)

	def test_stock_difference_goes_to_profit_and_loss_when_the_trial_balance_has_it(self):
		rent = get_account(self.company, root_type="Expense")
		capital = get_account(self.company, root_type="Equity", account_type=("!=", "Temporary"))
		self.doc.save_opening_balances([{"account": rent, "debit": 500}, {"account": capital, "credit": 500}])
		self.save_rows([{"item_name": self.yarn, "qty": 10, "uom": "Kg", "amount": 900}])
		self.doc.post_opening_stock()
		self.doc.reload()
		entry = self.doc.post_stock_difference(checked=True)
		difference = get_account(self.company, account_name="Opening Stock Difference")
		self.assertEqual(frappe.db.get_value("Account", difference, "root_type"), "Expense")
		self.assertEqual(
			frappe.db.get_value("Journal Entry", entry, ["voucher_type", "is_opening"]),
			("Journal Entry", "No"),
		)
		self.assertEqual((get_balance(difference, entry), get_balance(self.temporary)), (100, -1000))

		# a closed Accounting Period stops its undo, and a correction that would undo it
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "Accounting Period",
				"period_name": "_Test Setup March",
				"company": self.company,
				"start_date": "2026-03-01",
				"end_date": "2026-03-31",
				"closed_documents": [{"document_type": "Journal Entry", "closed": 1}],
			}
		).insert()
		frappe.set_user(USER)
		row = self.row_of(item_code=self.yarn)
		for method, args in (
			(self.doc.undo_stock_difference, ()),
			(self.doc.correct_opening_stock, ([{"name": row.name, "qty": 10, "amount": 1000}],)),
		):
			self.doc.reload()
			self.assertRaisesRegex(
				frappe.ValidationError, "Accounting Period .* is closed for Journal Entry", method, *args
			)

	def wip_warehouse(self) -> str:
		"""The company's Work In Progress warehouse, with no stock line of its own."""
		warehouse = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Work In Progress"}
		)
		frappe.db.set_value("Warehouse", warehouse, "account", None)
		return warehouse

	def save_warehouses(self, wip_account: str | None = None):
		self.doc.reload()
		self.doc.save_warehouses(
			[
				{
					"warehouse": self.stores,
					"warehouse_name": "Stores",
					"kind": "Our warehouse",
					"account": self.stock_in_hand,
				},
				{
					"warehouse": self.wip_warehouse(),
					"warehouse_name": "Work In Progress",
					"kind": "Work in progress",
					"account": wip_account,
				},
			]
		)

	def test_each_stock_line_must_match_on_its_own(self):
		wip_line = add_account(
			self.company,
			"Work in Progress",
			"Stock",
			"Asset",
			parent=frappe.db.get_value("Account", self.stock_in_hand, "parent_account"),
		)
		self.doc.sheet_accounts = {"Stock": {self.stock_in_hand: 1000, wip_line: 500}}
		self.doc.save()
		# saved before the line was there: the warehouse falls back to the company's stock account
		self.save_warehouses()
		self.save_rows(
			[
				{"item_name": self.yarn, "qty": 10, "uom": "Kg", "amount": 1000},
				{
					"item_name": self.yarn,
					"qty": 5,
					"uom": "Kg",
					"amount": 500,
					"warehouse_name": "Work In Progress",
				},
			]
		)
		self.assertEqual(self.reds()["wrong_lines"], [self.row_of(warehouse_name="Work In Progress").idx])

		# posted on one line, the total matches but the lines do not
		self.save_warehouses(self.stock_in_hand)
		self.assertNotIn("wrong_lines", self.reds())
		self.doc.post_opening_stock()
		self.assertEqual(
			(get_balance(self.stock_in_hand), get_balance(wip_line), get_balance(self.temporary)),
			(1500, 0, -1500),
		)
		self.doc.reload()
		match = stock.get_stock_match(self.doc)
		self.assertEqual((match["off"], match["matches"]), (0, False))
		self.assertEqual(sorted(match["lines_off"]), sorted([self.stock_in_hand, wip_line]))
		self.assertIn("stock is on the wrong accounts", self.doc.get_stock_difference()["blocker"])

	def test_moved_work_in_progress_gets_its_warehouse_and_can_move_back(self):
		wip = add_account(
			self.company,
			"Work in Progress",
			"",
			"Asset",
			parent=frappe.db.get_value("Account", self.stock_in_hand, "parent_account"),
		)
		capital = get_account(self.company, root_type="Equity", account_type=("!=", "Temporary"))
		self.doc.save_opening_balances([{"account": wip, "debit": 500}, {"account": capital, "credit": 500}])
		self.save_warehouses()
		self.doc.reload()
		self.doc.move_to_opening_stock(wip)
		warehouse = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Work In Progress"}
		)
		self.assertEqual(frappe.db.get_value("Warehouse", warehouse, "account"), wip)

		self.doc.reload()
		trial_balance = self.doc.get_opening_stock()["trial_balance"]
		self.assertEqual((trial_balance["movable"], trial_balance["total"]), ([wip], 1500))
		self.doc.move_to_opening_balances(wip)
		self.doc.reload()
		self.assertEqual(frappe.db.get_value("Account", wip, "account_type"), "")
		self.assertIsNone(frappe.db.get_value("Warehouse", warehouse, "account"))
		self.assertEqual(stock.get_stock_lines(self.doc), {self.stock_in_hand: 1000})
		self.assertEqual(self.doc.get_opening_stock()["trial_balance"]["total"], 1000)
		self.assertEqual(
			sorted((row.account, row.debit, row.credit) for row in self.doc.opening_balances),
			sorted([(capital, 0, 500), (wip, 500, 0)]),
		)

	def test_units_convert_rounded_and_a_correction_checks_later_entries_in_stock_units(self):
		boxed = make_stock_item("_Test Setup Boxed Bulb", "Nos", {"Box": 12})
		dozen = make_stock_item("_Test Setup Dozen Pack", "Box", {"Nos": 1 / 12})
		self.save_rows(
			[
				{"item_name": boxed, "qty": 5, "uom": "Box", "amount": 600},
				{"item_name": dozen, "qty": 24, "uom": "Nos", "amount": 240},
			]
		)
		prepared = {row.item_code: row for row in stock.prepare_rows(self.doc, self.doc.opening_stock)}
		self.assertEqual((prepared[boxed].stock_qty, prepared[dozen].stock_qty), (60, 2))
		self.doc.post_opening_stock()
		frappe.set_user("Administrator")
		make_stock_entry(
			item_code=boxed,
			qty=50,
			from_warehouse=self.stores,
			company=self.company,
			posting_date="2026-04-10",
		)
		frappe.set_user(USER)
		row = self.row_of(item_code=boxed)
		self.doc.reload()
		check = self.doc.check_stock_correction([{"name": row.name, "qty": 0, "amount": 0}])
		self.assertEqual(
			[(below["item_code"], below["qty"]) for below in check["below_zero"]], [(boxed, -50)]
		)
		self.assertIn("would go below zero", check["blocker"])

		# with later entries, a correction and an undo need the back-dated stock role
		frappe.set_user("Administrator")
		frappe.db.set_single_value(
			"Stock Settings", "role_allowed_to_create_edit_back_dated_transactions", "System Manager"
		)
		frappe.set_user(USER)
		for method, args in (
			(self.doc.correct_opening_stock, ([{"name": row.name, "qty": 5, "amount": 700}],)),
			(self.doc.undo_opening_stock, ("stock.xlsx",)),
		):
			self.doc.reload()
			self.assertRaisesRegex(frappe.ValidationError, "Ask for the back-dated stock role", method, *args)
