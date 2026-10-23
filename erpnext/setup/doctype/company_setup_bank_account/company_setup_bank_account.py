# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class CompanySetupBankAccount(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		account: DF.Link
		bank: DF.Data | None
		bank_account: DF.Link | None
		bank_account_no: DF.Data | None
		branch_code: DF.Data | None
		has_statement_balance: DF.Check
		is_credit_card: DF.Check
		is_default: DF.Check
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		statement_balance: DF.Currency
	# end: auto-generated types

	_DOCTYPE_NAME = "Company Setup Bank Account"
