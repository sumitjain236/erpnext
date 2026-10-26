# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class CompanySetupBOM(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bom: DF.Link | None
		bom_qty: DF.Float
		error: DF.SmallText | None
		item_code: DF.Link | None
		item_name: DF.Data | None
		material_code: DF.Link | None
		material_name: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		qty: DF.Float
		rate: DF.Currency
		source: DF.Data | None
		status: DF.Literal["Draft", "Made"]
		uom: DF.Data | None
	# end: auto-generated types

	_DOCTYPE_NAME = "Company Setup BOM"
