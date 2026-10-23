# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class CompanySetupFixedAsset(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		asset: DF.Link | None
		asset_account: DF.Link | None
		asset_account_name: DF.Data | None
		asset_name: DF.Data
		gross: DF.Currency
		hand_booked: DF.Currency
		life: DF.Int
		method: DF.Literal["", "Straight Line", "Written Down Value"]
		moved: DF.Currency
		net_value_entry: DF.Link | None
		opening_depreciation: DF.Currency
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		purchase_date: DF.Date | None
		qty: DF.Int
		rate: DF.Float
		salvage: DF.Currency
		source: DF.Data | None
		status: DF.Literal["Draft", "Posted"]
		wdv_gap: DF.Currency
	# end: auto-generated types

	_DOCTYPE_NAME = "Company Setup Fixed Asset"
