# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class CompanySetupAssetCategory(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		accumulated_account: DF.Link | None
		asset_account: DF.Link
		asset_category: DF.Data
		depreciation_account: DF.Link | None
		hsn_code: DF.Data | None
		life: DF.Int
		method: DF.Literal["", "Straight Line", "Written Down Value"]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		rate: DF.Float
	# end: auto-generated types

	_DOCTYPE_NAME = "Company Setup Asset Category"
