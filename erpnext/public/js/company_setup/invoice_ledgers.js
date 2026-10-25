import { make_alerts, plural, short_account } from "./common";
import { get_ledger_overs, ledger_over_title } from "./invoice_posting";

// Several receivable or payable ledgers in the invoices window: the sheet's Post to, the ledgers
// over their trial balance line, and the bar that sets the ledger of the rows ticked
export class InvoiceLedgers {
	constructor(win) {
		this.win = win;
		this.make_bulk_bar();
	}

	get grid() {
		return this.win.grid;
	}

	// the ledger Post to starts on: an upload's, the one the rows were moved to, or with no rows yet
	// the trial balance's only ledger line
	get_sheet_account(drafts, result) {
		const { win } = this;
		if (win.accounts.length < 2) return null;
		const used = [...new Set(drafts.map((row) => row.account).filter(Boolean))];
		// a sheet's ledger shows again only while rows are on it in place of their own ledger
		const moved = drafts.some(
			(row) =>
				row.account &&
				!row.account_by_hand &&
				row.account !==
					(win.own_accounts[row.party] ||
						win.party_accounts[row.party] ||
						win.names.default_account)
		);
		const line_accounts = this.get_line_accounts();
		return (
			result?.post_to ||
			(used.length === 1 && moved ? used[0] : null) ||
			(!drafts.length && line_accounts.length === 1 ? line_accounts[0] : null)
		);
	}

	// several receivable or payable lines in the trial balance: the sheet's ledger, picked once
	get_ledger_lines() {
		const { win } = this;
		return (win.frm.doc.__onload?.sheet_match?.[win.party_type]?.lines || []).filter(
			(line) => line.sheet !== null
		);
	}

	// the trial balance's ledgers, when it has them: a ledger with no line is never the sheet's
	get_line_accounts() {
		return this.get_ledger_lines()
			.map((line) => line.account)
			.filter((account) => this.win.accounts.includes(account));
	}

	make_post_to($parent) {
		const { win } = this;
		const { is_customer, accounts } = win;
		if (accounts.length < 2) return;
		const ledger_lines = this.get_ledger_lines();
		const line_accounts = this.get_line_accounts();
		const post_to = frappe.ui.form.make_control({
			df: {
				fieldtype: "Link",
				fieldname: "post_to",
				options: "Account",
				label: __("Post to"),
				only_select: 1,
				placeholder: is_customer
					? __("Each customer's own ledger")
					: __("Each supplier's own ledger"),
				description: [
					ledger_lines.length > 1 &&
						(is_customer
							? __(
									"Your trial balance has {0} receivable ledgers. Pick the one this sheet is for.",
									[ledger_lines.length]
							  )
							: __(
									"Your trial balance has {0} payable ledgers. Pick the one this sheet is for.",
									[ledger_lines.length]
							  )),
					__("Post to moves every row, except rows the sheet's Ledger column or you set."),
				]
					.filter(Boolean)
					.join(" "),
				get_query: () => ({
					filters: { name: ["in", line_accounts.length ? line_accounts : accounts] },
				}),
				filter_description: is_customer ? __("Receivable ledgers") : __("Payable ledgers"),
				change: () => this.set_sheet_account(post_to.get_value() || null),
			},
			parent: $parent,
			render_input: true,
		});
		// typing replaces the picked ledger instead of adding to it
		post_to.$input.on("focus", () => setTimeout(() => post_to.$input.select()));
		if (win.sheet_account) post_to.set_value(win.sheet_account);
	}

	set_sheet_account(value) {
		const { win } = this;
		if (value === win.sheet_account) return;
		win.sheet_account = value;
		for (const row of win.get_rows()) {
			if (row.account_by_hand || !win.in_sheet(row)) continue;
			row.account = value || win.own_accounts[row.party] || win.party_accounts[row.party];
		}
		this.grid.refresh();
		win.refresh_state();
	}

	// the ledgers over their trial balance line with the rows as they are in the window, saved or not
	get_overs(all) {
		const { win } = this;
		if (win.accounts.length < 2) return [];
		const moved = {};
		const add = (row, sign) => {
			const account = win.account_of(row);
			if (
				row.status === "Created" ||
				!account ||
				(!row.party && !row.create_party) ||
				win.other_currency[row.party] ||
				win.disabled.has(row.party)
			)
				return;
			moved[account] = (moved[account] || 0) + sign * flt(row.outstanding_amount);
		};
		all.forEach((row) => add(row, 1));
		(win.frm.doc[win.party.table] || []).forEach((row) => add(row, -1));
		return get_ledger_overs(win.frm, win.party_type, [], win.disabled, moved);
	}

	make_over_alerts(overs) {
		const { win } = this;
		const { frm, is_customer } = win;
		const shown = this.grid.filter.account?.value;
		if (win.accounts.includes(shown) && !overs.some((over) => over.account === shown)) {
			this.filter_account(null);
		}
		win.show_only_unread();
		if (!overs.length) return [];
		const all = win.get_rows();
		return make_alerts(
			overs.map((over) => {
				const only = over.account === shown;
				const count = this.grid.data.length;
				const has_rows = all.some(
					(row) => row.status !== "Created" && win.account_of(row) === over.account
				);
				const posted = flt(over.entered - over.draft, 2);
				return {
					theme: "red",
					title: ledger_over_title(frm, over, win.currency),
					description: [
						only
							? plural(
									count,
									__("Showing only the 1 row on it."),
									__("Showing only the {0} rows on it.", [count])
							  )
							: __("{0} is {1} short.", [
									over.short.map((line) => short_account(frm, line.account)).join(", "),
									format_currency(
										over.short.reduce((sum, line) => sum + line.sheet - line.entered, 0),
										win.currency
									),
							  ]),
						posted >= 1 &&
							(!has_rows || posted - flt(over.sheet) >= 1) &&
							(is_customer
								? __("{0} is in posted invoices.", [format_currency(posted, win.currency)])
								: __("{0} is in posted bills.", [format_currency(posted, win.currency)])),
						has_rows && __("Tick the rows that belong to another ledger, pick it, and Apply."),
					]
						.filter(Boolean)
						.join(" "),
					buttons: has_rows
						? [
								{
									label: only ? __("Show all") : __("Show rows"),
									onclick: () => this.show_rows_on(only ? null : over.account),
								},
						  ]
						: [],
				};
			})
		).addClass("mb-3");
	}

	// a ledger's rows only, in the table's own Account filter
	filter_account(account) {
		const grid = this.grid;
		if (account) {
			grid.filter.account = {
				df: grid.docfields.find((df) => df.fieldname === "account"),
				value: account,
			};
		} else {
			delete grid.filter.account;
		}
		grid.header_search?.search_columns.account?.find("input").val(account || "");
		grid.grid_pagination.go_to_page(1);
		grid.refresh();
	}

	show_rows_on(account) {
		const { win } = this;
		// rows with no ledger set go to the default one: named on them so the filter finds them
		for (const row of win.get_rows()) {
			if (account && !row.account && win.account_of(row) === account) row.account = account;
		}
		this.filter_account(account);
		win.refresh_state();
	}

	// the rows ticked in the table: set their ledger at once
	make_bulk_bar() {
		const { win } = this;
		const grid = this.grid;
		this.$bulk = $(`<div class="flex flex-wrap items-center gap-2 p-2 mb-2 border rounded-lg"></div>`)
			.hide()
			.prependTo(grid.wrapper);
		this.$bulk_text = $(`<span class="text-base text-ink-gray-8"></span>`).appendTo(this.$bulk);
		this.bulk_account = frappe.ui.form.make_control({
			df: {
				fieldtype: "Link",
				fieldname: "bulk_account",
				options: "Account",
				only_select: 1,
				placeholder: __("Pick a ledger"),
				get_query: () => ({ filters: { name: ["in", win.accounts] } }),
				filter_description: win.is_customer ? __("Receivable ledgers") : __("Payable ledgers"),
				change: () => this.update_bulk(),
			},
			parent: $(`<div class="w-64"></div>`).appendTo(this.$bulk),
			render_input: true,
		});
		this.$apply = frappe.ui
			.button({ label: __("Apply"), variant: "solid", onclick: () => this.apply_bulk() })
			.appendTo(this.$bulk);
		// the table's own Delete is hidden while rows are ticked, so a ledger change deletes nothing
		frappe.ui
			.button({
				label: __("Delete"),
				icon: "trash-2",
				variant: "ghost",
				onclick: () => {
					const ticked = grid.get_selected_children();
					win.remove_rows((row) => ticked.includes(row), ticked.length);
				},
			})
			.addClass("ml-auto")
			.appendTo(this.$bulk);
		// the header tick takes every row shown, on all pages
		grid.wrapper.on("click", ".grid-row-check", (e) => {
			const $check = $(e.currentTarget);
			if ($check.closest(".grid-heading-row").length) {
				for (const row of grid.data) {
					row.__checked = $check.prop("checked") && win.is_shown(row) ? 1 : 0;
				}
				for (const grid_row of grid.grid_rows || []) grid_row?.refresh_check();
			}
			this.update_bulk();
		});
	}

	update_bulk() {
		const grid = this.grid;
		const count = this.win.accounts.length > 1 ? grid.get_selected_children().length : 0;
		this.$bulk.toggle(!!count);
		grid.wrapper.toggleClass("company-setup-bulk-ledger", !!count);
		if (!count) return;
		this.$bulk_text.text(
			plural(
				count,
				__("1 row selected · Set its ledger to"),
				__("{0} rows selected · Set their ledger to", [count])
			)
		);
		this.$apply
			.prop("disabled", !this.bulk_account.get_value())
			.find(".es-button__label")
			.text(plural(count, __("Apply to 1 row"), __("Apply to {0} rows", [count])));
	}

	apply_bulk() {
		const { win } = this;
		const grid = this.grid;
		const account = this.bulk_account.get_value();
		const ticked = grid.get_selected_children();
		if (!account || !ticked.length) return;
		for (const row of ticked) {
			Object.assign(row, { account: account, account_by_hand: 1, __checked: 0 });
		}
		grid.wrapper.find(".grid-heading-row .grid-row-check").prop("checked", false);
		grid.refresh();
		win.refresh_state();
		frappe.ui.toast({
			type: "success",
			message: plural(
				ticked.length,
				__("1 row moved to {0}. Save draft to keep it.", [short_account(win.frm, account)]),
				__("{0} rows moved to {1}. Save draft to keep it.", [
					ticked.length,
					short_account(win.frm, account),
				])
			),
		});
	}
}
