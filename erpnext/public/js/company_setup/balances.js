import {
	ask_before_closing,
	call_method,
	close_and_reload,
	day_before_text,
	download_sheet,
	get_rows_key,
	grey_line,
	make_alerts,
	make_lines_card,
	make_posting_reds,
	make_sheet_buttons,
	money,
	plural,
	reload_fresh,
	short_account,
	show_in_place_of,
	upload_sheet,
} from "./common";
import { fill_where, where_to_find_fields } from "./where_to_find";
import { setup_dialog_grid } from "./sheet_window";
import { show_posting_preview } from "./books";

export function show_opening_balances(frm, result, replacing) {
	const company = frm.doc.company;
	const abbr = frm.doc.__onload?.company_details?.abbr;
	const short_name = (account) => (abbr ? account.replace(new RegExp(` - ${abbr}$`), "") : account);
	const posted = (frm.doc.opening_balances || []).filter((row) => row.status === "Posted");
	const get_drafts = () => (frm.doc.opening_balances || []).filter((row) => row.status !== "Posted");
	const drafts = get_drafts();
	// a zero is left blank, so the side with the amount stands out
	const amount = (value, df, options, doc) =>
		flt(value) ? frappe.form.formatters.Currency(value, df, options, doc) : "";
	const dialog = new frappe.ui.Dialog({
		title: __("Opening balances"),
		size: "extra-large",
		fields: [
			{ fieldname: "intro", fieldtype: "HTML" },
			{
				fieldname: "balances",
				fieldtype: "Table",
				label: __("Balances"),
				cannot_add_rows: false,
				in_place_edit: true,
				data: drafts.map((row) => ({ ...row, name: undefined })),
				fields: [
					{
						fieldname: "original_account",
						fieldtype: "Data",
						label: __("From Sheet"),
						read_only: 1,
						in_list_view: 1,
						columns: 2,
					},
					{
						fieldname: "account",
						fieldtype: "Link",
						options: "Account",
						label: __("Account"),
						in_list_view: 1,
						columns: 2,
						formatter: (value) => frappe.utils.escape_html(short_name(value || "")),
						// a new account is added with this company's Add account, not the full form
						only_select: 1,
						filter_description: __(
							"Customer, supplier, stock and temporary accounts aren't listed: they come in their own way."
						),
						get_query: () => ({
							filters: [
								["company", "=", company],
								["is_group", "=", 0],
								["disabled", "=", 0],
								["account_type", "not in", OPENING_EXCLUDED_TYPES],
							],
						}),
						onchange() {
							const row = this.doc;
							if (!row || !row.account) return;
							frappe.db
								.get_value("Account", row.account, ["account_type", "root_type"])
								.then((r) => {
									Object.assign(row, r.message || {});
									// the row being edited isn't redrawn by a grid refresh
									for (const fieldname of ["account_type", "root_type"]) {
										this.grid_row?.refresh_field(fieldname);
									}
									update_totals();
								});
						},
					},
					{
						fieldname: "account_type",
						fieldtype: "Data",
						label: __("Account Type"),
						read_only: 1,
						in_list_view: drafts.some((row) => row.account_type) ? 1 : 0,
						columns: 1,
					},
					{
						fieldname: "root_type",
						fieldtype: "Data",
						label: __("Category"),
						read_only: 1,
						in_list_view: 1,
						columns: 1,
					},
					{
						fieldname: "debit",
						fieldtype: "Currency",
						label: __("Debit"),
						in_list_view: 1,
						columns: 2,
						formatter: amount,
						onchange: () => update_totals(),
					},
					{
						fieldname: "credit",
						fieldtype: "Currency",
						label: __("Credit"),
						in_list_view: 1,
						columns: 2,
						formatter: amount,
						onchange: () => update_totals(),
					},
				],
			},
			...where_to_find_fields(frm, "opening_balances", {
				has_data: (frm.doc.opening_balances || []).length > posted.length,
				posted: posted.length,
			}),
		],
		primary_action_label: __("Preview and post"),
		primary_action: () => save().then(() => show_posting_preview(frm)),
		secondary_action_label: __("Save draft"),
		secondary_action: () => save_draft(),
	});
	fill_where(frm, dialog, "opening_balances");
	const save_rows = () =>
		call_method(frm, "save_opening_balances", { rows: dialog.get_values().balances || [] });
	const save = () => save_rows().then(() => close_and_reload(frm, dialog));
	const save_draft = () =>
		save_rows()
			.then(() => reload_fresh(frm))
			.then(() => {
				const grid = dialog.fields_dict.balances.grid;
				grid.df.data = get_drafts().map((row) => ({ ...row, name: undefined }));
				grid.refresh();
				saved_key = get_rows_key(grid);
				update_totals();
				frappe.ui.toast({ type: "success", message: __("Saved as a draft.") });
			});
	let saved_key = get_rows_key(dialog.fields_dict.balances.grid);
	ask_before_closing(
		dialog,
		() => get_rows_key(dialog.fields_dict.balances.grid) !== saved_key,
		() => save_rows().then(() => reload_fresh(frm))
	);

	const $intro = dialog.get_field("intro").$wrapper;
	$(`<div class="flex flex-wrap items-center justify-between gap-2 mb-3"></div>`)
		.append(
			$(`<div class="text-p-sm text-ink-gray-6"></div>`).text(
				__(
					"Bank, cash, loans, capital, tax and the rest, as on {0}. Customers, suppliers and stock come in their own way.",
					[day_before_text(frm)]
				)
			),
			make_sheet_buttons(
				() => upload_opening_balances(frm, dialog),
				[
					{
						label: __("Download sheet"),
						icon: "download",
						onclick: () => download_sheet(company, "chart_sheet.download_opening_balances"),
					},
					{
						label: __("Add account"),
						icon: "plus",
						onclick: () =>
							frappe.model.with_doctype("Account", () => add_balance_account(frm, dialog)),
					},
				]
			)
		)
		.appendTo($intro);
	$intro.append(
		make_posting_reds(frm, frm.doc.__onload?.posting_reds?.balances, () =>
			show_opening_balances(frm, null, dialog)
		)
	);
	const $cards = $(`<div class="es-stat-card-grid company-setup-stats mb-3"></div>`).appendTo($intro);
	// nothing entered yet: the table's place says how to start, and the cards wait for rows
	let typing = false;
	const $empty = $(`<div class="border rounded-lg py-10 mb-3"></div>`)
		.append(
			frappe.ui.empty_state({
				icon: "file-spreadsheet",
				title: __("Upload your trial balance"),
				description: __("Excel or CSV. Each account is read with its debit or credit."),
				actions: [
					{
						label: __("Upload sheet"),
						icon: "upload",
						variant: "solid",
						onclick: () => upload_opening_balances(frm, dialog),
					},
					{
						label: __("Add row"),
						onclick: () => {
							typing = true;
							update_totals();
							dialog.fields_dict.balances.grid.add_new_row();
						},
					},
					{
						label: __("Download sample sheet"),
						variant: "ghost",
						onclick: () => download_sheet(company, "chart_sheet.download_opening_balances"),
					},
				],
			})
		)
		.appendTo($intro);
	if (result) $intro.append(make_upload_result(frm, result));
	// drawn from the rows as they change: see update_totals
	$intro.append($(`<div data-unmatched></div>`));
	// accounts suggested for rows without one, applied only when the user says so
	let suggested = result?.suggestions || null;
	// rows no ledger fits: the ledger to add, with its type and group
	let new_accounts = result?.new_accounts || {};
	$intro.append(make_balance_yellows(frm, dialog));
	if (posted.length) {
		grey_line(
			plural(
				posted.length,
				__("1 balance is posted already and stays as it is."),
				__("{0} balances are posted already and stay as they are.", [posted.length])
			)
		)
			.addClass("mb-3")
			.appendTo($intro);
	}

	const currency = erpnext.get_currency(company);
	const update_totals = () => {
		const rows = dialog.fields_dict.balances.df.data || [];
		const empty = !rows.length && !typing;
		$empty.toggleClass("hide", !empty);
		$cards.toggleClass("hide", empty);
		dialog.set_df_property("balances", "hidden", empty ? 1 : 0);
		const waiting = rows.filter((row) => !row.account && row.original_account);
		if (waiting.length && !suggested) {
			suggested = {};
			frappe
				.call("erpnext.setup.doctype.company_setup.company_setup.suggest_balance_accounts", {
					company: company,
					labels: waiting.map((row) => row.original_account),
					root_types: Object.fromEntries(
						waiting.map((row) => [row.original_account, row.root_type])
					),
					sides: Object.fromEntries(
						waiting.map((row) => [
							row.original_account,
							flt(row.debit) >= flt(row.credit) ? "debit" : "credit",
						])
					),
				})
				.then((r) => {
					suggested = r.message?.suggestions || {};
					new_accounts = r.message?.new_accounts || {};
					update_totals();
				});
		}
		const suggestions = waiting.filter((row) => suggested?.[row.original_account]);
		const to_add = waiting.filter(
			(row) => !suggested?.[row.original_account] && new_accounts[row.original_account]
		);
		const pairs = suggestions.map(
			(row) => `${row.original_account} → ${short_name(suggested[row.original_account])}`
		);
		$intro
			.find("[data-unmatched]")
			.empty()
			.append(
				waiting.length
					? $(`<div class="mb-2"></div>`).append(
							frappe.ui.alert({
								theme: "yellow",
								title: plural(
									waiting.length,
									__("Pick an account for 1 row"),
									__("Pick an account for {0} rows", [waiting.length])
								),
								description:
									[
										suggestions.length === 1 &&
											__("1 close match: {0}. Check it before you use it.", [pairs[0]]),
										suggestions.length > 1 &&
											__("{0} close matches: {1}. Check them before you use them.", [
												suggestions.length,
												pairs.join(", "),
											]),
										to_add.length &&
											plural(
												to_add.length,
												__(
													"No ledger yet for {0}: add it with its type and group filled in.",
													[to_add[0].original_account]
												),
												__(
													"No ledger yet for {0}: add each with its type and group filled in.",
													[to_add.map((row) => row.original_account).join(", ")]
												)
											),
									]
										.filter(Boolean)
										.join(" ") || undefined,
								footer: $(`<div class="flex flex-wrap items-center gap-2"></div>`).append(
									suggestions.length
										? frappe.ui.button({
												label:
													suggestions.length === 1
														? __("Use the match: {0}", [pairs[0]])
														: __("Use these {0} matches", [suggestions.length]),
												size: "sm",
												onclick: () => use_suggestions(suggestions),
										  })
										: [],
									to_add.length
										? frappe.ui.button({
												label: __("Add account"),
												size: "sm",
												onclick: () =>
													frappe.model.with_doctype("Account", () =>
														add_balance_account(frm, dialog, new_accounts)
													),
										  })
										: [],
									frappe.ui.button({
										label: __("Go to next empty row"),
										variant: "outline",
										size: "sm",
										onclick: () => go_to_empty_row(),
									})
								),
							})
					  )
					: []
			);
		const sum = (field) => rows.reduce((total, row) => total + flt(row[field]), 0);
		// what the rows of these categories add up to, on the side they usually sit; posted ones too
		const net = (root_types, sign) =>
			[...rows, ...posted]
				.filter((row) => root_types.includes(row.root_type))
				.reduce((total, row) => total + sign * (flt(row.debit) - flt(row.credit)), 0);
		const debit = sum("debit");
		const credit = sum("credit");
		$cards.empty().append(
			make_lines_card(
				__("Debit and credit"),
				[
					[__("Debit"), money(frm, debit, 0)],
					[__("Credit"), money(frm, credit, 0)],
					[__("Difference"), money(frm, Math.abs(debit - credit), 0), true],
				],
				get_difference_caption(frm, debit - credit)
			),
			// what customers owe, stock and what suppliers are owed are in, so the sides add up
			make_lines_card(
				__("Your books"),
				[
					[
						__("Assets"),
						money(
							frm,
							net(["Asset"], 1) + flt(frm.doc.sheet_receivables) + flt(frm.doc.sheet_stock)
						),
					],
					[
						__("Liabilities and capital"),
						money(frm, net(["Liability", "Equity"], -1) + flt(frm.doc.sheet_payables), 0),
					],
					[__("Profit"), money(frm, net(["Income"], -1) - net(["Expense"], 1), 0), true],
				],
				get_profit_caption(frm)
			),
			get_left_out_card(frm)
		);
	};
	const use_suggestions = (suggestions) =>
		frappe.db
			.get_list("Account", {
				filters: { name: ["in", suggestions.map((row) => suggested[row.original_account])] },
				fields: ["name", "account_type", "root_type"],
				limit: 0,
			})
			.then((accounts) => {
				const by_name = Object.fromEntries(accounts.map((account) => [account.name, account]));
				for (const row of suggestions) {
					const account = by_name[suggested[row.original_account]];
					if (account) {
						Object.assign(row, {
							account: account.name,
							account_type: account.account_type,
							root_type: account.root_type,
						});
					}
				}
				dialog.fields_dict.balances.grid.refresh();
				update_totals();
				frappe.ui.toast({
					type: "success",
					message:
						suggestions.length === 1
							? __("1 account matched: {0}", [
									`${suggestions[0].original_account} → ${short_name(
										suggestions[0].account
									)}`,
							  ])
							: __("{0} accounts matched.", [suggestions.length]),
					duration: 8000,
				});
			});
	// the next row without an account, on its page, with its account field open
	const go_to_empty_row = () => {
		const grid = dialog.fields_dict.balances.grid;
		const index = (grid.df.data || []).findIndex((row) => !row.account && row.original_account);
		if (index < 0) return;
		const pagination = grid.grid_pagination;
		const page = Math.floor(index / (pagination?.page_length || 50)) + 1;
		if (pagination && pagination.page_index !== page) pagination.go_to_page(page);
		const grid_row = grid.grid_rows[index];
		if (!grid_row) return;
		grid_row.row[0].scrollIntoView({ block: "center" });
		grid_row.toggle_editable_row(true);
		grid_row.on_grid_fields_dict?.account?.$input?.focus();
	};
	// the window's own buttons, put back after a view inside it
	dialog.restore_actions = () => {
		dialog.set_primary_action(__("Preview and post"), () => save().then(() => show_posting_preview(frm)));
		dialog.set_secondary_action_label(__("Save draft"));
		dialog.set_secondary_action(() => save_draft());
	};
	dialog.update_totals = update_totals;
	setup_dialog_grid(dialog.fields_dict.balances.grid, update_totals);
	show_in_place_of(dialog, replacing);
	update_totals();
	// an account's type or category may have changed in the chart since the rows were saved
	const picked = (dialog.fields_dict.balances.df.data || []).filter((row) => row.account);
	if (picked.length) {
		frappe.db
			.get_list("Account", {
				filters: { name: ["in", [...new Set(picked.map((row) => row.account))]] },
				fields: ["name", "account_type", "root_type"],
				limit: 0,
			})
			.then((accounts) => {
				const by_name = Object.fromEntries(accounts.map((account) => [account.name, account]));
				for (const row of picked) {
					const account = by_name[row.account];
					if (account) {
						row.account_type = account.account_type;
						row.root_type = account.root_type;
					}
				}
				dialog.fields_dict.balances.grid.refresh();
				update_totals();
			});
	}
}

// What the profit in the balances is: of the year so far, or of a whole year that then needs closing
function get_profit_caption(frm) {
	const period = frm.doc.__onload?.report_period;
	if (!period) return __("Income minus expenses this year. Assets include customers and stock.");
	const from = frappe.datetime.str_to_user(period.from_date);
	const to = frappe.datetime.str_to_user(period.to_date);
	return period.to_date === period.year_end
		? __(
				"Income minus expenses for the year {0} to {1}. Close that year once everything is posted. Assets include customers and stock.",
				[from, to]
		  )
		: __("Income minus expenses from {0} to {1}. Assets include customers and stock.", [from, to]);
}

const OPENING_EXCLUDED_TYPES = ["Receivable", "Payable", "Stock", "Temporary"];

// A new account, in a view inside the balances window; the row picked gets it
function add_balance_account(frm, balances_dialog, new_accounts = {}) {
	const company = frm.doc.company;
	const currency = erpnext.get_currency(company);
	const types = frappe.meta
		.get_docfield("Account", "account_type")
		.options.split("\n")
		.filter((type) => !OPENING_EXCLUDED_TYPES.includes(type));
	const grid = balances_dialog.fields_dict.balances.grid;
	// the rows that can get the new account; the one being edited is picked first
	const waiting = grid.df.data.filter((row) => !row.account && row.original_account);
	const editing = frappe.ui.form.editable_row?.doc;
	let row =
		(waiting.includes(editing) && editing) ||
		waiting.find((option) => new_accounts[option.original_account]) ||
		waiting[0];
	const type_of = (row) => {
		const hint = row && new_accounts[row.original_account];
		return hint ? hint.account_type || "Other" : undefined;
	};
	// the sheet's label may carry the account number, "1001 - Rent Deposit", but "Security
	// Deposit - Office Rent" is all name; "Payroll:Taxes" is a QuickBooks sub-account
	const name_of = (label) => {
		const [number, ...rest] = cstr(label).split(" - ");
		const name = rest.length && /^[\w.]*\d[\w.]*$/.test(number) ? rest.join(" - ") : cstr(label);
		return name.split(":").pop().trim();
	};
	const $layout = balances_dialog.$body.children().first();
	const $view = $(`<div class="flex flex-col gap-3"></div>`).insertBefore($layout);
	const close = () => {
		$view.remove();
		$layout.show();
		balances_dialog.restore_actions();
	};
	$layout.hide();
	$view.append(
		$(`<div class="flex items-center gap-2"></div>`).append(
			frappe.ui.button({
				icon: "arrow-left",
				variant: "ghost",
				size: "sm",
				tooltip: __("Back"),
				onclick: close,
			}),
			$(`<div class="flex flex-col min-w-0"></div>`).append(
				$(`<span class="text-base-medium text-ink-gray-8 truncate"></span>`).text(
					__("Add an account")
				),
				waiting.length ? grey_line(__("The row you pick gets the new account.")) : []
			)
		)
	);
	const no_row = __("No row, only add the account");
	const label_of = (row) =>
		`${row.original_account} · ${format_currency(flt(row.debit) || flt(row.credit), currency)}`;
	const form = new frappe.ui.FieldGroup({
		body: $(`<div></div>`).appendTo($view),
		fields: [
			{ fieldname: "group_note", fieldtype: "HTML" },
			{
				fieldname: "for_row",
				fieldtype: "Select",
				label: __("For row"),
				options: [...waiting.map(label_of), no_row],
				default: row ? label_of(row) : no_row,
				hidden: !waiting.length,
				change: () => {
					row = waiting.find((option) => label_of(option) === form.get_value("for_row"));
					form.set_value("account_name", row ? name_of(row.original_account) : "");
					if (type_of(row)) form.set_value("account_type", type_of(row));
					suggest_parent();
				},
			},
			{
				fieldname: "account_name",
				fieldtype: "Data",
				label: __("Account Name"),
				reqd: 1,
				default: row ? name_of(row.original_account) : "",
				change: () => !row && suggest_parent(),
			},
			{
				fieldname: "account_type",
				fieldtype: "Select",
				label: __("Account Type"),
				reqd: 1,
				// "Other" is an account of no type, like a deposit or a provision
				options: [
					...types.filter(Boolean).map((type) => ({ value: type, label: __(type) })),
					{ value: "Other", label: __("Other") },
				],
				description: __("Bank or Cash for money accounts; Other for most others."),
				default: type_of(row),
				change: () => suggest_parent(),
			},
			{
				fieldname: "parent_account",
				fieldtype: "Link",
				options: "Account",
				label: __("Under"),
				reqd: 1,
				only_select: 1,
				get_query: () => ({ filters: { company: company, is_group: 1 } }),
			},
		],
	});
	form.make();
	// the group the account goes under is suggested from the row's kind and the type picked, never
	// over what the user typed or picked there
	let picked_parent = false;
	const $parent = form.get_field("parent_account").$input;
	$parent.on("input", () => (picked_parent = true));
	// typing replaces the suggested group instead of adding to it
	$parent.on("focus", () => setTimeout(() => $parent.select()));
	// the group of the type picked, on the row's side, like Bank Accounts or Duties and Taxes
	const suggest_parent = () => {
		const account_type = form.get_value("account_type");
		const side = !row ? null : flt(row.debit) >= flt(row.credit) ? "Asset" : "Liability";
		frappe
			.call("erpnext.setup.doctype.company_setup.company_setup.get_account_parent", {
				company: company,
				account_type: account_type === "Other" ? null : account_type,
				root_type: row?.root_type || side,
				account_name: form.get_value("account_name") || "",
			})
			.then((r) => {
				if (!picked_parent) form.set_value("parent_account", r.message || "");
			});
	};
	suggest_parent();
	balances_dialog.set_secondary_action_label(__("Back"));
	balances_dialog.set_secondary_action(close);
	balances_dialog.set_primary_action(__("Add"), async () => {
		const values = form.get_values();
		if (!values) return;
		// an asset line can't go under liabilities, nor the other way
		const parent_root = (await frappe.db.get_value("Account", values.parent_account, "root_type")).message
			?.root_type;
		if (row?.root_type && parent_root && parent_root !== row.root_type) {
			frappe.msgprint({
				title: __("Wrong group"),
				message: __("{0} is on the {1} side, but {2} is under {3}. Pick a group under {1}.", [
					frappe.utils.escape_html(row.original_account),
					__(row.root_type),
					frappe.utils.escape_html(values.parent_account),
					__(parent_root),
				]),
				indicator: "red",
			});
			return;
		}
		frappe
			.call({
				method: "erpnext.accounts.utils.add_ac",
				args: {
					doctype: "Account",
					company: company,
					account_name: values.account_name,
					account_type: values.account_type === "Other" ? "" : values.account_type,
					parent: values.parent_account,
					is_group: 0,
					is_root: 0,
				},
				freeze: true,
			})
			.then((r) =>
				frappe.db.get_value("Account", r.message, ["account_type", "root_type"]).then((info) => {
					if (row) Object.assign(row, { account: r.message, ...info.message });
					else grid.df.data.push({ account: r.message, ...info.message });
					close();
					grid.refresh();
					balances_dialog.update_totals?.();
					frappe.ui.toast({
						type: "success",
						message: row
							? __("{0} now uses {1}.", [row.original_account, values.account_name])
							: __("{0} added.", [values.account_name]),
						duration: 8000,
					});
				})
			);
	});
	show_group_chart_note(company, form.get_field("group_note").$wrapper);
}

// ERPNext keeps one chart for a group of companies: accounts are added in the parent and copied to
// its subsidiaries, and a subsidiary can't add one of its own unless the company allows it
function show_group_chart_note(company, $wrapper) {
	Promise.all([
		frappe.db.get_list("Company", { filters: { parent_company: company }, fields: ["name"] }),
		frappe.db.get_value("Company", company, [
			"parent_company",
			"allow_account_creation_against_child_company",
		]),
	]).then(([subsidiaries, r]) => {
		const { parent_company, allow_account_creation_against_child_company } = r.message || {};
		let note;
		if (subsidiaries.length) {
			note = frappe.ui.alert({
				theme: "blue",
				title: __("Also added to {0}", [subsidiaries.map((row) => row.name).join(", ")]),
				description: __(
					"ERPNext keeps the chart of a group of companies the same, so an account added here is added to its subsidiaries too."
				),
			});
		} else if (parent_company && !allow_account_creation_against_child_company) {
			note = frappe.ui.alert({
				theme: "yellow",
				title: __("Accounts of {0} are added in {1}", [company, parent_company]),
				description: __(
					"ERPNext keeps a group's chart in its parent company. To add accounts here instead, turn on Allow Account Creation Against Child Company on this company."
				),
			});
		}
		if (note) $wrapper.append($(`<div class="mb-3"></div>`).append(note));
	});
}

function upload_opening_balances(frm, dialog) {
	const drafts = (dialog.fields_dict.balances.df.data || []).filter(
		(row) => row.account || flt(row.debit) || flt(row.credit)
	).length;
	upload_sheet(
		frm,
		"import_opening_balances",
		{},
		{
			replaces:
				drafts &&
				__(
					"The sheet replaces the {0} balances in this table, unsaved changes included. Posted balances stay as they are. Continue?",
					[drafts]
				),
			after: (result) => show_opening_balances(frm, result, dialog),
		}
	);
}

// What the upload did, and where what it left out goes
function make_upload_result(frm, result) {
	const $result = $(`<div class="flex flex-col gap-1 mb-3"></div>`);
	if (result.skipped) {
		$result.append(
			$(`<div class="mb-2"></div>`).append(
				frappe.ui.alert({
					theme: "yellow",
					title: result.added
						? __("{0} of {1} balances are posted already", [
								result.skipped,
								result.skipped + result.added,
						  ])
						: plural(
								result.skipped,
								__("The balance on this sheet is posted already"),
								__("All {0} balances on this sheet are posted already", [result.skipped])
						  ),
					description: __(
						"They were left out, so nothing counts twice. To change a posted balance, undo the posting first."
					),
				})
			)
		);
	}

	return $result;
}

// What the trial balance holds that comes in on other tabs, each with how far it is
function get_left_out_card(frm) {
	const lines = [
		[__("Customers"), frm.doc.sheet_receivables, __("Selling")],
		[__("Suppliers"), frm.doc.sheet_payables, __("Buying")],
		[__("Stock"), frm.doc.sheet_stock, __("Stock")],
	].filter(([, amount]) => flt(amount, 2));
	if (!lines.length) return [];
	return make_lines_card(
		__("Left out of these balances"),
		lines.map(([label, amount]) => [label, money(frm, amount, 0)]),
		__("They come in on the {0} tabs.", [frappe.utils.comma_and(lines.map(([, , tab]) => tab))])
	);
}

// What the balances' difference waits for: the customer invoices and supplier bills posted so far
// cover none of it, all of it, or part of it
function get_difference_caption(frm, difference) {
	if (!flt(difference, 2)) return __("Debit and credit match.");
	const posted = (table) =>
		(frm.doc[table] || [])
			.filter((row) => row.status === "Created")
			.reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
	const invoices = posted("opening_sales_invoices") - posted("opening_purchase_invoices");
	if (flt(frm.doc.sheet_stock)) {
		const stock = frm.doc.__onload?.stock_match;
		const covered = invoices + flt(stock?.books) + flt(stock?.difference_posted);
		if (!covered) return __("Waiting for your customer invoices, supplier bills and stock to be posted.");
		const left = flt(difference + covered, 2);
		if (Math.abs(left) < 1) return __("Covered by the invoices, bills and stock posted.");
		if (Math.sign(left) !== Math.sign(difference)) {
			return __(
				"The invoices, bills and stock posted are {0} more than this. Check them or the trial balance.",
				[money(frm, Math.abs(left))]
			);
		}
		return __("{0} not covered yet by the invoices, bills and stock posted.", [
			money(frm, Math.abs(left)),
		]);
	}
	if (!invoices) return __("Waiting for your customer invoices and supplier bills to be posted.");
	const left = flt(difference + invoices, 2);
	if (Math.abs(left) < 1) return __("Covered by the customer invoices and supplier bills posted.");
	if (Math.sign(left) !== Math.sign(difference)) {
		return __("The invoices and bills posted are {0} more than this. Check them or the trial balance.", [
			money(frm, Math.abs(left)),
		]);
	}
	return __("{0} not covered yet by the invoices and bills posted.", [money(frm, Math.abs(left))]);
}

// What a sheet held for customers, suppliers or stock, which come in their own way, against the
// invoices and bills entered so far
export function left_out_text(frm, left_out = {}) {
	const currency = erpnext.get_currency(frm.doc.company);
	const money = (amount) => format_currency(Math.abs(amount), currency);
	const entered = (table) =>
		(frm.doc[table] || []).reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
	const party_line = (amount, table, is_customer) => {
		const total = entered(table);
		if (!(frm.doc[table] || []).length) {
			return is_customer
				? __("Customers {0} come in as opening invoices in Selling.", [money(amount)])
				: __("Suppliers {0} come in as opening bills in Buying.", [money(amount)]);
		}
		if (Math.abs(Math.abs(total) - Math.abs(amount)) < 1) {
			return is_customer
				? __("Customers {0} match your opening invoices.", [money(amount)])
				: __("Suppliers {0} match your opening bills.", [money(amount)]);
		}
		return is_customer
			? __("Customers {0}: your opening invoices so far add up to {1}.", [money(amount), money(total)])
			: __("Suppliers {0}: your opening bills so far add up to {1}.", [money(amount), money(total)]);
	};
	const lines = [
		left_out.Receivable && party_line(left_out.Receivable, "opening_sales_invoices", true),
		left_out.Payable && party_line(left_out.Payable, "opening_purchase_invoices", false),
		left_out.Stock && __("Stock {0} needs an opening stock entry.", [money(left_out.Stock)]),
	].filter(Boolean);
	if (!lines.length) return "";
	return [__("Not added as balances:"), ...lines].join(" ");
}

// Opening entries are dated the day before the migration date, in a year a new site may not have
export function fiscal_year_text(frm) {
	const missing = frm.doc.__onload?.missing_fiscal_year;
	// without the right to add it, the posting reds ask for it instead
	if (!missing || !frappe.model.can_create("Fiscal Year")) return "";
	return __("They're dated {0}; the financial year {1} is added when you post.", [
		frappe.datetime.str_to_user(missing.date),
		missing.year,
	]);
}

// ---------- Opening balances: stock in the trial balance ----------

// Work in progress or job worker lines kept as balances: move them to Opening stock, or keep them
function make_balance_yellows(frm, dialog) {
	const currency = erpnext.get_currency(frm.doc.company);
	const reopen = () => {
		reload_fresh(frm).then(() => show_opening_balances(frm, null, dialog));
	};
	const alerts = [];
	for (const line of frm.doc.__onload?.stock_like_lines || []) {
		alerts.push({
			theme: "yellow",
			title: __("Move {0} of {1} into Opening stock?", [
				format_currency(line.amount, currency, 0),
				short_account(frm, line.account),
			]),
			description: __(
				"Move it if you count this as stock, by item, in a warehouse: it posts with Opening stock. Keep it if it is only a balance. You can change this later in Opening stock."
			),
			buttons: [
				{
					label: __("Move it"),
					onclick: () =>
						call_method(frm, "move_to_opening_stock", { account: line.account }).then(reopen),
				},
				{
					label: __("Keep as a balance"),
					variant: "outline",
					onclick: () =>
						call_method(frm, "keep_as_balance", { account: line.account }).then(reopen),
				},
			],
		});
	}
	return make_alerts(alerts).addClass("mb-3");
}
