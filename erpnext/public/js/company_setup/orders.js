import {
	ORDER_TYPES,
	after_run,
	call_method,
	count_rows,
	download_sheet,
	fix_button,
	format_qty,
	get_data,
	grey_line,
	make_kv_lines,
	make_read_table,
	plural,
	set_primary_disabled,
} from "./common";
import { SheetWindow, list_column } from "./sheet_window";
import { show_tab } from "./overview";
import { kept_cancelled_line } from "./invoices_posted";
import { open_party_import } from "./imports";
import { show_warehouses } from "./warehouses";
import { show_opening_stock } from "./stock";
import { show_posted_window } from "./posted";

function order_rows(frm, order_type, made) {
	return (frm.doc.open_orders || []).filter(
		(row) => row.order_type === order_type && (row.status === "Made") === made
	);
}

export function order_buttons(frm, step) {
	const order_type = ORDER_TYPES[step.name];
	const drafts = order_rows(frm, order_type, false).length;
	return [
		{
			label: drafts
				? __("Edit orders")
				: order_type === "Work Order"
				? __("Add work orders")
				: __("Add orders"),
			onclick: () => show_open_orders(frm, order_type),
		},
	];
}

export function order_menu(frm, step) {
	const order_type = ORDER_TYPES[step.name];
	return [
		(order_rows(frm, order_type, true).length || has_undone_orders(frm, order_type)) && {
			label: made_orders_title(order_type),
			icon: "list",
			onclick: () => show_made_orders(frm, order_type),
		},
		{
			label: __("Download sheet"),
			icon: "download",
			onclick: () =>
				download_sheet(frm.doc.company, "open_orders.download_open_orders", {
					order_type: order_type,
				}),
		},
	].filter(Boolean);
}

function has_undone_orders(frm, order_type) {
	return (frm.doc.undone_orders || []).some((row) => row.order_type === order_type);
}

function made_orders_title(order_type) {
	return {
		"Sales Order": __("Made sales orders"),
		"Purchase Order": __("Made purchase orders"),
		"Work Order": __("Made work orders"),
		"Subcontracting Order": __("Made subcontracting orders"),
	}[order_type];
}

// ---------- BOMs ----------

export function count_boms(count) {
	return plural(count, __("1 BOM"), __("{0} BOMs", [count]));
}

function make_boms_label(count) {
	return count ? plural(count, __("Make 1 BOM"), __("Make {0} BOMs", [count])) : __("Make BOMs");
}

function bom_drafts(frm) {
	return (frm.doc.boms || []).filter((row) => row.status !== "Made");
}

export function bom_buttons(frm) {
	return [
		{
			label: bom_drafts(frm).length ? __("Edit BOMs") : __("Add BOMs"),
			onclick: () => show_boms(frm),
		},
	];
}

export function bom_menu(frm) {
	return [
		(frm.doc.boms || []).some((row) => row.status === "Made") && {
			label: __("Made BOMs"),
			icon: "list",
			onclick: () => show_made_boms(frm),
		},
		{
			label: __("Download sample"),
			icon: "download",
			onclick: () => download_sheet(frm.doc.company, "boms.download_boms"),
		},
		{
			label: __("Open BOM Creator"),
			icon: "arrow-up-right",
			onclick: () => frappe.new_doc("BOM Creator"),
		},
	].filter(Boolean);
}

export function show_boms(frm, result) {
	get_data(frm, "get_boms").then((r) => new BomsWindow(frm, r.message, result).show());
}

class BomsWindow extends SheetWindow {
	constructor(frm, checks, result) {
		super(frm, {
			title: __("BOMs"),
			where: "boms",
			has_data: bom_drafts(frm).length,
			save_method: "save_boms",
			get_method: "get_boms",
			primary_label: __("Make BOMs"),
			checks: checks,
		});
		this.result = result;
		this.load(checks);
	}

	get_layout(checks) {
		return checks?.has_rate ? "rate" : "";
	}

	get_fields(checks) {
		const rate = Boolean(checks?.has_rate);
		return [
			list_column("item_name", "Data", __("Item you make"), 3),
			list_column("bom_qty", "Float", __("Makes"), 1),
			list_column("material_name", "Data", __("Material"), rate ? 3 : 4),
			list_column("qty", "Float", __("Qty"), 1),
			list_column("uom", "Data", __("Unit"), 1),
			{
				fieldname: "rate",
				fieldtype: "Currency",
				label: __("Rate"),
				columns: 1,
				in_list_view: rate ? 1 : 0,
			},
			{ fieldname: "source", fieldtype: "Data", hidden: 1 },
			{ fieldname: "doc_idx", fieldtype: "Int", hidden: 1 },
		];
	}

	load(checks) {
		this.checks = checks;
		const fields = ["item_name", "bom_qty", "material_name", "qty", "uom", "rate", "source"];
		this.drafts = bom_drafts(this.frm).map((row) => ({
			...Object.fromEntries(fields.map((field) => [field, row[field]])),
			doc_idx: row.idx,
		}));
	}

	render() {
		const { checks, frm } = this;
		this.make_head(
			__(
				"One row per material of each item you make. Each item gets one BOM, submitted and set as its default."
			),
			{
				upload: () => this.upload("import_boms", null),
				menu: [
					checks.made && {
						label: __("Made BOMs"),
						icon: "list",
						onclick: () => {
							this.dialog.hide();
							show_made_boms(frm);
						},
					},
					{
						label: __("Download sample"),
						icon: "download",
						onclick: () => download_sheet(frm.doc.company, "boms.download_boms"),
					},
				].filter(Boolean),
			}
		);
		this.set_stats(
			checks.drafts || checks.made
				? [
						frappe.ui.stat_card({
							label: __("On your sheet"),
							value: checks.drafts ? count_boms(checks.boms) : __("Nothing to make"),
							caption: checks.drafts
								? [
										plural(
											checks.drafts,
											__("1 material row"),
											__("{0} material rows", [checks.drafts])
										),
										...checks.sources,
								  ].join(" · ")
								: null,
						}),
						frappe.ui.stat_card({
							label: __("Made"),
							value: checks.made ? count_boms(checks.made) : __("Nothing yet"),
						}),
				  ]
				: []
		);
		const go_to_items = {
			label: __("Go to Items"),
			onclick: () => {
				this.dialog.hide();
				open_party_import(frm, "Item");
			},
		};
		const fields = {
			missing_items: ["item_name"],
			missing_materials: ["material_name"],
			no_qty: ["qty"],
			unknown_units: ["uom"],
		};
		const alerts = [
			...(checks.reds || []).map((red) => ({
				theme: "red",
				key: red.key,
				title: red.text,
				description: red.hint,
				rows: red.rows,
				fields: fields[red.key],
				badge: red.key.startsWith("missing_")
					? __("Not in ERPNext")
					: red.key === "no_qty"
					? __("Needed")
					: undefined,
				buttons: red.key.startsWith("missing_") ? [go_to_items] : [],
			})),
			...(checks.failed || []).map((failed) => ({
				theme: "red",
				key: `failed_${failed.rows[0]}`,
				title: failed.text,
				rows: failed.rows,
			})),
			checks.kept && {
				key: "kept",
				title: checks.kept.text,
				rows: checks.kept.rows,
				fields: ["item_name"],
				badge: __("Has a BOM"),
				badge_theme: "gray",
			},
		].filter(Boolean);
		this.set_alerts(alerts, [
			this.result &&
				plural(
					this.result.added,
					__("1 row read from the sheet."),
					__("{0} rows read from the sheet.", [this.result.added])
				),
			this.result?.replaced &&
				plural(
					this.result.replaced,
					__("1 draft row of the same items was replaced."),
					__("{0} draft rows of the same items were replaced.", [this.result.replaced])
				),
		]);
		this.refresh_footer();
	}

	refresh_footer() {
		if (this.view !== "sheet") return;
		const { checks } = this;
		this.dialog.set_primary_action(make_boms_label(checks.to_make), () => this.post());
		this.dialog.set_secondary_action_label(__("Save draft"));
		this.dialog.set_secondary_action(() => this.save_draft());
		const empty = !this.get_rows().length;
		set_primary_disabled(this.dialog, empty || (!this.dirty && Boolean(checks.blocker)));
	}

	confirm_post() {
		const { frm, checks } = this;
		this.show_confirm({
			title: __("{0}?", [make_boms_label(checks.to_make)]),
			back: __("Nothing is made until you confirm."),
			body: [
				make_kv_lines([[__("To make"), count_boms(checks.to_make)]]),
				grey_line(__("Each BOM is submitted and set as its item's default.")),
				checks.kept && grey_line(checks.kept.text),
			].filter(Boolean),
			primary: make_boms_label(checks.to_make),
			on_primary: () =>
				call_method(frm, "make_boms", {}, { freeze: true }).then((r) => {
					this.dialog.hide();
					after_run(
						frm,
						r.message,
						__("Making the BOMs in the background. This page updates when it's done."),
						(result) => show_boms_made(frm, result)
					);
				}),
		});
	}
}

export function show_boms_made(frm, result) {
	if (result.failed) {
		frappe.ui.toast({
			type: "error",
			message: plural(
				result.failed,
				__("1 BOM was not made."),
				__("{0} BOMs were not made.", [result.failed])
			),
			description: __("The reason is on its rows."),
		});
		return show_boms(frm);
	}
	frappe.ui.toast({
		type: "success",
		message: __("{0} made.", [count_boms(result.made)]),
		action: { label: __("View"), onclick: () => show_made_boms(frm) },
	});
}

function show_made_boms(frm) {
	const count = (rows) => count_boms(rows.length);
	show_posted_window(frm, {
		title: __("Made BOMs"),
		load: () => get_data(frm, "get_made_boms").then((r) => r.message),
		posted_word: (when) => __("Made {0}", [when]),
		group_line: (group) => count_boms(group.count),
		fields: [
			{ fieldname: "document", fieldtype: "Link", options: "BOM", label: __("BOM"), columns: 3 },
			{ fieldname: "item_code", fieldtype: "Data", label: __("Item you make"), columns: 3 },
			{ fieldname: "bom_qty", fieldtype: "Float", label: __("Makes"), columns: 1 },
			{ fieldname: "materials", fieldtype: "Int", label: __("Materials"), columns: 1 },
			{ fieldname: "used", fieldtype: "Int", label: __("Used by"), columns: 2 },
		],
		rows: (group) => group.rows.map((row) => ({ ...row })),
		blocked: (row) => row.used > 0,
		blocked_alert: (rows) => ({
			title: plural(
				rows.length,
				__("1 BOM is used by work orders or stock entries."),
				__("{0} BOMs are used by work orders or stock entries.", [rows.length])
			),
			description: __("Cancel those first."),
		}),
		undo_label: (selected) =>
			selected.length ? __("Undo {0} selected", [selected.length]) : __("Undo this upload"),
		confirm_title: (rows) => __("Undo {0}?", [count(rows)]),
		confirm_text: (rows) =>
			plural(
				rows.length,
				__("Its rows are kept, so you can fix it and make it again."),
				__("Their rows are kept, so you can fix them and make them again.")
			),
		confirm_what: (rows) => ({
			count: rows.length,
			what: count(rows),
			one: __("BOM"),
			has_ledger: false,
		}),
		confirm_rows: (rows) =>
			rows.map((row) => [
				$(`<span class="text-base text-ink-gray-8 shrink-0"></span>`).text(row.document),
				$(`<span class="text-base text-ink-gray-5 grow truncate"></span>`).text(row.item_code),
			]),
		confirm_button: (rows) => __("Undo {0}", [count(rows)]),
		undo: (group, selected) =>
			call_method(frm, "undo_boms", {
				source: group.source,
				documents: selected ? selected.map((row) => row.document) : null,
			}),
		after_undo: (result) =>
			frappe.ui.toast({
				type: "success",
				message: __("{0} undone", [count_boms(result.removed)]),
				description: [__("They're cancelled and deleted in ERPNext."), kept_cancelled_line(result)]
					.filter(Boolean)
					.join(" "),
			}),
	});
}

// ---------- Open orders ----------

const ORDER_TEXT = {
	"Sales Order": {
		title: __("Open sales orders"),
		intro: __(
			"Sales orders open on {0}. Enter only the qty not yet delivered; do not add advances again."
		),
		party: __("Customer"),
		note: __("Advances are not added again."),
	},
	"Purchase Order": {
		title: __("Open purchase orders"),
		intro: __(
			"Purchase orders open on {0}. Enter only the qty not yet received; do not add advances again."
		),
		party: __("Supplier"),
		note: __("Advances are not added again."),
	},
	"Work Order": {
		title: __("Open work orders"),
		intro: __("Work orders running on {0}. Enter the qty still to make."),
		note: __(
			"A job with its material in the work in progress warehouse makes from there. The others transfer material first, as usual."
		),
	},
	"Subcontracting Order": {
		title: __("Open subcontracting orders"),
		intro: __("Orders with goods still at the job worker on {0}."),
		party: __("Job worker"),
		note: __("The material is already at the job worker from Opening stock, so nothing is sent again."),
	},
};

function format_quantities(quantities) {
	return quantities.map(([qty, unit]) => [format_qty(qty), unit].filter(Boolean).join(" ")).join(", ");
}

export function count_orders(order_type, count) {
	return order_type === "Work Order"
		? plural(count, __("1 work order"), __("{0} work orders", [count]))
		: plural(count, __("1 order"), __("{0} orders", [count]));
}

export function show_open_orders(frm, order_type, result) {
	get_data(frm, "get_open_orders", { order_type: order_type }).then((r) =>
		new OpenOrdersWindow(frm, order_type, r.message, result).show()
	);
}

// Per item: its service item, and a copy of its BOM that leaves out what is added in-house
function show_job_work_setup(frm, items, after) {
	get_data(frm, "get_job_work_setups", { items: items }, { freeze: true }).then((r) => {
		const setups = r.message;
		const ready = setups.filter((setup) => setup.bom);
		const fields = setups.flatMap((setup, i) => [
			{
				fieldtype: "Section Break",
				label: frappe.utils.escape_html(
					[
						setup.item_name && setup.item_name !== setup.item_code
							? `${setup.item_code}: ${setup.item_name}`
							: setup.item_code,
						...setup.job_workers,
					].join(" · ")
				),
			},
			...(setup.bom ? job_work_fields(setup, i) : [{ fieldname: `no_bom_${i}`, fieldtype: "HTML" }]),
		]);
		const dialog = new frappe.ui.Dialog({
			title: __("Set up job work"),
			size: "large",
			fields: fields,
			primary_action_label: plural(
				ready.length,
				__("Save 1 job-work setup"),
				__("Save {0} job-work setups", [ready.length])
			),
			primary_action: (values) =>
				call_method(
					frm,
					"set_job_work_setups",
					{
						setups: setups
							.map((setup, i) => ({ setup, i }))
							.filter(({ setup }) => setup.bom)
							.map(({ setup, i }) => ({
								item_code: setup.item_code,
								materials: dialog.fields_dict[`materials_${i}`].grid
									.get_selected_children()
									.map((row) => row.item_code),
								service_item: values[`service_item_${i}`],
								finished_good_qty: values[`finished_good_qty_${i}`],
							})),
					},
					{ freeze: true }
				).then(() => {
					dialog.hide();
					after();
				}),
		});
		setups.forEach((setup, i) => {
			if (!setup.bom) {
				dialog
					.get_field(`no_bom_${i}`)
					.$wrapper.append(grey_line(__("This item has no default BOM. Add its BOM first.")));
			}
		});
		set_primary_disabled(dialog, !ready.length);
		dialog.show();
	});
}

function job_work_fields(setup, i) {
	return [
		{
			fieldname: `service_item_${i}`,
			fieldtype: "Link",
			options: "Item",
			label: __("Service item"),
			reqd: 1,
			default: setup.service_item,
			get_query: () => ({ filters: { is_stock_item: 0, is_fixed_asset: 0, disabled: 0 } }),
			filter_description: __("Only services are listed: no stock or asset items."),
		},
		{ fieldtype: "Column Break" },
		{
			fieldname: `finished_good_qty_${i}`,
			fieldtype: "Float",
			label: __("Finished goods per service unit"),
			reqd: 1,
			non_negative: 1,
			default: setup.finished_good_qty,
		},
		{ fieldtype: "Section Break" },
		{
			fieldname: `materials_${i}`,
			fieldtype: "Table",
			label: __("Materials sent to the job worker"),
			cannot_add_rows: 1,
			cannot_delete_rows: 1,
			data: setup.materials.map((row) => ({
				...row,
				__checked: row.held ? 1 : 0,
				material:
					row.item_name && row.item_name !== row.item_code
						? `${row.item_code}: ${row.item_name}`
						: row.item_code,
				note: row.held ? "" : __("added by you"),
			})),
			fields: [
				{
					fieldname: "material",
					fieldtype: "Data",
					label: __("Material"),
					in_list_view: 1,
					read_only: 1,
					columns: 5,
				},
				{
					fieldname: "qty",
					fieldtype: "Float",
					label: __("Qty"),
					in_list_view: 1,
					read_only: 1,
					columns: 2,
				},
				{
					fieldname: "uom",
					fieldtype: "Data",
					label: __("Unit"),
					in_list_view: 1,
					read_only: 1,
					columns: 1,
				},
				{
					fieldname: "note",
					fieldtype: "Data",
					label: __("Note"),
					in_list_view: 1,
					read_only: 1,
					columns: 2,
				},
				{ fieldname: "item_code", fieldtype: "Data", hidden: 1 },
			],
		},
	];
}

class OpenOrdersWindow extends SheetWindow {
	constructor(frm, order_type, checks, result) {
		const text = ORDER_TEXT[order_type];
		const warehouse = (fieldname, label, columns) =>
			list_column(fieldname, "Link", label, columns, {
				options: "Warehouse",
				get_query: () => ({ filters: { company: frm.doc.company, is_group: 0 } }),
			});
		const fields = {
			"Work Order": [
				list_column("order_no", "Data", __("Order no"), 1),
				list_column("item_name", "Data", __("Item to make"), 2),
				list_column("bom", "Link", __("BOM"), 2, { options: "BOM" }),
				list_column("qty", "Float", __("Qty left"), 1),
				list_column("uom", "Data", __("Unit"), 1),
				warehouse("wip_warehouse", __("WIP warehouse"), 1),
				warehouse("warehouse", __("Finished goods to"), 2),
			],
			"Subcontracting Order": [
				list_column("order_no", "Data", __("Purchase order"), 1),
				list_column("party_name", "Data", __("Job worker"), 2),
				list_column("item_name", "Data", __("Item to receive"), 2),
				list_column("qty", "Float", __("Qty left"), 1),
				list_column("uom", "Data", __("Unit"), 1),
				list_column("rate", "Currency", __("Job work rate"), 1),
				warehouse("warehouse", __("Receive into"), 2),
				{ fieldname: "order_date", fieldtype: "Date", label: __("Order date") },
				{ fieldname: "delivery_date", fieldtype: "Date", label: __("Required by") },
				{ fieldname: "service_item", fieldtype: "Link", options: "Item", label: __("Service item") },
				{ fieldname: "bom", fieldtype: "Link", options: "BOM", label: __("BOM") },
				{ fieldname: "material", fieldtype: "Data", label: __("Material at the job worker") },
			],
		}[order_type] || [
			list_column("order_no", "Data", __("Order no"), 1),
			list_column("party_name", "Data", text.party, 2),
			list_column("item_name", "Data", __("Item"), 2),
			list_column("qty", "Float", __("Qty left"), 1),
			list_column("uom", "Data", __("Unit"), 1),
			list_column("rate", "Currency", __("Rate"), 1),
			list_column(
				"delivery_date",
				"Date",
				order_type === "Sales Order" ? __("Delivery date") : __("Required by"),
				1
			),
			warehouse("warehouse", order_type === "Sales Order" ? __("Deliver from") : __("Receive into"), 1),
			{ fieldname: "order_date", fieldtype: "Date", label: __("Order date") },
		];
		const where = {
			"Sales Order": "open_orders",
			"Purchase Order": "open_orders",
			"Subcontracting Order": "job_work_orders",
		}[order_type];
		super(frm, {
			title: text.title,
			where: where,
			has_data: order_rows(frm, order_type, false).length,
			save_method: "save_open_orders",
			get_method: "get_open_orders",
			args: { order_type: order_type },
			fields: [
				...fields,
				{ fieldname: "source", fieldtype: "Data", hidden: 1 },
				{ fieldname: "doc_idx", fieldtype: "Int", hidden: 1 },
			],
		});
		this.order_type = order_type;
		this.text = text;
		this.result = result;
		this.fieldnames = [...fields.map((df) => df.fieldname), "source"];
		this.load(checks);
	}

	load(checks) {
		this.checks = checks;
		this.drafts = order_rows(this.frm, this.order_type, false).map((row) => ({
			...Object.fromEntries(this.fieldnames.map((field) => [field, row[field]])),
			doc_idx: row.idx,
		}));
	}

	render() {
		const { checks, frm, order_type } = this;
		this.make_head(this.text.intro.replace("{0}", this.day), {
			upload: () => this.upload("import_open_orders", null),
			menu: [
				checks.made && {
					label: made_orders_title(order_type),
					icon: "list",
					onclick: () => {
						this.dialog.hide();
						show_made_orders(frm, order_type);
					},
				},
				{
					label: __("Download sheet"),
					icon: "download",
					onclick: () =>
						download_sheet(frm.doc.company, "open_orders.download_open_orders", {
							order_type: order_type,
						}),
				},
			].filter(Boolean),
		});
		this.set_stats(
			checks.drafts
				? [
						frappe.ui.stat_card({
							label: __("On your sheet"),
							value: count_orders(order_type, checks.orders),
							caption: __("{0} left", [format_quantities(checks.quantities)]),
						}),
						frappe.ui.stat_card({
							label: __("Made"),
							value: checks.made ? count_orders(order_type, checks.made) : __("Nothing yet"),
						}),
				  ]
				: []
		);
		const go = (label, open) => ({
			label,
			onclick: () => {
				this.dialog.hide();
				open();
			},
		});
		const customer = order_type === "Sales Order";
		const actions = {
			missing_parties: [
				go(customer ? __("Go to Customers") : __("Go to Suppliers"), () =>
					open_party_import(frm, customer ? "Customer" : "Supplier")
				),
			],
			missing_items: [go(__("Go to Items"), () => open_party_import(frm, "Item"))],
			no_bom: [go(__("Go to BOMs"), () => show_boms(frm))],
			no_job_worker: [
				go(__("Go to Warehouses"), () =>
					show_warehouses(frm, {
						job_workers: true,
						after: () => show_open_orders(frm, order_type),
					})
				),
			],
			material_not_posted: [
				go(__("Go to Opening stock"), () => {
					show_tab(frm, "stock_tab");
					show_opening_stock(frm);
				}),
			],
			not_subcontracted: [
				{
					label: __("Mark them subcontracted"),
					onclick: () =>
						(this.dirty ? this.save() : Promise.resolve())
							.then(() => call_method(frm, "mark_items_subcontracted", {}, { freeze: true }))
							.then(() => this.refresh()),
				},
			],
		};
		// the names the rows carry, for reds about parties or items
		const field = { missing_parties: "party_name", missing_items: "item_name", no_bom: "item_name" };
		const names_of = (key, idxs) => {
			if (!field[key]) return undefined;
			const set = new Set(idxs);
			const names = [
				...new Set(
					this.get_rows()
						.filter((row) => set.has(row.doc_idx))
						.map((row) => row[field[key]])
						.filter(Boolean)
				),
			];
			return names.slice(0, 5).join(", ") + (names.length > 5 ? ", …" : "");
		};
		// the cells a red is about, marked in its rows
		const fields = {
			no_party_name: ["party_name"],
			missing_parties: ["party_name"],
			missing_items: ["item_name"],
			no_qty: ["qty"],
			unknown_units: ["uom"],
			no_bom: ["bom"],
			no_wip: ["wip_warehouse"],
			no_warehouse: ["warehouse"],
		};
		const alerts = (checks.reds || []).map((item) => ({
			theme: "red",
			key: item.key,
			title: item.text,
			description: names_of(item.key, item.rows),
			rows: item.rows,
			fields: fields[item.key],
			badge: ["missing_parties", "missing_items"].includes(item.key) ? __("Not in ERPNext") : undefined,
			buttons: [
				...(actions[item.key] || []),
				...(item.fix ? [fix_button(item.fix, frm, () => this.refresh())] : []),
				...(["job_work_setup", "material_not_at_job_worker"].includes(item.key)
					? [
							{
								label: __("Set up job work"),
								onclick: () =>
									(this.dirty ? this.save() : Promise.resolve()).then(() =>
										show_job_work_setup(frm, item.items, () => this.refresh())
									),
							},
					  ]
					: []),
			],
		}));
		this.set_alerts(alerts, [
			this.result &&
				plural(
					this.result.added,
					__("1 row read from the sheet."),
					__("{0} rows read from the sheet.", [this.result.added])
				),
			this.result?.skipped &&
				plural(
					this.result.skipped,
					__("1 row is already made and is left out."),
					__("{0} rows are already made and are left out.", [this.result.skipped])
				),
			this.result?.replaced &&
				plural(
					this.result.replaced,
					__("1 draft row with the same order number was replaced."),
					__("{0} draft rows with the same order number were replaced.", [this.result.replaced])
				),
			order_type === "Subcontracting Order" && __("Receive goods back with a Subcontracting Receipt."),
		]);
		this.refresh_footer();
	}

	refresh_footer() {
		if (this.view !== "sheet") return;
		const { checks } = this;
		this.dialog.set_primary_action(order_type_make_label(this.order_type, checks.orders), () =>
			this.post()
		);
		this.dialog.set_secondary_action_label(__("Save draft"));
		this.dialog.set_secondary_action(() => this.save_draft());
		const empty = !this.get_rows().length;
		set_primary_disabled(this.dialog, empty || (!this.dirty && Boolean(checks.blocker)));
	}

	confirm_post() {
		const { frm, order_type } = this;
		this.show_confirm({
			title: __("{0}?", [order_type_make_label(order_type, this.checks.orders)]),
			back: __("Nothing is made until you confirm."),
			body: [
				make_kv_lines([
					[
						__("To make"),
						__("{0} · {1} left", [
							count_orders(order_type, this.checks.orders),
							format_quantities(this.checks.quantities),
						]),
					],
				]),
				grey_line(__("No money moves: orders post nothing to your books.")),
				grey_line(this.text.note),
				this.checks.unnumbered &&
					grey_line(
						plural(
							this.checks.unnumbered,
							order_type === "Sales Order"
								? __("1 row has no order number: it becomes an order of its own customer.")
								: __("1 row has no order number: it becomes an order of its own supplier."),
							order_type === "Sales Order"
								? __("{0} rows have no order number: they become one order per customer.", [
										this.checks.unnumbered,
								  ])
								: __("{0} rows have no order number: they become one order per supplier.", [
										this.checks.unnumbered,
								  ])
						)
					),
			].filter(Boolean),
			primary: order_type_make_label(order_type, this.checks.orders),
			on_primary: () =>
				call_method(frm, "make_open_orders", { order_type: order_type }, { freeze: true }).then(
					(r) => {
						this.dialog.hide();
						after_run(
							frm,
							r.message,
							__("Making the orders in the background. This page updates when it's done."),
							(result) =>
								frappe.ui.toast({
									type: "success",
									message: __("{0} made.", [count_orders(order_type, result.length)]),
									action: {
										label: __("View"),
										onclick: () => show_made_orders(frm, order_type),
									},
								})
						);
					}
				),
		});
	}
}

function order_type_make_label(order_type, count) {
	if (!count) {
		return order_type === "Work Order"
			? __("Make work orders")
			: order_type === "Subcontracting Order"
			? __("Make subcontracting orders")
			: __("Make orders");
	}
	return order_type === "Work Order"
		? plural(count, __("Make 1 work order"), __("Make {0} work orders", [count]))
		: order_type === "Subcontracting Order"
		? plural(count, __("Make 1 subcontracting order"), __("Make {0} subcontracting orders", [count]))
		: plural(count, __("Make 1 order"), __("Make {0} orders", [count]));
}

function show_made_orders(frm, order_type) {
	const party = ORDER_TEXT[order_type].party;
	const unique = (rows) => [...new Set(rows.map((row) => row.document))];
	show_posted_window(frm, {
		title: made_orders_title(order_type),
		load: () => get_data(frm, "get_made_orders", { order_type: order_type }).then((r) => r.message),
		posted_word: (when) => __("Made {0}", [when]),
		group_line: (group) => count_orders(order_type, group.orders),
		undone: (made) =>
			(made.undone || []).map((group) => ({
				source: group.source,
				line: __("{0} · removed from ERPNext", [count_rows(group.count)]),
				download: () =>
					download_sheet(frm.doc.company, "open_orders.download_open_orders", {
						order_type: order_type,
						undone_from: group.source,
					}),
			})),
		fields: [
			{
				fieldname: "document",
				fieldtype: "Link",
				options: order_type,
				label: __(order_type),
				columns: 2,
			},
			{
				fieldname: "order_no",
				fieldtype: "Data",
				label: __("Order no"),
				columns: party ? 1 : 2,
			},
			...(party ? [{ fieldname: "party_name", fieldtype: "Data", label: party, columns: 2 }] : []),
			{
				fieldname: "item_code",
				fieldtype: "Data",
				label: order_type === "Work Order" ? __("Item to make") : __("Item"),
				columns: party ? 2 : 3,
			},
			{ fieldname: "qty", fieldtype: "Float", label: __("Qty left"), columns: 1 },
			{ fieldname: "uom", fieldtype: "Data", label: __("Unit"), columns: 1 },
			{
				fieldname: "done_since",
				fieldtype: "Float",
				label: order_type === "Work Order" ? __("Produced since") : __("Done since"),
				columns: 1,
			},
		],
		rows: (group) => group.rows.map((row) => ({ ...row })),
		blocked: (row) => flt(row.done_since) > 0,
		blocked_alert: (rows) => ({
			title: __("{0} already have deliveries, receipts or production.", [
				count_orders(order_type, unique(rows).length),
			]),
			description: __("Cancel those first."),
		}),
		undo_label: (selected) =>
			selected.length ? __("Undo {0} selected", [unique(selected).length]) : __("Undo this upload"),
		confirm_title: (rows) => __("Undo {0}?", [count_orders(order_type, unique(rows).length)]),
		confirm_text: (rows) =>
			plural(
				unique(rows).length,
				__("Its rows are kept, so you can fix it and make it again."),
				__("Their rows are kept, so you can fix them and make them again.")
			),
		confirm_what: (rows) => ({
			count: unique(rows).length,
			what: count_orders(order_type, unique(rows).length),
			one: __(order_type),
			has_ledger: false,
		}),
		confirm_rows: (rows) =>
			rows.map((row) => [
				$(`<span class="text-base text-ink-gray-8 shrink-0"></span>`).text(row.document),
				$(`<span class="text-base text-ink-gray-5 grow truncate"></span>`).text(
					[row.party_name, row.item_code].filter(Boolean).join(" · ")
				),
				$(`<span class="text-base text-ink-gray-8 shrink-0"></span>`).text(
					[format_qty(row.qty), row.uom].filter(Boolean).join(" ")
				),
			]),
		confirm_button: (rows) => __("Undo {0}", [count_orders(order_type, unique(rows).length)]),
		undo: (group, selected) =>
			call_method(frm, "undo_open_orders", {
				order_type: order_type,
				source: group.source,
				documents: selected ? unique(selected) : null,
			}),
		after_undo: (result) =>
			frappe.ui.toast({
				type: "success",
				message: __("{0} undone", [count_orders(order_type, result.removed)]),
				description: [__("They're cancelled and deleted in ERPNext."), kept_cancelled_line(result)]
					.filter(Boolean)
					.join(" "),
			}),
	});
}
