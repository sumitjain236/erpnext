# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class CompanySetupOpenOrder(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bom: DF.Link | None
		delivery_date: DF.Date | None
		document: DF.DynamicLink | None
		item_code: DF.Link | None
		item_name: DF.Data | None
		material: DF.Data | None
		order_date: DF.Date | None
		order_no: DF.Data | None
		order_type: DF.Literal["Sales Order", "Purchase Order", "Work Order", "Subcontracting Order"]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		party_name: DF.Data | None
		purchase_order: DF.Link | None
		qty: DF.Float
		rate: DF.Currency
		service_item: DF.Link | None
		source: DF.Data | None
		status: DF.Literal["Draft", "Made"]
		uom: DF.Data | None
		warehouse: DF.Link | None
		wip_warehouse: DF.Link | None
	# end: auto-generated types

	_DOCTYPE_NAME = "Company Setup Open Order"
