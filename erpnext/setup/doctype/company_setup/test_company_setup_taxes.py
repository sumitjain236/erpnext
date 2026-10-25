# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.setup.doctype.company_setup.opening_invoices import add_account
from erpnext.setup.doctype.company_setup.test_company_setup import (
	ERPNEXT_STEPS,
	get_account,
	get_setup,
	make_company,
	make_user,
	step_key,
	step_names,
	step_status,
)
from erpnext.tests.utils import ERPNextTestSuite


class TestTaxes(ERPNextTestSuite):
	"""ERPNext's own Taxes step, for countries with no compliance app."""

	def setUp(self):
		self.company = make_company()
		user = make_user("onboarding.accounts@example.com", "Accounts Manager")
		frappe.set_user(make_user(user, "Purchase Master Manager"))
		self.doc = frappe.get_doc("Company Setup", self.company)
		group = get_account(self.company, is_group=1, account_type="Tax")
		self.sales_tax = add_account(self.company, "CA Sales Tax Payable", "Tax", "Liability", parent=group)
		self.input_tax = add_account(self.company, "Input Tax", "Tax", "Liability", parent=group)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_save_makes_the_tax_templates_and_no_rows_is_done_too(self):
		key = step_key(get_setup(self.company), "taxes")
		self.assertEqual(step_status(get_setup(self.company))["taxes"], "Not Started")
		cash = get_account(self.company, account_type="Cash")
		self.assertRaisesRegex(
			frappe.ValidationError,
			"not a tax account",
			self.doc.save_taxes,
			[{"tax_type": "Sales", "title": "Wrong", "account": cash, "rate": 5}],
		)
		self.doc.save_taxes(
			[
				{"tax_type": "Sales", "title": "CA Sales Tax", "account": self.sales_tax, "rate": 7.25},
				{"tax_type": "Purchase", "title": "Input Tax", "account": self.input_tax, "rate": 5},
			],
			{"Sales": "CA Sales Tax"},
		)
		sales = frappe.get_doc("Sales Taxes and Charges Template", self.doc.tax_templates[0].template)
		self.assertEqual(
			(sales.company, sales.is_default, sales.taxes[0].account_head, sales.taxes[0].rate),
			(self.company, 1, self.sales_tax, 7.25),
		)
		# a template is the default only when picked: a company may sell exempt services too
		self.assertFalse(
			frappe.db.get_value(
				"Purchase Taxes and Charges Template",
				{"title": "Input Tax", "company": self.company},
				"is_default",
			)
		)
		self.assertTrue(
			frappe.db.exists(
				"Purchase Taxes and Charges Template", {"title": "Input Tax", "company": self.company}
			)
		)
		doc = get_setup(self.company)
		doc.run_method("onload")
		self.assertEqual(doc.get_onload().steps[key]["summary"], "2 tax templates")
		self.assertEqual(step_status(doc)["taxes"], "Done")

		# saved again with no rows: the company charges no tax; the templates stay
		self.doc.save_taxes([])
		doc = get_setup(self.company)
		doc.run_method("onload")
		self.assertEqual(
			(step_status(doc)["taxes"], doc.get_onload().steps[key]["summary"]), ("Done", "No taxes charged")
		)
		# a compliance app takes its place
		self.assertEqual(
			step_names(app_one=[{"step": "app_one.steps.gst", "replaces": "taxes"}]),
			[step if step != "taxes" else "gst" for step in [*ERPNEXT_STEPS, "go_live"]],
		)
