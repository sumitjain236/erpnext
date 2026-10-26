import { cheque_words, day_before_text, grey_line } from "./common";

// Where each report or list is in the software people move from
const WHERE_TO_FIND = {
	chart_of_accounts: [
		[
			"Tally",
			__(
				"Display More Reports › List of Accounts, then Alt+E to export (Tally ERP 9: Display › List of Accounts)"
			),
		],
		["Busy", __("Administration › Masters › Account › List, choose All Accounts, then Alt+E")],
		["Zoho Books", __("Accountant › Chart of Accounts › Export")],
		["QuickBooks", __("Reports › Account List, then Export to Excel")],
		["Xero", __("Accounting › Chart of accounts › Export")],
		["Marg", __("Books › All Ledgers, then the Excel icon and Format-2")],
		["Excel / other", __("Download the sample chart and fill it in.")],
	],
	opening_balances: [
		[
			"Tally",
			__(
				"Display More Reports › Trial Balance, Alt+F5 for every ledger, then Alt+E (Tally ERP 9: Display › Trial Balance)"
			),
		],
		["Busy", __("Display › Trial Balance › Closing Trial - Alphabetical, Balance Only, then Alt+E")],
		["Zoho Books", __("Reports › Trial Balance")],
		["QuickBooks", __("Reports › Trial Balance")],
		["Xero", __("Accounting › Reports › Trial Balance, then Export to Excel")],
		["Marg", __("Final Reports › Trial Balance, Alt+P, A/C Group Wise: No, then Excel")],
		["Excel / other", __("One row per account with its debit or credit.")],
	],
	opening_sales_invoices: [
		[
			"Tally",
			__(
				"Display More Reports › Statements of Accounts › Outstandings › Receivables (Tally ERP 9: Display › Statements of Accounts › Outstandings)"
			),
		],
		[
			"Busy",
			__("Display › Outstanding Analysis › Bills Receivable, Sundry Debtors, Bill-wise, then Alt+E"),
		],
		["Zoho Books", __("Reports › AR Aging Details")],
		["QuickBooks", __("Reports › A/R Aging Detail, then Export to Excel")],
		["Xero", __("Accounting › Reports › Aged Receivables Detail, then Export to Excel")],
		["Marg", __("Books › Outstanding › Whole, Alt+P › Outstanding List Party Wise, then the Excel icon")],
		["Excel / other", __("One row per unpaid invoice: customer, invoice number, date, amount.")],
	],
	opening_purchase_invoices: [
		[
			"Tally",
			__(
				"Display More Reports › Statements of Accounts › Outstandings › Payables (Tally ERP 9: Display › Statements of Accounts › Outstandings)"
			),
		],
		[
			"Busy",
			__("Display › Outstanding Analysis › Bills Payable, Sundry Creditors, Bill-wise, then Alt+E"),
		],
		["Zoho Books", __("Reports › AP Aging Details")],
		["QuickBooks", __("Reports › A/P Aging Detail, then Export to Excel")],
		["Xero", __("Accounting › Reports › Aged Payables Detail, then Export to Excel")],
		[
			"Marg",
			__(
				"Books › Outstanding › Creditors: Whole, Alt+P › Outstanding List Party Wise, then the Excel icon"
			),
		],
		["Excel / other", __("One row per unpaid bill: supplier, bill number, date, amount.")],
	],
	opening_stock: [
		[
			"Tally",
			__(
				"Display More Reports › Inventory Books › Godown Summary, then Alt+E for each godown (Tally ERP 9: Display › Inventory Books › Godown Summary)"
			),
		],
		[
			"Busy",
			__(
				"Display › Stock Status › Material Centre-wise, with closing value, then Alt+E (Busy calls a warehouse a Material Centre)"
			),
		],
		[
			"Zoho Books",
			__("Reports › Inventory › Inventory Valuation Summary, one warehouse at a time, then Export"),
		],
		["QuickBooks", __("Reports › Inventory Valuation Summary, then Export to Excel")],
		[
			"Excel / other",
			__(
				"One row per item per warehouse: item, warehouse, qty, unit, value. Job worker rows add the challan."
			),
		],
	],
	fixed_assets: [
		["Busy", __("Display › Final Results › Depreciation Chart, then Alt+E")],
		["QuickBooks", __("Reports › Fixed Asset List, then Export to Excel")],
		[
			"Excel / other",
			__("One row per asset: name, asset account, purchase date, cost, depreciation till date."),
		],
	],
	open_orders: [
		[
			"Tally",
			__(
				"Display More Reports › Statements of Inventory › Sales Order Outstandings, and Purchase Order Outstandings, then Alt+E"
			),
		],
		["Excel / other", __("One row per order line: order number, party, item, qty left, rate, date.")],
	],
	boms: [
		["Tally", __("Tally keeps the BOM on each stock item: fill the BOMs sheet from them.")],
		["Excel / other", __("One row per material: item you make, how many it makes, material, qty, unit.")],
	],
	job_work_orders: [
		[
			"Tally",
			__(
				"Display More Reports › Statements of Inventory › Job Work Out Order Outstandings, then Alt+E"
			),
		],
		[
			"Excel / other",
			__("One row per order line: order number, job worker, item, qty left, rate, date."),
		],
	],
	uncleared_cheques: [
		[
			"Tally",
			__("Banking › Bank Reconciliation, pick the bank, then Alt+E to export what is not cleared"),
		],
		["Busy", __("Display › Bank Reconciliation, pick the bank, show unreconciled entries, then Alt+E")],
		[
			"QuickBooks",
			__("Accounting › Reconcile › History by account, open the last report, then Export to Excel"),
		],
		[
			"Excel / other",
			__(
				"One row per cheque or deposit: bank account, issued or deposited, number, date, party, amount."
			),
		],
	],
};

// in the order people in the country use them
const SOFTWARE =
	frappe.sys_defaults?.country === "India"
		? ["Tally", "Busy", "Zoho Books", "QuickBooks", "Xero", "Marg", "Excel / other"]
		: ["QuickBooks", "Xero", "Zoho Books", "Tally", "Busy", "Marg", "Excel / other"];

// Each task's report comes from the software picked for it; the chart from where the trial balance is
export const SOURCES = {
	chart_of_accounts: ["accounts_source", __("Your chart comes from")],
	opening_balances: ["accounts_source", __("Your trial balance comes from")],
	opening_sales_invoices: ["receivables_source", __("Your unpaid invoices come from")],
	opening_purchase_invoices: ["payables_source", __("Your unpaid bills come from")],
	uncleared_cheques: ["cheques_source", __("Your bank book comes from")],
	opening_stock: ["stock_source", __("Your stock summary comes from")],
	fixed_assets: ["assets_source", __("Your asset register comes from")],
	open_orders: ["orders_source", __("Your open orders come from")],
	boms: ["boms_source", __("Your BOMs come from")],
	job_work_orders: ["job_work_source", __("Your job work orders come from")],
};

// reports that come from the trial balance's software until picked on their own
export const FOLLOWS_TRIAL_BALANCE = [
	"uncleared_cheques",
	"opening_stock",
	"fixed_assets",
	"open_orders",
	"boms",
	"job_work_orders",
];

// the trial balance's software, or an Excel sheet when that software has no such report
export function follow_trial_balance(task_name, accounts_source) {
	return !accounts_source || WHERE_TO_FIND[task_name].some(([software]) => software === accounts_source)
		? accounts_source || ""
		: "Excel / other";
}

export function get_source(frm, task_name) {
	const value = frm.doc[SOURCES[task_name][0]];
	return value || !FOLLOWS_TRIAL_BALANCE.includes(task_name)
		? value || ""
		: follow_trial_balance(task_name, frm.doc.accounts_source);
}

// a software without its own steps for this report gets the sheet's columns
export function get_places(task_name, source) {
	const places = WHERE_TO_FIND[task_name]
		.filter(([software]) => !source || software === source)
		.sort(([a], [b]) => SOFTWARE.indexOf(a) - SOFTWARE.indexOf(b));
	return places.length
		? places
		: WHERE_TO_FIND[task_name].filter(([software]) => software === "Excel / other");
}

export function make_source_select(value, onchange) {
	const $parent = $(`<div class="shrink-0"></div>`);
	const control = frappe.ui.form.make_control({
		df: {
			fieldtype: "Select",
			fieldname: "source",
			options: ["", ...SOFTWARE].map((software) => ({ value: software, label: __(software) })),
			placeholder: __("Pick one"),
			change: () => onchange(control.get_value()),
		},
		parent: $parent,
		only_input: true,
		render_input: true,
	});
	control.set_value(value || "");
	$parent.find(".form-group").addClass("mb-0");
	return [$parent, control];
}

// The "Where do I find this?" section at the end of a dialog: open while nothing is entered yet,
// folded once something is, and left out once anything is posted. fill_where draws it.
export function where_to_find_fields(frm, task_name, { has_data, posted }) {
	if (posted) return [];
	return [
		{
			fieldname: "where_section",
			fieldtype: "Section Break",
			label: __("Where do I find this?"),
			collapsible: 1,
			collapsible_depends_on: has_data ? "eval:0" : "eval:1",
		},
		{ fieldname: "where_to_find", fieldtype: "HTML" },
	];
}

// The steps for the software this task's report comes from, and a way to pick another
export function fill_where(frm, dialog, task_name) {
	const field = dialog.fields_dict.where_to_find;
	if (!field) return;
	const [source_field, says] = SOURCES[task_name];
	const source = get_source(frm, task_name);
	const [$select] = make_source_select(source, (value) => {
		if (!value || value === source) return;
		frm.set_value(source_field, value)
			.then(() => frm.save())
			.then(() => fill_where(frm, dialog, task_name));
	});
	const $line = $(`<div class="flex items-center justify-between gap-2"></div>`).append(
		$(`<div class="text-p-sm text-ink-gray-7"></div>`).text(says),
		$select
	);
	field.$wrapper
		.empty()
		.append($line.addClass("mb-2"), make_where_table(frm, get_places(task_name, source)));
}

function make_where_table(frm, places) {
	// one software picked: the picker above names it, so only its steps
	const $box =
		places.length === 1
			? $(`<div class="border rounded-lg px-3 py-2 text-p-sm text-ink-gray-8"></div>`).text(
					cheque_words(places[0][1])
			  )
			: make_where_box(places);
	const $table = $(`<div class="flex flex-col gap-2"></div>`).append($box);
	if (frm.doc.migration_date) {
		grey_line(__("Take it as on {0}, the day before you start.", [day_before_text(frm)])).appendTo(
			$table
		);
	}
	return $table[0];
}

// A box with each software beside where its report is, under the report's name when given
export function make_where_box(places, title) {
	return $(`<div class="flex flex-col border rounded-lg overflow-hidden"></div>`).append(
		title
			? $(`<div class="px-3 py-2 text-sm-medium text-ink-gray-8 bg-surface-gray-1"></div>`).text(title)
			: [],
		places.map(([software, where], index) =>
			$(
				`<div class="company-setup-where gap-4 px-3 py-2 ${title || index ? "border-t" : ""}"></div>`
			).append(
				grey_line(software),
				$(`<div class="text-p-sm text-ink-gray-8"></div>`).text(cheque_words(where))
			)
		)
	);
}
