import {
	after_run,
	call_method,
	count_assets,
	download_sheet,
	fix_button,
	get_data,
	grey_line,
	make_back_header,
	make_kv_lines,
	make_lines_card,
	money,
	plural,
	set_primary_disabled,
	short_account,
	source_name,
} from "./common";
import { SheetWindow, list_column, setup_dialog_grid } from "./sheet_window";
import { has_rows } from "./overview";
import { show_undo_result } from "./invoices_posted";
import { show_posted_window } from "./posted";

// ---------- Fixed assets ----------

const ASSET_FIELDS = [
	"asset_name",
	"asset_account_name",
	"asset_account",
	"purchase_date",
	"gross",
	"opening_depreciation",
	"hand_booked",
	"qty",
	"salvage",
	"method",
	"life",
	"rate",
	"source",
];

// result: what an upload just did, said at the top of the window
export function show_fixed_assets(frm, result, replacing) {
	get_data(frm, "get_fixed_assets").then((r) =>
		new FixedAssetsWindow(frm, r.message, result).show(replacing)
	);
}

class FixedAssetsWindow extends SheetWindow {
	constructor(frm, checks, result) {
		const drafts = (frm.doc.fixed_assets || []).filter((row) => row.status !== "Posted");
		super(frm, {
			title: __("Fixed assets"),
			open: (result, replacing) => show_fixed_assets(frm, result, replacing),
			save_method: "save_fixed_assets",
			get_method: "get_fixed_assets",
			where: "fixed_assets",
			has_data: drafts.length,
			row_form: true,
			fields: [
				list_column("asset_name", "Data", __("Asset name"), 2),
				list_column("asset_account", "Link", __("Asset account"), 2, {
					options: "Account",
					get_query: () => ({
						filters: { company: frm.doc.company, root_type: "Asset", is_group: 0 },
					}),
				}),
				list_column("purchase_date", "Date", __("Purchase date"), 1),
				list_column("gross", "Currency", __("Cost"), 2),
				list_column("opening_depreciation", "Currency", __("Depreciation till date"), 2),
				{ fieldname: "asset_account_name", fieldtype: "Data", label: __("Account on sheet") },
				{ fieldname: "qty", fieldtype: "Int", label: __("Qty") },
				{ fieldname: "salvage", fieldtype: "Currency", label: __("Salvage value") },
				{
					fieldname: "method",
					fieldtype: "Select",
					options: ["", "Straight Line", "Written Down Value"],
					label: __("Method"),
				},
				list_column("life", "Int", __("Life (years)"), 1),
				{ fieldname: "rate", fieldtype: "Float", label: __("Rate %") },
				{ fieldname: "hand_booked", fieldtype: "Currency", label: __("Booked by hand since") },
				{ fieldname: "source", fieldtype: "Data", hidden: 1 },
				{ fieldname: "doc_idx", fieldtype: "Int", hidden: 1 },
			],
			extra_fields: [
				{
					fieldname: "depreciation_booked",
					fieldtype: "Select",
					options: [
						{ value: "", label: "" },
						{ value: "Every month", label: __("Every month") },
						{ value: "Only at year end", label: __("Only at year end") },
					],
					hidden: 1,
				},
				{ fieldname: "categories_head", fieldtype: "HTML", hidden: 1 },
				{
					fieldname: "categories",
					fieldtype: "Table",
					hidden: 1,
					cannot_add_rows: 1,
					cannot_delete_rows: 1,
					in_place_edit: 1,
					data: [],
					fields: [
						list_column("asset_category", "Data", __("Category"), 2),
						list_column("method", "Select", __("Method"), 2, {
							options: ["", "Straight Line", "Written Down Value"],
							reqd: 1,
						}),
						list_column("life", "Int", __("Life (years)"), 1),
						list_column("rate", "Float", __("Rate %"), 1),
						list_column("accumulated_account", "Link", __("Accumulated depreciation"), 2, {
							options: "Account",
							read_only_depends_on: "eval:doc.net_value",
							get_query: () => ({
								filters: {
									company: frm.doc.company,
									root_type: "Asset",
									account_type: ["in", ["Accumulated Depreciation", ""]],
									is_group: 0,
								},
							}),
						}),
						list_column("depreciation_account", "Link", __("Depreciation expense"), 2, {
							options: "Account",
							get_query: () => ({
								filters: {
									company: frm.doc.company,
									root_type: "Expense",
									account_type: ["in", ["Depreciation", ""]],
									is_group: 0,
								},
							}),
						}),
						checks.hsn_label && list_column("hsn_code", "Data", checks.hsn_label, 1),
						{ fieldname: "asset_account", fieldtype: "Data", hidden: 1 },
						{ fieldname: "net_value", fieldtype: "Check", hidden: 1 },
					].filter(Boolean),
				},
			],
		});
		this.views.sheet.push("depreciation_booked");
		this.views.categories = ["categories_head", "categories"];
		this.result = result;
		this.load(checks);
	}

	load(checks) {
		this.checks = checks;
		this.drafts = (this.frm.doc.fixed_assets || [])
			.filter((row) => row.status !== "Posted")
			.map((row) => ({
				...Object.fromEntries(ASSET_FIELDS.map((field) => [field, row[field]])),
				doc_idx: row.idx,
			}));
	}

	render() {
		const { checks, frm } = this;
		const sheet = checks.sheet;
		const books = checks.books;
		this.make_head(__("One row per asset, as on {0}, with depreciation till that day.", [this.day]), {
			upload: () =>
				this.upload(
					"import_fixed_assets",
					this.get_rows().length &&
						__(
							"The sheet replaces draft rows of the same asset name, and its own if it was uploaded before. Posted assets stay. Continue?"
						)
				),
			menu: [
				(has_rows(frm.doc.fixed_assets, true) || (frm.doc.undone_assets || []).length) && {
					label: __("Posted assets"),
					icon: "list",
					onclick: () => {
						this.dialog.hide();
						show_posted_assets(frm);
					},
				},
				{
					label: __("Download sheet"),
					icon: "download",
					onclick: () => download_sheet(frm.doc.company, "fixed_assets.download_fixed_assets"),
				},
				...[...new Set((frm.doc.undone_assets || []).map((row) => row.source || ""))].map(
					(source) => ({
						label: __("Undone rows back to drafts: {0}", [source_name(source)]),
						icon: "undo-2",
						onclick: () =>
							call_method(frm, "restore_undone_assets", { source }).then((r) =>
								this.refresh(r.message)
							),
					})
				),
				sheet.categories && {
					label: __("Depreciation by category"),
					icon: "settings",
					onclick: () => this.show_categories(),
				},
			].filter(Boolean),
		});
		const net = books.fixed_assets - books.depreciation;
		const hand = checks.hand;
		this.set_stats(
			sheet.assets
				? [
						make_lines_card(
							__("On your sheet"),
							[
								[__("{0} · cost", [count_assets(sheet.assets)]), money(this.frm, sheet.cost)],
								[__("Depreciation till date"), money(this.frm, sheet.depreciation)],
							],
							[
								plural(
									sheet.categories,
									__("1 category"),
									__("{0} categories", [sheet.categories])
								),
								__("on {0}", [this.day]),
							].join(" · ")
						),
						make_lines_card(
							__("Your books today"),
							[
								[__("Fixed assets"), money(this.frm, books.fixed_assets)],
								[__("Depreciation"), money(this.frm, books.depreciation)],
							],
							[
								__("Net {0}", [money(this.frm, net, 0)]),
								hand.amount &&
									(hand.amount - hand.counted >= 1
										? __("{0} booked by hand since {1} still to count", [
												money(this.frm, hand.amount - hand.counted, 0),
												frappe.datetime.str_to_user(frm.doc.migration_date),
										  ])
										: __("{0} booked by hand since {1}, counted on the rows", [
												money(this.frm, hand.amount, 0),
												frappe.datetime.str_to_user(frm.doc.migration_date),
										  ])),
							]
								.filter(Boolean)
								.join(" · ")
						),
				  ]
				: []
		);
		this.render_question();
		this.set_alerts(this.get_alerts(), this.get_greys());
		this.refresh_footer();
	}

	// a migration date in the middle of the year: how the old books booked this year's depreciation
	render_question() {
		const { checks, frm } = this;
		const ask = checks.mid_year && this.view === "sheet";
		const field = this.dialog.get_field("depreciation_booked");
		this.dialog.set_df_property("depreciation_booked", "hidden", ask ? 0 : 1);
		if (!ask) return;
		const months = `${moment(checks.year_start).format("MMM")}–${moment(checks.day_before).format(
			"MMM YYYY"
		)}`;
		field.df.label = __("Did your old books book depreciation for {0}?", [months]);
		field.refresh();
		field.set_input(frm.doc.depreciation_booked || "");
		field.df.change = () => {
			const value = field.get_value();
			if ((value || "") === (frm.doc.depreciation_booked || "")) return;
			this.save(undefined, { depreciation_booked: value }).then(() => this.refresh());
		};
	}

	get_alerts() {
		const { checks, frm } = this;
		const reload = () => this.refresh();
		const set_up = [{ label: __("Set up"), onclick: () => this.show_categories() }];
		// reds about accounts or categories name them; the others are about rows
		const actions = {
			posting: checks.posting_fix ? [fix_button(checks.posting_fix, frm, () => this.refresh())] : [],
			untyped: [
				{
					label: get_mark_label(Object.values(checks.untyped_types || {})),
					onclick: () => call_method(frm, "mark_asset_accounts").then(reload),
				},
			],
			categories: set_up,
			hsn_code: set_up,
			bad_hsn: set_up,
			wrong_type: set_up,
			accounts: set_up,
			hand: [
				{
					label: __("Spread by schedule"),
					onclick: () => call_method(frm, "spread_hand_booked", {}, { freeze: true }).then(reload),
				},
				{
					label: __("Download sheet"),
					variant: "outline",
					onclick: () => download_sheet(frm.doc.company, "fixed_assets.download_fixed_assets"),
				},
			],
		};
		const named = { untyped: true, wrong_type: true, hsn_code: true, accounts: true };
		// one red at a time, in the order to fix them
		const reds = (checks.reds || []).filter((item) => !["empty", "answer"].includes(item.key));
		const more =
			reds.length > 1 &&
			plural(
				reds.length - 1,
				__("1 more to fix after this."),
				__("{0} more to fix after this.", [reds.length - 1])
			);
		// the cells a red is about, marked in its rows
		const fields = {
			no_account: ["asset_account"],
			no_date: ["purchase_date"],
			after_migration: ["purchase_date"],
			over_cost: ["opening_depreciation"],
			life_over: ["life"],
		};
		const alerts = reds.slice(0, 1).map((item) => ({
			theme: "red",
			key: item.key,
			title: item.text,
			description:
				[named[item.key] && item.rows.map((name) => short_account(frm, name)).join(", "), more]
					.filter(Boolean)
					.join(" · ") || undefined,
			buttons: actions[item.key] || [],
			rows: !named[item.key] && item.rows,
			fields: fields[item.key],
		}));
		alerts.unshift(...get_hand_after_alerts(frm), ...get_upload_alerts(this.result));
		const yellows = checks.yellows || {};
		const yellow = (title, description, extra = {}) =>
			alerts.push({ theme: "yellow", title, description, ...extra });
		for (const [account, amount] of Object.entries(checks.off || {})) {
			yellow(
				__("{0} is off by {1} from its line.", [
					short_account(frm, account),
					money(this.frm, Math.abs(amount), 0),
				]),
				__("Check the cost on the sheet, or the line in your trial balance.")
			);
		}
		for (const [account, amount] of Object.entries(checks.depreciation_off || {})) {
			yellow(
				__("{0} is off by {1} from the depreciation on the sheet.", [
					short_account(frm, account),
					money(this.frm, Math.abs(amount), 0),
				]),
				__("Check the depreciation till date on the sheet, or the line in your trial balance.")
			);
		}
		if (yellows.no_depreciation?.length) {
			yellow(
				plural(
					yellows.no_depreciation.length,
					__("1 asset bought before this year has no depreciation till date."),
					__("{0} assets bought before this year have no depreciation till date.", [
						yellows.no_depreciation.length,
					])
				),
				__("Check the sheet: ERPNext books their depreciation from the start."),
				{ rows: yellows.no_depreciation, fields: ["opening_depreciation"] }
			);
		}
		if (yellows.ending?.length) {
			yellow(
				plural(
					yellows.ending.length,
					__("1 asset reaches the end of its life with the first depreciation: {0}.", [
						money(this.frm, yellows.ending_amount),
					]),
					__("{0} assets reach the end of their life with the first depreciation: {1}.", [
						yellows.ending.length,
						money(this.frm, yellows.ending_amount),
					])
				),
				undefined,
				{ rows: yellows.ending }
			);
		}
		if (yellows.year_end_with_hand) {
			yellow(
				__(
					"Your old books book depreciation only at year end, but some was booked by hand this year."
				),
				__("Check the answer to the question above.")
			);
		}
		if (yellows.defaults?.length) {
			yellow(
				__("The company has no default {0} account.", [yellows.defaults.join(", ")]),
				__("Set it in the Company before you buy or scrap an asset.")
			);
		}
		return alerts;
	}

	// what the grey lines under the messages say
	get_greys() {
		const greys = this.checks.greys || {};
		return [
			greys.life_from_rate &&
				plural(
					greys.life_from_rate,
					__("1 Written Down Value asset has no life: it runs until its value is small."),
					__("{0} Written Down Value assets have no life: they run until their value is small.", [
						greys.life_from_rate,
					])
				),
			greys.existing_categories?.length &&
				__("Posting sets the accounts of {0}, made before, to the ones here.", [
					greys.existing_categories.join(", "),
				]),
		];
	}

	refresh_footer() {
		if (this.view !== "sheet") return;
		const { checks } = this;
		this.dialog.set_primary_action(__("Post to books"), () => this.post());
		this.dialog.set_secondary_action_label(__("Save draft"));
		this.dialog.set_secondary_action(() => this.save_draft());
		const first = (checks.reds || [])[0]?.key;
		set_primary_disabled(this.dialog, !this.get_rows().length || (!this.dirty && Boolean(first)));
	}

	// Depreciation by category: one row each, applied to all its assets
	show_categories() {
		const accounts = new Set(this.get_rows().map((row) => row.asset_account));
		const net_value = this.checks.net_value_accounts || {};
		const categories = (this.frm.doc.asset_categories || [])
			.filter((row) => accounts.has(row.asset_account))
			.map((row) => ({
				asset_account: row.asset_account,
				asset_category: row.asset_category,
				method: row.method,
				life: row.life,
				rate: row.rate,
				accumulated_account: net_value[row.asset_account] || row.accumulated_account,
				depreciation_account: row.depreciation_account,
				hsn_code: row.hsn_code,
				net_value: net_value[row.asset_account] ? 1 : 0,
			}));
		this.set_view("categories");
		const grid = this.dialog.fields_dict.categories.grid;
		setup_dialog_grid(grid);
		grid.df.data = categories;
		grid.refresh();
		this.dialog
			.get_field("categories_head")
			.$wrapper.empty()
			.append(
				make_back_header(
					__("Depreciation by category"),
					__("Rows with a life or rate of their own keep it."),
					() => this.show_sheet()
				).addClass("mb-3"),
				grey_line(__("Written Down Value uses the rate; Straight Line uses the life.")).addClass(
					"mb-3"
				)
			);
		set_primary_disabled(this.dialog, false);
		this.dialog.set_secondary_action_label(__("Back"));
		this.dialog.set_secondary_action(() => this.show_sheet());
		const count = this.get_rows().length;
		this.dialog.set_primary_action(
			count ? plural(count, __("Apply to 1 row"), __("Apply to {0} rows", [count])) : __("Apply"),
			() => {
				const no_method = (grid.df.data || []).filter((row) => !row.method);
				if (no_method.length) {
					return frappe.ui.toast({
						type: "error",
						message: __("Pick a Method for {0}.", [
							no_method.map((row) => row.asset_category || row.asset_account).join(", "),
						]),
					});
				}
				return this.save()
					.then(() =>
						call_method(this.frm, "save_asset_categories", {
							rows: (grid.df.data || []).map(({ net_value, ...row }) =>
								net_value ? { ...row, accumulated_account: undefined } : row
							),
						})
					)
					.then(() => this.refresh());
			}
		);
	}

	confirm_post() {
		const { checks, frm } = this;
		const count = checks.sheet.assets;
		const categories = Object.fromEntries(
			(frm.doc.asset_categories || []).map((row) => [row.asset_account, row])
		);
		const net_value = Object.entries(checks.net_value || {});
		const first = checks.first_depreciation;
		this.show_confirm({
			title: plural(count, __("Post 1 asset?"), __("Post {0} assets?", [count])),
			back: __("Nothing is posted until you confirm."),
			body: [
				make_kv_lines([
					[
						__("To post"),
						[
							count_assets(count),
							__("cost {0}", [money(this.frm, checks.sheet.cost)]),
							__("depreciation {0}", [money(this.frm, checks.sheet.depreciation)]),
						].join(" · "),
					],
				]),
				checks.hand.counted &&
					grey_line(
						__("{0} booked by hand since {1} is counted. Monthly from {2}.", [
							money(this.frm, checks.hand.counted),
							frappe.datetime.str_to_user(frm.doc.migration_date),
							frappe.datetime.str_to_user(first.date),
						])
					),
				...net_value.map(([account, amount]) =>
					grey_line(
						__(
							"Your balances do not change. Only the depreciation of {0} moves to its own line: {1} to {2}, dated {3}.",
							[
								categories[account]?.asset_category || short_account(frm, account),
								money(this.frm, amount),
								short_account(frm, checks.net_value_accounts?.[account]) ||
									__("its accumulated depreciation line"),
								frappe.datetime.str_to_user(checks.net_value_date),
							]
						)
					)
				),
				checks.wdv_gap &&
					grey_line(
						__("{0} less depreciation this year; ERPNext catches it up over the asset's life.", [
							money(this.frm, checks.wdv_gap),
						])
					),
				checks.fully_depreciated &&
					grey_line(
						plural(
							checks.fully_depreciated,
							__("1 asset is fully depreciated: it is posted with no schedule."),
							__("{0} assets are fully depreciated: they are posted with no schedule.", [
								checks.fully_depreciated,
							])
						)
					),
				make_kv_lines([
					[
						__("First depreciation"),
						[frappe.datetime.str_to_user(first.date), money(this.frm, first.amount)].join(" · "),
					],
				]),
				checks.year_missing &&
					frappe.model.can_create("Fiscal Year") &&
					grey_line(
						__("The financial year starting {0} is added when you post.", [
							frappe.datetime.str_to_user(checks.year_start),
						])
					),
				!checks.auto_depreciation &&
					grey_line(
						__("Depreciation is not booked by itself on this site. Book it from each asset.")
					),
			].filter(Boolean),
			primary: plural(count, __("Post 1 asset"), __("Post {0} assets", [count])),
			on_primary: () =>
				call_method(frm, "post_fixed_assets", {}, { freeze: true }).then(
					(r) => {
						this.dialog.hide();
						after_run(
							frm,
							r.message,
							__("Posting the assets in the background. This page updates when it's done."),
							() =>
								frappe.ui.toast({
									type: "success",
									message: __("{0} posted.", [count_assets(count)]),
								})
						);
					},
					// the confirm is stale once posting turns it down: back to the sheet and its reds
					() => this.refresh()
				),
		});
	}
}

// depreciation booked by hand after the assets were posted: ERPNext's schedule books it too
export const HAND_AFTER_TEXT = () =>
	__(
		"This depreciation was booked by hand after the assets were added. ERPNext's schedule books it too, so it would count twice. Cancel the entry."
	);

export function get_hand_after_alerts(frm) {
	const currency = erpnext.get_currency(frm.doc.company);
	return (frm.doc.__onload?.asset_match?.hand_after_entries || []).map((entry) => ({
		theme: "red",
		title: HAND_AFTER_TEXT(),
		description: [
			entry.name,
			frappe.datetime.str_to_user(entry.posting_date),
			format_currency(entry.amount, currency),
			entry.accounts.map((account) => short_account(frm, account)).join(", "),
		].join(" · "),
		buttons: [
			{
				label: __("Open entry"),
				onclick: () => frappe.set_route("Form", "Journal Entry", entry.name),
			},
		],
	}));
}

// What an upload of the asset sheet left out and replaced
// "Mark them", or what the one account becomes
function get_mark_label(types) {
	if (types.length !== 1) return __("Mark them");
	return {
		"Fixed Asset": __("Mark as fixed asset"),
		Depreciation: __("Mark as depreciation"),
		"Accumulated Depreciation": __("Mark as accumulated depreciation"),
	}[types[0]];
}

function get_upload_alerts(result) {
	if (!result?.posted && !result?.replaced) return [];
	const total = (result.posted || 0) + (result.added || 0);
	return [
		{
			theme: "yellow",
			title: result.posted
				? result.added
					? __("{0} of {1} assets on this sheet are posted already", [result.posted, total])
					: plural(
							result.posted,
							__("The asset on this sheet is posted already"),
							__("All {0} assets on this sheet are posted already", [result.posted])
					  )
				: plural(
						result.replaced,
						__("1 draft row was replaced by the new sheet."),
						__("{0} draft rows were replaced by the new sheet.", [result.replaced])
				  ),
			description: [
				result.posted &&
					__(
						"They were left out, so nothing counts twice. To change a posted asset, undo it first."
					),
				result.posted &&
					result.replaced &&
					plural(
						result.replaced,
						__("1 draft row was replaced by the new sheet."),
						__("{0} draft rows were replaced by the new sheet.", [result.replaced])
					),
			]
				.filter(Boolean)
				.join(" "),
		},
	];
}

export function show_posted_assets(frm) {
	show_posted_window(frm, {
		title: __("Posted assets"),
		undo_after_live: true,
		load: () => get_data(frm, "get_posted_entries", { kind: "assets" }).then((r) => r.message),
		posted_word: (when) => __("Posted {0}", [when]),
		group_line: (group) => count_assets(group.assets),
		group_amount: (group) => money(frm, group.cost),
		undone: (posted) =>
			(posted.undone || []).map((group) => ({
				source: group.source,
				line: __("{0} · {1} · taken out of your books", [
					count_assets(group.count),
					money(frm, group.cost),
				]),
				download: () =>
					download_sheet(frm.doc.company, "fixed_assets.download_fixed_assets", {
						undone_from: group.source,
					}),
			})),
		fields: [
			{ fieldname: "asset", fieldtype: "Link", options: "Asset", label: __("Asset"), columns: 2 },
			{ fieldname: "asset_name", fieldtype: "Data", label: __("Asset name"), columns: 2 },
			{ fieldname: "asset_account", fieldtype: "Data", label: __("Asset account"), columns: 2 },
			{ fieldname: "purchase_date", fieldtype: "Date", label: __("Purchase date"), columns: 1 },
			{ fieldname: "gross", fieldtype: "Currency", label: __("Cost"), columns: 2 },
			{
				fieldname: "depreciated_text",
				fieldtype: "Data",
				label: __("Depreciation posted"),
				columns: 1,
			},
		],
		rows: (group) =>
			group.rows.map((row) => ({
				...row,
				depreciated_text: row.depreciated ? __("Yes") : "",
			})),
		blocked: (row) => row.depreciated,
		blocked_alert: (rows) => ({
			title: plural(
				rows.length,
				__("1 asset already has depreciation posted."),
				__("{0} assets already have depreciation posted.", [rows.length])
			),
			description: __("Cancel those depreciation entries first. Ask your CA."),
		}),
		undo_label: (selected, group) =>
			selected.length
				? __("Undo {0} selected · {1}", [
						selected.length,
						money(
							frm,
							selected.reduce((sum, row) => sum + flt(row.gross), 0)
						),
				  ])
				: __("Undo this upload · {0}", [money(frm, group.cost)]),
		confirm_title: (rows) => __("Undo {0}?", [count_assets(rows.length)]),
		confirm_text: (rows) =>
			plural(
				rows.length,
				__("You can download it, fix it and upload again."),
				__("You can download them, fix them and upload again.")
			),
		confirm_what: (rows, selected) => ({
			count: rows.length,
			kind: "assets",
			names: selected ? selected.map((row) => row.asset) : null,
			what: count_assets(rows.length),
			one: __("asset"),
		}),
		confirm_rows: (rows) =>
			rows.map((row) => [
				$(`<span class="text-base text-ink-gray-8 grow truncate"></span>`).text(row.asset_name),
				$(`<span class="text-base text-ink-gray-5 shrink-0"></span>`).text(
					short_account(frm, row.asset_account)
				),
				$(`<span class="text-base text-ink-gray-8 shrink-0"></span>`).text(money(frm, row.gross)),
			]),
		total: (rows) => rows.reduce((sum, row) => sum + flt(row.gross), 0),
		confirm_button: (rows) => __("Undo {0}", [count_assets(rows.length)]),
		undo: (group, selected, delete_entries) =>
			call_method(frm, "undo_fixed_assets", {
				source: group.source,
				asset_names: selected ? selected.map((row) => row.asset) : null,
				delete_entries: delete_entries,
			}),
		after_undo: (result, group) => show_undo_result(frm, "assets", result, group.source),
	});
}
