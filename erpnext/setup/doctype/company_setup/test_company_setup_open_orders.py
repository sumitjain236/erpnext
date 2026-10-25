# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.setup.doctype.company_setup.opening_invoices import ensure_fiscal_year
from erpnext.setup.doctype.company_setup.test_company_setup import (
	make_bom,
	make_company,
	make_customer,
	make_file,
	make_stock_item,
	make_user,
)
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.tests.utils import ERPNextTestSuite


class TestOpenOrders(ERPNextTestSuite):
	"""Open sales, purchase, work and subcontracting orders: made for the qty left, no money moves."""

	def setUp(self):
		self.company = make_company()
		self.yarn = make_stock_item("_Test Setup Yarn", "Kg")
		self.fabric = make_stock_item("_Test Setup Fabric", "Nos")
		self.dyed = make_stock_item("_Test Setup Dyed Fabric", "Nos")
		frappe.db.set_value("Item", self.dyed, "is_sub_contracted_item", 1)
		if not frappe.db.exists("Item", "_Test Setup Dyeing"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "_Test Setup Dyeing",
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()
		self.bom = make_bom(self.fabric, self.yarn, self.company)
		make_bom(self.dyed, self.fabric, self.company)
		self.customer = make_customer("_Test Setup Order Customer")
		self.wip = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Work In Progress"}
		)
		user = make_user("onboarding.order.maker@example.com", "Accounts Manager")
		for role in (
			"Sales Manager",
			"Purchase Manager",
			"Manufacturing User",
			"Stock Manager",
			"Item Manager",
			"Purchase Master Manager",
		):
			make_user(user, role)
		frappe.set_user(user)
		self.doc = frappe.get_doc("Company Setup", self.company)
		self.doc.migration_date = "2026-04-01"
		self.doc.save()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_open_orders_are_made_for_the_qty_left_and_undone(self):
		sheet = make_file(
			"open_sales_orders.csv",
			[
				"Order No,Customer,Item,Qty left,Unit,Rate,Delivery date",
				f"SO-1,{self.customer},_Test Setup Fabric,30,Nos,100,2026-04-20",
				f"SO-1,{self.customer},_Test Setup Yarn,5,Kg,250,2026-04-20",
				"SO-2,_Test Setup Nobody,_Test Setup Fabric,5,Nos,100,",
			],
		)
		self.doc.import_open_orders("Sales Order", sheet)
		self.assertEqual(
			self.doc.get_open_orders("Sales Order")["blocker"],
			"1 customer is not in ERPNext. Import it in Customers first.",
		)
		self.doc.save_open_orders(
			"Sales Order",
			[row.as_dict() for row in self.doc.open_orders if row.party_name == self.customer],
			"open_sales_orders.csv",
		)
		[name] = self.doc.make_open_orders("Sales Order")
		order = frappe.get_doc("Sales Order", name)
		self.assertEqual(
			(order.docstatus, order.customer, [(row.item_code, row.qty) for row in order.items]),
			(1, self.customer, [(self.fabric, 30), (self.yarn, 5)]),
		)
		# nothing posted to the books
		self.assertFalse(frappe.db.exists("GL Entry", {"voucher_no": name}))
		self.doc.undo_open_orders("Sales Order", "open_sales_orders.csv")
		self.assertFalse(frappe.db.exists("Sales Order", name))
		self.doc.reload()
		self.assertEqual(len(self.doc.undone_orders), 2)

		# a work order for the qty left, its material not in the WIP warehouse yet
		self.doc.save_open_orders(
			"Work Order",
			[{"item_name": self.fabric, "qty": 40, "wip_warehouse": self.wip}],
		)
		[name] = self.doc.make_open_orders("Work Order")
		order = frappe.get_doc("Work Order", name)
		self.assertEqual(
			(order.docstatus, order.qty, order.bom_no, order.skip_transfer, order.wip_warehouse),
			(1, 40, self.bom, 0, self.wip),
		)
		self.assertEqual(self.doc.get_boms().boms, 2)
		frappe.db.set_value("Work Order", name, "produced_qty", 5)
		self.doc.reload()
		self.assertRaisesRegex(
			frappe.ValidationError, "already has deliveries", self.doc.undo_open_orders, "Work Order", None
		)

		# a subcontracting order at the job worker's warehouse
		self.doc.save_warehouses(
			[{"warehouse_name": "Velan Job Works", "kind": "Job worker", "job_worker": "_Test Setup Velan"}]
		)
		velan = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Velan Job Works"}
		)
		frappe.set_user("Administrator")
		ensure_fiscal_year("2026-03-31", self.company)
		make_stock_entry(
			item_code=self.fabric,
			qty=24,
			to_warehouse=velan,
			rate=10,
			company=self.company,
			posting_date="2026-03-31",
		)
		frappe.set_user("onboarding.order.maker@example.com")
		self.doc.save_open_orders(
			"Subcontracting Order",
			[
				{
					"order_no": "PO-7",
					"party_name": "_Test Setup Velan",
					"item_name": self.dyed,
					"qty": 12,
					"service_item": "_Test Setup Dyeing",
				}
			],
			"open_subcontracting_orders.xlsx",
		)
		[name] = self.doc.make_open_orders("Subcontracting Order")
		order = frappe.get_doc("Subcontracting Order", name)
		self.assertEqual(
			(
				order.docstatus,
				order.supplier,
				order.supplier_warehouse,
				order.items[0].item_code,
				order.items[0].qty,
			),
			(1, "_Test Setup Velan", velan, self.dyed, 12),
		)
		self.doc.reload()
		self.doc.undo_open_orders("Subcontracting Order", "open_subcontracting_orders.xlsx")
		self.assertFalse(frappe.db.exists("Subcontracting Order", name))
		self.assertFalse(frappe.db.exists("Purchase Order", order.purchase_order))
