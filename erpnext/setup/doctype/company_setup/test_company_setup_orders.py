# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from contextlib import contextmanager

import frappe

from erpnext.setup.doctype.company_setup import company_setup_steps, test_company_setup_open_orders
from erpnext.setup.doctype.company_setup import test_company_setup as base
from erpnext.setup.doctype.company_setup.opening_invoices import add_account, ensure_fiscal_year
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.tests.utils import ERPNextTestSuite


@contextmanager
def as_administrator():
	user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		yield
	finally:
		frappe.set_user(user)


class TestOpenOrderRows(ERPNextTestSuite):
	"""Open orders: units, re-uploads, undo and dates."""

	setUp = test_company_setup_open_orders.TestOpenOrders.setUp

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def make_sales_orders(self, rows: list[dict], source: str = "open_sales_orders.csv") -> list[str]:
		self.doc.save_open_orders("Sales Order", rows, source)
		return self.doc.make_open_orders("Sales Order")

	def test_sheet_unit_is_kept_and_made_rows_are_not_made_again(self):
		with as_administrator():
			item = frappe.get_doc("Item", self.fabric)
			item.append("uoms", {"uom": "Box", "conversion_factor": 10})
			item.sales_uom = "Box"
			item.save()
		row = {
			"order_no": "SO-1",
			"party_name": self.customer,
			"item_name": self.fabric,
			"qty": 30,
			"rate": 100,
		}
		self.doc.save_open_orders("Sales Order", [row | {"uom": "Kg"}], "open_sales_orders.csv")
		self.assertEqual(
			self.doc.get_open_orders("Sales Order")["blocker"],
			f"Unit Kg is not a unit of {self.fabric}. Add it to the item, or fix the sheet.",
		)
		self.doc.save_open_orders(
			"Sales Order", [row | {"uom": "Kg"}, row | {"order_no": "SO-0", "uom": "Litre"}]
		)
		checks = self.doc.get_open_orders("Sales Order")
		self.assertEqual(
			(checks["blocker"], [(red["key"], red["rows"]) for red in checks["reds"]]),
			(
				f"These units are not units of their items: Kg for {self.fabric}, Litre for {self.fabric}. Add them to the items, or fix the sheet.",
				[("unknown_units", [1, 2])],
			),
		)

		[name] = self.make_sales_orders([row | {"uom": "Nos"}])
		self.assertEqual(
			[(item.qty, item.uom, item.stock_qty) for item in frappe.get_doc("Sales Order", name).items],
			[(30, "Nos", 30)],
		)

		# the same sheet again, with one new row: only the new row is a draft
		new = row | {"order_no": "SO-2"}
		result = self.doc.save_open_orders("Sales Order", [row, new], "open_sales_orders.csv")
		self.assertEqual(result.skipped, 1)
		self.assertEqual(
			[r.order_no for r in self.doc.open_orders if r.status != "Made"],
			["SO-2"],
		)

	def receive(self, item: str, warehouse: str, qty: float = 50):
		with as_administrator():
			ensure_fiscal_year("2026-03-31", self.company)
			make_stock_entry(
				item_code=item,
				qty=qty,
				to_warehouse=warehouse,
				rate=10,
				company=self.company,
				posting_date="2026-03-31",
			)

	def test_orders_use_the_warehouse_that_holds_the_item_and_material_in_wip(self):
		finished = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Finished Goods"}
		)
		stores = frappe.db.get_value("Warehouse", {"company": self.company, "warehouse_name": "Stores"})
		self.receive(self.fabric, finished)
		with as_administrator():
			template = frappe.get_doc(
				{
					"doctype": "Sales Taxes and Charges Template",
					"title": "_Test Setup GST",
					"company": self.company,
					"is_default": 1,
					"taxes": [
						{
							"charge_type": "On Net Total",
							"account_head": add_account(
								self.company,
								"_Test Setup GST",
								"Tax",
								"Liability",
								parent=base.get_account(self.company, is_group=1, account_type="Tax"),
							),
							"description": "GST",
							"rate": 18,
						}
					],
				}
			).insert()
		[name] = self.make_sales_orders(
			[
				{"order_no": "SO-1", "party_name": self.customer, "item_name": self.fabric, "qty": 30},
				{"order_no": "SO-1", "party_name": self.customer, "item_name": self.yarn, "qty": 5},
			]
		)
		order = frappe.get_doc("Sales Order", name)
		self.assertEqual(
			(
				[(row.item_code, row.warehouse) for row in order.items],
				order.taxes_and_charges,
				[(row.account_head, row.rate) for row in order.taxes],
			),
			(
				[(self.fabric, finished), (self.yarn, stores)],
				template.name,
				[(template.taxes[0].account_head, 18)],
			),
		)
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{"reference_name": name, "content": ("like", "%SO-1 in your old books%")},
			)
		)
		self.assertEqual(
			company_setup_steps.open_sales_orders(self.doc)["summary"], "1 order · 5 Kg, 30 Nos to deliver"
		)

		# 100 Kg yarn in WIP covers the first job (2 Kg a piece), not the second as well
		self.receive(self.yarn, self.wip, 100)
		jobs = [
			{"order_no": "JC-141", "item_name": self.fabric, "qty": 40, "wip_warehouse": self.wip},
			{"order_no": "JC-146", "item_name": self.fabric, "qty": 25, "wip_warehouse": stores},
		]
		self.doc.save_open_orders("Work Order", jobs, "jobs.xlsx")
		names = self.doc.make_open_orders("Work Order")
		self.assertEqual(
			[
				frappe.db.get_value(
					"Work Order", name, ["skip_transfer", "from_wip_warehouse", "fg_warehouse"]
				)
				for name in names
			],
			[(1, 1, finished), (0, 0, finished)],
		)
		[group] = self.doc.get_made_orders("Work Order")["groups"]
		self.assertEqual(sorted(row["order_no"] for row in group["rows"]), ["JC-141", "JC-146"])

		# undone and uploaded again with no WIP column: the WIP warehouse picked before stays
		self.doc.undo_open_orders("Work Order", "jobs.xlsx", [names[1]])
		self.doc.reload()
		self.doc.save_open_orders("Work Order", [jobs[1] | {"wip_warehouse": None}], "jobs.xlsx")
		self.assertEqual(
			[row.wip_warehouse for row in self.doc.open_orders if row.status == "Draft"], [stores]
		)

	def test_job_work_setup_saves_each_item_and_leaves_out_material_added_in_house(self):
		self.doc.save_warehouses(
			[{"warehouse_name": "Velan Job Works", "kind": "Job worker", "job_worker": "_Test Setup Velan"}]
		)
		frappe.db.set_value("Item", self.fabric, "is_sub_contracted_item", 1)
		# saving the setup marks it subcontracted, so only the setup is asked for
		frappe.db.set_value("Item", self.dyed, "is_sub_contracted_item", 0)
		with as_administrator():
			bom = frappe.copy_doc(
				frappe.get_doc("BOM", frappe.db.get_value("Item", self.dyed, "default_bom"))
			)
			bom.append("items", {"item_code": self.yarn, "qty": 1, "rate": 10})
			bom.is_default = 1
			bom.insert().submit()
		velan = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Velan Job Works"}
		)
		# the 10 yarn go to the fabric order; the dyed order's yarn is added in-house
		self.receive(self.yarn, velan, 10)
		self.receive(self.fabric, velan, 24)
		order = {"order_no": "JWO-41", "party_name": "_Test Setup Velan"}
		self.doc.save_open_orders(
			"Subcontracting Order",
			[{**order, "item_name": self.fabric, "qty": 5}, {**order, "item_name": self.dyed, "qty": 12}],
		)
		reds = self.doc.get_open_orders("Subcontracting Order")["reds"]
		self.assertEqual(
			[red["text"] for red in reds if red["key"] in ("not_subcontracted", "job_work_setup")],
			["2 items need job-work setup: _Test Setup Dyed Fabric, _Test Setup Fabric"],
		)
		setups = self.doc.get_job_work_setups([self.dyed, self.fabric])
		self.assertEqual(
			[[(row["item_code"], row["held"]) for row in setup["materials"]] for setup in setups],
			[[(self.fabric, True), (self.yarn, False)], [(self.yarn, True)]],
		)

		dyed_bom, fabric_bom = self.doc.set_job_work_setups(
			[
				{"item_code": self.dyed, "materials": [self.fabric], "service_item": "_Test Setup Dyeing"},
				{"item_code": self.fabric, "materials": [self.yarn], "service_item": "_Test Setup Dyeing"},
			]
		)
		self.assertFalse(self.doc.get_open_orders("Subcontracting Order")["reds"])
		self.assertEqual(
			(frappe.get_all("BOM Item", {"parent": dyed_bom}, pluck="item_code"), fabric_bom),
			([self.fabric], self.bom),
		)
		self.assertTrue(frappe.db.get_value("Item", self.dyed, "is_sub_contracted_item"))
		# saved again with the same materials: the same BOM, no new copy
		again = self.doc.set_job_work_setups(
			[{"item_code": self.dyed, "materials": [self.fabric], "service_item": "_Test Setup Dyeing"}]
		)
		self.assertEqual(again, [dyed_bom])
