import {
	after_run,
	ask_before_closing,
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
	make_sheet_head,
	money,
	name_with_badge,
	new_badge,
	plural,
	reload_fresh,
	set_primary_disabled,
	short_account,
	show_in_place_of,
	upload_sheet,
} from "./common";
import { fill_where, where_to_find_fields } from "./where_to_find";
import { fill_confirm, setup_dialog_grid, show_view_fields } from "./sheet_window";
import { show_opening_balances } from "./balances";
import { bank_menu, count_cheque_rows, show_bank_accounts, show_cheques_posted } from "./bank";

export class BankAccountsWindow {
	constructor(frm, details, bank_names) {
		this.frm = frm;
		this.details = details;
		this.known_banks = new Set(bank_names.map((name) => name.trim().toLowerCase()));
		this.currency = erpnext.get_currency(frm.doc.company);
		this.day = details.day_before ? frappe.datetime.str_to_user(details.day_before) : null;
		this.accounts = details.rows.map((row) => ({
			account: row.account,
			bank: row.bank,
			bank_account_no: row.bank_account_no,
			branch_code: row.branch_code,
			statement_balance: row.statement_balance,
			is_default: row.is_default ? 1 : 0,
			is_credit_card: row.is_credit_card ? 1 : 0,
			account_currency: row.account_currency,
			book: row.book,
			line: row.line,
			root_type: row.root_type,
		}));
		// no default yet: the only account that isn't overdrawn or owed is ticked, to change if need be
		const in_credit = this.accounts.filter(
			(row) => row.bank && row.root_type === "Asset" && flt(row.book) >= 0
		);
		if (!this.accounts.some((row) => row.is_default) && in_credit.length === 1) {
			in_credit[0].is_default = 1;
		}
		this.make_dialog();
	}

	is_new_bank(bank) {
		return Boolean(bank) && !this.known_banks.has(bank.trim().toLowerCase());
	}

	show(replacing) {
		const status = this.frm.doc.__onload?.bank || {};
		// bank details saved and cheques waiting: straight to them
		const to_cheques =
			this.details.has_uncleared_cheques === "Yes" &&
			status.accounts &&
			status.added === status.accounts &&
			status.drafts;
		show_in_place_of(this.dialog, replacing);
		to_cheques ? this.show_cheques() : this.show_accounts();
	}

	make_dialog() {
		const country = this.frm.doc.__onload?.company_details?.country;
		const india = country === "India";
		const refresh = () => this.refresh_accounts();
		const refresh_cheques = () => this.refresh_cheques();
		const company = this.frm.doc.company;
		const win = this;
		this.dialog = new frappe.ui.Dialog({
			title: __("Bank accounts"),
			size: "extra-large",
			fields: [
				{ fieldname: "accounts_head", fieldtype: "HTML" },
				{
					fieldname: "accounts",
					fieldtype: "Table",
					description: __(
						"Statement: type it as the bank shows it. For an overdrawn account, what you owe the bank."
					),
					cannot_add_rows: 1,
					cannot_delete_rows: 1,
					in_place_edit: 1,
					data: this.accounts,
					fields: [
						{
							fieldname: "account",
							fieldtype: "Link",
							options: "Account",
							label: __("Account"),
							in_list_view: 1,
							read_only: 1,
							columns: 3,
							formatter: (value) =>
								value
									? `<span class="block truncate" title="${frappe.utils.escape_html(
											value
									  )}">${frappe.utils.escape_html(short_account(this.frm, value))}</span>`
									: "",
						},
						{
							fieldname: "bank",
							fieldtype: "Link",
							options: "Bank",
							label: __("Bank"),
							in_list_view: 1,
							columns: 2,
							ignore_link_validation: 1,
							formatter: (value) =>
								value
									? name_with_badge(
											frappe.utils.escape_html(value),
											this.is_new_bank(value) && new_badge()
									  )
									: "",
							onchange: refresh,
						},
						{
							fieldname: "bank_account_no",
							fieldtype: "Data",
							label: __("Account number"),
							in_list_view: 1,
							columns: 2,
							onchange: refresh,
						},
						{
							fieldname: "branch_code",
							fieldtype: "Data",
							label: india
								? __("IFSC")
								: country === "United States"
								? __("Routing number")
								: __("Branch code"),
							in_list_view: 1,
							columns: 1,
						},
						{
							fieldname: "statement_balance",
							fieldtype: "Currency",
							options: "account_currency",
							// the date is on the cards above: the column is too narrow for it
							label: __("Statement"),
							in_list_view: 1,
							columns: 1,
							onchange: refresh,
							formatter: (value, df, options, row) =>
								[null, undefined, ""].includes(value)
									? ""
									: `<span class="flex items-center justify-end gap-2">${
											is_overdrawn(row)
												? frappe.ui.badge.html({
														label: __("Overdrawn"),
														theme: "gray",
												  })
												: ""
									  }${format_currency(Math.abs(flt(value)), row.account_currency)}</span>`,
						},
						{
							fieldname: "is_default",
							fieldtype: "Check",
							label: __("Default"),
							in_list_view: 1,
							columns: 1,
							onchange: function () {
								win.set_default(this.doc);
							},
						},
						{
							fieldname: "is_credit_card",
							fieldtype: "Check",
							label: __("Credit card"),
							description: cheque_words(
								__("Its charges post as Credit Card Entries and aren't counted as cheques.")
							),
						},
						{ fieldname: "account_currency", fieldtype: "Link", options: "Currency", hidden: 1 },
					],
				},
				// under the rows: a box that comes or goes while a statement is typed doesn't move them
				{ fieldname: "accounts_alerts", fieldtype: "HTML" },
				{
					fieldname: "has_uncleared_cheques",
					fieldtype: "Select",
					label: this.day
						? cheque_words(
								__("Any cheques or deposits not cleared by the bank on {0}?", [this.day])
						  )
						: cheque_words(
								__(
									"Any cheques or deposits not cleared by the bank on the day before migration?"
								)
						  ),
					options: [
						{ value: "", label: "" },
						{ value: "No", label: __("No") },
						{ value: "Yes", label: __("Yes") },
					],
					default: this.details.has_uncleared_cheques || "",
					reqd: 1,
					change: refresh,
				},
				{ fieldname: "cheques_head", fieldtype: "HTML", hidden: 1 },
				{
					fieldname: "cheques",
					fieldtype: "Table",
					hidden: 1,
					in_place_edit: 1,
					data: [],
					fields: [
						{
							fieldname: "bank_account",
							fieldtype: "Link",
							options: "Account",
							label: __("Bank account"),
							in_list_view: 1,
							columns: 2,
							get_query: () => ({
								filters: { company: company, account_type: "Bank", is_group: 0 },
							}),
							onchange: refresh_cheques,
						},
						{
							fieldname: "entry_type",
							fieldtype: "Select",
							options: ["", "Issued", "Deposited"],
							label: __("Issued/Deposited"),
							in_list_view: 1,
							columns: 1,
							onchange: refresh_cheques,
						},
						{
							fieldname: "reference_no",
							fieldtype: "Data",
							label: cheque_words(__("Cheque or reference no")),
							in_list_view: 1,
							columns: 2,
							onchange: refresh_cheques,
						},
						{
							fieldname: "reference_date",
							fieldtype: "Date",
							label: __("Date"),
							in_list_view: 1,
							columns: 1,
							onchange: refresh_cheques,
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
							onchange: refresh_cheques,
						},
						{ fieldname: "source", fieldtype: "Data", hidden: 1 },
					],
				},
				{ fieldname: "cheques_notes", fieldtype: "HTML", hidden: 1 },
				{ fieldname: "confirm", fieldtype: "HTML", hidden: 1 },
				...where_to_find_fields(this.frm, "uncleared_cheques", {
					has_data: (this.frm.doc.uncleared_cheques || []).length,
					posted: false,
				}),
			],
			primary_action_label: __("Save bank details"),
			primary_action: () => this.save_accounts(),
			secondary_action_label: __("Save draft"),
			secondary_action: () => this.save_accounts(true),
		});
		fill_where(this.frm, this.dialog, "uncleared_cheques");
		// a No or Yes needs no full-width box
		this.dialog
			.get_field("has_uncleared_cheques")
			.$wrapper.find(".control-input-wrapper")
			.addClass("max-w-xs");
		this.accounts_grid = this.dialog.fields_dict.accounts.grid;
		setup_dialog_grid(this.accounts_grid);
		this.cheques_grid = this.dialog.fields_dict.cheques.grid;
		setup_dialog_grid(this.cheques_grid, () => this.refresh_cheques());
		ask_before_closing(
			this.dialog,
			() => this.saved_key !== undefined && this.get_draft_key() !== this.saved_key,
			() =>
				this.view === "cheques"
					? this.save_cheques().then(() => reload_fresh(this.frm))
					: this.save_accounts(true)
		);
		this.$back = $(`<div class="hide"></div>`)
			.append(frappe.ui.button({ label: __("Back"), onclick: () => this.show_accounts() }))
			.appendTo(this.dialog.custom_actions);
	}

	// one window, three views: the bank details, the uncleared cheques, and the confirm
	set_view(view) {
		show_view_fields(
			this.dialog,
			{
				accounts: ["accounts_head", "accounts", "accounts_alerts", "has_uncleared_cheques"],
				cheques: ["cheques_head", "cheques", "cheques_notes", "where_section"],
				confirm: ["confirm"],
			},
			view
		);
		this.view = view;
		this.$back.toggleClass("hide", view !== "cheques");
		this.dialog.get_primary_btn().removeAttr("data-theme");
	}

	// what Save draft keeps in the view shown, to tell whether it changed since it was saved
	get_draft_key() {
		if (this.view === "cheques") return get_rows_key(this.cheques_grid, this.sync());
		if (this.view !== "accounts") return this.saved_key;
		return JSON.stringify([
			this.get_account_rows(),
			// the answer saved fills the question only once the window shows
			this.dialog.get_value("has_uncleared_cheques") || this.details.has_uncleared_cheques || null,
		]);
	}

	// one default bank: ticking one unticks the others
	set_default(row) {
		if (row?.is_default && !row.bank) {
			row.is_default = 0;
			this.accounts_grid.refresh();
			frappe.ui.toast({
				type: "error",
				message: __("Pick a bank for {0} first.", [short_account(this.frm, row.account)]),
			});
		} else if (row?.is_default) {
			for (const other of this.accounts) if (other !== row) other.is_default = 0;
			this.accounts_grid.refresh();
		}
		this.refresh_accounts();
	}

	show_accounts() {
		this.set_view("accounts");
		this.dialog.set_title(__("Bank accounts"));
		this.dialog.set_primary_action(__("Save bank details"), () => this.save_accounts());
		this.dialog.set_secondary_action_label(__("Save draft"));
		this.dialog.set_secondary_action(() => this.save_accounts(true));
		this.refresh_accounts();
		this.saved_key = this.get_draft_key();
	}

	refresh_accounts() {
		const rows = this.accounts;
		const no_bank_default = rows.find((row) => row.is_default && !row.bank);
		if (no_bank_default) {
			no_bank_default.is_default = 0;
			this.accounts_grid.refresh();
		}
		const $head = this.dialog.get_field("accounts_head").$wrapper.empty();
		$head.append(
			make_sheet_head(
				this.frm.doc.__onload?.company_details?.country === "India"
					? __("One row per bank account, overdraft and cash credit too.")
					: __("One row per bank account, overdraft and credit card too."),
				null,
				bank_menu(this.frm)
			)
		);
		const in_credit = rows.filter((row) => flt(row.book) >= 0);
		const overdrawn = rows.filter((row) => flt(row.book) < 0);
		const sum = (list) => list.reduce((total, row) => total + flt(row.book), 0);
		const typed = rows.filter((row) => ![null, undefined, ""].includes(row.statement_balance)).length;
		const on_day = this.day ? __("On {0}", [this.day]) : null;
		if (rows.length) {
			$head.append(
				$(`<div class="es-stat-card-grid company-setup-stats mb-3"></div>`).append(
					frappe.ui.stat_card({
						label: __("Bank accounts"),
						value: format_number(rows.length, null, 0),
						caption: __("{0} have bank details", [
							rows.filter((row) => row.bank && row.bank_account_no).length,
						]),
					}),
					make_lines_card(
						__("Book balance"),
						[
							[__("In credit · {0}", [in_credit.length]), money(this.frm, sum(in_credit), 0)],
							[
								__("Overdrawn or owed · {0}", [overdrawn.length]),
								money(this.frm, sum(overdrawn), 0),
							],
						],
						on_day
					),
					frappe.ui.stat_card({
						label: __("Statement balance"),
						value: __("{0} of {1} typed", [typed, rows.length]),
						caption: on_day,
					})
				)
			);
		}
		const new_banks = [...new Set(rows.map((row) => row.bank).filter((bank) => this.is_new_bank(bank)))];
		const alerts = [];
		if (new_banks.length && !this.details.can_create_bank) {
			alerts.push({
				theme: "red",
				title: __("You can't add a Bank, so {0} can't be made.", [new_banks.join(", ")]),
				description: __("Pick an existing Bank, or ask someone who can add one."),
			});
		}
		const defaults = rows.filter((row) => row.is_default).length;
		if (defaults > 1) alerts.push({ theme: "red", title: __("Pick one default bank.") });
		// the same number on two banks: details typed on the wrong row
		const numbers = {};
		for (const row of rows.filter((row) => row.bank_account_no)) {
			(numbers[[row.bank_account_no, row.branch_code || ""]] ||= []).push(row);
		}
		for (const same of Object.values(numbers).filter((list) => list.length > 1)) {
			alerts.push({
				theme: "red",
				title: __("Account number {0} is on {1}.", [
					same[0].bank_account_no,
					same.map((row) => short_account(this.frm, row.account)).join(", "),
				]),
				description: __("Each bank account has its own number."),
			});
		}
		// the statement is the book balance (or the trial balance's line) with the uncleared rows added back
		const uncleared = {};
		for (const cheque of this.frm.doc.uncleared_cheques || []) {
			uncleared[cheque.bank_account] =
				(uncleared[cheque.bank_account] || 0) +
				(cheque.entry_type === "Issued" ? 1 : -1) * flt(cheque.amount);
		}
		const off_banks = rows
			.map((row) => {
				const books = flt(row.book) || (row.line ?? null);
				const statement = get_statement(row);
				if (statement === null || books === null) return null;
				const off = statement - flt(books) - (uncleared[row.account] || 0);
				return Math.abs(off) < 1 ? null : { name: short_account(this.frm, row.account), off: off };
			})
			.filter(Boolean);
		// one box for all the banks off, said once
		if (off_banks.length) {
			const [first] = off_banks;
			const amount = money(this.frm, Math.abs(first.off), 0);
			const cheques_next =
				this.dialog.get_value("has_uncleared_cheques") !== "No" &&
				!(this.frm.doc.uncleared_cheques || []).length;
			alerts.push({
				theme: "yellow",
				title:
					off_banks.length > 1
						? __("{0} banks don't match their statement yet.", [off_banks.length])
						: first.off > 0
						? __("{0}: statement is {1} more than the books.", [first.name, amount])
						: __("{0}: statement is {1} less than the books.", [first.name, amount]),
				description: [
					cheque_words(
						cheques_next
							? __("Usually uncleared cheques; you add them next.")
							: __(
									"Usually a bank charge or interest not booked yet, or an uncleared cheque missing from the list. Check it, or go ahead."
							  )
					),
					off_banks.length > 1 &&
						off_banks
							.map((bank) =>
								__("{0} ({1})", [
									bank.name,
									bank.off > 0
										? __("{0} more", [money(this.frm, bank.off, 0)])
										: __("{0} less", [money(this.frm, -bank.off, 0)]),
								])
							)
							.join(", "),
				]
					.filter(Boolean)
					.join(" "),
			});
		}
		const no_bank = rows.filter((row) => !cstr(row.bank).trim());
		if (no_bank.length && no_bank.length < rows.length) {
			alerts.push({
				theme: "yellow",
				title:
					no_bank.length === 1
						? __("{0}: pick its bank to add it.", [short_account(this.frm, no_bank[0].account)])
						: __("{0}: pick their bank to add them.", [
								no_bank.map((row) => short_account(this.frm, row.account)).join(", "),
						  ]),
				description: __("A row with no bank is left out when you save."),
			});
		}
		if (rows.length && !defaults) {
			alerts.push({
				theme: "yellow",
				title: __("Tick the default bank."),
				description: cheque_words(
					__("Payments by cheque or transfer use it. This step is done once one is ticked.")
				),
			});
		}
		const unmarked = this.details.unmarked || [];
		if (unmarked.length) {
			alerts.push({
				theme: "yellow",
				title:
					unmarked.length === 1
						? __("1 account under a bank group is not marked as a bank account: {0}.", [
								short_account(this.frm, unmarked[0]),
						  ])
						: __("{0} accounts under a bank group are not marked as bank accounts: {1}.", [
								unmarked.length,
								unmarked.map((account) => short_account(this.frm, account)).join(", "),
						  ]),
				description: plural(
					unmarked.length,
					__("Mark it to add its bank details."),
					__("Mark them to add their bank details.")
				),
				buttons: [
					{
						label: plural(
							unmarked.length,
							__("Mark it as a bank account"),
							__("Mark them as bank accounts")
						),
						onclick: () => this.mark_accounts(unmarked),
					},
				],
			});
		}
		this.dialog
			.get_field("accounts_alerts")
			.$wrapper.empty()
			.append(make_alerts(alerts).addClass("mb-3"));
		this.dialog.set_df_property("accounts", "hidden", rows.length ? 0 : 1);
		set_primary_disabled(this.dialog, !rows.length || alerts.some((alert) => alert.theme === "red"));
	}

	get_account_rows() {
		return this.accounts.map((row) => ({
			account: row.account,
			bank: row.bank,
			bank_account_no: row.bank_account_no,
			branch_code: row.branch_code,
			statement_balance: get_statement(row),
			is_default: row.is_default ? 1 : 0,
			is_credit_card: row.is_credit_card ? 1 : 0,
		}));
	}

	save_accounts(draft = false) {
		const answer = this.dialog.get_value("has_uncleared_cheques");
		// get_values names the unanswered question
		if (!draft && !answer) return this.dialog.get_values();
		return call_method(
			this.frm,
			"save_bank_accounts",
			{ rows: this.get_account_rows(), has_uncleared_cheques: answer || null, draft: draft ? 1 : 0 },
			{ freeze: !draft }
		).then(() =>
			reload_fresh(this.frm).then(() => {
				if (draft) {
					this.refresh_accounts();
					this.saved_key = this.get_draft_key();
					frappe.ui.toast({ type: "success", message: __("Saved as a draft.") });
				} else if (answer === "Yes") {
					this.show_cheques();
				} else {
					this.dialog.hide();
					const off = this.frm.doc.__onload?.bank?.statement_off || [];
					const has_default = this.accounts.some((row) => row.is_default);
					frappe.ui.toast({
						type: "info",
						message: off.length
							? __("Bank details saved. {0}", [off[0]])
							: has_default
							? __("Bank details saved.")
							: __("Bank details saved. Tick a default bank to finish this step."),
						indicator: has_default && !off.length ? "green" : "orange",
					});
				}
			})
		);
	}

	mark_accounts(accounts) {
		call_method(this.frm, "save_bank_accounts", {
			rows: this.get_account_rows(),
			has_uncleared_cheques: this.dialog.get_value("has_uncleared_cheques") || null,
			draft: 1,
		})
			.then(() => call_method(this.frm, "mark_bank_accounts"))
			.then(() => {
				reload_fresh(this.frm).then(() => show_bank_accounts(this.frm, this.dialog));
				frappe.ui.toast({
					type: "success",
					message: plural(
						accounts.length,
						__("{0} is now a bank account.", [short_account(this.frm, accounts[0])]),
						__("{0} accounts are now bank accounts.", [accounts.length])
					),
				});
			});
	}

	// Part 2: the drafts of every bank, filtered to one by the picker
	show_cheques(account) {
		return get_data(this.frm, "get_uncleared_cheques").then((r) => {
			this.checks = r.message;
			this.cheques = this.get_drafts();
			const with_rows = this.checks.banks.find((bank) =>
				this.cheques.some((row) => row.bank_account === bank.account)
			);
			const added = this.checks.banks.filter((bank) => bank.bank);
			const default_bank = this.frm.doc.__onload?.bank?.default_bank;
			this.picked =
				account ||
				with_rows?.account ||
				added.find((bank) => bank.account === default_bank)?.account ||
				(added[0] || this.checks.banks[0])?.account;
			this.set_view("cheques");
			this.dialog.set_title(__("Bank accounts"));
			this.dialog.set_primary_action(__("Post to books"), () => this.post());
			this.dialog.set_secondary_action_label(__("Save draft"));
			this.dialog.set_secondary_action(() =>
				this.save_cheques()
					.then(() => reload_fresh(this.frm))
					.then(() => this.show_cheques(this.picked))
					.then(() => frappe.ui.toast({ type: "success", message: __("Saved as a draft.") }))
			);
			this.make_cheques_head();
			this.show_bank_rows();
			this.saved_key = this.get_draft_key();
		});
	}

	make_cheques_head() {
		const $head = this.dialog.get_field("cheques_head").$wrapper.empty();
		const menu = bank_menu(this.frm);
		$head.append(
			make_back_header(
				cheque_words(__("Uncleared cheques and deposits")),
				this.day
					? cheque_words(__("Cheques and deposits the bank had not cleared by {0}.", [this.day]))
					: null,
				() => this.show_accounts(),
				make_sheet_buttons(() => this.upload(), menu)
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
		this.$stats = $(`<div class="es-stat-card-grid company-setup-stats mb-3"></div>`).appendTo($head);
		this.$alerts = $(`<div class="mb-3"></div>`).appendTo($head);
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
							onclick: () => this.upload(),
						},
						{
							label: __("Add row"),
							onclick: () => {
								this.typing = true;
								this.refresh_cheques();
								this.cheques_grid.add_new_row();
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
							onclick: () => this.answer_no(),
						},
					],
				})
			)
			.appendTo($head);
	}

	// the grid's rows go back into the list of all drafts; rows added under a bank get that bank
	sync() {
		const shown = this.cheques_grid.df.data || [];
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
		this.cheques_grid.df.data = first
			? [...rows.filter(first), ...rows.filter((row) => !first(row))]
			: rows;
		this.cheques_grid.set_column_disp_in_list_view("bank_account", !this.picked);
		this.cheques_grid.refresh();
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
		const company_currency = this.currency;
		const foreign = new Set(
			banks.filter((bank) => bank.account_currency !== company_currency).map((bank) => bank.account)
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
		if (!this.checks) return;
		const state = this.get_state();
		const { rows, banks } = state;
		const name_of = (account) =>
			banks.find((bank) => bank.account === account)?.bank || short_account(this.frm, account);
		const picked = banks.find((bank) => bank.account === this.picked);
		const on = (label) => (picked ? __("{0} · {1}", [label, name_of(picked.account)]) : label);
		const sum = (field) => banks.reduce((total, bank) => total + flt(bank[field]), 0);
		const shown = picked ? [picked] : banks;
		const statement = picked?.statement;
		this.$stats.empty().append(
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
			})
		);

		const count = (test) => rows.filter(test).length;
		const show_rows = (test) => ({
			label: __("Show rows"),
			onclick: () => {
				const row = this.sync().find(test);
				if (row && this.picked && row.bank_account !== this.picked) this.pick(row.bank_account);
				this.show_bank_rows(test);
			},
		});
		const red = (title, test, description) => ({
			theme: "red",
			title,
			description,
			buttons: test ? [show_rows(test)] : [],
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

		const yellows = banks
			.filter((bank) => bank.off)
			.map((bank) => ({
				theme: "yellow",
				title: __("{0} is off by {1}. Check the statement balance or the rows.", [
					name_of(bank.account),
					money(this.frm, Math.abs(bank.off), 0),
				]),
				buttons: [{ label: __("Show rows"), onclick: () => this.pick(bank.account) }],
			}));
		// what only points something out is said for the bank shown
		const mine = this.picked ? rows.filter((row) => row.bank_account === this.picked) : rows;
		const count_mine = (test) => mine.filter(test).length;
		const yellow = (test, title, description) =>
			count_mine(test) &&
			yellows.push({ theme: "yellow", title, description, buttons: [show_rows(test)] });
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
		const empty = !this.typing && !this.sync().length;
		this.$empty.toggleClass("hide", !empty);
		this.$stats.toggleClass("hide", empty);
		this.dialog.set_df_property("cheques", "hidden", empty ? 1 : 0);
		// with no rows only what stops adding them is said
		this.$alerts
			.empty()
			.append(
				make_alerts(
					empty ? reds.slice(0, this.checks.balances_posted ? 0 : 1) : [...reds, ...yellows]
				)
			);
		set_primary_disabled(this.dialog, !rows.length || reds.length > 0);
	}

	// Yes was picked, but there are none to add: the question is answered No, so the step can finish
	answer_no() {
		this.dialog.set_value("has_uncleared_cheques", "No");
		return call_method(
			this.frm,
			"save_bank_accounts",
			{ rows: this.get_account_rows(), has_uncleared_cheques: "No" },
			{ freeze: true }
		).then(() =>
			reload_fresh(this.frm).then(() => {
				this.dialog.hide();
				frappe.ui.toast({
					type: "success",
					message: cheque_words(__("Bank details saved. No uncleared cheques.")),
				});
			})
		);
	}

	save_cheques() {
		return call_method(this.frm, "save_uncleared_cheques", {
			rows: this.sync().filter((row) => row.bank_account || flt(row.amount)),
		});
	}

	upload() {
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
			const $notes = this.dialog.get_field("cheques_notes").$wrapper.empty();
			const bank = banks.length === 1 && short_account(this.frm, banks[0]);
			$notes.append(
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
								__("Rows of these banks are left out, no bank account matches them: {0}.", [
									result.unknown_banks.join(", "),
								])
						  )
						: []
				)
			);
		});
	}

	post() {
		return this.save_cheques()
			.then(() => get_data(this.frm, "get_uncleared_cheques"))
			.then((r) => {
				this.checks = r.message;
				this.cheques = this.get_drafts();
				this.show_bank_rows();
				this.saved_key = this.get_draft_key();
				if (this.checks.blocker) {
					frappe.ui.toast({ type: "error", message: this.checks.blocker });
				} else if (this.cheques.length) {
					this.show_confirm();
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

	show_confirm() {
		const rows = this.cheques;
		const issued = rows.filter((row) => row.entry_type === "Issued");
		const deposited = rows.filter((row) => row.entry_type === "Deposited");
		const total = (list) => list.reduce((sum, row) => sum + flt(row.amount), 0);
		const counts = count_cheque_rows(this.frm, rows);
		this.set_view("confirm");
		fill_confirm(this.dialog, {
			title: cheque_words(__("Post uncleared cheques?")),
			back: cheque_words(__("Nothing is posted until you confirm.")),
			on_back: () => this.show_cheques(),
			body: [
				make_kv_lines([
					[
						__("To post"),
						__("{0} · {1} issued, {2} deposited", [
							counts,
							money(this.frm, total(issued)),
							money(this.frm, total(deposited)),
						]),
					],
					[
						rows.length && new Set(rows.map((row) => row.bank_account)).size === 1
							? __("Bank")
							: __("Banks"),
						[...new Set(rows.map((row) => row.bank_account))]
							.map((account) => short_account(this.frm, account))
							.join(", "),
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

// ---------- One window for a sheet: upload, checks, grid, confirm ----------

// A grid in a dialog. Frappe's Delete all rows clears this.frm.doc, which it has not, so it clears its
// own rows. Frappe tells nothing of deleted rows outside a form, so a refresh with fewer rows than the
// last one runs on_delete.
// An overdraft or cash credit: what the bank is owed, or an account its books show overdrawn
function is_overdrawn(row) {
	return row.root_type === "Liability" || (flt(row.book) || flt(row.line)) < 0;
}

// The statement balance as the books count it: the bank shows what an overdrawn account owes as a
// plain number, so it is taken as overdrawn either way it is typed
function get_statement(row) {
	if ([null, undefined, ""].includes(row.statement_balance)) return null;
	const amount = flt(row.statement_balance);
	return is_overdrawn(row) ? -Math.abs(amount) : amount;
}
