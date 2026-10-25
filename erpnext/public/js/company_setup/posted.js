import {
	UNDOING_TEXT,
	after_run,
	call_method,
	count_rows,
	count_warehouses,
	day_before_text,
	download_sheet,
	format_qty,
	format_time_posted,
	get_data,
	grey_line,
	make_alerts,
	make_back_header,
	make_back_line,
	make_box,
	make_kv_lines,
	make_read_table,
	make_text,
	make_title_cell,
	make_view_button,
	money,
	plural,
	set_primary_disabled,
	short_account,
	source_name,
} from "./common";
import { SheetWindow, keep_rate_on_qty, set_rate_on_amount, setup_dialog_grid } from "./sheet_window";
import { show_tab } from "./overview";
import { make_delete_choice } from "./go_live";
import { show_undo_result } from "./invoices_posted";
import { show_opening_stock, show_stock_corrected, stock_change_text } from "./stock";

// ---------- Posted stock, assets and made orders: by upload, then View, undo ----------

// What was posted from each upload, to view and undo in part or whole. opts says what one row is.
export function show_posted_window(frm, opts) {
	const live = frm.doc.status === "Live";
	const can_undo = !live || opts.undo_after_live;
	const dialog = new frappe.ui.Dialog({
		title: opts.title,
		size: "extra-large",
		fields: [
			{ fieldname: "body", fieldtype: "HTML" },
			{
				fieldname: "rows",
				fieldtype: "Table",
				hidden: 1,
				cannot_add_rows: 1,
				cannot_delete_rows: 1,
				in_place_edit: 1,
				data: [],
				fields: opts.fields.map((df) => ({ in_list_view: 1, read_only: 1, ...df })),
			},
			{ fieldname: "extra", fieldtype: "HTML" },
		],
		secondary_action_label: __("Close"),
		secondary_action: () => dialog.hide(),
	});
	const $body = dialog.get_field("body").$wrapper;
	const grid = dialog.fields_dict.rows.grid;
	setup_dialog_grid(grid);
	const line_of = (group) =>
		[group.posted_on && opts.posted_word(format_time_posted(group.posted_on)), opts.group_line(group)]
			.filter(Boolean)
			.join(" · ");
	const show_grid = (rows) => {
		dialog.set_df_property("rows", "hidden", rows ? 0 : 1);
		grid.df.data = rows || [];
		grid.refresh();
	};
	const set_primary = (label, action) => {
		const $primary = dialog.get_primary_btn();
		if (!label) return $primary.addClass("hide");
		dialog.set_primary_action(label, action);
		$primary.attr("data-theme", "red");
	};
	let posted;

	const show_list = () => {
		dialog.set_title(opts.title);
		set_primary(null);
		dialog.set_secondary_action_label(__("Close"));
		dialog.set_secondary_action(() => dialog.hide());
		show_grid(null);
		dialog.get_field("extra").$wrapper.empty();
		const extra_rows = opts.extra_rows?.(posted) || [];
		const undone = opts.undone?.(posted) || [];
		$body.empty().append(
			$(`<div class="flex flex-col gap-3"></div>`).append(
				grey_line(
					can_undo
						? __("You can undo these until you go live.")
						: __("One row for each sheet or typed-in batch.")
				),
				posted.groups.length || extra_rows.length
					? make_box([
							...posted.groups.map((group) => [
								make_title_cell(source_name(group.source), line_of(group)),
								opts.group_amount
									? make_text(opts.group_amount(group), "text-ink-gray-8 shrink-0")
									: [],
								make_view_button(() => show_group(group)),
							]),
							...extra_rows,
					  ])
					: [],
				undone.length
					? [
							$(`<div class="text-base-medium text-ink-gray-8 mt-2"></div>`).text(__("Undone")),
							make_box(
								undone.map((group) => [
									make_title_cell(source_name(group.source), group.line),
									group.amount ? make_text(group.amount, "text-ink-gray-8 shrink-0") : [],
									group.download
										? frappe.ui.button({
												label: __("Download"),
												icon: "download",
												variant: "ghost",
												size: "sm",
												onclick: () => group.download(),
										  })
										: [],
								])
							),
					  ]
					: []
			)
		);
	};

	const show_group = (group, first) => {
		dialog.set_title(opts.title);
		set_primary(null);
		dialog.set_secondary_action_label(__("Close"));
		dialog.set_secondary_action(() => dialog.hide());
		const rows = opts.rows(group);
		show_grid(first ? [...rows.filter(first), ...rows.filter((row) => !first(row))] : rows);
		const $head = $(`<div class="flex flex-col gap-3 mb-3"></div>`);
		$body.empty().append($head);
		const refresh = () => {
			const selected = grid.get_selected_children();
			const blocked = (selected.length ? selected : grid.df.data).filter(opts.blocked);
			const alert = blocked.length && can_undo && opts.blocked_alert(blocked, group, dialog);
			$head.empty().append(
				make_back_header(source_name(group.source), line_of(group), () => show_list(), [
					...(opts.group_actions?.(group, dialog) || []),
					...(can_undo
						? [
								frappe.ui.button({
									label: opts.undo_label(selected, group),
									icon: "undo-2",
									theme: "red",
									variant: selected.length ? "subtle" : "outline",
									disabled: Boolean(blocked.length),
									onclick: () => show_confirm(group, selected.length ? selected : null),
								}),
						  ]
						: []),
				]),
				alert
					? make_alerts([
							{
								theme: "red",
								...alert,
								buttons: [
									...(alert.buttons || []),
									{
										label: __("Show rows"),
										variant: "outline",
										onclick: () => show_group(group, opts.blocked),
									},
								],
							},
					  ])
					: [],
				opts.group_note ? opts.group_note(group) : []
			);
		};
		grid.wrapper.off("change.undo").on("change.undo", ".grid-row-check", () => refresh());
		refresh();
	};

	const show_confirm = (group, selected) => {
		const rows = selected || opts.rows(group);
		show_grid(null);
		dialog.set_title(opts.confirm_title(rows, selected));
		const $choice = make_delete_choice(frm, {
			...opts.confirm_what(rows, selected),
			source: group.source,
		});
		$body.empty().append(
			$(`<div class="flex flex-col gap-3"></div>`).append(
				$choice,
				make_back_line(opts.confirm_text(rows, selected), () => show_group(group)),
				make_box([
					...opts.confirm_rows(rows),
					...(opts.total
						? [
								[
									make_text(__("Total"), "text-ink-gray-6 grow"),
									make_text(
										money(frm, opts.total(rows)),
										"text-base-medium text-ink-gray-8 shrink-0"
									),
								],
						  ]
						: []),
				]),
				opts.confirm_note?.(rows, selected) || []
			)
		);
		dialog.set_secondary_action_label(__("Back"));
		dialog.set_secondary_action(() => show_group(group));
		set_primary(opts.confirm_button(rows, selected), () =>
			opts.undo(group, selected, $choice.delete_entries()).then((r) => {
				dialog.hide();
				after_run(frm, r.message, UNDOING_TEXT(), (result) => opts.after_undo(result, group));
			})
		);
	};

	opts.load().then((data) => {
		posted = data;
		show_list();
		dialog.show();
	});
}

export function show_posted_stock(frm) {
	const match = frm.doc.__onload?.stock_match;
	const line_of = Object.fromEntries(
		(match?.warehouses || []).map((row) => [row.warehouse, row.line ?? row.account])
	);
	let difference;
	show_posted_window(frm, {
		title: __("Posted stock"),
		load: () =>
			get_data(frm, "get_posted_entries", { kind: "stock" }).then((r) => {
				difference = r.message.difference;
				return r.message;
			}),
		posted_word: (when) => __("Posted {0}", [when]),
		group_line: (group) =>
			[count_rows(group.rows), count_warehouses(group.warehouses.length)].join(" · "),
		group_amount: (group) => money(frm, group.value),
		extra_rows: (posted) =>
			posted.difference
				? [
						[
							make_title_cell(
								__("Stock difference"),
								[
									posted.difference.posted_on &&
										__("Posted {0}", [
											frappe.datetime
												.str_to_user(posted.difference.posted_on)
												.split(" ")[0],
										]),
									[
										...new Set(
											Object.keys(posted.difference.lines).map(
												(account) => short_account(frm, account) || __("Stock")
											)
										),
									].join(", "),
								]
									.filter(Boolean)
									.join(" · "),
								"text-ink-gray-8"
							),
							make_text(money(frm, posted.difference.amount), "text-ink-gray-8 shrink-0"),
						],
				  ]
				: [],
		undone: (posted) =>
			posted.undone.map((group) => ({
				source: group.source,
				line: __("{0} · taken out of your books", [count_rows(group.rows)]),
				download: () =>
					download_sheet(frm.doc.company, "opening_stock.download_opening_stock", {
						undone_from: group.source,
					}),
			})),
		fields: [
			{ fieldname: "warehouse", fieldtype: "Data", label: __("Warehouse"), columns: 4 },
			{ fieldname: "rows", fieldtype: "Int", label: __("Rows"), columns: 2 },
			{ fieldname: "value", fieldtype: "Currency", label: __("Value"), columns: 2 },
			{ fieldname: "later_entries", fieldtype: "Int", label: __("Later stock entries"), columns: 2 },
		],
		rows: (group) => group.warehouses.map((row) => ({ ...row })),
		blocked: (row) => row.later_entries > 0,
		blocked_alert: (rows, group, dialog) => ({
			title:
				rows.length === 1
					? __("{0} can not be undone.", [rows[0].warehouse])
					: __("{0} warehouses can not be undone.", [rows.length]),
			description: __("{0} stock entries came after {1}. Correct the wrong rows instead.", [
				rows.reduce((sum, row) => sum + row.later_entries, 0),
				day_before_text(frm),
			]),
			buttons: [{ label: __("Correct rows"), onclick: () => show_correct_rows(frm, group, dialog) }],
		}),
		group_actions: (group, dialog) =>
			frm.doc.status === "Live"
				? []
				: [
						frappe.ui.button({
							label: __("Correct rows"),
							variant: "ghost",
							onclick: () => show_correct_rows(frm, group, dialog),
						}),
				  ],
		undo_label: (selected, group) =>
			selected.length
				? __("Undo {0} · {1}", [
						count_warehouses(selected.length),
						money(
							frm,
							selected.reduce((sum, row) => sum + flt(row.value), 0)
						),
				  ])
				: __("Undo this upload · {0}", [money(frm, group.value)]),
		confirm_title: (rows, selected) =>
			selected ? __("Undo {0}?", [count_warehouses(rows.length)]) : __("Undo this upload?"),
		confirm_text: () => __("You can download the rows, fix them and upload again."),
		// "these": a warehouse's stock entries are many
		confirm_what: (rows, selected) => ({
			count: 2,
			kind: "stock",
			names: selected ? selected.map((row) => row.warehouse) : null,
			what: __("stock entries of {0}", [count_warehouses(rows.length)]),
		}),
		confirm_rows: (rows) =>
			rows.map((row) => [
				$(`<span class="text-base text-ink-gray-8 grow truncate"></span>`).text(row.warehouse),
				$(`<span class="text-base text-ink-gray-5 shrink-0"></span>`).text(count_rows(row.rows)),
				$(`<span class="text-base text-ink-gray-8 shrink-0"></span>`).text(money(frm, row.value)),
			]),
		total: (rows) => rows.reduce((sum, row) => sum + flt(row.value), 0),
		confirm_note: (rows) => {
			const lines = new Set(Object.keys(difference?.lines || {}));
			return rows.some((row) => lines.has(line_of[row.warehouse]))
				? grey_line(
						__("The posted stock difference of {0} is undone too.", [
							money(frm, difference.amount),
						])
				  )
				: [];
		},
		confirm_button: (rows, selected) =>
			selected ? __("Undo {0}", [count_warehouses(rows.length)]) : __("Undo this upload"),
		undo: (group, selected, delete_entries) =>
			call_method(frm, "undo_opening_stock", {
				source: group.source,
				warehouses: selected ? selected.map((row) => row.warehouse) : null,
				delete_entries: delete_entries,
			}),
		after_undo: (result, group) => show_undo_result(frm, "stock", result, group.source),
	});
}

// Change the qty or value of posted rows: one correcting entry per warehouse, after the opening
function show_correct_rows(frm, group, posted_dialog) {
	posted_dialog.hide();
	const posted = (frm.doc.opening_stock || []).filter(
		(row) => row.status === "Posted" && (row.source || "") === group.source
	);
	const originals = Object.fromEntries(
		posted.map((row) => [row.name, { qty: row.qty, amount: row.amount }])
	);
	// a batch or serial column tells rows of one item apart
	const tracked = posted.some((row) => row.batch_no)
		? "batch_no"
		: posted.some((row) => row.serial_no)
		? "serial_no"
		: null;
	const win = new SheetWindow(frm, {
		title: __("Posted stock"),
		fields: [
			{
				fieldname: "item_name",
				fieldtype: "Data",
				label: __("Item"),
				in_list_view: 1,
				read_only: 1,
				columns: tracked ? 2 : 3,
			},
			{
				fieldname: "warehouse",
				fieldtype: "Data",
				label: __("Warehouse"),
				in_list_view: 1,
				read_only: 1,
				columns: 2,
			},
			{
				fieldname: "qty",
				fieldtype: "Float",
				label: __("Qty"),
				in_list_view: 1,
				columns: 1,
				onchange: keep_rate_on_qty,
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
				fieldname: "amount",
				fieldtype: "Currency",
				label: __("Value"),
				in_list_view: 1,
				columns: 2,
				onchange: set_rate_on_amount,
			},
			{
				fieldname: "batch_no",
				fieldtype: "Data",
				label: __("Batch"),
				read_only: 1,
				in_list_view: tracked === "batch_no" ? 1 : 0,
				columns: 1,
			},
			{
				fieldname: "serial_no",
				fieldtype: "Small Text",
				label: __("Serial No"),
				read_only: 1,
				in_list_view: tracked === "serial_no" ? 1 : 0,
				columns: 1,
			},
			{ fieldname: "name", fieldtype: "Data", hidden: 1 },
			{ fieldname: "item_code", fieldtype: "Data", hidden: 1 },
		],
	});
	win.grid.df.cannot_add_rows = 1;
	win.grid.df.cannot_delete_rows = 1;
	const changed = () =>
		win.get_rows().filter((row) => {
			const old = originals[row.name];
			return (
				Math.abs(flt(row.qty) - flt(old.qty)) > 0.0001 ||
				Math.abs(flt(row.amount) - flt(old.amount)) >= 0.01
			);
		});
	win.render = () => {
		win.dialog
			.get_field("head")
			.$wrapper.empty()
			.append(
				make_back_header(
					__("Correct rows"),
					__("{0} · Dated {1}, against Temporary Opening.", [
						source_name(group.source),
						day_before_text(frm),
					]),
					() => {
						win.dialog.hide();
						show_posted_stock(frm);
					}
				).addClass("mb-3")
			);
		win.refresh_footer();
	};
	win.refresh_footer = () => {
		if (win.view !== "sheet") return;
		const count = changed().length;
		win.dialog
			.get_field("notes")
			.$wrapper.empty()
			.append(
				grey_line(
					plural(
						win.get_rows().length,
						__("{0} changed of 1 row", [count]),
						__("{0} changed of {1} rows", [count, win.get_rows().length])
					)
				).addClass("mt-2 text-right")
			);
		win.dialog.set_secondary_action_label(__("Close"));
		win.dialog.set_secondary_action(() => win.dialog.hide());
		win.dialog.set_primary_action(
			count ? plural(count, __("Correct 1 row"), __("Correct {0} rows", [count])) : __("Correct rows"),
			() => check()
		);
		set_primary_disabled(win.dialog, !count || Boolean(win.refused));
	};
	win.changed = () => {
		win.refused = null;
		win.set_alerts([]);
		win.refresh_footer();
	};
	// what the server would refuse, said here before the confirm
	const check = () => {
		const rows = changed();
		return get_data(frm, "check_stock_correction", {
			rows: rows.map((row) => ({ name: row.name, qty: row.qty, amount: row.amount })),
		}).then((r) => {
			const result = r.message;
			if (!result.blocker) return confirm(result.later_entries);
			win.refused = result;
			const below = new Set(
				(result.below_zero || []).map((item) => `${item.item_code}::${item.warehouse}`)
			);
			const is_below = (row) => below.has(`${row.item_code}::${row.warehouse}`);
			win.set_alerts([
				{
					theme: "red",
					title: result.blocker,
					description: (result.below_zero || []).length
						? __("Put their quantities back, or ask your CA.")
						: undefined,
					rows: is_below,
				},
			]);
			win.refresh_footer();
		});
	};
	const confirm = (later_entries) => {
		const rows = changed();
		const net = rows.reduce((sum, row) => sum + flt(row.amount) - flt(originals[row.name].amount), 0);
		win.show_confirm({
			title: plural(rows.length, __("Correct 1 row?"), __("Correct {0} rows?", [rows.length])),
			back: __("Nothing changes until you confirm."),
			body: [
				make_kv_lines([
					[__("To correct"), [count_rows(rows.length), stock_change_text(frm, net)].join(" · ")],
					[__("Dated"), day_before_text(frm)],
				]),
				later_entries &&
					grey_line(
						__(
							"Cost of goods sold and profit since {0} change. ERPNext works out the new costs in the background, usually within the hour.",
							[frappe.datetime.str_to_user(frm.doc.migration_date)]
						)
					),
			].filter(Boolean),
			primary: plural(rows.length, __("Correct 1 row"), __("Correct {0} rows", [rows.length])),
			on_primary: () =>
				call_method(
					frm,
					"correct_opening_stock",
					{ rows: rows.map((row) => ({ name: row.name, qty: row.qty, amount: row.amount })) },
					{ freeze: true }
				).then((r) => {
					win.dialog.hide();
					after_run(
						frm,
						r.message,
						__("Correcting the rows in the background. This page updates when it's done."),
						(result) => show_stock_corrected(frm, result)
					);
				}),
		});
	};
	win.set_rows(
		posted.map((row) => ({
			name: row.name,
			item_name: row.item_name,
			item_code: row.item_code,
			warehouse: row.warehouse,
			qty: row.qty,
			uom: row.uom,
			amount: row.amount,
			old_qty: row.qty,
			old_amount: row.amount,
			batch_no: row.batch_no,
			serial_no: row.serial_no,
		}))
	);
	win.show_sheet();
	win.dialog.show();
}

// Stock at job workers: the job worker rows of Opening stock, read only
export function show_job_worker_stock(frm) {
	const job_workers = new Set(
		(frm.doc.warehouses || [])
			.filter((row) => row.kind === "Job worker" && row.warehouse)
			.map((row) => row.warehouse)
	);
	const rows = (frm.doc.opening_stock || []).filter((row) => job_workers.has(row.warehouse));
	const soon = frappe.datetime.add_days(frappe.datetime.get_today(), 30);
	const due = rows.filter((row) => row.return_due && row.return_due <= soon);
	const currency = erpnext.get_currency(frm.doc.company);
	const dialog = new frappe.ui.Dialog({
		title: __("Stock at job workers"),
		size: "extra-large",
		fields: [{ fieldname: "body", fieldtype: "HTML" }],
		secondary_action_label: __("Close"),
		secondary_action: () => dialog.hide(),
	});
	const draw = (first) => {
		const sorted = first ? [...rows.filter(first), ...rows.filter((row) => !first(row))] : rows;
		dialog
			.get_field("body")
			.$wrapper.empty()
			.append(
				$(`<div class="flex flex-col gap-3"></div>`).append(
					$(`<div class="flex items-center justify-between gap-3"></div>`).append(
						$(`<div class="text-p-base text-ink-gray-7"></div>`).text(
							__("Stock at your job workers on {0}, from Opening stock.", [
								day_before_text(frm),
							])
						),
						frappe.ui.button({
							label: __("Open in Opening stock"),
							onclick: () => {
								dialog.hide();
								show_tab(frm, "stock_tab");
								show_opening_stock(frm);
							},
						})
					),
					due.length
						? make_alerts([
								{
									theme: "yellow",
									title: plural(
										due.length,
										__("1 row is due back within 30 days."),
										__("{0} rows are due back within 30 days.", [due.length])
									),
									buttons: [
										{
											label: __("Show rows"),
											onclick: () => draw((row) => due.includes(row)),
										},
									],
								},
						  ])
						: [],
					rows.length
						? make_read_table(
								[
									[__("Item")],
									[__("Job worker")],
									[__("Qty"), true],
									[__("Value"), true],
									[__("Challan no")],
									[__("Challan date")],
									[__("Return due")],
								],
								sorted.map((row) => [
									row.item_name,
									row.warehouse_name || row.warehouse,
									format_qty(row.qty),
									format_currency(row.amount, currency),
									row.challan_no || "",
									row.challan_date ? frappe.datetime.str_to_user(row.challan_date) : "",
									row.return_due ? frappe.datetime.str_to_user(row.return_due) : "",
								])
						  )
						: grey_line(__("No stock at job workers in Opening stock yet."))
				)
			);
	};
	draw();
	dialog.show();
}
