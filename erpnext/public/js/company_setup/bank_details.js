import {
	call_method,
	cheque_words,
	get_data,
	make_alerts,
	make_lines_card,
	make_sheet_head,
	money,
	name_with_badge,
	new_badge,
	plural,
	reload_fresh,
	set_primary_disabled,
	short_account,
} from "./common";
import { setup_dialog_grid } from "./sheet_window";
import { bank_menu } from "./bank";

// What the bank accounts window reads: each bank ledger with its details, and the banks there are
export function get_bank_details(frm) {
	return Promise.all([
		get_data(frm, "get_bank_accounts"),
		frappe.db.get_list("Bank", { fields: ["name"], limit: 0 }),
	]).then(([r, banks]) => [r.message, banks.map((bank) => bank.name)]);
}

// The fields of the bank details view, in the bank accounts window
export function get_bank_details_fields(win, details) {
	const country = win.frm.doc.__onload?.company_details?.country;
	const day = details.day_before ? frappe.datetime.str_to_user(details.day_before) : null;
	const refresh = () => win.details_view.refresh();
	return [
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
			data: [],
			fields: [
				{
					fieldname: "account",
					fieldtype: "Link",
					options: "Account",
					label: __("Account"),
					in_list_view: 1,
					read_only: 1,
					columns: 2,
					formatter: (value) =>
						value
							? `<span class="block truncate" title="${frappe.utils.escape_html(
									value
							  )}">${frappe.utils.escape_html(short_account(win.frm, value))}</span>`
							: "",
				},
				{
					fieldname: "bank",
					fieldtype: "Link",
					options: "Bank",
					label: __("Bank"),
					in_list_view: 1,
					columns: 1,
					ignore_link_validation: 1,
					formatter: (value) =>
						value
							? name_with_badge(
									frappe.utils.escape_html(value),
									win.details_view.is_new_bank(value) && new_badge()
							  )
							: "",
					onchange: refresh,
				},
				{
					fieldname: "bank_account_no",
					fieldtype: "Data",
					label: __("Account number"),
					in_list_view: 1,
					columns: 1,
					onchange: refresh,
				},
				{
					fieldname: "branch_code",
					fieldtype: "Data",
					label:
						country === "India"
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
					columns: 4,
					onchange: refresh,
					formatter: (value, df, options, row) =>
						[null, undefined, ""].includes(value)
							? ""
							: `<span class="flex items-center justify-end gap-2">${
									is_overdrawn(row)
										? frappe.ui.badge.html({ label: __("Overdrawn"), theme: "gray" })
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
						win.details_view.set_default(this.doc);
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
			label: day
				? cheque_words(__("Any cheques or deposits not cleared by the bank on {0}?", [day]))
				: cheque_words(
						__("Any cheques or deposits not cleared by the bank on the day before migration?")
				  ),
			options: [
				{ value: "", label: "" },
				{ value: "No", label: __("No") },
				{ value: "Yes", label: __("Yes") },
			],
			default: details.has_uncleared_cheques || "",
			reqd: 1,
			change: refresh,
		},
	];
}

// The bank details of each bank ledger: a view of the bank accounts window
export class BankDetailsView {
	constructor(win) {
		this.win = win;
		this.dialog = win.dialog;
		// a No or Yes needs no full-width box
		this.dialog
			.get_field("has_uncleared_cheques")
			.$wrapper.find(".control-input-wrapper")
			.addClass("max-w-xs");
		this.grid = this.dialog.fields_dict.accounts.grid;
		setup_dialog_grid(this.grid);
	}

	get frm() {
		return this.win.frm;
	}

	load(details, bank_names) {
		this.win.details = details;
		this.details = details;
		this.known_banks = new Set(bank_names.map((name) => name.trim().toLowerCase()));
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
		this.grid.df.data = this.accounts;
		this.grid.refresh();
	}

	// the details read again and drawn in this window, as after marking bank accounts
	reload() {
		return reload_fresh(this.frm)
			.then(() => get_bank_details(this.frm))
			.then(([details, bank_names]) =>
				this.win.keep_scroll(() => {
					this.load(details, bank_names);
					this.show();
				})
			);
	}

	is_new_bank(bank) {
		return Boolean(bank) && !this.known_banks.has(bank.trim().toLowerCase());
	}

	show() {
		const { dialog } = this;
		this.win.set_view("accounts");
		dialog.set_title(__("Bank accounts"));
		dialog.set_primary_action(__("Save bank details"), () => this.save());
		dialog.set_secondary_action_label(__("Save draft"));
		dialog.set_secondary_action(() => this.save(true));
		this.refresh();
		this.win.saved_key = this.get_key();
	}

	// what Save draft keeps, to tell whether it changed since it was saved
	get_key() {
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
			this.grid.refresh();
			frappe.ui.toast({
				type: "error",
				message: __("Pick a bank for {0} first.", [short_account(this.frm, row.account)]),
			});
		} else if (row?.is_default) {
			for (const other of this.accounts) if (other !== row) other.is_default = 0;
			this.grid.refresh();
		}
		this.refresh();
	}

	refresh() {
		const rows = this.accounts;
		const no_bank_default = rows.find((row) => row.is_default && !row.bank);
		if (no_bank_default) {
			no_bank_default.is_default = 0;
			this.grid.refresh();
		}
		this.make_head(rows);
		const alerts = this.get_alerts(rows);
		this.dialog
			.get_field("accounts_alerts")
			.$wrapper.empty()
			.append(make_alerts(alerts).addClass("mb-3"));
		this.dialog.set_df_property("accounts", "hidden", rows.length ? 0 : 1);
		set_primary_disabled(this.dialog, !rows.length || alerts.some((alert) => alert.theme === "red"));
	}

	make_head(rows) {
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
		if (!rows.length) return;
		const in_credit = rows.filter((row) => flt(row.book) >= 0);
		const overdrawn = rows.filter((row) => flt(row.book) < 0);
		const sum = (list) => list.reduce((total, row) => total + flt(row.book), 0);
		const typed = rows.filter((row) => ![null, undefined, ""].includes(row.statement_balance)).length;
		const on_day = this.day ? __("On {0}", [this.day]) : null;
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

	get_alerts(rows) {
		const alerts = [];
		const new_banks = [...new Set(rows.map((row) => row.bank).filter((bank) => this.is_new_bank(bank)))];
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
		const off = this.get_statement_alert(rows);
		if (off) alerts.push(off);
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
		return alerts;
	}

	// the statement is the book balance (or the trial balance's line) with the uncleared rows added
	// back; one box for all the banks off, said once
	get_statement_alert(rows) {
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
		if (!off_banks.length) return null;
		const [first] = off_banks;
		const amount = money(this.frm, Math.abs(first.off), 0);
		const cheques_next =
			this.dialog.get_value("has_uncleared_cheques") !== "No" &&
			!(this.frm.doc.uncleared_cheques || []).length;
		return {
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
		};
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

	save(draft = false) {
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
					this.refresh();
					this.win.saved_key = this.get_key();
					frappe.ui.toast({ type: "success", message: __("Saved as a draft.") });
				} else if (answer === "Yes") {
					this.win.show_cheques();
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
				this.reload();
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
}

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
