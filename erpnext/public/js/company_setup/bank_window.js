import {
	after_run,
	call_method,
	cheque_words,
	count_rows,
	download_sheet,
	fix_button,
	get_data,
	get_rows_key,
	grey_line,
	make_alerts,
	make_back_header,
	make_kv_lines,
	make_lines_card,
	make_sheet_buttons,
	money,
	plural,
	reload_fresh,
	set_primary_disabled,
	short_account,
	upload_sheet,
} from "./common";
import { SheetWindow } from "./sheet_window";
import { show_opening_balances } from "./balances";
import { bank_menu, count_cheque_rows, show_cheques_posted } from "./bank";
import { BankDetailsView, get_bank_details_fields } from "./bank_details";

// One window, three views: the bank details, the uncleared cheques as its sheet, and the confirm
export class BankAccountsWindow extends SheetWindow {
	constructor(frm, details, bank_names) {
		super(frm, {
			title: __("Bank accounts"),
			where: "uncleared_cheques",
			has_data: (frm.doc.uncleared_cheques || []).length,
			primary_label: __("Save bank details"),
			get_method: "get_uncleared_cheques",
			checks: details,
		});
		this.views.accounts = ["accounts_head", "accounts", "accounts_alerts", "has_uncleared_cheques"];
		this.currency = erpnext.get_currency(frm.doc.company);
		this.cheques = [];
		this.day = details.day_before ? frappe.datetime.str_to_user(details.day_before) : null;
		this.details_view = new BankDetailsView(this);
		this.details_view.load(details, bank_names);
		this.set_view("accounts");
	}

	get_extra_fields(details) {
		return get_bank_details_fields(this, details);
	}

	get_fields() {
		const company = this.frm.doc.company;
		const bank_query = () => ({ filters: { company: company, account_type: "Bank", is_group: 0 } });
		return [
			{
				fieldname: "bank_account",
				fieldtype: "Link",
				options: "Account",
				label: __("Bank account"),
				in_list_view: 1,
				columns: 2,
				get_query: bank_query,
			},
			{
				fieldname: "entry_type",
				fieldtype: "Select",
				options: ["", "Issued", "Deposited"],
				label: __("Issued/Deposited"),
				in_list_view: 1,
				columns: 1,
			},
			{
				fieldname: "reference_no",
				fieldtype: "Data",
				label: cheque_words(__("Cheque or reference no")),
				in_list_view: 1,
				columns: 2,
			},
			{
				fieldname: "reference_date",
				fieldtype: "Date",
				label: __("Date"),
				in_list_view: 1,
				columns: 1,
			},
			{
				fieldname: "party_name",
				fieldtype: "Data",
				label: __("Party"),
				in_list_view: 1,
				columns: 2,
			},
			{
				fieldname: "amount",
				fieldtype: "Currency",
				label: __("Amount"),
				in_list_view: 1,
				columns: 2,
			},
			{ fieldname: "source", fieldtype: "Data", hidden: 1 },
		];
	}

	show() {
		const status = this.frm.doc.__onload?.bank || {};
		// bank details saved and cheques waiting: straight to them
		const to_cheques =
			this.details.has_uncleared_cheques === "Yes" &&
			status.accounts &&
			status.added === status.accounts &&
			status.drafts;
		this.dialog.show();
		to_cheques ? this.show_cheques() : this.details_view.show();
	}

	// what Save draft keeps in the view shown, to tell whether it changed since it was saved
	get_draft_key() {
		if (this.view === "sheet") return get_rows_key(this.grid, this.sync());
		if (this.view !== "accounts") return this.saved_key;
		return this.details_view.get_key();
	}

	is_dirty() {
		return this.saved_key !== undefined && this.get_draft_key() !== this.saved_key;
	}

	save_before_close() {
		return this.view === "sheet"
			? this.save_cheques().then(() => reload_fresh(this.frm))
			: this.details_view.save(true);
	}

	changed() {
		this.refresh_cheques();
	}

	// Part 2: the drafts of every bank, filtered to one by the picker
	show_cheques(account) {
		return get_data(this.frm, "get_uncleared_cheques").then((r) =>
			this.keep_scroll(() => {
				this.load(r.message);
				const with_rows = this.checks.banks.find((bank) =>
					this.cheques.some((row) => row.bank_account === bank.account)
				);
				const added = this.checks.banks.filter((bank) => bank.bank);
				const default_bank = this.frm.doc.__onload?.bank?.default_bank;
				this.dialog.get_field("notes").$wrapper.empty();
				this.picked =
					account ||
					with_rows?.account ||
					added.find((bank) => bank.account === default_bank)?.account ||
					(added[0] || this.checks.banks[0])?.account;
				this.show_sheet();
				this.saved_key = this.get_draft_key();
			})
		);
	}

	load(checks) {
		this.checks = checks;
		this.cheques = this.get_drafts();
	}

	render() {
		this.set_back(() => this.details_view.show());
		this.refresh_footer();
		this.make_head();
		this.show_bank_rows();
	}

	refresh_footer() {
		if (this.view !== "sheet") return;
		this.dialog.set_primary_action(__("Post to books"), () => this.post());
		this.dialog.set_secondary_action_label(__("Save draft"));
		this.dialog.set_secondary_action(() => this.save_draft());
	}

	make_head() {
		const $head = this.dialog.get_field("head").$wrapper.empty();
		$head.append(
			make_back_header(
				cheque_words(__("Uncleared cheques and deposits")),
				this.day
					? cheque_words(__("Cheques and deposits the bank had not cleared by {0}.", [this.day]))
					: null,
				() => this.details_view.show(),
				make_sheet_buttons(() => this.upload_cheques(), bank_menu(this.frm))
			).addClass("mb-3")
		);
		const $picker = $(`<div class="max-w-sm"></div>`).appendTo($head);
		this.picker = frappe.ui.form.make_control({
			df: {
				fieldtype: "Link",
				fieldname: "bank_account",
				options: "Account",
				label: __("Bank account"),
				placeholder: __("All bank accounts"),
				get_query: () => ({
					filters: { company: this.frm.doc.company, account_type: "Bank", is_group: 0 },
				}),
				change: () => {
					const value = this.picker.get_value() || null;
					if (value !== this.picked) this.pick(value);
				},
			},
			parent: $picker,
			render_input: true,
		});
		this.picker.set_value(this.picked || "");
		const $messages = this.dialog.get_field("alerts").$wrapper.empty();
		this.$alerts = $(`<div class="mb-3"></div>`).appendTo($messages);
		// nothing entered yet: the table's place says how to start, and the cards wait for rows
		this.typing = false;
		this.$empty = $(`<div class="border rounded-lg py-10 mb-3"></div>`)
			.append(
				frappe.ui.empty_state({
					icon: "file-spreadsheet",
					title: cheque_words(__("Upload your uncleared cheques")),
					description: cheque_words(
						__(
							"Excel or CSV of the cheques and deposits the bank had not cleared. Each is read with its bank, number, date and amount."
						)
					),
					actions: [
						{
							label: __("Upload sheet"),
							icon: "upload",
							variant: "solid",
							onclick: () => this.upload_cheques(),
						},
						{
							label: __("Add row"),
							onclick: () => {
								this.typing = true;
								this.refresh_cheques();
								this.grid.add_new_row();
							},
						},
						{
							label: __("Download sample sheet"),
							variant: "ghost",
							onclick: () =>
								download_sheet(
									this.frm.doc.company,
									"bank_accounts.download_uncleared_cheques"
								),
						},
						{
							label: cheque_words(__("None after all: answer No")),
							variant: "ghost",
							onclick: () => this.details_view.answer_no(),
						},
					],
				})
			)
			.appendTo($messages);
	}

	// the grid's rows go back into the list of all drafts; rows added under a bank get that bank
	sync() {
		const shown = this.get_rows();
		if (this.picked) {
			for (const row of shown) row.bank_account ||= this.picked;
		}
		this.cheques = [
			...this.cheques.filter(
				(row) => this.picked && row.bank_account !== this.picked && !shown.includes(row)
			),
			...shown,
		];
		return this.cheques;
	}

	pick(account) {
		this.sync();
		this.picked = account;
		if ((this.picker.get_value() || null) !== account) this.picker.set_value(account || "");
		this.show_bank_rows();
	}

	show_bank_rows(first) {
		const rows = this.picked
			? this.cheques.filter((row) => row.bank_account === this.picked)
			: this.cheques;
		this.grid.df.data = first ? [...rows.filter(first), ...rows.filter((row) => !first(row))] : rows;
		this.grid.set_column_disp_in_list_view("bank_account", !this.picked);
		this.grid.refresh();
		this.refresh_cheques();
	}

	// what the server checks again when posting, from the rows as they are now
	get_state() {
		const rows = this.sync();
		const posted = (this.frm.doc.uncleared_cheques || []).filter((row) => row.status === "Posted");
		const banks = this.checks.banks.map((bank) => {
			const mine = [...posted, ...rows].filter((row) => row.bank_account === bank.account);
			const total = (type) =>
				mine.filter((row) => row.entry_type === type).reduce((sum, row) => sum + flt(row.amount), 0);
			const count = (type) => mine.filter((row) => row.entry_type === type).length;
			const worked_out = flt(bank.book + total("Issued") - total("Deposited"), 2);
			const off = bank.statement === null ? null : flt(worked_out - bank.statement, 2);
			return {
				...bank,
				issued: total("Issued"),
				issued_count: count("Issued"),
				deposited: total("Deposited"),
				deposited_count: count("Deposited"),
				worked_out: worked_out,
				off: off === null ? null : Math.abs(off) < 1 ? 0 : off,
			};
		});
		const day_before = this.checks.day_before;
		// a cheque posted on one bank, again on another: a sheet's rows put on the wrong bank
		const posted_on = {};
		for (const row of posted) {
			if (row.reference_no) {
				posted_on[[row.entry_type, row.reference_no, flt(row.amount, 2)]] = row.bank_account;
			}
		}
		const stale_before = frappe.datetime.add_months(day_before, -this.checks.stale_months);
		const foreign = new Set(
			banks.filter((bank) => bank.account_currency !== this.currency).map((bank) => bank.account)
		);
		return {
			rows,
			banks,
			no_bank: (row) => !row.bank_account,
			other_currency: (row) => foreign.has(row.bank_account),
			no_type: (row) => !row.entry_type,
			no_date: (row) => !row.reference_date,
			no_amount: (row) => flt(row.amount) <= 0,
			stale: (row) => row.reference_date && row.reference_date < stale_before,
			post_dated: (row) => row.reference_date && row.reference_date > day_before,
			no_reference: (row) => !row.reference_no,
			posted_elsewhere: (row) => {
				const on =
					row.reference_no && posted_on[[row.entry_type, row.reference_no, flt(row.amount, 2)]];
				return Boolean(on) && on !== row.bank_account;
			},
		};
	}

	refresh_cheques() {
		if (!this.checks || this.view !== "sheet") return;
		const state = this.get_state();
		const reds = this.get_reds(state);
		const yellows = this.get_yellows(state);
		const empty = !this.typing && !this.sync().length;
		this.$empty.toggleClass("hide", !empty);
		this.set_stats(empty ? [] : this.make_cards(state));
		this.dialog.set_df_property("rows", "hidden", empty ? 1 : 0);
		// with no rows only what stops adding them is said
		this.$alerts
			.empty()
			.append(
				make_alerts(
					empty ? reds.slice(0, this.checks.balances_posted ? 0 : 1) : [...reds, ...yellows]
				)
			);
		set_primary_disabled(this.dialog, !state.rows.length || reds.length > 0);
	}

	name_of(banks, account) {
		return banks.find((bank) => bank.account === account)?.bank || short_account(this.frm, account);
	}

	make_cards({ banks }) {
		const picked = banks.find((bank) => bank.account === this.picked);
		const on = (label) =>
			picked ? __("{0} · {1}", [label, this.name_of(banks, picked.account)]) : label;
		const sum = (field) => banks.reduce((total, bank) => total + flt(bank[field]), 0);
		const shown = picked ? [picked] : banks;
		const statement = picked?.statement;
		return [
			frappe.ui.stat_card({
				label: on(__("Book balance")),
				value: money(this.frm, picked ? picked.book : sum("book"), 0),
				caption: shown.some((bank) => bank.book_from_sheet)
					? __("From your trial balance, not posted yet")
					: this.day
					? __("On {0}", [this.day])
					: null,
			}),
			make_lines_card(on(__("Uncleared")), [
				[
					__("Issued · {0}", [shown.reduce((n, bank) => n + bank.issued_count, 0)]),
					money(this.frm, picked ? picked.issued : sum("issued"), 0),
				],
				[
					__("Deposited · {0}", [shown.reduce((n, bank) => n + bank.deposited_count, 0)]),
					money(this.frm, picked ? picked.deposited : sum("deposited"), 0),
				],
			]),
			frappe.ui.stat_card({
				label: on(__("Expected statement")),
				value: money(this.frm, picked ? picked.worked_out : sum("worked_out"), 0),
				caption: picked
					? statement === null
						? __("No statement balance typed")
						: picked.off
						? __("Statement {0} · off by {1}", [
								money(this.frm, statement, 0),
								money(this.frm, Math.abs(picked.off), 0),
						  ])
						: __("Statement {0} · matches", [money(this.frm, statement, 0)])
					: __("{0} of {1} match their statement", [
							banks.filter((bank) => bank.off === 0).length,
							banks.filter((bank) => bank.off !== null).length,
					  ]),
			}),
		];
	}

	// the rows a message is about, first; on their bank when another is picked
	show_rows_button(test) {
		return {
			label: __("Show rows"),
			onclick: () => {
				const row = this.sync().find(test);
				if (row && this.picked && row.bank_account !== this.picked) this.pick(row.bank_account);
				this.show_bank_rows(test);
			},
		};
	}

	get_reds(state) {
		const { rows } = state;
		const count = (test) => rows.filter(test).length;
		const red = (title, test, description) => ({
			theme: "red",
			title,
			description,
			buttons: test ? [this.show_rows_button(test)] : [],
		});
		const reds = [];
		if (!this.checks.balances_posted) {
			reds.push({
				...red(cheque_words(__("Post opening balances first to add uncleared cheques."))),
				buttons: [
					{
						label: __("Opening balances"),
						onclick: () => {
							this.dialog.hide();
							show_opening_balances(this.frm);
						},
					},
				],
			});
		}
		if (this.checks.posting_blocker) {
			reds.push({
				...red(this.checks.posting_blocker),
				buttons: this.checks.posting_fix
					? [fix_button(this.checks.posting_fix, this.frm, () => this.show_cheques(this.picked))]
					: [],
			});
		}
		if (count(state.no_bank)) {
			reds.push(
				red(__("Bank account missing for {0}.", [count_rows(count(state.no_bank))]), state.no_bank)
			);
		}
		if (count(state.posted_elsewhere)) {
			reds.push(
				red(
					__("{0} posted already on another bank.", [count_rows(count(state.posted_elsewhere))]),
					state.posted_elsewhere,
					count(state.posted_elsewhere) === 1
						? __("Remove it, or fix its bank.")
						: __("Remove them, or fix their bank.")
				)
			);
		}
		if (count(state.other_currency)) {
			reds.push(
				red(
					__("Foreign currency bank accounts come later. Remove {0}.", [
						count_rows(count(state.other_currency)),
					]),
					state.other_currency
				)
			);
		}
		if (count(state.no_type)) {
			reds.push(
				red(
					__("Issued or deposited missing for {0}.", [count_rows(count(state.no_type))]),
					state.no_type
				)
			);
		}
		const no_date = rows.filter(state.no_date);
		const no_date_banks = [...new Set(no_date.map((row) => row.bank_account))];
		if (no_date.length) {
			reds.push(
				red(
					no_date_banks.length === 1
						? __("Date missing for {0} of {1}.", [
								count_rows(no_date.length),
								short_account(this.frm, no_date_banks[0]),
						  ])
						: __("Date missing for {0} of {1} banks.", [
								count_rows(no_date.length),
								no_date_banks.length,
						  ]),
					state.no_date
				)
			);
		}
		if (count(state.no_amount)) {
			reds.push(
				red(__("Amount missing for {0}.", [count_rows(count(state.no_amount))]), state.no_amount)
			);
		}
		return reds;
	}

	get_yellows(state) {
		const { rows, banks } = state;
		const yellows = banks
			.filter((bank) => bank.off)
			.map((bank) => ({
				theme: "yellow",
				title: __("{0} is off by {1}. Check the statement balance or the rows.", [
					this.name_of(banks, bank.account),
					money(this.frm, Math.abs(bank.off), 0),
				]),
				buttons: [{ label: __("Show rows"), onclick: () => this.pick(bank.account) }],
			}));
		// what only points something out is said for the bank shown
		const mine = this.picked ? rows.filter((row) => row.bank_account === this.picked) : rows;
		const count_mine = (test) => mine.filter(test).length;
		const yellow = (test, title, description) =>
			count_mine(test) &&
			yellows.push({
				theme: "yellow",
				title,
				description,
				buttons: [this.show_rows_button(test)],
			});
		yellow(
			state.stale,
			__("{0} older than {1} months.", [count_rows(count_mine(state.stale)), this.checks.stale_months]),
			cheque_words(__("A cheque this old may have lapsed. Check with the bank before you post it."))
		);
		yellow(
			state.post_dated,
			__("{0} dated after {1}.", [count_rows(count_mine(state.post_dated)), this.day]),
			__("They are posted on {0} with their own date.", [this.day])
		);
		yellow(
			state.no_reference,
			cheque_words(
				__("{0} with no cheque or reference number.", [count_rows(count_mine(state.no_reference))])
			),
			__("Add it, so Bank Clearance, where you mark them cleared later, can tell them apart.")
		);
		return yellows;
	}

	save_cheques() {
		return call_method(this.frm, "save_uncleared_cheques", {
			rows: this.sync().filter((row) => row.bank_account || flt(row.amount)),
		});
	}

	refresh() {
		return reload_fresh(this.frm).then(() => this.show_cheques(this.picked));
	}

	save_draft() {
		return this.save_cheques()
			.then(() => this.refresh())
			.then(() => frappe.ui.toast({ type: "success", message: __("Saved as a draft.") }));
	}

	upload_cheques() {
		upload_sheet(
			this.frm,
			"import_uncleared_cheques",
			{ bank_account: this.picked },
			{
				replaces:
					this.sync().length &&
					__(
						"The sheet replaces the draft rows of the banks on it, unsaved changes included. Posted rows stay as they are. Continue?"
					),
				after: (result) => this.after_upload(result),
			}
		);
	}

	after_upload(result) {
		const banks = result.banks || [];
		this.show_cheques(banks.length === 1 ? banks[0] : null).then(() => {
			const bank = banks.length === 1 && short_account(this.frm, banks[0]);
			this.dialog
				.get_field("notes")
				.$wrapper.empty()
				.append(
					$(`<div class="flex flex-col gap-1 mt-2"></div>`).append(
						grey_line(__("{0} read from the sheet.", [count_rows(result.added)])),
						result.no_bank_column && bank
							? grey_line(
									result.title_bank
										? __(
												"The sheet has no bank column; its title names {0}, so its rows are for it.",
												[bank]
										  )
										: __("The sheet has no bank column, so its rows are for {0}.", [bank])
							  )
							: [],
						result.replaced
							? grey_line(
									plural(
										result.replaced,
										__("1 draft replaced."),
										__("{0} drafts replaced.", [result.replaced])
									)
							  )
							: [],
						result.skipped
							? grey_line(__("{0} already posted are left out.", [count_rows(result.skipped)]))
							: [],
						result.unknown_banks?.length
							? grey_line(
									__(
										"Rows of these banks are left out, no bank account matches them: {0}.",
										[result.unknown_banks.join(", ")]
									)
							  )
							: []
					)
				);
		});
	}

	post() {
		if (!this.checks) return;
		return this.save_cheques()
			.then(() => get_data(this.frm, "get_uncleared_cheques"))
			.then((r) => {
				this.load(r.message);
				this.show_bank_rows();
				this.saved_key = this.get_draft_key();
				if (this.checks.blocker) {
					frappe.ui.toast({ type: "error", message: this.checks.blocker });
				} else if (this.cheques.length) {
					this.confirm_post();
				}
			});
	}

	get_drafts() {
		return (this.frm.doc.uncleared_cheques || [])
			.filter((row) => row.status !== "Posted")
			.map((row) => ({
				bank_account: row.bank_account,
				entry_type: row.entry_type,
				reference_no: row.reference_no,
				reference_date: row.reference_date,
				party_name: row.party_name,
				amount: row.amount,
				source: row.source,
			}));
	}

	confirm_post() {
		const rows = this.cheques;
		const issued = rows.filter((row) => row.entry_type === "Issued");
		const deposited = rows.filter((row) => row.entry_type === "Deposited");
		const total = (list) => list.reduce((sum, row) => sum + flt(row.amount), 0);
		const banks = [...new Set(rows.map((row) => row.bank_account))];
		this.show_confirm({
			title: cheque_words(__("Post uncleared cheques?")),
			back: cheque_words(__("Nothing is posted until you confirm.")),
			body: [
				make_kv_lines([
					[
						__("To post"),
						__("{0} · {1} issued, {2} deposited", [
							count_cheque_rows(this.frm, rows),
							money(this.frm, total(issued)),
							money(this.frm, total(deposited)),
						]),
					],
					[
						rows.length && banks.length === 1 ? __("Bank") : __("Banks"),
						banks.map((account) => short_account(this.frm, account)).join(", "),
					],
					[__("Dated"), this.day],
				]),
				grey_line(__("Your bank balance and Temporary Opening do not change.")),
			],
			primary: __("Post {0}", [count_rows(rows.length)]),
			on_primary: () =>
				call_method(this.frm, "post_uncleared_cheques", {}, { freeze: true }).then((r) => {
					this.dialog.hide();
					after_run(
						this.frm,
						r.message,
						__("Posting in the background. This page updates when it's done."),
						() => show_cheques_posted(this.frm)
					);
				}),
		});
	}
}
