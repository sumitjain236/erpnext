import { grey_line, name_with_badge, plural } from "./common";
import { setup_dialog_grid } from "./sheet_window";

// found, a guess to confirm (check), no match (new), confirmed as new (create), or one that can't post
export function get_name_kind(row, names, disabled) {
	if (!row.party) {
		if (row.create_party) return "create";
		return names.suggestions?.[row.party_name] ? "check" : "new";
	}
	if (disabled.has(row.party)) return "disabled";
	if (names.other_currency?.[row.party]) return "other_currency";
	return "found";
}

const REVIEW_KINDS = ["check", "new", "disabled", "other_currency"];

// only these stop posting; disabled and other-currency rows wait as drafts and the rest post
export const OPEN_KINDS = ["check", "new"];

export function get_name_badge(kind, party_type, currency) {
	const badge = (label, theme) => frappe.ui.badge.html({ label: label, theme: theme });
	switch (kind) {
		case "check":
			return badge(__("Check"), "orange");
		case "new":
			return badge(__("New"), "gray");
		case "create":
			return badge(party_type === "Customer" ? __("New customer") : __("New supplier"), "gray");
		case "confirmed":
			return badge(__("Confirmed"), "green");
		case "disabled":
			return badge(__("Disabled"), "orange");
		case "other_currency":
			return badge(currency || __("Other currency"), "orange");
		default:
			return "";
	}
}

// One entry per name on the sheet, with its kind, invoice count and amount
export function get_name_state(rows, kind_of) {
	const named = rows.filter((row) => row.party_name || row.party);
	const by_name = new Map();
	for (const row of named) {
		const key = row.party_name || row.party;
		const name = by_name.get(key) || {
			name_on_sheet: key,
			party: row.party,
			kind: kind_of(row),
			invoices: 0,
			amount: 0,
		};
		name.invoices += 1;
		name.amount += flt(row.outstanding_amount);
		by_name.set(key, name);
	}
	const names = [...by_name.values()];
	return {
		invoices: named.filter((row) => flt(row.outstanding_amount) > 0),
		advance: -named
			.filter((row) => flt(row.outstanding_amount) < 0)
			.reduce((sum, row) => sum + flt(row.outstanding_amount), 0),
		names: names,
		review: names.filter((name) => REVIEW_KINDS.includes(name.kind)),
		ready: names.filter((name) => ["found", "create"].includes(name.kind)),
		ready_rows: named.filter((row) => ["found", "create"].includes(kind_of(row))),
	};
}

// The names on the sheet, a view of the invoices window with a way back to the invoices
export class InvoiceNamesView {
	constructor(win) {
		this.win = win;
		this.$names = win.dialog.get_field("names_view").$wrapper.addClass("company-setup-names");
		this.filter = "review";
		this.line = null;
		// confirming the last names posts, when Post to books brought them here
		this.post_after = false;
	}

	show(line = null, post_after = false) {
		this.line = line;
		this.post_after = post_after;
		this.filter = "review";
		this.win.set_view("names");
		this.render();
	}

	back() {
		this.post_after = false;
		this.win.show_sheet();
	}

	render() {
		const { win } = this;
		const state = get_name_state(win.get_rows(), (row) => win.kind_of(row));
		const order = { check: 0, new: 1, disabled: 2, other_currency: 3, create: 4, found: 5 };
		const list = state.names.sort((a, b) => order[a.kind] - order[b.kind]);
		const shown = (this.filter === "review" ? state.review : list).map((name) => ({
			...name,
			original_party: name.party,
			party: name.party || (name.kind === "check" ? win.names.suggestions[name.name_on_sheet] : null),
		}));
		this.$names.empty();
		this.make_head(state, list);
		this.make_table(shown);
		this.make_group_picker();
		this.grid.wrapper.on("click", ".grid-row-check", () => setTimeout(() => this.update_confirm()));
		win.dialog.set_secondary_action_label(__("Save draft"));
		this.update_confirm();
	}

	make_head(state, list) {
		const { win } = this;
		const { names, is_customer } = win;
		const new_names = state.review.filter((name) => name.kind === "new");
		const blocked = state.review.filter((name) => ["disabled", "other_currency"].includes(name.kind));
		const $head = $(`<div class="flex flex-col gap-4 mb-4"></div>`).appendTo(this.$names);
		$head.append(
			$(`<div class="flex flex-wrap items-center justify-between gap-3"></div>`).append(
				$(`<div class="flex items-center gap-2"></div>`).append(
					frappe.ui.button({
						icon: "arrow-left",
						variant: "ghost",
						tooltip: is_customer ? __("Back to invoices") : __("Back to bills"),
						onclick: () => this.back(),
					}),
					$(`<div class="text-lg-semibold text-ink-gray-9"></div>`).text(
						is_customer ? __("Customer names") : __("Supplier names")
					)
				),
				frappe.ui.tab_buttons({
					label: __("Show"),
					value: this.filter,
					options: [
						{ label: __("To review {0}", [state.review.length]), value: "review" },
						{ label: __("All {0}", [list.length]), value: "all" },
					],
					on_change: (value) => {
						this.filter = value;
						this.render();
					},
				})
			),
			// what to do comes first, then what the badges mean
			$(`<div class="flex flex-col gap-1"></div>`).append(
				$(`<div class="text-base text-ink-gray-8"></div>`).text(
					this.line ||
						(this.filter === "review"
							? __("Tick the names that are right, then confirm.")
							: is_customer
							? __("A change here applies to all that name's invoices.")
							: __("A change here applies to all that name's bills."))
				),
				grey_line(
					[
						__("Check: a guess · New: created when you confirm"),
						blocked.length &&
							(is_customer
								? __("Red: can't be posted, so pick another customer.")
								: __("Red: can't be posted, so pick another supplier.")),
					]
						.filter(Boolean)
						.join(" · ")
				)
			)
		);
		if (new_names.length && !names.can_create) {
			$head.append(
				frappe.ui.alert({
					theme: "blue",
					title: is_customer ? __("You can't add customers") : __("You can't add suppliers"),
					description: is_customer
						? __("Pick an existing customer for {0}, or ask someone who can add customers.", [
								new_names.map((name) => name.name_on_sheet).join(", "),
						  ])
						: __("Pick an existing supplier for {0}, or ask someone who can add suppliers.", [
								new_names.map((name) => name.name_on_sheet).join(", "),
						  ]),
				})
			);
		}
	}

	make_table(shown) {
		const { win } = this;
		const { names, is_customer, party_type } = win;
		const group = new frappe.ui.FieldGroup({
			body: $(`<div></div>`).appendTo(this.$names),
			fields: [
				{
					fieldname: "names",
					fieldtype: "Table",
					hide_label: 1,
					cannot_add_rows: true,
					cannot_delete_rows: true,
					in_place_edit: true,
					data: shown,
					fields: [
						{
							fieldname: "name_on_sheet",
							fieldtype: "Data",
							label: __("Name on sheet"),
							read_only: 1,
							in_list_view: 1,
							columns: 4,
							formatter: (value, df, options, doc) => {
								const kind = win.confirmed.has(value) ? "confirmed" : doc?.kind;
								const badge = get_name_badge(
									kind,
									party_type,
									win.other_currency[doc?.party]
								);
								return name_with_badge(frappe.form.formatters.Data(value, df), badge);
							},
						},
						{
							fieldname: "party",
							fieldtype: "Link",
							options: party_type,
							label: __(party_type),
							in_list_view: 1,
							columns: 3,
							get_query: () => ({ filters: { disabled: 0 } }),
							formatter: (value, df, options, doc) =>
								value
									? frappe.form.formatters.Link(value, df, options, doc)
									: doc?.kind === "create"
									? `<span class="text-ink-gray-6">${
											is_customer ? __("New customer") : __("New supplier")
									  }</span>`
									: `<span class="text-ink-gray-4">${
											names.can_create
												? __("Pick one, or confirm to create")
												: __("Pick one")
									  }</span>`,
							onchange: () => this.update_confirm(),
						},
						{
							fieldname: "invoices",
							fieldtype: "Int",
							label: is_customer ? __("Invoices") : __("Bills"),
							read_only: 1,
							in_list_view: 1,
							columns: 1,
						},
						{
							fieldname: "amount",
							fieldtype: "Currency",
							label: __("Amount"),
							read_only: 1,
							in_list_view: 1,
							columns: 2,
						},
					],
				},
			],
		});
		group.make();
		this.grid = group.fields_dict.names.grid;
		setup_dialog_grid(this.grid);
	}

	// without a default group in the settings, the ones created here get the group picked here
	make_group_picker() {
		const { is_customer } = this.win;
		this.$group = $(`<div class="mt-3 max-w-sm hide"></div>`).appendTo(this.$names);
		const form = new frappe.ui.FieldGroup({
			body: this.$group,
			fields: [
				{
					fieldtype: "Link",
					fieldname: "party_group",
					options: is_customer ? "Customer Group" : "Supplier Group",
					label: is_customer ? __("Group for new customers") : __("Group for new suppliers"),
					get_query: () => ({ filters: { is_group: 0 } }),
				},
			],
		});
		form.make();
		this.group_control = form.get_field("party_group");
	}

	get default_group() {
		return this.win.frm.doc.__onload?.default_groups?.[this.win.party_type];
	}

	// what confirming a ticked name does: link it to the customer shown, or create it as new
	get_action(name) {
		if (name.party) {
			const changed = name.party !== name.original_party;
			return OPEN_KINDS.includes(name.kind) || changed ? "link" : null;
		}
		return OPEN_KINDS.includes(name.kind) && this.win.names.can_create ? "create" : null;
	}

	update_confirm() {
		const { win } = this;
		const { is_customer } = win;
		const actions = this.grid
			.get_selected_children()
			.map((name) => this.get_action(name))
			.filter(Boolean);
		const creates = actions.filter((action) => action === "create").length;
		this.$group.toggleClass("hide", !creates || !!this.default_group);
		const territory = win.frm.doc.__onload?.default_groups?.territory;
		this.group_control.set_description(
			[
				is_customer
					? __("Your selling settings have no default group. Leave empty to add them without one.")
					: __("Your buying settings have no default group. Leave empty to add them without one."),
				is_customer && territory && __("Territory: {0}.", [territory]),
			]
				.filter(Boolean)
				.join(" ")
		);
		win.dialog.set_primary_action(
			!actions.length
				? __("Confirm")
				: creates
				? __("Confirm and create {0}", [creates])
				: __("Confirm {0}", [actions.length]),
			() => this.confirm()
		);
		win.dialog.get_primary_btn().prop("disabled", !actions.length);
	}

	confirm() {
		const { win } = this;
		const { names } = win;
		let done = 0;
		for (const name of this.grid.get_selected_children()) {
			const action = this.get_action(name);
			if (!action) continue;
			const group = this.group_control.get_value() || null;
			const rows = win.set_rows_party(
				name.name_on_sheet,
				action === "link"
					? { party: name.party, create_party: 0 }
					: { party: null, create_party: 1, party_group: group }
			);
			// a ledger the sheet or the user gave stays
			const account =
				action === "link"
					? win.own_accounts[name.party]
					: names.group_accounts?.[group || this.default_group];
			rows.filter((row) => !row.account_by_hand).forEach(
				(row) => (row.account = (win.in_sheet(row) && win.sheet_account) || account)
			);
			win.confirmed.add(name.name_on_sheet);
			done += 1;
		}
		if (!done) return;
		win.fill_accounts();
		const open = win.get_rows().some((row) => OPEN_KINDS.includes(win.kind_of(row)));
		frappe.ui.toast({
			type: "success",
			message: plural(done, __("1 name confirmed."), __("{0} names confirmed.", [done])),
		});
		if (open) {
			this.render();
			return;
		}
		const then_post = this.post_after;
		this.back();
		if (then_post) win.post();
	}
}
