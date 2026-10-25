import { call_method, close_and_reload, download_sheet, grey_line, plural, reload_fresh } from "./common";
import { fill_where, where_to_find_fields } from "./where_to_find";
import { left_out_text } from "./balances";

// The chart to edit; until a chart is chosen, the one made during setup is confirmed from here
export function show_chart(frm) {
	const company = frm.doc.company;
	const chosen = !!frm.doc.chart_of_accounts_source;
	const dialog = new frappe.ui.Dialog({
		title: __("Chart of accounts"),
		size: "large",
		fields: [{ fieldname: "chart", fieldtype: "HTML" }],
		primary_action_label: chosen ? __("Done") : __("Use this chart"),
		primary_action: () =>
			chosen
				? dialog.hide()
				: call_method(frm, "use_existing_chart")
						.then(() => reload_fresh(frm))
						.then(() => dialog.hide()),
		secondary_action_label: __("Open full page"),
		secondary_action: () => {
			frappe.route_options = { company: company };
			frappe.set_route("Tree", "Account");
		},
	});
	new CompanyChart(company, dialog.get_field("chart").$wrapper);
	dialog.show();
}

// The company's accounts with the same rows and actions as the Chart of Accounts page
class CompanyChart {
	constructor(company, $wrapper) {
		this.company = company;
		this.make_header($wrapper);
		this.$body = $(`<div></div>`).appendTo($wrapper);
		// loads the Account tree settings too, for the labels and the new account fields
		frappe.model.with_doctype("Account", () => this.make_tree());
	}

	make_header($wrapper) {
		const $header = $(`<div class="flex items-center gap-2 mb-3">
			<div class="text-p-sm text-ink-gray-6 grow min-w-0"></div>
		</div>`).appendTo($wrapper);
		$header.find(".text-p-sm").text(__("Add, rename or delete accounts. Changes are saved right away."));

		this.$toggle = $(
			frappe.ui.button({
				label: __("Collapse all"),
				variant: "ghost",
				onclick: () => this.toggle_all(),
			})
		).appendTo($header);
	}

	make_tree() {
		this.tree = new frappe.ui.Tree({
			parent: this.$body,
			label: this.company,
			root_value: this.company,
			expandable: true,
			use_row_actions: true,
			args: { doctype: "Account", company: this.company },
			method: "erpnext.accounts.utils.get_children",
			get_label: frappe.treeview_settings.Account.get_label,
			on_render: (node) => this.add_badges(node),
			on_node_render: open_all_once(),
			toolbar: this.get_actions(),
		});
	}

	add_badges(node) {
		if (node.is_root || !node.data) return;
		const flags = [];
		if (node.data.account_number) {
			flags.push(frappe.ui.badge({ label: node.data.account_number }));
		}
		const currency = node.data.account_currency;
		if (currency && currency !== erpnext.get_currency(this.company)) {
			flags.push(frappe.ui.badge({ label: currency, theme: "blue" }));
		}
		erpnext.utils.render_tree_node_flags(node, flags);
	}

	get_actions() {
		return [
			{
				label: __("Edit"),
				icon: "pencil",
				inline: true,
				condition: (node) => !node.is_root,
				click: (node) => frappe.set_route("Form", "Account", node.label),
			},
			{
				label: __("Add account"),
				icon: "plus",
				condition: (node) => node.expandable && frappe.model.can_create("Account"),
				click: (node) => this.add_account(node),
			},
			{
				label: __("Rename"),
				icon: "text-cursor-input",
				condition: (node) => !node.is_root && frappe.model.can_write("Account"),
				click: (node) => this.rename_account(node),
			},
			{
				label: __("Delete"),
				icon: "trash",
				danger: true,
				condition: (node) => !node.is_root && frappe.model.can_delete("Account"),
				click: (node) => frappe.model.delete_doc("Account", node.label, () => node.parent.remove()),
			},
		];
	}

	add_account(parent) {
		const dialog = new frappe.ui.Dialog({
			title: __("New account under {0}", [parent.data.account_name || parent.label]),
			fields: frappe.treeview_settings.Account.fields,
			primary_action_label: __("Add"),
			primary_action: (values) =>
				frappe
					.call({
						method: "erpnext.accounts.utils.add_ac",
						args: {
							...values,
							doctype: "Account",
							company: this.company,
							parent: parent.label,
							is_root: parent.is_root ? 1 : 0,
						},
						freeze: true,
					})
					.then(() => {
						dialog.hide();
						this.tree.load_children(parent);
					}),
		});
		dialog.show();
	}

	// ERPNext's own rename: it also rebuilds the "number - name - abbr" account name
	rename_account(node) {
		const dialog = new frappe.ui.Dialog({
			title: __("Rename account"),
			fields: [
				{
					fieldname: "account_name",
					fieldtype: "Data",
					label: __("Account Name"),
					reqd: 1,
					default: node.data.account_name,
				},
				{
					fieldname: "account_number",
					fieldtype: "Data",
					label: __("Account Number"),
					default: node.data.account_number,
				},
			],
			primary_action_label: __("Rename"),
			primary_action: (values) =>
				frappe
					.call({
						method: "erpnext.accounts.doctype.account.account.update_account_number",
						args: { name: node.label, ...values },
						freeze: true,
					})
					.then(() => {
						dialog.hide();
						this.tree.load_children(node.parent_node);
					}),
		});
		dialog.show();
	}

	toggle_all() {
		const expand = ["collapsed", "partial"].includes(this.tree.get_expansion_state());
		Promise.resolve(this.tree.load_children(this.tree.root_node, expand)).then(() =>
			this.$toggle.find(".es-button__label").text(expand ? __("Collapse all") : __("Expand all"))
		);
	}
}

// Opens every account of a tree once its first level is in
function open_all_once() {
	let opened = false;
	return function (node, deep) {
		if (node.is_root && !deep && !opened) {
			opened = true;
			this.load_children(node, true);
		}
	};
}

// Two steps, like the invoices: the sheet, then what it will do, before anything changes
export function show_chart_import(frm) {
	const company = frm.doc.company;
	const dialog = new frappe.ui.Dialog({
		title: __("Import a chart of accounts"),
		size: "large",
		fields: [
			{ fieldname: "steps", fieldtype: "HTML" },
			{
				fieldname: "file",
				fieldtype: "Attach",
				label: __("Your chart as a sheet"),
				description: __(
					"Excel or CSV, one row per account. Fill the Opening Debit and Opening Credit columns to bring balances too."
				),
				options: { restrictions: { allowed_file_types: [".csv", ".xlsx", ".xls"] } },
				change: () => preview(),
			},
			{ fieldname: "downloads", fieldtype: "HTML" },
			{ fieldname: "review", fieldtype: "HTML", hidden: 1 },
			{
				fieldname: "new_chart_section",
				fieldtype: "Section Break",
				label: __("View the new chart"),
				collapsible: 1,
				hidden: 1,
			},
			{ fieldname: "new_chart", fieldtype: "HTML" },
			...where_to_find_fields(frm, "chart_of_accounts", {
				has_data: true,
				posted: frm.doc.chart_of_accounts_source === "Imported",
			}),
		],
		primary_action_label: __("Next"),
		primary_action: () => show_step(1),
	});
	fill_where(frm, dialog, "chart_of_accounts");

	let last_preview;
	// what each group of a Busy list that Busy doesn't ship holds, as the user says
	const group_kinds = {};
	const stepper = new frappe.ui.Stepper({
		steps: [{ label: __("Upload") }, { label: __("Review") }],
		is_locked: (index) => index === 1 && !last_preview,
		on_step_click: (index) => show_step(index),
	});
	dialog.get_field("steps").$wrapper.append(stepper.$el.addClass("company-setup-stepper mb-2"));

	const $downloads = $(`<div class="flex flex-wrap items-center gap-1"></div>`).appendTo(
		dialog.get_field("downloads").$wrapper
	);
	$downloads.append(
		grey_line(__("Start from")),
		frappe.ui.button({
			label: __("the sample sheet"),
			icon: "download",
			variant: "ghost",
			size: "sm",
			onclick: () =>
				download_sheet(company, "chart_sheet.download_chart_template", {
					template: "Sample Template",
				}),
		}),
		grey_line(__("or")),
		frappe.ui.button({
			label: __("your current chart"),
			icon: "download",
			variant: "ghost",
			size: "sm",
			onclick: () =>
				download_sheet(company, "chart_sheet.download_chart_template", { template: "Current Chart" }),
		})
	);

	const $review = dialog.get_field("review").$wrapper;
	const $new_chart = dialog.get_field("new_chart").$wrapper;
	const show_step = (index) => {
		const review = index === 1 && Boolean(last_preview);
		stepper.set_current(review ? 1 : 0);
		for (const fieldname of ["file", "downloads"]) dialog.get_field(fieldname).$wrapper.toggle(!review);
		dialog.$body
			.find(".form-section")
			.filter((i, section) => !section.contains($review[0]) && !section.contains($new_chart[0]))
			.toggle(!review);
		dialog.set_df_property("review", "hidden", !review);
		dialog.set_df_property("new_chart_section", "hidden", !review);
		if (review) {
			dialog.set_primary_action(__("Replace chart"), replace);
			dialog.set_secondary_action_label(__("Back"));
			dialog.set_secondary_action(() => show_step(0));
			dialog.get_secondary_btn().removeClass("hidden");
		} else {
			dialog.set_primary_action(__("Next"), () => show_step(1));
			dialog.get_secondary_btn().addClass("hidden");
		}
		dialog.get_primary_btn().prop("disabled", !last_preview);
	};
	const preview = () => {
		const file_url = dialog.get_value("file");
		last_preview = null;
		$review.empty();
		$new_chart.empty();
		show_step(0);
		if (!file_url) return;
		call_method(frm, "preview_chart", { file_url: file_url, group_kinds: group_kinds }).then((r) => {
			// another file was picked meanwhile
			if (dialog.get_value("file") !== file_url) return;
			last_preview = r.message;
			render_import_preview(frm, $review, $new_chart, r.message, company, file_url, () => {
				dialog.set_value("file", "");
			});
			$review.prepend(
				make_group_kinds(r.message.unknown_groups, group_kinds, (group, kind) => {
					if (!kind || kind === group_kinds[group]) return;
					group_kinds[group] = kind;
					preview();
				})
			);
			show_step(1);
		});
	};
	const replace = () =>
		frappe.warn(
			__("Replace the chart?"),
			__(
				"Delete the {0} accounts and tax templates of {1} and make the chart from your sheet? This can't be undone.",
				[last_preview?.current_accounts, frappe.utils.escape_html(company)]
			),
			() =>
				call_method(
					frm,
					"import_chart",
					{ file_url: dialog.get_value("file"), group_kinds: group_kinds },
					{ freeze: true, freeze_message: __("Importing the chart...") }
				).then((r) => {
					close_and_reload(frm, dialog);
					const added = r.message?.added_accounts || [];
					frappe.ui.toast({
						type: "success",
						message: added.length
							? __("Chart imported. Added accounts ERPNext needs: {0}", [added.join(", ")])
							: __("Chart imported"),
						duration: (added.length ? 10 : 5) * 1000,
					});
				}),
			__("Replace chart")
		);

	dialog.show();
	show_step(0);
}

// For each group of a Busy list that Busy doesn't ship: does it hold customers, suppliers or
// accounts of the company's own; customers and suppliers become one ledger of the group's name
function make_group_kinds(groups, kinds, on_change) {
	if (!groups?.length) return [];
	return $(`<div class="flex flex-col gap-2 mb-3"></div>`).append(
		frappe.ui.alert({
			theme: groups.every((group) => kinds[group]) ? "blue" : "yellow",
			title: __("Busy doesn't say what these groups hold"),
			description: __(
				"A group of customers, like insurers under Sundry Debtors, becomes one receivable account; their invoices come in under Selling."
			),
		}),
		groups.map((group) => {
			const [$select] = make_kind_select(kinds[group], (kind) => on_change(group, kind));
			return $(`<div class="flex items-center justify-between gap-3"></div>`).append(
				$(`<span class="text-base text-ink-gray-8"></span>`).text(group),
				$select
			);
		})
	);
}

function make_kind_select(value, onchange) {
	const $parent = $(`<div class="shrink-0"></div>`);
	const control = frappe.ui.form.make_control({
		df: {
			fieldtype: "Select",
			fieldname: "kind",
			options: [
				{ value: "", label: __("Pick one") },
				{ value: "Customers", label: __("Customers") },
				{ value: "Suppliers", label: __("Suppliers") },
				{ value: "Accounts", label: __("Our own accounts") },
			],
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

// What the sheet holds and what replacing the chart does; the new chart itself folds away
function render_import_preview(frm, $preview, $tree, data, company, file_url, replace_file) {
	const currency = erpnext.get_currency(company);
	const $body = $(`<div class="flex flex-col gap-3"></div>`).appendTo($preview);
	$(`<div class="flex items-center gap-2 text-p-sm text-ink-gray-6"></div>`)
		.append(
			$(`<span class="text-ink-gray-5"></span>`).html(frappe.utils.icon("file-spreadsheet", "sm")),
			$(`<span class="grow truncate"></span>`).text(file_url.split("/").pop()),
			frappe.ui.button({
				label: __("Replace file"),
				icon: "upload",
				size: "sm",
				onclick: replace_file,
			})
		)
		.appendTo($body);

	const left_out = data.left_out || {};
	const left_out_total =
		Math.abs(left_out.Receivable || 0) + Math.abs(left_out.Payable || 0) + Math.abs(left_out.Stock || 0);
	const cards = [
		{
			label: __("Accounts"),
			value: format_number(data.accounts.length, null, 0),
			caption: data.added_accounts?.length
				? __("Replaces your {0} · ERPNext adds {1}", [
						data.current_accounts,
						data.added_accounts.join(", "),
				  ])
				: __("Replaces your {0}", [data.current_accounts]),
		},
		{
			label: __("Opening balances"),
			value: format_number(data.balances || 0, null, 0),
			caption: data.balances
				? data.draft_balances
					? plural(
							data.draft_balances,
							__("Replace your 1 draft"),
							__("Replace your {0} drafts", [data.draft_balances])
					  )
					: __("Added as drafts, not posted")
				: data.draft_balances
				? plural(
						data.draft_balances,
						__("Your 1 draft stays"),
						__("Your {0} drafts stay", [data.draft_balances])
				  )
				: __("None on the sheet"),
		},
	];
	if (left_out_total) {
		cards.push({
			label: __("Left out"),
			value: format_currency(left_out_total, currency),
			caption: left_out.Stock
				? __("Customers, suppliers and stock come in their own way")
				: __("Customers and suppliers come in as invoices and bills"),
		});
	}
	frappe.ui.stat_cards({ items: cards }).appendTo($body);

	$body.append(
		frappe.ui.alert({
			theme: "yellow",
			title: __("Replaces your {0} accounts and tax templates", [data.current_accounts]),
			description: __(
				"Your sheet becomes the chart. The company's default accounts are set again. Add your tax templates in Taxes after the import."
			),
		})
	);
	if (data.renamed?.length) {
		grey_line(
			__("Names used under more than one parent get the parent's name: {0}", [data.renamed.join(", ")])
		).appendTo($body);
	}
	const left_out_note = left_out_text(frm, left_out);
	if (left_out_note) {
		grey_line(left_out_note).appendTo($body);
	}
	const difference = data.balances ? flt(data.debit - data.credit, 2) : 0;
	if (difference) {
		grey_line(
			__(
				"Debit and credit on the sheet differ by {0}. It shows in Opening balances until the invoices are in.",
				[format_currency(Math.abs(difference), currency)]
			)
		).appendTo($body);
	}
	if (!data.balances && data.draft_balances) {
		grey_line(
			__(
				"Your draft balances move to the new accounts with the same name. Check them after the import."
			)
		).appendTo($body);
	}

	const children_of = {};
	for (const account of data.accounts) {
		const parent = account.parent_account || "";
		children_of[parent] = children_of[parent] || [];
		children_of[parent].push(account);
	}
	new SheetTree({
		parent: $tree,
		label: __("New chart"),
		root_value: "",
		expandable: true,
		row_style: true,
		// no doctype: these accounts don't exist yet, so no link previews
		args: {},
		children_of: children_of,
		get_label: (node) => frappe.utils.escape_html(node.label),
		on_node_render: open_all_once(),
	});
}

// Frappe's tree over the accounts of an uploaded sheet, so it needs no server call per group
class SheetTree extends frappe.ui.Tree {
	get_nodes(value, is_root) {
		return Promise.resolve(this.children_of[is_root ? "" : value] || []);
	}

	get_all_nodes() {
		return Promise.resolve(
			Object.entries(this.children_of).map(([parent, data]) => ({
				parent: parent || this.label,
				data: data,
			}))
		);
	}
}
