import {
	call_method,
	download_sheet,
	get_data,
	get_rows_key,
	grey_line,
	make_alerts,
	make_button,
	make_posting_reds,
	make_sheet_buttons,
	name_with_badge,
	plural,
	reload_fresh,
	says_check,
} from "./common";
import { get_source } from "./where_to_find";
import { SheetWindow } from "./sheet_window";
import { PARTY, make_posting_bar } from "./overview";
import { invoice_menu } from "./invoices_posted";
import {
	count_invoices,
	make_hand_posted_test,
	make_opening_invoice_stats,
	post_opening_invoices,
} from "./invoice_posting";
import { InvoiceNamesView, OPEN_KINDS, get_name_badge, get_name_kind, get_name_state } from "./invoice_names";
import { InvoiceLedgers } from "./invoice_ledgers";
import { import_blocked, open_party_import } from "./imports";

export function show_opening_invoices(frm, party_type) {
	get_data(frm, "get_invoice_names", { party_type: party_type }).then((r) =>
		new OpeningInvoicesWindow(frm, party_type, r.message).show()
	);
}

class OpeningInvoicesWindow extends SheetWindow {
	constructor(frm, party_type, names) {
		const is_customer = party_type === "Customer";
		const table = PARTY[party_type].table;
		super(frm, {
			title: is_customer ? __("Opening sales invoices") : __("Unpaid supplier bills"),
			where: table,
			has_data: (frm.doc[table] || []).some(
				(row) => row.status !== "Created" && (row.party_name || row.party)
			),
			posted: (frm.doc[table] || []).some((row) => row.status === "Created"),
			table: { label: is_customer ? __("Unpaid invoices") : __("Unpaid bills") },
			save_method: "save_opening_invoices",
			get_method: "get_invoice_names",
			args: { party_type: party_type },
			checks: names,
			extra_fields: [{ fieldname: "names_view", fieldtype: "HTML", hidden: 1 }],
		});
		this.views.names = ["names_view"];
		this.currency = erpnext.get_currency(frm.doc.company);
		this.ledgers = new InvoiceLedgers(this);
		this.load(names);
		this.names_view = new InvoiceNamesView(this);
	}

	get party_type() {
		return this.args.party_type;
	}

	get is_customer() {
		return this.party_type === "Customer";
	}

	get party() {
		return PARTY[this.party_type];
	}

	get_layout(names) {
		return String(Boolean(names.accounts?.length));
	}

	get_fields(names) {
		const win = this;
		const { is_customer, party_type } = this;
		const accounts = names.accounts || [];
		const date_formatter = (value, df, options, row) =>
			!value && row?.unread_dates?.[df.fieldname]
				? `<span title="${frappe.utils.escape_html(
						__("On the sheet: {0}", [row.unread_dates[df.fieldname]])
				  )}">${frappe.ui.badge.html({ label: __("Check date"), theme: "orange" })}</span>`
				: value
				? frappe.datetime.str_to_user(value)
				: "";
		return [
			{
				fieldname: "party_name",
				fieldtype: "Data",
				label: __("From sheet"),
				in_list_view: 1,
				columns: accounts.length ? 2 : 3,
				formatter: (value, df, options, doc) => {
					const kind = doc && value ? this.kind_of(doc) : null;
					const badge =
						doc?.status === "Failed" && kind === "found"
							? frappe.ui.badge.html({ label: __("Failed"), theme: "red" })
							: get_name_badge(kind, party_type, this.other_currency[doc?.party]);
					return name_with_badge(frappe.form.formatters.Data(value, df), badge);
				},
			},
			{
				fieldname: "party",
				fieldtype: "Link",
				options: party_type,
				label: __(party_type),
				in_list_view: 1,
				columns: 2,
				get_query: () => ({ filters: { disabled: 0 } }),
				onchange: function () {
					const row = this.doc;
					if (row && row.party && !row.party_name) row.party_name = row.party;
					if (row) {
						row.account = (win.in_sheet(row) && win.sheet_account) || win.own_accounts[row.party];
						row.account_by_hand = 0;
						row.create_party = 0;
					}
					this.grid_row?.refresh_field("party_name");
					win.fill_accounts();
				},
			},
			{
				fieldname: "account",
				fieldtype: "Link",
				options: "Account",
				label: __("Account"),
				in_list_view: 1,
				columns: 2,
				hidden: !accounts.length,
				only_select: 1,
				get_query: () => ({ filters: { name: ["in", this.accounts] } }),
				filter_description: is_customer ? __("Receivable ledgers") : __("Payable ledgers"),
				onchange: function () {
					win.set_party_account(this.doc);
				},
			},
			{
				fieldname: "invoice_number",
				fieldtype: "Data",
				label: is_customer ? __("Invoice No") : __("Bill No"),
				in_list_view: 1,
				columns: 1,
			},
			{
				fieldname: "invoice_date",
				fieldtype: "Date",
				label: is_customer ? __("Invoice Date") : __("Bill Date"),
				in_list_view: 1,
				columns: 1,
				formatter: date_formatter,
			},
			{
				fieldname: "due_date",
				fieldtype: "Date",
				label: __("Due Date"),
				in_list_view: 1,
				columns: 1,
				formatter: date_formatter,
			},
			{
				fieldname: "outstanding_amount",
				fieldtype: "Currency",
				label: __("Outstanding"),
				in_list_view: 1,
				columns: accounts.length ? 1 : 2,
				reqd: 1,
			},
		];
	}

	load(names) {
		const { frm, result } = this;
		this.names = names;
		this.disabled = new Set(names.disabled || []);
		// disabled customers or suppliers the user chose to post anyway
		this.post_disabled = new Set();
		this.other_currency = names.other_currency || {};
		// a row an invoice made by hand stands for has its own alert, not the names' ones
		this.is_hand_posted = make_hand_posted_test(names);
		// several receivable or payable accounts: each invoice shows the one it goes to, and can change
		this.accounts = names.accounts || [];
		this.party_accounts = { ...names.party_accounts };
		// a ledger saved on the customer or supplier, or on its group, for a sheet with no "Post to"
		this.own_accounts = names.own_accounts || {};
		// dates the sheet had that could not be read: what it said, by row and field, until typed in
		const unread = {};
		for (const bad of result?.bad_dates || []) {
			const key = [bad.party_name, bad.invoice_number || ""];
			unread[key] = { ...unread[key], [bad.field]: bad.text };
		}
		this.drafts = (frm.doc[this.party.table] || [])
			.filter((row) => row.status !== "Created" && (row.party_name || row.party))
			.map((row) => {
				// linked to the customer or supplier their name means today
				const party = row.party || names.found?.[row.party_name];
				return {
					...row,
					party: party,
					name: undefined,
					account: this.accounts.length ? row.account || this.party_accounts[party] : undefined,
					unread_dates: unread[[row.party_name, row.invoice_number || ""]],
				};
			});
		this.created = (frm.doc[this.party.table] || []).filter((row) => row.status === "Created").length;
		this.only_unread = false;
		// names confirmed in this window, shown as Confirmed in the names list
		this.confirmed = new Set();
		// nothing entered yet: the table's place says how to start
		this.typing = this.drafts.length > 0;
		// a posting in the background: the window shows it and changes wait until it's done
		this.posting = frm.posting?.[this.party_type];
		// the ledger picked for the whole sheet: every row goes there, names confirmed later too
		this.sheet_account = this.ledgers.get_sheet_account(this.drafts, result);
	}

	kind_of(row) {
		return this.is_hand_posted(row) ? "hand_posted" : get_name_kind(row, this.names, this.disabled);
	}

	// the ledger a row goes to: its own, its customer's or supplier's, else the company's default
	account_of(row) {
		return row.account || this.party_accounts[row.party] || this.names.default_account;
	}

	// after an upload, Post to is for that sheet's rows and rows typed in, not another sheet's
	in_sheet(row) {
		const source = this.result?.source;
		return !source || !row.source || row.source === source;
	}

	has_unread_date(row) {
		return Object.keys(row.unread_dates || {}).some((field) => !row[field]);
	}

	is_shown(row) {
		return !this.only_unread || this.has_unread_date(row);
	}

	set_rows(rows) {
		super.set_rows(rows);
		this.saved_key = get_rows_key(this.grid);
	}

	is_dirty() {
		return !this.posting && get_rows_key(this.grid) !== this.saved_key;
	}

	save_before_close() {
		return this.dialog.get_values() ? this.save() : Promise.reject();
	}

	changed() {
		this.refresh_state();
	}

	render() {
		const { frm } = this;
		if (this.posting) {
			frm.posting_dialog = Object.assign(this.dialog, { party_type: this.party_type });
			this.dialog.set_df_property("rows", "read_only", 1);
		}
		this.dialog.set_primary_action(__("Post to books"), () => this.post());
		this.dialog.get_primary_btn().toggle(!this.posting);
		this.dialog.set_secondary_action_label(__("Save draft"));
		this.dialog.set_secondary_action(() => this.save_draft());
		this.make_head();
		this.make_notes();
		this.refresh_state();
		this.grid.refresh();
	}

	// the posting reds, one line and the sheet's buttons, and the Post to picker
	make_head() {
		const { frm, is_customer, party_type } = this;
		const $head = this.dialog.get_field("head").$wrapper.empty();
		$head.append(
			make_posting_reds(frm, frm.doc.__onload?.posting_reds?.[party_type], () => this.refresh())
		);
		this.$sheet_buttons = make_sheet_buttons(() => this.upload_invoices(), this.get_menu());
		$(`<div class="flex items-center justify-between gap-3 mb-3"></div>`)
			.append(
				$(`<div class="text-p-sm text-ink-gray-6"></div>`).text(
					is_customer
						? __(
								"Invoices still unpaid on {0}, one row each. Enter a credit or advance as a negative amount.",
								[this.day]
						  )
						: says_check
						? __(
								"Bills still unpaid on {0}, one row each. Enter an advance or vendor credit as a negative amount.",
								[this.day]
						  )
						: __(
								"Bills still unpaid on {0}, one row each. Enter an advance or debit note as a negative amount.",
								[this.day]
						  )
				),
				this.$sheet_buttons
			)
			.appendTo($head);
		this.$post_to = $(`<div class="max-w-sm"></div>`).appendTo($head);
		this.ledgers.make_post_to(this.$post_to);
	}

	// each sheet's draft rows can go at once; saving keeps the change, closing without it undoes it
	get_menu() {
		const { frm, is_customer, party_type, names } = this;
		const draft_sources = {};
		for (const row of frm.doc[this.party.table] || []) {
			if (row.status !== "Created") {
				draft_sources[row.source || ""] = (draft_sources[row.source || ""] || 0) + 1;
			}
		}
		return [
			...Object.entries(draft_sources).map(([source, count]) => ({
				label:
					count === 1
						? __("Remove {0} · 1 row", [source || __("rows typed in")])
						: __("Remove {0} · {1} rows", [source || __("rows typed in"), count]),
				icon: "trash-2",
				onclick: () => this.remove_rows((row) => (row.source || "") === source, count),
			})),
			...invoice_menu(frm, party_type).filter((item) => item.icon === "download"),
			names.new?.length &&
				names.can_create && {
					label: is_customer ? __("Download new customers") : __("Download new suppliers"),
					icon: "download",
					onclick: () => this.download_new_names(),
				},
		].filter(Boolean);
	}

	// what was read and left out, under the table
	make_notes() {
		const { is_customer, result, created } = this;
		const $notes = $(`<div class="flex flex-col gap-1 mt-2"></div>`);
		this.dialog.get_field("notes").$wrapper.empty().append($notes);
		const grey = (text) => grey_line(text).appendTo($notes);
		// said from the rows as they are now, so it follows rows removed or typed in
		this.$advances = grey("");
		if (result?.unused_columns?.length) {
			grey(
				is_customer
					? __(
							"Not read: {0}. Only the name, invoice number, dates, amount and account are used.",
							[result.unused_columns.join(", ")]
					  )
					: __("Not read: {0}. Only the name, bill number, dates, amount and account are used.", [
							result.unused_columns.join(", "),
					  ])
			);
		}
		if (result?.unknown_ledgers?.length) {
			grey(
				is_customer
					? __("Not an account of this company: {0}. Those rows go to their customer's account.", [
							result.unknown_ledgers.join(", "),
					  ])
					: __("Not an account of this company: {0}. Those rows go to their supplier's account.", [
							result.unknown_ledgers.join(", "),
					  ])
			);
		}
		if (created && !result?.skipped) {
			grey(
				is_customer
					? plural(
							created,
							__("1 invoice is posted already and stays as it is."),
							__("{0} invoices are posted already and stay as they are.", [created])
					  )
					: plural(
							created,
							__("1 bill is posted already and stays as it is."),
							__("{0} bills are posted already and stay as they are.", [created])
					  )
			);
		}
	}

	show_advances(count) {
		const { is_customer } = this;
		this.$advances
			.toggle(!!count)
			.text(
				is_customer
					? count === 1
						? __(
								"1 negative row is a credit: an advance, payment or credit note not yet set against an invoice."
						  )
						: __(
								"{0} negative rows are credits: advances, payments or credit notes not yet set against an invoice.",
								[count]
						  )
					: count === 1
					? __(
							"1 negative row is a debit: an advance, payment or debit note not yet set against a bill."
					  )
					: __(
							"{0} negative rows are debits: advances, payments or debit notes not yet set against a bill.",
							[count]
					  )
			);
	}

	// the cards, the messages and the buttons, from the rows as they are now
	refresh_state() {
		const { frm } = this;
		const all = this.get_rows();
		const empty = !all.length && !this.typing;
		const state = get_name_state(all, (row) => this.kind_of(row));
		const $review = this.dialog.get_field("alerts").$wrapper.empty();
		this.$sheet_buttons.toggle(!empty);
		this.$post_to.toggle(!empty);
		this.grid.wrapper.closest(".frappe-control").toggle(!empty);
		const overs = this.ledgers.get_overs(all);
		// posted rows on an over ledger: the post confirm may offer to move them
		const can_move = (frm.doc[this.party.table] || []).some(
			(row) => row.status === "Created" && overs.some((over) => over.account === row.account)
		);
		this.dialog.get_primary_btn().prop("disabled", empty || (overs.length && !can_move));
		this.ledgers.update_bulk();
		this.dialog.get_secondary_btn().toggle(!empty);
		// shown with the table, rows or not, so the table doesn't move down on the first pick
		this.set_stats(empty ? [] : make_opening_invoice_stats(frm, this.party_type, state, this.currency));
		$review.append(this.make_skipped_box());
		this.show_advances(all.filter((row) => flt(row.outstanding_amount) < 0).length);
		if (empty) return $review.append(this.make_empty());
		if (this.posting) return $review.append(this.make_posting());
		$review.append(
			this.make_failed_alert(all),
			this.make_waiting_alerts(all),
			this.make_unread_dates_alert(all),
			this.ledgers.make_over_alerts(overs),
			state.review.length ? this.make_review_box(state) : []
		);
	}

	// rows of the sheet that were posted before: easy to miss as a grey line, so said on top
	make_skipped_box() {
		const { is_customer, result } = this;
		const skipped = result?.skipped;
		if (!skipped) return [];
		const total = skipped + (result.added || 0);
		return frappe.ui
			.alert({
				theme: "yellow",
				title: !result.added
					? __("Nothing new on this sheet")
					: is_customer
					? __("{0} of {1} invoices were posted already", [skipped, total])
					: __("{0} of {1} bills were posted already", [skipped, total]),
				description: !result.added
					? is_customer
						? plural(
								skipped,
								__("The invoice on it is posted already, so none was added."),
								__("All {0} invoices on it are posted already, so none were added.", [
									skipped,
								])
						  )
						: plural(
								skipped,
								__("The bill on it is posted already, so none was added."),
								__("All {0} bills on it are posted already, so none were added.", [skipped])
						  )
					: __("They were left out; the other {0} are below.", [result.added]),
			})
			.addClass("mb-3");
	}

	// Nothing entered yet: the table's place says how to start
	make_empty() {
		const { frm, is_customer, party_type } = this;
		const source = get_source(frm, this.party.table);
		return $(`<div class="border rounded-lg py-10"></div>`).append(
			frappe.ui.empty_state({
				icon: "file-spreadsheet",
				title: is_customer ? __("Upload your receivables report") : __("Upload your payables report"),
				description: [
					!source
						? __("Excel or CSV from Tally, Busy, Zoho or QuickBooks.")
						: source === "Excel / other"
						? __("Excel or CSV.")
						: __("Excel or CSV from {0}.", [__(source)]),
					is_customer
						? __("Each unpaid invoice is read with its customer, number, date and amount.")
						: __("Each unpaid bill is read with its supplier, number, date and amount."),
				].join(" "),
				actions: [
					{
						label: __("Upload sheet"),
						icon: "upload",
						variant: "solid",
						onclick: () => this.upload_invoices(),
					},
					{
						label: __("Add row"),
						onclick: () => {
							this.typing = true;
							this.refresh_state();
							this.grid.add_new_row();
						},
					},
					{
						label: __("Download sample sheet"),
						variant: "ghost",
						onclick: () =>
							download_sheet(frm.doc.company, "opening_invoices.download_opening_invoices", {
								party_type: party_type,
							}),
					},
				],
			})
		);
	}

	// a posting in the background: changes wait until it's done
	make_posting() {
		const { frm, is_customer, party_type, posting } = this;
		this.$sheet_buttons.find("button").prop("disabled", true);
		this.dialog.get_primary_btn().hide();
		this.dialog.set_secondary_action_label(__("Close"));
		this.dialog.set_secondary_action(() => this.dialog.hide());
		return frappe.ui
			.alert({
				theme: "blue",
				icon: "loader-circle",
				title: is_customer
					? __("Posting invoices in the background")
					: __("Posting bills in the background"),
				description: __(
					"You can close this window; the page updates when they are done. Changes wait until then."
				),
				footer: () =>
					make_posting_bar(frm, party_type, {
						label: posting.total
							? __("{0} of {1} posted", [posting.done, posting.total])
							: __("Starting"),
						hint: true,
					}).addClass("w-full"),
			})
			.addClass("mb-3");
	}

	// rows posting turned down for a reason of their own, not their name
	make_failed_alert(all) {
		const failed = all.filter((row) => row.status === "Failed" && this.kind_of(row) === "found");
		if (!failed.length) return [];
		return frappe.ui
			.alert({
				theme: "red",
				title: __("{0} could not be posted", [count_invoices(failed.length, this.party_type)]),
				description: __(
					"Each shows Failed in the table; the reason is in the row. Fix them and post again, or remove them."
				),
				footer: () =>
					$(`<div class="flex gap-2"></div>`).append(
						frappe.ui.button({
							label: __("Remove these rows"),
							onclick: () => {
								this.grid.df.data = all.filter((row) => !failed.includes(row));
								this.grid.refresh();
								return this.save().then(() => {
									this.saved_key = get_rows_key(this.grid);
									this.refresh_state();
								});
							},
						})
					),
			})
			.addClass("mb-3");
	}

	// rows whose sheet dates could not be read: one box for all of them, and a way to see just those
	make_unread_dates_alert(all) {
		const { is_customer } = this;
		const rows = all.filter((row) => this.has_unread_date(row));
		if (!rows.length) this.only_unread = false;
		this.show_only_unread();
		if (!rows.length) return [];
		const kind = is_customer
			? plural(rows.length, __("1 invoice"), __("{0} invoices", [rows.length]))
			: plural(rows.length, __("1 bill"), __("{0} bills", [rows.length]));
		return frappe.ui
			.alert({
				theme: "yellow",
				title: __("{0} with dates we couldn't read", [kind]),
				description: !this.only_unread
					? __("They're left empty. Type them in, or they're dated {0}.", [this.day])
					: rows.length === 1
					? __("Showing only this one of {0}. Type its dates in, or it's dated {1}.", [
							all.length,
							this.day,
					  ])
					: __("Showing only these {0} of {1}. Type the dates in, or they're dated {2}.", [
							rows.length,
							all.length,
							this.day,
					  ]),
				footer: () =>
					frappe.ui.button({
						label: this.only_unread ? __("Show all") : __("Show rows"),
						onclick: () => {
							this.only_unread = !this.only_unread;
							if (this.only_unread) {
								// first, so they are on the table's first page
								this.grid.df.data = [...rows, ...all.filter((row) => !rows.includes(row))];
								this.grid.refresh();
							}
							this.refresh_state();
						},
					}),
			})
			.addClass("mb-3");
	}

	show_only_unread() {
		for (const grid_row of this.grid.grid_rows || []) {
			grid_row?.wrapper.toggle(this.is_shown(grid_row.doc));
		}
	}

	// rows that stay drafts whatever the names say: a disabled customer's, or in another currency
	make_waiting_alerts(all) {
		const { is_customer } = this;
		const alerts = [];
		const hand = all.filter(this.is_hand_posted);
		if (hand.length) alerts.push(this.make_hand_posted_alert(hand));
		const by_currency = {};
		for (const row of all.filter((row) => this.kind_of(row) === "other_currency")) {
			(by_currency[this.other_currency[row.party]] ||= []).push(row);
		}
		for (const [code, list] of Object.entries(by_currency)) {
			alerts.push({
				theme: "yellow",
				title: is_customer
					? list.length === 1
						? __("1 invoice is in {0}.", [code])
						: __("{0} invoices are in {1}.", [list.length, code])
					: list.length === 1
					? __("1 bill is in {0}.", [code])
					: __("{0} bills are in {1}.", [list.length, code]),
				description: is_customer
					? __(
							"Foreign-currency invoices can't be posted yet; leave them out or post them by hand. They stay as drafts."
					  )
					: __(
							"Foreign-currency bills can't be posted yet; leave them out or post them by hand. They stay as drafts."
					  ),
			});
		}
		const off = [
			...new Set(all.filter((row) => this.kind_of(row) === "disabled").map((row) => row.party)),
		];
		if (off.length) {
			alerts.push({
				theme: "yellow",
				title: is_customer
					? __("Disabled customers: {0}", [off.join(", ")])
					: __("Disabled suppliers: {0}", [off.join(", ")]),
				description: is_customer
					? __(
							"Their invoices stay as drafts. Post them anyway, or pick another customer in Review names."
					  )
					: __(
							"Their bills stay as drafts. Post them anyway, or pick another supplier in Review names."
					  ),
				buttons: [
					{
						label: __("Post them anyway"),
						onclick: () => {
							// enabled only while posting, disabled again after
							off.forEach((name) => {
								this.disabled.delete(name);
								this.post_disabled.add(name);
							});
							this.grid.refresh();
							this.refresh_state();
						},
					},
				],
			});
		}
		return alerts.length ? make_alerts(alerts).addClass("mb-3") : [];
	}

	make_hand_posted_alert(hand) {
		const { frm, is_customer, party_type } = this;
		return {
			theme: "yellow",
			title: is_customer
				? plural(
						hand.length,
						__("1 draft row looks already posted by hand"),
						__("{0} draft rows look already posted by hand", [hand.length])
				  )
				: plural(
						hand.length,
						__("1 draft bill looks already posted by hand"),
						__("{0} draft bills look already posted by hand", [hand.length])
				  ),
			description: [
				hand
					.map((row) =>
						[
							row.party,
							row.invoice_number,
							format_currency(row.outstanding_amount, this.currency),
						]
							.filter(Boolean)
							.join(" · ")
					)
					.join("; "),
				__(
					"An opening invoice made outside Company Setup has the same party and number or amount. They are not posted again or counted twice."
				),
			].join(". "),
			buttons: [
				{
					label: __("Remove them"),
					onclick: () =>
						frappe.confirm(
							hand.length === 1
								? __("Remove this draft row? The invoice made by hand stays as it is.")
								: __(
										"Remove these {0} draft rows? The invoices made by hand stay as they are.",
										[hand.length]
								  ),
							() =>
								this.save()
									.then(() =>
										call_method(frm, "remove_hand_posted", {
											party_type: party_type,
											rows: hand.map((row) => ({
												party: row.party,
												invoice_number: row.invoice_number,
												amount: row.outstanding_amount,
											})),
										})
									)
									.then(() => this.refresh())
						),
				},
			],
		};
	}

	// The names that need a look, counted by kind, and the way to them
	make_review_box(state) {
		const { frm, is_customer, party_type } = this;
		const count = (kind) => state.review.filter((name) => name.kind === kind).length;
		const parties = (n) =>
			is_customer
				? plural(n, __("1 customer"), __("{0} customers", [n]))
				: plural(n, __("1 supplier"), __("{0} suppliers", [n]));
		const currencies = [
			...new Set(
				state.review
					.filter((name) => name.kind === "other_currency")
					.map((name) => this.other_currency[name.party])
			),
		];
		const parts = [
			["check", __("{0} guessed", [count("check")])],
			["new", __("{0} to create", [count("new")])],
			["disabled", parties(count("disabled"))],
			["other_currency", parties(count("other_currency"))],
		].filter(([kind]) => count(kind));
		return $(
			`<div class="flex flex-wrap items-center justify-between gap-3 mb-3 border rounded-lg p-3"></div>`
		).append(
			$(`<div class="flex flex-col gap-2"></div>`).append(
				$(`<div class="text-base-medium text-ink-gray-8"></div>`).text(
					is_customer
						? plural(
								state.review.length,
								__("1 customer name needs a look before posting"),
								__("{0} customer names need a look before posting", [state.review.length])
						  )
						: plural(
								state.review.length,
								__("1 supplier name needs a look before posting"),
								__("{0} supplier names need a look before posting", [state.review.length])
						  )
				),
				$(`<div class="flex flex-wrap items-center gap-4"></div>`).append(
					parts.map(([kind, text]) =>
						$(`<span class="flex items-center gap-1.5"></span>`).append(
							get_name_badge(kind, party_type, currencies.length === 1 ? currencies[0] : null),
							$(`<span class="text-p-sm text-ink-gray-7"></span>`).text(text)
						)
					)
				),
				count("new")
					? $(`<div class="text-p-sm text-ink-gray-6"></div>`).text(
							is_customer
								? __(
										"Names not in ERPNext are added as spelt on the sheet. Import your customer list first to bring their addresses and tax IDs."
								  )
								: __(
										"Names not in ERPNext are added as spelt on the sheet. Import your supplier list first to bring their addresses and tax IDs."
								  )
					  )
					: []
			),
			$(`<div class="flex flex-wrap gap-2"></div>`).append(
				count("new") && frappe.model.can_create("Data Import")
					? make_button({
							label: is_customer ? __("Import customers") : __("Import suppliers"),
							icon: "import",
							variant: "subtle",
							disabled: !!import_blocked(frm, party_type),
							tooltip: import_blocked(frm, party_type),
							onclick: () => {
								if (!this.dialog.get_values()) return;
								this.save().then(() => {
									this.dialog.hide();
									open_party_import(frm, party_type, () =>
										show_opening_invoices(frm, party_type)
									);
								});
							},
					  })
					: [],
				frappe.ui.button({
					label: __("Review names"),
					icon: "user-check",
					onclick: () => this.names_view.show(),
				})
			)
		);
	}

	remove_rows(gone, count) {
		this.grid.df.data = this.get_rows().filter((row) => !gone(row));
		this.grid.wrapper.find(".grid-heading-row .grid-row-check").prop("checked", false);
		this.grid.refresh();
		this.refresh_state();
		frappe.ui.toast({
			type: "info",
			message:
				plural(count, __("1 row removed."), __("{0} rows removed.", [count])) +
				" " +
				__("Save draft to keep this, or close without saving to undo it."),
		});
	}

	// picked by hand for all of the customer's rows: Post to and its own ledger no longer change them
	set_party_account(picked) {
		if (!picked) return;
		const same = (row) =>
			row === picked ||
			(picked.party
				? row.party === picked.party
				: !!picked.party_name && row.party_name === picked.party_name);
		for (const row of this.get_rows().filter(same)) {
			row.account = picked.account;
			row.account_by_hand = picked.account ? 1 : 0;
		}
		this.grid.refresh();
	}

	set_rows_party(name, values) {
		const rows = this.get_rows().filter((row) => row.party_name === name);
		rows.forEach((row) => Object.assign(row, values));
		return rows;
	}

	// a customer or supplier linked here gets the account its invoices go to today
	fill_accounts() {
		const { frm, accounts, party_accounts } = this;
		if (!accounts.length) return;
		const rows = this.get_rows().filter((row) => row.party && !row.account);
		const missing = [...new Set(rows.map((row) => row.party))].filter(
			(name) => !(name in party_accounts)
		);
		Promise.all(
			missing.map((name) =>
				frappe
					.xcall("erpnext.accounts.party.get_party_account", {
						party_type: this.party_type,
						party: name,
						company: frm.doc.company,
					})
					// an account in another currency isn't one to pick; posting leaves those rows out
					.then((account) => (party_accounts[name] = accounts.includes(account) ? account : null))
			)
		).then(() => {
			rows.forEach((row) => (row.account = row.account || party_accounts[row.party]));
			this.grid.refresh();
			this.refresh_state();
		});
	}

	download_new_names() {
		return this.save().then(() =>
			download_sheet(this.frm.doc.company, "opening_invoices.download_new_parties", {
				party_type: this.party_type,
			})
		);
	}

	upload_invoices() {
		const drafts = this.get_rows().filter((row) => row.party_name || flt(row.outstanding_amount)).length;
		this.upload(
			"import_opening_invoices",
			drafts &&
				(this.is_customer
					? __(
							"The sheet replaces the {0} in this table, unsaved changes included. Posted invoices stay as they are. Continue?",
							[count_invoices(drafts, this.party_type)]
					  )
					: __(
							"The sheet replaces the {0} in this table, unsaved changes included. Posted bills stay as they are. Continue?",
							[count_invoices(drafts, this.party_type)]
					  ))
		);
	}

	save() {
		return call_method(this.frm, "save_opening_invoices", {
			party_type: this.party_type,
			rows: this.get_rows(),
			post_to: this.sheet_account,
		}).then(() => reload_fresh(this.frm));
	}

	save_draft() {
		if (!this.dialog.get_values()) return;
		this.save().then(() => {
			this.saved_key = get_rows_key(this.grid);
			if (this.view === "sheet") this.refresh_state();
			frappe.ui.toast({ type: "success", message: __("Saved as a draft.") });
		});
	}

	post() {
		const all = this.get_rows();
		const open = new Set(
			all.filter((row) => OPEN_KINDS.includes(this.kind_of(row))).map((row) => row.party_name)
		);
		if (open.size) {
			this.names_view.show(
				plural(
					open.size,
					__("Confirm this name first, then post."),
					__("Confirm these {0} names first, then post.", [open.size])
				),
				true
			);
			return;
		}
		if (!this.dialog.get_values()) return;
		this.save().then(() =>
			post_opening_invoices(this.frm, this.party_type, {
				rows: all,
				names: { ...this.names, disabled: [...this.disabled] },
				disabled_parties: [...this.post_disabled],
				before_post: () => this.dialog.hide(),
				show_rows: (account) => this.ledgers.show_rows_on(account),
			})
		);
	}
}
