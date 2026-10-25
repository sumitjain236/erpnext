# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.setup.doctype.company_setup import company_setup_api as api
from erpnext.setup.doctype.company_setup.test_company_setup import make_company, make_user
from erpnext.tests.utils import ERPNextTestSuite


class TestCompanySetupApi(ERPNextTestSuite):
	"""The getters the windows read: only their data, and only for users who may read the setup."""

	def setUp(self):
		self.company = make_company()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_a_getter_returns_the_data_of_the_doc_method(self):
		frappe.set_user(make_user("onboarding.api@example.com", "Accounts Manager"))
		doc = frappe.get_doc("Company Setup", self.company)
		self.assertEqual(api.get_taxes(self.company), doc.get_taxes())
		self.assertEqual(api.get_stock_status(self.company), {"posting": False})

	def test_a_user_who_cannot_read_the_setup_is_refused(self):
		frappe.set_user(make_user("onboarding.api.stock@example.com", "Stock User"))
		with self.assertRaises(frappe.PermissionError):
			api.get_taxes(self.company)
		with self.assertRaises(frappe.PermissionError):
			api.get_stock_status(self.company)
