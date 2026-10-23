# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class CompanySetupOpeningStock(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		batch_no: DF.Data | None
		challan_date: DF.Date | None
		challan_no: DF.Data | None
		correction: DF.SmallText | None
		expiry_date: DF.Date | None
		item_code: DF.Link | None
		item_name: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		qty: DF.Float
		rate: DF.Float
		return_due: DF.Date | None
		serial_no: DF.SmallText | None
		source: DF.Data | None
		status: DF.Literal["Draft", "Posted"]
		stock_entry: DF.Link | None
		stock_group: DF.Data | None
		uom: DF.Data | None
		warehouse: DF.Link | None
		warehouse_name: DF.Data | None
	# end: auto-generated types

	_DOCTYPE_NAME = "Company Setup Opening Stock"
