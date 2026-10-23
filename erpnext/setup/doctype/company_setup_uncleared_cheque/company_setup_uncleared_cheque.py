# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class CompanySetupUnclearedCheque(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		bank_account: DF.Link
		entry_type: DF.Literal["", "Issued", "Deposited"]
		journal_entry: DF.Link | None
		opening_entry: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		party_name: DF.Data | None
		reference_date: DF.Date | None
		reference_no: DF.Data | None
		source: DF.Data | None
		status: DF.Literal["Draft", "Posted"]
	# end: auto-generated types

	_DOCTYPE_NAME = "Company Setup Uncleared Cheque"
