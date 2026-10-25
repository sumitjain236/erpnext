import {
	after_run,
	call_method,
	close_and_reload,
	count_rows,
	count_warehouses,
	day_before_text,
	download_sheet,
	fix_button,
	get_data,
	grey_line,
	make_alerts,
	make_kv_lines,
	make_read_table,
	money,
	plural,
	set_primary_disabled,
	short_account,
	source_name,
} from "./common";
import { SheetWindow, keep_rate_on_qty, list_column, set_rate_on_amount } from "./sheet_window";
import { open_party_import } from "./imports";
import { show_warehouses } from "./warehouses";
import { show_posted_stock } from "./posted";

// ---------- Opening stock ----------

const STOCK_FIELDS = [
	"item_name",
	"warehouse_name",
	"qty",
	"uom",
	"rate",
	"amount",
	"stock_group",
	"batch_no",
	"expiry_date",
	"serial_no",
	"challan_no",
	"challan_date",
	"return_due",
	"source",
	"idx",
];

export function show_opening_stock(frm, result) {
	get_data(frm, "get_opening_stock").then((r) => new OpeningStockWindow(frm, r.message, result).show());
}

// The columns opening stock shows: the challan ones for job work, else the batch ones
function get_stock_layout(frm, checks) {
	const drafts = (frm.doc.opening_stock || []).filter((row) => row.status !== "Posted");
	const job_work =
		checks.reds.some((red) => red.key === "challan") ||
		drafts.some((row) => row.challan_no || row.challan_date);
	const batched = !job_work && drafts.some((row) => row.batch_no || row.expiry_date);
	return { drafts, job_work, batched };
}

class OpeningStockWindow extends SheetWindow {
	constructor(frm, checks, result) {
		super(frm, {
			title: __("Opening stock"),
			save_method: "save_opening_stock",
			get_method: "get_opening_stock",
			where: "opening_stock",
			has_data: get_stock_layout(frm, checks).drafts.length,
			checks: checks,
			extra_fields: [{ fieldname: "lines", fieldtype: "HTML" }],
		});
		this.views.sheet.push("lines");
		this.result = result;
		this.load(checks);
	}

	get_layout(checks) {
		const { job_work, batched } = get_stock_layout(this.frm, checks);
		return [job_work, batched].join();
	}

	get_fields(checks) {
		const { job_work, batched } = get_stock_layout(this.frm, checks);
		return [
			list_column("item_name", "Data", __("Item"), job_work || batched ? 2 : 3),
			list_column("warehouse_name", "Data", __("Warehouse"), job_work || batched ? 1 : 2),
			list_column("qty", "Float", __("Qty"), 1, { onchange: keep_rate_on_qty }),
			list_column("uom", "Data", __("Unit"), 1),
			// the rate shows a typo in qty or value at a glance
			list_column("rate", "Currency", __("Rate"), 1, { onchange: keep_rate_on_qty }),
			list_column("amount", "Currency", __("Value"), job_work ? 1 : 2, {
				onchange: set_rate_on_amount,
			}),
			job_work
				? list_column("challan_no", "Data", __("Challan no"), 1)
				: { fieldname: "challan_no", fieldtype: "Data", label: __("Challan no") },
			job_work
				? list_column("challan_date", "Date", __("Challan date"), 1)
				: { fieldname: "challan_date", fieldtype: "Date", label: __("Challan date") },
			job_work
				? list_column("return_due", "Date", __("Return due"), 1)
				: { fieldname: "return_due", fieldtype: "Date", label: __("Return due") },
			batched
				? list_column("batch_no", "Data", __("Batch"), 1)
				: { fieldname: "batch_no", fieldtype: "Data", label: __("Batch") },
			batched
				? list_column("expiry_date", "Date", __("Expiry date"), 1)
				: { fieldname: "expiry_date", fieldtype: "Date", label: __("Expiry date") },
			{ fieldname: "serial_no", fieldtype: "Small Text", label: __("Serial No") },
			{ fieldname: "stock_group", fieldtype: "Data", label: __("Stock group") },
			{ fieldname: "source", fieldtype: "Data", hidden: 1 },
			{ fieldname: "doc_idx", fieldtype: "Int", hidden: 1 },
		];
	}

	// back in front after warehouses were added in another browser tab: the checks again, unless
	// rows are being edited
	show() {
		super.show();
		const refresh = () => this.dialog.display && !this.dirty && this.refresh();
		$(window).on("focus", refresh);
		this.dialog.onhide = () => $(window).off("focus", refresh);
	}

	load(checks) {
		this.checks = checks;
		this.drafts = (this.frm.doc.opening_stock || [])
			.filter((row) => row.status !== "Posted")
			.map((row) => ({
				...Object.fromEntries(STOCK_FIELDS.map((field) => [field, row[field]])),
				old_qty: row.qty,
				old_amount: row.amount,
				doc_idx: row.idx,
			}));
	}

	render() {
		const { checks, frm } = this;
		const drafts = checks.drafts;
		const posted = checks.posted;
		const tb = checks.trial_balance;
		const lines = Object.keys(tb.lines).length;
		this.make_head(
			drafts.rows || !posted.rows
				? __("One row per item per warehouse, as on {0}. Upload one sheet or several.", [this.day])
				: __("One row per item per warehouse, as on {0}.", [this.day]),
			{
				upload: () =>
					this.upload(
						"import_opening_stock",
						this.get_rows().length &&
							__(
								"The sheet replaces draft rows of the same item and warehouse, and its own drafts if it was uploaded before. Other drafts and posted rows stay as they are. Continue?"
							)
					),
				menu: [
					posted.rows && {
						label: __("Posted stock"),
						icon: "list",
						onclick: () => {
							this.dialog.hide();
							show_posted_stock(frm);
						},
					},
					{
						label: __("Download sheet"),
						icon: "download",
						onclick: () =>
							download_sheet(frm.doc.company, "opening_stock.download_opening_stock"),
					},
					...[...new Set((frm.doc.undone_stock || []).map((row) => row.source || ""))].map(
						(source) => ({
							label: __("Undone rows back to drafts: {0}", [source_name(source)]),
							icon: "undo-2",
							onclick: () =>
								call_method(frm, "restore_undone_stock", { source }).then(() =>
									this.refresh()
								),
						})
					),
					...(tb.movable || []).map((account) => ({
						label: __("Keep {0} as a balance", [short_account(frm, account)]),
						icon: "undo-2",
						onclick: () =>
							frappe.confirm(
								__(
									"{0} goes back to Opening balances as a plain balance, and no stock rows post to it. Continue?",
									[short_account(frm, account)]
								),
								() =>
									call_method(frm, "move_to_opening_balances", { account }).then(() =>
										this.refresh()
									)
							),
					})),
				].filter(Boolean),
			}
		);
		const sources = (list) => list.map(source_name).join(", ");
		const value = (rows, amount) => __("{0} · {1}", [count_rows(rows), money(this.frm, amount)]);
		this.set_stats(
			[
				drafts.rows &&
					frappe.ui.stat_card({
						label: drafts.sources.length > 1 ? __("In draft") : __("On this sheet"),
						value: value(drafts.rows, drafts.value),
						caption: [
							this.result?.read &&
								plural(
									this.result.read,
									__("1 row read"),
									__("{0} rows read", [this.result.read])
								),
							sources(drafts.sources),
							count_warehouses(drafts.warehouses),
						]
							.filter(Boolean)
							.join(" · "),
					}),
				frappe.ui.stat_card({
					label: __("Posted"),
					value: posted.rows ? value(posted.rows, posted.value) : __("Nothing yet"),
					caption: posted.rows
						? drafts.rows
							? sources(posted.sources)
							: [
									plural(
										posted.sources.length,
										__("1 upload"),
										__("{0} uploads", [posted.sources.length])
									),
									count_warehouses(posted.warehouses),
							  ].join(" · ")
						: null,
				}),
				frappe.ui.stat_card({
					label: __("Your trial balance"),
					value: money(this.frm, tb.total),
					caption: lines
						? plural(
								lines,
								__("1 stock account"),
								__("{0} stock accounts, each checked on its own", [lines])
						  )
						: __("No stock in your trial balance"),
				}),
			].filter(Boolean)
		);
		const kept = Object.entries(tb.kept || {}).map(([account, amount]) =>
			__("{0} {1} is kept in your opening balances, so it is not counted here.", [
				short_account(frm, account),
				money(this.frm, amount, 0),
			])
		);
		const upload_lines = [];
		if (this.result?.replaced) {
			upload_lines.push(
				plural(
					this.result.replaced,
					__("1 draft row was replaced by the new sheet."),
					__("{0} draft rows were replaced by the new sheet.", [this.result.replaced])
				)
			);
		}
		if (this.result?.skipped) {
			upload_lines.push(
				plural(
					this.result.skipped,
					__("1 row is already posted and is left out."),
					__("{0} rows are already posted and are left out.", [this.result.skipped])
				)
			);
		}
		this.set_alerts(this.get_alerts(), [...upload_lines, ...kept]);
		this.dialog.set_df_property("rows", "hidden", drafts.rows || !posted.rows ? 0 : 1);
		this.dialog.set_df_property("where_section", "hidden", drafts.rows || !posted.rows ? 0 : 1);
		this.render_lines();
		this.refresh_footer();
	}

	get_alerts() {
		const { checks, frm } = this;
		const go = (label, open) => ({
			label: label,
			onclick: () => {
				this.dialog.hide();
				open();
			},
		});
		const reopen = () => show_opening_stock(frm);
		// the server's reds, in order; a few get a button to the step that fixes them
		const actions = {
			posting_blocker: checks.posting_fix
				? [fix_button(checks.posting_fix, frm, () => this.refresh())]
				: [],
			perpetual_inventory: [
				{
					label: __("Turn it on"),
					onclick: () => call_method(frm, "turn_on_perpetual_inventory").then(() => this.refresh()),
				},
			],
			missing_warehouses: [go(__("Go to Warehouses"), () => show_warehouses(frm, { after: reopen }))],
			wrong_lines: [go(__("Go to Warehouses"), () => show_warehouses(frm, { after: reopen }))],
			no_serials: [
				{
					label: __("Make serial numbers"),
					onclick: () =>
						(this.dirty ? this.save() : Promise.resolve())
							.then(() => call_method(frm, "make_serial_numbers"))
							.then(() => this.refresh()),
				},
			],
			untracked: frm.doc.__onload?.serial_batch_off
				? [
						{
							label: __("Turn on batch and serial numbers"),
							onclick: () =>
								call_method(frm, "turn_on_serial_and_batch").then(() => this.refresh()),
						},
				  ]
				: [],
			missing_items: [go(__("Go to Items"), () => open_party_import(frm, "Item"))],
			in_stock: [go(__("Posted stock"), () => show_posted_stock(frm))],
			kept: checks.reds.find((red) => red.key === "kept")?.account
				? [
						{
							label: __("Move it to stock"),
							onclick: () =>
								call_method(frm, "move_to_opening_stock", {
									account: checks.reds.find((red) => red.key === "kept").account,
								}).then(() => this.refresh()),
						},
				  ]
				: [],
		};
		const names_of = (idxs) => {
			const set = new Set(idxs);
			const names = [
				...new Set(
					this.get_rows()
						.filter((row) => set.has(row.doc_idx))
						.map((row) => row.item_name)
				),
			];
			return names.slice(0, 5).join(", ") + (names.length > 5 ? ", …" : "");
		};
		// the cells a red is about, marked in its rows
		const fields = {
			missing_warehouses: ["warehouse_name"],
			missing_items: ["item_name"],
			negative: ["qty"],
			no_qty: ["qty"],
			unknown_units: ["uom"],
			part_qty: ["qty"],
			challan: ["challan_no", "challan_date", "return_due"],
			no_batch: ["batch_no"],
		};
		const alerts = (checks.reds || [])
			.filter((item) => item.key !== "no_drafts")
			.map((item) => ({
				theme: "red",
				key: item.key,
				title: item.text,
				description: item.key === "missing_items" ? names_of(item.rows) : undefined,
				buttons: actions[item.key] || [],
				rows: item.rows,
				fields: fields[item.key],
				badge: item.key.startsWith("missing_") ? __("Not in ERPNext") : undefined,
			}));
		let red_count = alerts.length;
		const close = Object.entries(checks.close_items || {});
		if (close.length) {
			alerts.push({
				theme: "yellow",
				title: plural(
					close.length,
					__("1 item has a close match: {0}.", [
						close.map(([sheet, item]) => `${sheet} → ${item}`)[0],
					]),
					__("{0} items have close matches.", [close.length])
				),
				description: __("Check them before you use them."),
				buttons: [{ label: __("Use them"), onclick: () => this.use_close_items(close) }],
				rows: (row) => close.some(([sheet]) => sheet === row.item_name),
				fields: ["item_name"],
			});
		}
		if (checks.expired?.length) {
			alerts.push({
				theme: "yellow",
				title: plural(
					checks.expired.length,
					__("1 batch expired by {0}.", [this.day]),
					__("{0} batches expired by {1}.", [checks.expired.length, this.day])
				),
				description: __("They post as stock. Leave them out if you will write them off."),
				rows: checks.expired,
			});
		}
		if (this.result?.differ) {
			alerts.push({
				theme: "yellow",
				title: plural(
					this.result.differ,
					__("1 row left out differs from what was posted."),
					__("{0} rows left out differ from what was posted.", [this.result.differ])
				),
				description: __("Correct them in Posted stock."),
				buttons: [go(__("Posted stock"), () => show_posted_stock(frm))],
			});
		}
		red_count = alerts.filter((alert) => alert.theme === "red").length;
		const off = checks.drafts.rows ? checks.off : checks.match?.off ?? checks.off;
		if (
			!red_count &&
			off !== null &&
			Math.abs(off) >= 1 &&
			Object.keys(checks.trial_balance.lines).length
		) {
			alerts.push({
				theme: "yellow",
				title:
					off > 0
						? __("{0} less than your trial balance.", [money(this.frm, off)])
						: __("{0} more than your trial balance.", [money(this.frm, -off)]),
				description: this.get_biggest_rows(),
				buttons:
					checks.drafts.rows || !checks.posted.rows
						? []
						: [
								{ label: __("Post the difference"), onclick: () => this.show_difference() },
								{
									label: __("Show rows"),
									variant: "outline",
									onclick: () =>
										this.dialog
											.get_field("lines")
											.$wrapper[0].scrollIntoView({ block: "center" }),
								},
						  ],
			});
		} else if (!red_count && !checks.drafts.rows && checks.match?.lines_off?.length) {
			alerts.push({
				theme: "red",
				title: __("The total matches, but stock is on the wrong accounts."),
				description: __(
					"Undo the warehouses on the wrong account, set their stock account in Warehouses, and post them again."
				),
				buttons: [
					go(__("Posted stock"), () => show_posted_stock(frm)),
					go(__("Go to Warehouses"), () => show_warehouses(frm, { after: reopen })),
				],
			});
		}
		return alerts;
	}

	// the rows of most value: a typo in qty or value is usually one of them
	get_biggest_rows() {
		const rows = this.checks.drafts.rows
			? this.get_rows()
			: (this.frm.doc.opening_stock || []).filter((row) => row.status === "Posted");
		const biggest = [...rows]
			.sort((a, b) => Math.abs(flt(b.amount)) - Math.abs(flt(a.amount)))
			.slice(0, 3)
			.map((row) => `${row.item_name} ${money(this.frm, row.amount)}`);
		return biggest.length ? __("Biggest rows: {0}", [biggest.join(", ")]) : undefined;
	}

	// every warehouse posted with its stock line, then each line against the trial balance
	render_lines() {
		const $lines = this.dialog.get_field("lines").$wrapper.empty();
		const match = this.checks.match;
		if (this.checks.drafts.rows || !match?.warehouses?.length) return;
		$lines.append(
			make_read_table(
				[
					[__("Warehouse")],
					[__("Rows"), true],
					[__("Posted"), true],
					[__("Stock account in your books")],
				],
				match.warehouses.map((warehouse) => [
					warehouse.warehouse,
					format_number(warehouse.rows, null, 0),
					money(this.frm, warehouse.posted),
					short_account(this.frm, warehouse.account),
				])
			).addClass("mb-3"),
			make_read_table(
				[
					[__("Stock account in your books")],
					[__("Posted"), true],
					...(match.difference_posted ? [[__("Difference posted"), true]] : []),
					[__("Trial balance"), true],
					[__("Still off"), true],
				],
				match.lines.map((line) => [
					short_account(this.frm, line.account) || __("Stock"),
					money(this.frm, line.books),
					...(match.difference_posted ? [money(this.frm, line.difference_posted)] : []),
					money(this.frm, line.line),
					money(this.frm, line.difference),
				])
			)
		);
	}

	// the checks were for the saved rows: keep only the alerts about rows, which follow the edits
	changed() {
		if (!this.dirty) {
			this.alerts = this.alerts.filter((alert) => alert.seen);
			this.lines = [__("Rows changed. Save draft to check them again.")];
			this.dialog.get_field("lines").$wrapper.empty();
		}
		super.changed();
	}

	refresh_footer() {
		if (this.view !== "sheet") return;
		const has_drafts = this.get_rows().length;
		this.dialog.set_primary_action(__("Post to books"), () => this.post());
		this.dialog.get_primary_btn().toggleClass("hide", !has_drafts);
		this.dialog.set_secondary_action_label(has_drafts || this.dirty ? __("Save draft") : __("Close"));
		this.dialog.set_secondary_action(() =>
			has_drafts || this.dirty ? this.save_draft() : this.dialog.hide()
		);
		const blocker = this.checks.blocker;
		set_primary_disabled(
			this.dialog,
			!this.dirty && has_drafts > 0 && (!this.checks.perpetual_inventory || Boolean(blocker))
		);
	}

	use_close_items(close) {
		const map = Object.fromEntries(close);
		const rows = this.get_rows().map((row) =>
			map[row.item_name] ? { ...row, item_name: map[row.item_name] } : row
		);
		this.save(rows).then(() => this.refresh());
	}

	confirm_post() {
		const { checks } = this;
		const warehouses = [...new Set(this.get_rows().map((row) => row.warehouse_name))];
		const off = checks.off;
		const migration = frappe.datetime.str_to_user(this.frm.doc.migration_date);
		this.show_confirm({
			title: __("Post opening stock?"),
			back: __("Nothing is posted until you confirm."),
			body: [
				make_kv_lines(
					[
						[
							__("To post"),
							[
								count_rows(checks.drafts.rows),
								warehouses.length <= 2
									? warehouses.join(", ")
									: __("{0} warehouses", [warehouses.length]),
								money(this.frm, checks.drafts.value),
							].join(" · "),
						],
						checks.posted.rows && [
							__("Posted before"),
							[
								count_rows(checks.posted.rows),
								money(this.frm, checks.posted.value),
								checks.posted.sources.map(source_name).join(", "),
							].join(" · "),
						],
					].filter(Boolean)
				),
				off !== null &&
					Math.abs(off) >= 1 &&
					grey_line(
						off > 0
							? __(
									"{0} less than your trial balance. Fix rows or post the difference after this.",
									[money(this.frm, off)]
							  )
							: __(
									"{0} more than your trial balance. Fix rows or post the difference after this.",
									[money(this.frm, -off)]
							  )
					),
				checks.later_entries &&
					grey_line(
						__(
							"Cost of goods sold and profit since {0} change. ERPNext works out the new costs in the background, usually within the hour.",
							[migration]
						)
					),
				checks.reposting_timeslot &&
					grey_line(
						__("Later entries are valued again between {0} and {1}.", [
							checks.reposting_timeslot.start_time,
							checks.reposting_timeslot.end_time,
						])
					),
			].filter(Boolean),
			primary: __("Post {0}", [count_rows(checks.drafts.rows)]),
			on_primary: () =>
				call_method(this.frm, "post_opening_stock", {}, { freeze: true }).then((r) => {
					this.dialog.hide();
					after_run(
						this.frm,
						r.message,
						__("Posting the stock in the background. This page updates when it's done."),
						() => show_stock_posted(this.frm, checks.drafts.rows)
					);
				}),
		});
	}

	show_difference() {
		get_data(this.frm, "get_stock_difference").then((r) => {
			const difference = r.message;
			if (difference.blocker) {
				frappe.ui.toast({ type: "error", message: difference.blocker });
				return;
			}
			const total = difference.lines.reduce((sum, line) => sum + flt(line.difference), 0);
			const big = difference.lines.find((line) => line.big);
			let $big = null;
			let checked = null;
			if (big) {
				$big = make_alerts([
					{
						theme: "yellow",
						title: [
							big.difference > 0
								? __("{0} less than your trial balance", [money(this.frm, big.difference)])
								: __("{0} more than your trial balance", [money(this.frm, -big.difference)]),
							this.get_biggest_rows(),
						]
							.filter(Boolean)
							.join(" · "),
						description: __(
							"A gap this big is often a missing warehouse or a typo. Upload the missing warehouse or fix the row, or tick below to post the difference."
						),
					},
				]);
				checked = frappe.ui.form.make_control({
					df: {
						fieldtype: "Check",
						fieldname: "checked",
						label: __("I checked: no warehouse is missing"),
					},
					parent: $big,
					render_input: true,
				});
				checked.$wrapper.addClass("mb-0 mt-2");
				checked.$input.on("change", () =>
					this.dialog.get_primary_btn().prop("disabled", !checked.get_value())
				);
			}
			this.show_confirm({
				title: __("Post {0} to {1}?", [
					money(this.frm, Math.abs(total)),
					short_account(this.frm, difference.account),
				]),
				back: __("Nothing is posted until you confirm."),
				body: [
					make_read_table(
						[
							[__("Line")],
							[__("Posted"), true],
							[__("Trial balance"), true],
							[__("Difference"), true],
						],
						difference.lines.map((line) => [
							short_account(this.frm, line.account) || __("Stock"),
							money(this.frm, line.books),
							money(this.frm, line.line),
							money(this.frm, line.difference),
						])
					),
					$big,
					difference.empty_warehouses?.length &&
						make_alerts([
							{
								theme: "red",
								title: __("No stock posted in {0}.", [
									difference.empty_warehouses.join(", "),
								]),
								description: __(
									"Upload their rows first. Post the difference only if they are empty."
								),
							},
						]),
					grey_line(__("Temporary Opening goes down by {0}.", [money(this.frm, Math.abs(total))])),
					grey_line(
						total > 0
							? __(
									"Your trial balance has {0} more stock than the rows posted. Your stock stays as posted, and the {0} goes to {1}. Show this to your CA.",
									[money(this.frm, total, 0), short_account(this.frm, difference.account)]
							  )
							: __(
									"Your trial balance has {0} less stock than the rows posted. Your stock stays as posted, and the {0} goes to {1}. Show this to your CA.",
									[money(this.frm, -total, 0), short_account(this.frm, difference.account)]
							  )
					),
				].filter(Boolean),
				primary: __("Post the difference"),
				on_primary: () =>
					call_method(
						this.frm,
						"post_stock_difference",
						{ checked: checked?.get_value() ? 1 : 0 },
						{ freeze: true }
					).then(() => {
						close_and_reload(this.frm, this.dialog);
						frappe.ui.toast({
							type: "success",
							message: __("{0} posted to {1}.", [
								money(this.frm, Math.abs(total)),
								short_account(this.frm, difference.account),
							]),
						});
					}),
			});
			if (big) this.dialog.get_primary_btn().prop("disabled", true);
		});
	}
}

// a correction as it changes the stock value, like "Lowers stock by ₹23,31,000"
export function stock_change_text(frm, amount) {
	if (Math.abs(flt(amount)) < 0.01) return __("Stock value unchanged");
	return amount > 0
		? __("Raises stock by {0}", [money(frm, amount)])
		: __("Lowers stock by {0}", [money(frm, -amount)]);
}

export function show_stock_corrected(frm, result) {
	frappe.ui.toast({
		type: "success",
		message: [__("{0} corrected", [count_rows(result.rows)]), stock_change_text(frm, result.amount)].join(
			" · "
		),
	});
}

function show_stock_posted(frm, rows) {
	frappe.ui.toast({
		type: "success",
		message: __("{0} posted on {1}.", [count_rows(rows), day_before_text(frm)]),
	});
}
