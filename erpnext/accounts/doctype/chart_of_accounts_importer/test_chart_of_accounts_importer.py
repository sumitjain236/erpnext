# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.accounts.doctype.chart_of_accounts_importer.chart_of_accounts_importer import (
	build_forest,
	replace_chart,
	validate_columns,
	validate_missing_roots,
)
from erpnext.tests.utils import ERPNextTestSuite

# columns: account_name, parent_account, account_number, parent_account_number,
#          is_group, account_type, root_type, account_currency
ROOT = ["Assets", "Assets", "", "", 1, "", "Asset", "INR"]
CHILD = ["Cash", "Assets", "", "", 0, "Cash", "Asset", "INR"]


class TestChartofAccountsImporter(ERPNextTestSuite):
	"""The importer parses an uploaded CoA into a nested tree and validates its
	shape. These cover the parsing/validation helpers without a file upload."""

	def test_validate_columns_rejects_blank_file(self):
		self.assertRaises(frappe.ValidationError, validate_columns, [])

	def test_validate_columns_requires_eight_columns(self):
		self.assertRaises(frappe.ValidationError, validate_columns, [["a", "b", "c"]])
		# the standard template width passes
		validate_columns([ROOT])

	def test_build_forest_nests_child_under_parent(self):
		forest = build_forest([ROOT, CHILD])
		self.assertIn("Assets", forest)
		self.assertIn("Cash", forest["Assets"])

	def test_build_forest_rejects_unknown_parent(self):
		orphan = ["Cash", "Missing Parent", "", "", 0, "Cash", "Asset", "INR"]
		self.assertRaises(frappe.ValidationError, build_forest, [orphan])

	def test_build_forest_requires_account_name(self):
		nameless = ["", "Assets", "", "", 0, "Cash", "Asset", "INR"]
		self.assertRaises(frappe.ValidationError, build_forest, [ROOT, nameless])

	def test_validate_missing_roots_requires_all_root_types(self):
		present = ("Asset", "Liability", "Expense", "Income")  # Equity missing
		self.assertRaises(
			frappe.ValidationError,
			validate_missing_roots,
			[{"root_type": rt} for rt in present],
		)
		# all five root types present -> no error
		validate_missing_roots(
			[{"root_type": rt} for rt in ("Asset", "Liability", "Expense", "Income", "Equity")]
		)

	def test_replace_chart_clears_links_to_removed_accounts(self):
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "_Test Chart Import Company",
				"abbr": "_TCIC",
				"country": "India",
				"default_currency": "INR",
				"chart_of_accounts": "Standard",
			}
		).insert()
		warehouse = f"Stores - {company.abbr}"
		frappe.db.set_value("Warehouse", warehouse, "account", company.default_inventory_account)
		category = frappe.get_doc(
			{
				"doctype": "Asset Category",
				"asset_category_name": "_Test Chart Import Category",
				"accounts": [
					{
						"company_name": company.name,
						"fixed_asset_account": frappe.db.get_value(
							"Account", {"company": company.name, "account_type": "Fixed Asset", "is_group": 0}
						),
					}
				],
			}
		).insert()

		replace_chart(
			[
				[*root, "", "", 1, "", root_type, "INR"]
				for root, root_type in (
					(["Assets", "Assets"], "Asset"),
					(["Liabilities", "Liabilities"], "Liability"),
					(["Capital", "Capital"], "Equity"),
					(["Income", "Income"], "Income"),
					(["Expenses", "Expenses"], "Expense"),
				)
			]
			+ [
				["Godown Stock", "Assets", "", "", 0, "Stock", "Asset", "INR"],
				["Plant", "Assets", "", "", 0, "Fixed Asset", "Asset", "INR"],
			],
			company.name,
		)

		self.assertIsNone(frappe.db.get_value("Warehouse", warehouse, "account"))
		self.assertFalse(frappe.get_doc("Asset Category", category.name).accounts)
