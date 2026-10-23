# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class CompanySetupOpeningInvoice(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		account: DF.Link | None
		account_by_hand: DF.Check
		create_party: DF.Check
		due_date: DF.Date | None
		error: DF.SmallText | None
		invoice: DF.Data | None
		invoice_date: DF.Date | None
		invoice_number: DF.Data | None
		outstanding_amount: DF.Currency
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		party: DF.DynamicLink | None
		party_group: DF.Data | None
		party_name: DF.Data
		party_type: DF.Literal["Customer", "Supplier"]
		source: DF.Data | None
		status: DF.Literal["Draft", "Created", "Failed"]
	# end: auto-generated types

	_DOCTYPE_NAME = "Company Setup Opening Invoice"
