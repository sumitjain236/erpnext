# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.setup.doctype.company_setup.test_company_setup import (
	make_bom,
	make_company,
	make_file,
	make_stock_item,
	make_user,
)
from erpnext.tests.utils import ERPNextTestSuite


class TestCompanySetupBOMs(ERPNextTestSuite):
	"""BOMs: one per item made, sub-assemblies first, submitted and set as default."""

	def setUp(self):
		self.company = make_company()
		self.casting = make_stock_item("_Test Setup Casting", "Nos")
		self.primer = make_stock_item("_Test Setup Primer", "Litre")
		self.hub = make_stock_item("_Test Setup Hub", "Nos")
		self.wheel = make_stock_item("_Test Setup Wheel", "Nos")
		self.drum = make_stock_item("_Test Setup Drum", "Nos")
		self.drum_bom = make_bom(self.drum, self.casting, self.company)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_boms_are_made_sub_assemblies_first_and_set_as_default(self):
		make_user("onboarding.bom.maker@example.com", "Accounts Manager")
		frappe.set_user(make_user("onboarding.bom.maker@example.com", "Manufacturing User"))
		doc = frappe.get_doc("Company Setup", self.company)
		# the item to make is written once, above its materials; the finished item comes first
		sheet = make_file(
			"boms.csv",
			[
				"Item to manufacture,Quantity (Output Qty),Item Code (items),Qty (items),UOM (items),Rate (items)",
				f"{self.wheel},1,{self.hub},1,Nos,640",
				f",,{self.primer},0.1,Litre,285",
				f"{self.hub},2,{self.casting},2,Nos,412",
				f"{self.drum},1,{self.casting},1,Nos,",
			],
		)
		doc.import_boms(sheet)
		checks = doc.get_boms()
		self.assertEqual((checks["to_make"], checks["kept"]["rows"]), (2, [4]))

		self.assertEqual(doc.make_boms(), {"made": 2, "failed": 0, "kept": 1})
		hub_bom = frappe.db.get_value("Item", self.hub, "default_bom")
		wheel = frappe.get_doc("BOM", frappe.db.get_value("Item", self.wheel, "default_bom"))
		self.assertEqual(
			(
				frappe.db.get_value("BOM", hub_bom, ["docstatus", "is_default", "quantity"]),
				(wheel.docstatus, wheel.is_default),
				[(row.item_code, row.qty, row.bom_no) for row in wheel.items],
				frappe.db.get_value("Item", self.drum, "default_bom"),
			),
			(
				(1, 1, 2),
				(1, 1),
				[(self.hub, 1, hub_bom), (self.primer, 0.1, "")],
				self.drum_bom,
			),
		)
		doc.reload()
		self.assertEqual(
			{(row.item_name, row.status) for row in doc.boms},
			{(self.wheel, "Made"), (self.hub, "Made")},
		)

	def test_making_boms_needs_bom_permission(self):
		frappe.set_user(make_user("onboarding.bom.viewer@example.com", "Accounts Manager"))
		doc = frappe.get_doc("Company Setup", self.company)
		doc.save_boms([{"item_name": self.hub, "material_name": self.casting, "qty": 1}])
		self.assertRaisesRegex(
			frappe.PermissionError,
			"To make BOMs, you need the Manufacturing Manager / Manufacturing User role.",
			doc.make_boms,
		)
