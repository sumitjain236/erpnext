# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from contextlib import contextmanager
from io import BytesIO

import frappe
from frappe.utils import getdate
from openpyxl import load_workbook

from erpnext.accounts.utils import get_fiscal_year
from erpnext.controllers.accounts_controller import get_default_taxes_and_charges
from erpnext.selling.doctype.sales_order.mapper import make_sales_invoice
from erpnext.setup.doctype.company_setup import company_setup_steps
from erpnext.setup.doctype.company_setup import test_company_setup as base
from erpnext.setup.doctype.company_setup.open_orders import download_open_orders, read_order_sheet
from erpnext.setup.doctype.company_setup.opening_invoices import ensure_fiscal_year
from erpnext.setup.doctype.company_setup.test_company_setup import make_bom, make_stock_item
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

	setUp = base.TestOpenOrders.setUp

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

	def test_made_rows_stay_when_a_form_save_leaves_them_out(self):
		[name] = self.make_sales_orders(
			[
				{
					"order_no": "SO-1",
					"party_name": self.customer,
					"item_name": self.fabric,
					"qty": 3,
					"rate": 100,
				}
			]
		)
		doc = frappe.get_doc("Company Setup", self.company)
		doc.open_orders = []
		doc.save()
		doc.reload()
		self.assertEqual([(row.status, row.document) for row in doc.open_orders], [("Made", name)])

	def test_undone_rows_are_listed_and_downloaded(self):
		self.make_sales_orders(
			[
				{
					"order_no": "SO-1",
					"party_name": self.customer,
					"item_name": self.fabric,
					"qty": 3,
					"rate": 100,
				}
			]
		)
		self.doc.undo_open_orders("Sales Order", "open_sales_orders.csv")
		self.assertEqual(
			self.doc.get_made_orders("Sales Order")["undone"],
			[{"source": "open_sales_orders.csv", "count": 1, "qty": 3}],
		)
		download_open_orders(self.company, "Sales Order", "open_sales_orders.csv")
		sheet = load_workbook(BytesIO(frappe.response["filecontent"])).active
		self.assertEqual([cell.value for cell in sheet[2]][:4], ["SO-1", self.customer, self.fabric, 3])

	def test_opening_balances_count_only_drafts_as_ready(self):
		self.doc.append("opening_balances", {"original_account": "Cash", "debit": 100, "status": "Posted"})
		self.doc.append("opening_balances", {"original_account": "Bank", "credit": 100, "status": "Draft"})
		self.assertEqual(
			company_setup_steps.opening_balances(self.doc)["summary"], "1 in draft · 1 need an account"
		)

	def test_undo_is_refused_for_a_billed_order(self):
		[name] = self.make_sales_orders(
			[
				{
					"order_no": "SO-1",
					"party_name": self.customer,
					"item_name": self.fabric,
					"qty": 3,
					"rate": 100,
				}
			]
		)
		with as_administrator():
			invoice = make_sales_invoice(name)
			invoice.update({"posting_date": "2026-04-02", "set_posting_time": 1})
			invoice.insert().submit()
		self.doc.reload()
		self.assertRaisesRegex(
			frappe.ValidationError,
			"1 order already has deliveries, receipts, production, invoices or payments",
			self.doc.undo_open_orders,
			"Sales Order",
			"open_sales_orders.csv",
		)
		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 1)

	def test_an_old_order_is_dated_in_the_first_open_year(self):
		[name] = self.make_sales_orders(
			[
				{
					"order_no": "SO-1",
					"party_name": self.customer,
					"item_name": self.fabric,
					"qty": 3,
					"rate": 100,
					"order_date": "2009-05-01",
				}
			]
		)
		self.assertEqual(
			frappe.db.get_value("Sales Order", name, "transaction_date"),
			getdate(get_fiscal_year("2026-03-31", company=self.company)[1]),
		)

	def test_work_order_asks_for_the_direct_materials_of_its_bom(self):
		with as_administrator():
			cotton = make_stock_item("_Test Setup Cotton", "Kg")
			make_bom(self.yarn, cotton, self.company)
		self.doc.save_open_orders(
			"Work Order", [{"item_name": self.fabric, "qty": 40, "wip_warehouse": self.wip}]
		)
		[name] = self.doc.make_open_orders("Work Order")
		order = frappe.get_doc("Work Order", name)
		self.assertEqual(
			(order.use_multi_level_bom, [row.item_code for row in order.required_items]), (0, [self.yarn])
		)

		# another Fabric work order, with no number, is not taken for the one made
		result = self.doc.save_open_orders(
			"Work Order", [{"item_name": self.fabric, "qty": 25, "wip_warehouse": self.wip}], "wo2.xlsx"
		)
		self.assertEqual(result.skipped, 0)
		[second] = self.doc.make_open_orders("Work Order")
		self.assertEqual(frappe.db.get_value("Work Order", second, "qty"), 25)

	def test_purchase_order_of_a_non_stock_item_found_by_name(self):
		with as_administrator():
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "_Test Setup AMC",
					"item_name": "_Test Setup Annual Maintenance",
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()
			supplier = frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": "_Test Setup Order Supplier"}
			).insert()
		self.doc.save_open_orders(
			"Purchase Order",
			[
				{
					"order_no": "PO-1",
					"party_name": supplier.name,
					"item_name": "_Test Setup Annual Maintenance",
					"qty": 1,
					"rate": 12000,
				}
			],
		)
		[name] = self.doc.make_open_orders("Purchase Order")
		order = frappe.get_doc("Purchase Order", name)
		self.assertEqual(
			(order.docstatus, order.supplier, [(row.item_code, row.qty, row.rate) for row in order.items]),
			(1, supplier.name, [("_Test Setup AMC", 1, 12000)]),
		)

	def test_tally_and_zoho_order_reports_are_read(self):
		tally = base.make_file(
			"sales_orders_tally.csv",
			[
				"Sales Order Outstandings,,,,,,",
				",,,,,,",
				"Date,Order No.,Party's Name,Name of Item,Ordered Qty,Balance Qty,Rate,Due on",
				f'2026-03-02,SO/318,{self.customer},{self.fabric},"1,200 Nos",520 Nos,1295,2026-04-15',
				f"2026-03-10,SO/327,,{self.yarn},50 Kg,30 Kg,318,2026-04-20",
			],
		)
		rows = read_order_sheet(tally)
		self.assertEqual(
			[(r["order_no"], r["party_name"], r["qty"], r["uom"], r["delivery_date"]) for r in rows],
			[
				("SO/318", self.customer, 520, "Nos", "2026-04-15"),
				("SO/327", None, 30, "Kg", "2026-04-20"),
			],
		)
		self.doc.import_open_orders("Sales Order", tally)
		self.assertEqual(
			self.doc.get_open_orders("Sales Order")["blocker"],
			"1 row has no customer. Fill it in, or add a Customer column to the sheet.",
		)

		# Zoho has no qty left: ordered less shipped, and a fully shipped row is left out
		zoho = base.make_file(
			"sales_orders_zoho.csv",
			[
				"SalesOrder Date,SalesOrder Number,Customer Name,Expected Shipment Date,Item Name,Quantity Ordered,Quantity Shipped,Usage unit,Item Price",
				f"2026-03-19,SO-01180,{self.customer},2026-04-08,{self.fabric},30,15,Nos,1085.6",
				f"2026-03-19,SO-01180,{self.customer},2026-04-08,{self.yarn},10,10,Kg,90",
			],
		)
		self.assertEqual(
			[(r["order_no"], r["party_name"], r["qty"], r["uom"], r["rate"]) for r in read_order_sheet(zoho)],
			[("SO-01180", self.customer, 15, "Nos", 1085.6)],
		)

	def test_an_upload_replaces_only_the_drafts_of_its_order_numbers(self):
		row = {"party_name": self.customer, "item_name": self.fabric, "qty": 3, "rate": 100}
		self.doc.save_open_orders(
			"Sales Order",
			[
				row | {"order_no": "SO-1"},
				row | {"order_no": "SO-1", "qty": 4},
				row | {"order_no": "SO-2"},
				row,
			],
			"first.csv",
		)
		sheet = base.make_file(
			"second.csv",
			["Order No,Customer,Item,Qty left,Rate", f"SO-1,{self.customer},{self.fabric},9,100"],
		)
		self.assertEqual(
			self.doc.import_open_orders("Sales Order", sheet), {"added": 1, "skipped": 0, "replaced": 2}
		)
		self.assertEqual(
			sorted((r.order_no or "", r.qty) for r in self.doc.open_orders if r.status == "Draft"),
			[("", 3), ("SO-1", 9), ("SO-2", 3)],
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
							"account_head": base.add_account(
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

	def test_subcontracting_order_keeps_its_rate_and_date(self):
		frappe.db.set_value("Item", self.dyed, "is_sub_contracted_item", 0)
		self.doc.save_warehouses(
			[{"warehouse_name": "Velan Job Works", "kind": "Job worker", "job_worker": "_Test Setup Velan"}]
		)
		row = {
			"order_no": "JWO-41",
			"party_name": "_Test Setup Velan",
			"item_name": self.dyed,
			"qty": 12,
			"rate": 85,
			"order_date": "2026-03-12",
			"service_item": "_Test Setup Dyeing",
		}
		self.doc.save_open_orders("Subcontracting Order", [row])
		self.assertEqual(
			self.doc.get_open_orders("Subcontracting Order")["reds"][0]["key"], "not_subcontracted"
		)
		self.assertEqual(self.doc.mark_items_subcontracted(), [self.dyed])
		# the default BOM also has yarn, added in-house after the job work
		with as_administrator():
			job_work_bom = frappe.db.get_value("Item", self.dyed, "default_bom")
			in_house = frappe.copy_doc(frappe.get_doc("BOM", job_work_bom))
			in_house.append("items", {"item_code": self.yarn, "qty": 1, "rate": 10})
			in_house.is_default = 1
			in_house.insert().submit()
		# 12 dyed take 24 fabric at the job worker
		velan = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Velan Job Works"}
		)
		self.receive(self.fabric, velan, 24)
		self.assertEqual(
			self.doc.get_open_orders("Subcontracting Order")["blocker"],
			"The job worker does not hold all the material its orders need: _Test Setup Yarn. Add it at the job worker in Opening stock. If you add it in-house, give the item a Subcontracting BOM whose BOM leaves it out.",
		)

		# a Subcontracting BOM on the job work's own BOM: only its fabric goes to the job worker
		with as_administrator():
			frappe.get_doc(
				{
					"doctype": "Subcontracting BOM",
					"finished_good": self.dyed,
					"finished_good_bom": job_work_bom,
					"service_item": "_Test Setup Dyeing",
				}
			).insert()
		self.assertFalse(self.doc.get_open_orders("Subcontracting Order")["reds"])
		[name] = self.doc.make_open_orders("Subcontracting Order")
		order = frappe.get_doc("Subcontracting Order", name)
		purchase_order = frappe.get_doc("Purchase Order", order.purchase_order)
		self.assertEqual(
			(
				str(purchase_order.transaction_date),
				purchase_order.items[0].rate,
				[(row.rm_item_code, row.required_qty) for row in order.supplied_items],
			),
			("2026-03-12", 85, [(self.fabric, 24)]),
		)

	def test_job_work_bom_leaves_out_the_material_added_in_house(self):
		self.doc.save_warehouses(
			[{"warehouse_name": "Velan Job Works", "kind": "Job worker", "job_worker": "_Test Setup Velan"}]
		)
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
		self.receive(self.fabric, velan, 24)
		self.doc.save_open_orders(
			"Subcontracting Order",
			[{"order_no": "JWO-41", "party_name": "_Test Setup Velan", "item_name": self.dyed, "qty": 12}],
		)
		self.assertEqual(
			[(row["item_code"], row["held"]) for row in self.doc.get_job_work_bom(self.dyed)["materials"]],
			[(self.fabric, True), (self.yarn, False)],
		)

		result = self.doc.set_job_work_bom(self.dyed, [self.fabric], "_Test Setup Dyeing", 1)
		self.assertFalse(result["short"])
		self.assertFalse(self.doc.get_open_orders("Subcontracting Order")["reds"])
		self.assertEqual(
			frappe.get_all("BOM Item", {"parent": result["bom"]}, pluck="item_code"), [self.fabric]
		)
		self.assertEqual(
			frappe.db.get_value(
				"Subcontracting BOM", {"finished_good": self.dyed, "is_active": 1}, "finished_good_bom"
			),
			result["bom"],
		)

	def save_job_work(self):
		"""A draft subcontracting order of 12 dyed at Velan, rate 85, its fabric at Velan, and a
		Subcontracting BOM of 2 Dyeing for each dyed piece."""
		self.doc.save_warehouses(
			[{"warehouse_name": "Velan Job Works", "kind": "Job worker", "job_worker": "_Test Setup Velan"}]
		)
		velan = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Velan Job Works"}
		)
		self.receive(self.fabric, velan, 24)
		with as_administrator():
			frappe.get_doc(
				{
					"doctype": "Subcontracting BOM",
					"finished_good": self.dyed,
					"finished_good_bom": frappe.db.get_value("Item", self.dyed, "default_bom"),
					"service_item": "_Test Setup Dyeing",
					"service_item_qty": 2,
					"service_item_uom": "Nos",
				}
			).insert()
		self.doc.save_open_orders(
			"Subcontracting Order",
			[
				{
					"order_no": "JWO-41",
					"party_name": "_Test Setup Velan",
					"item_name": self.dyed,
					"qty": 12,
					"rate": 85,
				}
			],
		)

	def test_job_work_line_follows_the_subcontracting_bom(self):
		self.save_job_work()
		[name] = self.doc.make_open_orders("Subcontracting Order")
		line = frappe.get_doc(
			"Purchase Order", frappe.db.get_value("Subcontracting Order", name, "purchase_order")
		).items[0]
		self.assertEqual(
			(line.item_code, line.qty, line.uom, line.rate, line.amount, line.fg_item_qty),
			("_Test Setup Dyeing", 24, "Nos", 42.5, 1020, 12),
		)

	def test_one_subcontracting_order_when_the_purchase_order_makes_one(self):
		self.save_job_work()
		with as_administrator():
			frappe.db.set_single_value("Buying Settings", "auto_create_subcontracting_order", 1)
		try:
			[name] = self.doc.make_open_orders("Subcontracting Order")
		finally:
			with as_administrator():
				frappe.db.set_single_value("Buying Settings", "auto_create_subcontracting_order", 0)
		purchase_order = frappe.db.get_value("Subcontracting Order", name, "purchase_order")
		self.assertEqual(
			frappe.get_all(
				"Subcontracting Order",
				filters={"purchase_order": purchase_order, "docstatus": ("<", 2)},
				fields=["name", "docstatus"],
			),
			[{"name": name, "docstatus": 1}],
		)

	def test_work_order_gets_the_operations_of_its_bom(self):
		with as_administrator():
			if not frappe.db.exists("Workstation", "_Test Setup Loom"):
				frappe.get_doc({"doctype": "Workstation", "workstation_name": "_Test Setup Loom"}).insert()
			if not frappe.db.exists("Operation", "_Test Setup Weaving"):
				frappe.get_doc(
					{"doctype": "Operation", "name": "_Test Setup Weaving", "workstation": "_Test Setup Loom"}
				).insert()
			bom = frappe.copy_doc(frappe.get_doc("BOM", self.bom))
			bom.with_operations = 1
			bom.append(
				"operations",
				{"operation": "_Test Setup Weaving", "workstation": "_Test Setup Loom", "time_in_mins": 30},
			)
			bom.is_default = 1
			bom.insert().submit()
		self.doc.save_open_orders(
			"Work Order",
			[{"order_no": "JC-1", "item_name": self.fabric, "qty": 4, "wip_warehouse": self.wip}],
		)
		[name] = self.doc.make_open_orders("Work Order")
		order = frappe.get_doc("Work Order", name)
		self.assertEqual(
			(order.bom_no, [(row.operation, row.time_in_mins) for row in order.operations]),
			(bom.name, [("_Test Setup Weaving", 120)]),
		)
		self.assertTrue(frappe.get_all("Job Card", {"work_order": name}))

		# undone: its job cards go with it, so nothing stops the delete
		self.doc.reload()
		self.doc.undo_open_orders("Work Order", None)
		self.assertEqual(
			(frappe.db.exists("Work Order", name), frappe.get_all("Job Card", {"work_order": name})),
			(None, []),
		)

	def test_orders_use_the_company_and_item_group_default_warehouses(self):
		finished, stores = (
			frappe.db.get_value("Warehouse", {"company": self.company, "warehouse_name": name})
			for name in ("Finished Goods", "Stores")
		)
		with as_administrator():
			frappe.db.set_value("Company", self.company, "default_warehouse", finished)
			group = frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "_Test Setup Cloth",
					"parent_item_group": "All Item Groups",
					"item_group_defaults": [{"company": self.company, "default_warehouse": stores}],
				}
			).insert()
			frappe.db.set_value("Item", self.yarn, "item_group", group.name)
		self.doc.save_open_orders(
			"Sales Order",
			[
				{"order_no": "SO-1", "party_name": self.customer, "item_name": item, "qty": 1}
				for item in (self.fabric, self.yarn)
			],
		)
		self.assertEqual(
			{row.item_name: row.warehouse for row in self.doc.open_orders},
			{self.fabric: finished, self.yarn: stores},
		)


class TestTaxTemplates(ERPNextTestSuite):
	"""Taxes: removed rows stop taxing, and templates the step did not make are left alone."""

	setUp = base.TestTaxes.setUp

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def get_default(self) -> str | None:
		return get_default_taxes_and_charges("Sales Taxes and Charges Template", company=self.company)[
			"taxes_and_charges"
		]

	def test_removed_rows_disable_their_templates(self):
		row = {"tax_type": "Sales", "title": "CA Sales Tax", "account": self.sales_tax, "rate": 7.25}
		self.doc.save_taxes([row], {"Sales": "CA Sales Tax"})
		template = self.doc.tax_templates[0].template
		self.assertEqual(self.get_default(), template)

		# no taxes charged: new invoices get no tax
		self.doc.save_taxes([])
		self.assertIsNone(self.get_default())
		self.assertEqual(frappe.db.get_value("Sales Taxes and Charges Template", template, "disabled"), 1)

		# renamed: the new template is the default, the old one stays off
		self.doc.save_taxes([row], {"Sales": "CA Sales Tax"})
		self.doc.save_taxes([row | {"title": "California Tax"}], {"Sales": "California Tax"})
		renamed = self.doc.tax_templates[0].template
		self.assertEqual(self.get_default(), renamed)
		self.assertEqual(
			frappe.db.get_value("Sales Taxes and Charges Template", template, ["disabled", "is_default"]),
			(1, 0),
		)

		# saved again unchanged: a line added in ERPNext stays
		with as_administrator():
			doc = frappe.get_doc("Sales Taxes and Charges Template", renamed)
			doc.append(
				"taxes",
				{
					"charge_type": "On Net Total",
					"account_head": self.input_tax,
					"rate": 1,
					"description": "Levy",
				},
			)
			doc.save()
		self.doc.save_taxes([row | {"title": "California Tax"}])
		self.assertEqual(len(frappe.get_doc("Sales Taxes and Charges Template", renamed).taxes), 2)

	def test_rows_of_one_title_make_one_template_with_a_line_each(self):
		group = base.get_account(self.company, is_group=1, account_type="Tax")
		cgst, sgst = (
			base.add_account(self.company, name, "Tax", "Liability", parent=group)
			for name in ("Output CGST", "Output SGST")
		)
		rows = [
			{"tax_type": "Sales", "title": "GST 18%", "account": cgst, "rate": 9},
			{"tax_type": "Sales", "title": "GST 18%", "account": sgst, "rate": 9},
		]
		self.assertRaisesRegex(frappe.ValidationError, "twice", self.doc.save_taxes, [rows[0], rows[0]])
		self.doc.save_taxes(rows, {"Sales": "GST 18%"})
		(template,) = {row.template for row in self.doc.tax_templates}
		self.assertEqual(
			[
				(tax.account_head, tax.rate, tax.description)
				for tax in frappe.get_doc("Sales Taxes and Charges Template", template).taxes
			],
			[(cgst, 9, "Output CGST 9%"), (sgst, 9, "Output SGST 9%")],
		)
		self.assertEqual(self.get_default(), template)
		self.assertEqual(company_setup_steps.get_taxes_status(self.doc).templates, 1)

		# a line removed: the same template keeps the line left
		self.doc.save_taxes(rows[:1], {"Sales": "GST 18%"})
		self.assertEqual(
			[
				(tax.account_head, tax.rate)
				for tax in frappe.get_doc("Sales Taxes and Charges Template", template).taxes
			],
			[(cgst, 9)],
		)

	def test_a_template_the_step_did_not_make_is_not_changed(self):
		with as_administrator():
			vat = frappe.get_doc(
				{
					"doctype": "Sales Taxes and Charges Template",
					"title": "VAT",
					"company": self.company,
					"taxes": [
						{
							"charge_type": "On Net Total",
							"account_head": self.sales_tax,
							"rate": 5,
							"description": "VAT",
						},
						{
							"charge_type": "On Net Total",
							"account_head": self.input_tax,
							"rate": 1,
							"description": "Levy",
						},
					],
				}
			).insert()
		self.assertRaisesRegex(
			frappe.ValidationError,
			"already exists",
			self.doc.save_taxes,
			[{"tax_type": "Sales", "title": "VAT", "account": self.sales_tax, "rate": 5}],
		)
		self.assertEqual(len(frappe.get_doc("Sales Taxes and Charges Template", vat.name).taxes), 2)
