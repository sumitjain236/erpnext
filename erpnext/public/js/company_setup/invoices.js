import {
	ask_before_closing,
	call_method,
	day_before_text,
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
	short_account,
	show_in_place_of,
} from "./common";
import { fill_where, get_source, where_to_find_fields } from "./where_to_find";
import { setup_dialog_grid } from "./sheet_window";
import { PARTY, make_posting_bar } from "./overview";
import { invoice_menu } from "./invoices_posted";
import {
	count_invoices,
	get_ledger_overs,
	ledger_over_title,
	make_hand_posted_test,
	make_opening_invoice_stats,
	post_opening_invoices,
	upload_opening_invoices,
} from "./invoice_posting";
import { import_blocked, open_party_import } from "./imports";

export function show_opening_invoices(frm, party_type, result, replacing) {
	get_data(frm, "get_invoice_names", { party_type: party_type }).then((r) =>
		make_opening_invoices_dialog(frm, party_type, r.message, result, replacing)
	);
}

// The draft rows, linked to the customer or supplier their name means today
function get_draft_rows(frm, party_type, names) {
	return (frm.doc[PARTY[party_type].table] || [])
		.filter((row) => row.status !== "Created" && (row.party_name || row.party))
		.map((row) => ({ ...row, party: row.party || names.found?.[row.party_name] }));
}

// found, a guess to confirm (check), no match (new), confirmed as new (create), or one that can't post
function get_name_kind(row, names, disabled) {
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
const OPEN_KINDS = ["check", "new"];

function get_name_badge(kind, party_type, currency) {
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
function get_name_state(rows, kind_of) {
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

function make_opening_invoices_dialog(frm, party_type, names, result, replacing) {
	const party = PARTY[party_type];
	const is_customer = party_type === "Customer";
	const rows = frm.doc[party.table] || [];
	const created = rows.filter((row) => row.status === "Created").length;
	const disabled = new Set(names.disabled || []);
	// disabled customers or suppliers the user chose to post anyway
	const post_disabled = new Set();
	const other_currency = names.other_currency || {};
	// a row an invoice made by hand stands for has its own alert, not the names' ones
	const is_hand_posted = make_hand_posted_test(names);
	const kind_of = (row) => (is_hand_posted(row) ? "hand_posted" : get_name_kind(row, names, disabled));
	// several receivable or payable accounts: each invoice shows the one it goes to, and can change
	const accounts = names.accounts || [];
	const party_accounts = { ...names.party_accounts };
	// a ledger saved on the customer or supplier, or on its group, for a sheet with no "Post to"
	const own_accounts = names.own_accounts || {};
	// the ledger a row goes to: its own, its customer's or supplier's, else the company's default
	const account_of = (row) => row.account || party_accounts[row.party] || names.default_account;
	// dates the sheet had that could not be read: what it said, by row and field, until typed in
	const unread = {};
	for (const bad of result?.bad_dates || []) {
		unread[[bad.party_name, bad.invoice_number || ""]] = {
			...unread[[bad.party_name, bad.invoice_number || ""]],
			[bad.field]: bad.text,
		};
	}
	const data = get_draft_rows(frm, party_type, names).map((row) => ({
		...row,
		name: undefined,
		account: accounts.length ? row.account || party_accounts[row.party] : undefined,
		unread_dates: unread[[row.party_name, row.invoice_number || ""]],
	}));
	const has_unread_date = (row) => Object.keys(row.unread_dates || {}).some((field) => !row[field]);
	const date_formatter = (value, df, options, row) =>
		!value && row?.unread_dates?.[df.fieldname]
			? `<span title="${frappe.utils.escape_html(
					__("On the sheet: {0}", [row.unread_dates[df.fieldname]])
			  )}">${frappe.ui.badge.html({ label: __("Check date"), theme: "orange" })}</span>`
			: value
			? frappe.datetime.str_to_user(value)
			: "";
	let only_unread = false;
	// names confirmed in this window, shown as Confirmed in the names list
	const confirmed = new Set();
	const day_before = day_before_text(frm);
	const dialog = new frappe.ui.Dialog({
		title: is_customer ? __("Opening sales invoices") : __("Unpaid supplier bills"),
		size: "extra-large",
		fields: [
			{ fieldname: "intro", fieldtype: "HTML" },
			{ fieldname: "names_view", fieldtype: "HTML", hidden: 1 },
			{ fieldname: "review", fieldtype: "HTML" },
			{
				fieldname: "invoices",
				fieldtype: "Table",
				label: is_customer ? __("Unpaid invoices") : __("Unpaid bills"),
				in_place_edit: true,
				data: data,
				fields: [
					{
						fieldname: "party_name",
						fieldtype: "Data",
						label: __("From sheet"),
						in_list_view: 1,
						columns: accounts.length ? 2 : 3,
						formatter: (value, df, options, doc) => {
							const kind = doc && value ? kind_of(doc) : null;
							const badge =
								doc?.status === "Failed" && kind === "found"
									? frappe.ui.badge.html({ label: __("Failed"), theme: "red" })
									: get_name_badge(kind, party_type, other_currency[doc?.party]);
							return name_with_badge(frappe.form.formatters.Data(value, df), badge);
						},
						onchange: () => refresh_state(),
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
								row.account = (in_sheet(row) && sheet_account) || own_accounts[row.party];
								row.account_by_hand = 0;
								row.create_party = 0;
							}
							this.grid_row?.refresh_field("party_name");
							fill_accounts();
							refresh_state();
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
						get_query: () => ({ filters: { name: ["in", accounts] } }),
						filter_description: is_customer ? __("Receivable ledgers") : __("Payable ledgers"),
						onchange: function () {
							// picked by hand for all of the customer's rows: Post to and its own ledger no longer change them
							const picked = this.doc;
							if (!picked) return;
							const same = (row) =>
								row === picked ||
								(picked.party
									? row.party === picked.party
									: !!picked.party_name && row.party_name === picked.party_name);
							for (const row of (dialog.get_value("invoices") || []).filter(same)) {
								row.account = picked.account;
								row.account_by_hand = picked.account ? 1 : 0;
							}
							grid.refresh();
							refresh_state();
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
						onchange: () => refresh_state(),
					},
					{
						fieldname: "due_date",
						fieldtype: "Date",
						label: __("Due Date"),
						in_list_view: 1,
						columns: 1,
						formatter: date_formatter,
						onchange: () => refresh_state(),
					},
					{
						fieldname: "outstanding_amount",
						fieldtype: "Currency",
						label: __("Outstanding"),
						in_list_view: 1,
						columns: accounts.length ? 1 : 2,
						reqd: 1,
						onchange: () => refresh_state(),
					},
				],
			},
			{ fieldname: "notes", fieldtype: "HTML" },
			...where_to_find_fields(frm, party.table, { has_data: data.length, posted: created }),
		],
		primary_action_label: __("Post to books"),
		primary_action: () => post(),
		secondary_action_label: __("Save draft"),
		secondary_action: () => save_draft(),
	});
	fill_where(frm, dialog, party.table);
	const save = () =>
		call_method(frm, "save_opening_invoices", {
			party_type: party_type,
			rows: dialog.get_value("invoices") || [],
			post_to: sheet_account,
		}).then(() => reload_fresh(frm));
	const grid = dialog.fields_dict.invoices.grid;
	setup_dialog_grid(grid, () => refresh_state());
	const currency = erpnext.get_currency(frm.doc.company);

	// one line and the sheet's buttons; the cards under them
	const $intro = dialog.get_field("intro").$wrapper;
	$intro.append(
		make_posting_reds(frm, frm.doc.__onload?.posting_reds?.[party_type], () =>
			show_opening_invoices(frm, party_type, null, dialog)
		)
	);
	// each sheet's draft rows can go at once; saving keeps the change, closing without it undoes it
	const draft_sources = {};
	for (const row of frm.doc[PARTY[party_type].table] || []) {
		if (row.status !== "Created")
			draft_sources[row.source || ""] = (draft_sources[row.source || ""] || 0) + 1;
	}
	const remove_rows = (gone, count) => {
		dialog.fields_dict.invoices.df.data = (dialog.get_value("invoices") || []).filter(
			(row) => !gone(row)
		);
		grid.wrapper.find(".grid-heading-row .grid-row-check").prop("checked", false);
		grid.refresh();
		refresh_state();
		frappe.ui.toast({
			type: "info",
			message:
				plural(count, __("1 row removed."), __("{0} rows removed.", [count])) +
				" " +
				__("Save draft to keep this, or close without saving to undo it."),
		});
	};
	const menu = [
		...Object.entries(draft_sources).map(([source, count]) => ({
			label:
				count === 1
					? __("Remove {0} · 1 row", [source || __("rows typed in")])
					: __("Remove {0} · {1} rows", [source || __("rows typed in"), count]),
			icon: "trash-2",
			onclick: () => remove_rows((row) => (row.source || "") === source, count),
		})),
		...invoice_menu(frm, party_type).filter((item) => item.icon === "download"),
		names.new?.length &&
			names.can_create && {
				label: is_customer ? __("Download new customers") : __("Download new suppliers"),
				icon: "download",
				onclick: () => download_new_names(),
			},
	].filter(Boolean);
	const $sheet_buttons = make_sheet_buttons(() => upload_opening_invoices(frm, party_type, dialog), menu);
	$(`<div class="flex items-center justify-between gap-3 mb-3"></div>`)
		.append(
			$(`<div class="text-p-sm text-ink-gray-6"></div>`).text(
				is_customer
					? __(
							"Invoices still unpaid on {0}, one row each. Enter a credit or advance as a negative amount.",
							[day_before]
					  )
					: says_check
					? __(
							"Bills still unpaid on {0}, one row each. Enter an advance or vendor credit as a negative amount.",
							[day_before]
					  )
					: __(
							"Bills still unpaid on {0}, one row each. Enter an advance or debit note as a negative amount.",
							[day_before]
					  )
			),
			$sheet_buttons
		)
		.appendTo($intro);
	// several receivable or payable lines in the trial balance: the sheet's ledger, picked once
	const ledger_lines = (frm.doc.__onload?.sheet_match?.[party_type]?.lines || []).filter(
		(line) => line.sheet !== null
	);
	const $post_to = $(`<div class="max-w-sm"></div>`).appendTo($intro);
	// the ledger picked for the whole sheet: every row goes there, names confirmed later too
	let sheet_account = null;
	// after an upload, Post to is for that sheet's rows and rows typed in, not another sheet's
	const in_sheet = (row) => !result?.source || !row.source || row.source === result.source;
	if (accounts.length > 1) {
		const used = [...new Set(data.map((row) => row.account).filter(Boolean))];
		// the trial balance's ledgers, when it has them: a ledger with no line is never the sheet's
		const line_accounts = ledger_lines.map((line) => line.account).filter((a) => accounts.includes(a));
		// a sheet's ledger shows again only while rows are on it in place of their own ledger
		const moved = data.some(
			(row) =>
				row.account &&
				!row.account_by_hand &&
				row.account !==
					(own_accounts[row.party] || party_accounts[row.party] || names.default_account)
		);
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
				change: () => {
					const value = post_to.get_value() || null;
					if (value === sheet_account) return;
					sheet_account = value;
					for (const row of dialog.get_value("invoices") || []) {
						if (row.account_by_hand || !in_sheet(row)) continue;
						row.account = value || own_accounts[row.party] || party_accounts[row.party];
					}
					grid.refresh();
					refresh_state();
				},
			},
			parent: $post_to,
			render_input: true,
		});
		// typing replaces the picked ledger instead of adding to it
		post_to.$input.on("focus", () => setTimeout(() => post_to.$input.select()));
		// with no rows yet it starts on the trial balance's only ledger line; with several, the user picks
		const picked =
			result?.post_to ||
			(used.length === 1 && moved ? used[0] : null) ||
			(!data.length && line_accounts.length === 1 ? line_accounts[0] : null);
		if (picked) {
			sheet_account = picked;
			post_to.set_value(picked);
		}
	}
	const $stats = $(`<div class="es-stat-card-grid company-setup-stats mb-3"></div>`).appendTo($intro);

	// what was read and left out, under the table
	const $notes = $(`<div class="flex flex-col gap-1 mt-2"></div>`).appendTo(
		dialog.get_field("notes").$wrapper
	);
	const grey = (text) => grey_line(text).appendTo($notes);
	// said from the rows as they are now, so it follows rows removed or typed in
	const $advances = grey("");
	const show_advances = (count) =>
		$advances
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
	if (result?.unused_columns?.length) {
		grey(
			is_customer
				? __("Not read: {0}. Only the name, invoice number, dates, amount and account are used.", [
						result.unused_columns.join(", "),
				  ])
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
	// rows of the sheet that were posted before: easy to miss as a grey line, so said on top
	const make_skipped_box = () => {
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
					: is_customer
					? __("They were left out; the other {0} are below.", [result.added])
					: __("They were left out; the other {0} are below.", [result.added]),
			})
			.addClass("mb-3");
	};

	// Nothing entered yet: the table's place says how to start
	let typing = data.length > 0;
	const $review = dialog.get_field("review").$wrapper;
	const show_empty = () => {
		const source = get_source(frm, party.table);
		$review.append(
			$(`<div class="border rounded-lg py-10"></div>`).append(
				frappe.ui.empty_state({
					icon: "file-spreadsheet",
					title: is_customer
						? __("Upload your receivables report")
						: __("Upload your payables report"),
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
							onclick: () => upload_opening_invoices(frm, party_type, dialog),
						},
						{
							label: __("Add row"),
							onclick: () => {
								typing = true;
								refresh_state();
								grid.add_new_row();
							},
						},
						{
							label: __("Download sample sheet"),
							variant: "ghost",
							onclick: () =>
								download_sheet(
									frm.doc.company,
									"opening_invoices.download_opening_invoices",
									{
										party_type: party_type,
									}
								),
						},
					],
				})
			)
		);
	};

	// The names that need a look, counted by kind, and the way to them
	const make_review_box = (state) => {
		const count = (kind) => state.review.filter((name) => name.kind === kind).length;
		const parties = (n) =>
			is_customer
				? plural(n, __("1 customer"), __("{0} customers", [n]))
				: plural(n, __("1 supplier"), __("{0} suppliers", [n]));
		const currencies = [
			...new Set(
				state.review
					.filter((name) => name.kind === "other_currency")
					.map((name) => other_currency[name.party])
			),
		];
		const parts = [
			["check", __("{0} guessed", [count("check")])],
			["new", __("{0} to create", [count("new")])],
			["disabled", parties(count("disabled"))],
			["other_currency", parties(count("other_currency"))],
		].filter(([kind]) => count(kind));
		$(`<div class="flex flex-wrap items-center justify-between gap-3 mb-3 border rounded-lg p-3"></div>`)
			.append(
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
								get_name_badge(
									kind,
									party_type,
									currencies.length === 1 ? currencies[0] : null
								),
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
									if (!dialog.get_values()) return;
									save().then(() => {
										dialog.hide();
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
						onclick: () => show_names(),
					})
				)
			)
			.appendTo($review);
	};

	// a posting in the background: the window shows it and changes wait until it's done
	const posting = frm.posting?.[party_type];
	const show_posting = () => {
		$review.append(
			frappe.ui
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
				.addClass("mb-3")
		);
		$sheet_buttons.find("button").prop("disabled", true);
		dialog.get_primary_btn().hide();
		dialog.set_secondary_action_label(__("Close"));
		dialog.set_secondary_action(() => dialog.hide());
	};
	const refresh_state = () => {
		const all = dialog.get_value("invoices") || [];
		const empty = !all.length && !typing;
		const state = get_name_state(all, kind_of);
		$review.empty();
		$sheet_buttons.toggle(!empty);
		$post_to.toggle(!empty);
		grid.wrapper.closest(".frappe-control").toggle(!empty);
		const overs = get_window_overs(all);
		// posted rows on an over ledger: the post confirm may offer to move them
		const can_move = (frm.doc[party.table] || []).some(
			(row) => row.status === "Created" && overs.some((over) => over.account === row.account)
		);
		dialog.get_primary_btn().prop("disabled", empty || (overs.length && !can_move));
		update_bulk();
		dialog.get_secondary_btn().toggle(!empty);
		// shown with the table, rows or not, so the table doesn't move down on the first pick
		$stats
			.empty()
			.toggle(!empty)
			.append(empty ? [] : make_opening_invoice_stats(frm, party_type, state, currency));
		$review.append(make_skipped_box());
		show_advances(all.filter((row) => flt(row.outstanding_amount) < 0).length);
		if (empty) return show_empty();
		if (posting) return show_posting();
		// rows posting turned down for a reason of their own, not their name
		const failed = all.filter((row) => row.status === "Failed" && kind_of(row) === "found");
		if (failed.length) {
			$review.append(
				frappe.ui
					.alert({
						theme: "red",
						title: __("{0} could not be posted", [count_invoices(failed.length, party_type)]),
						description: __(
							"Each shows Failed in the table; the reason is in the row. Fix them and post again, or remove them."
						),
						footer: () =>
							$(`<div class="flex gap-2"></div>`).append(
								frappe.ui.button({
									label: __("Remove these rows"),
									onclick: () => {
										dialog.fields_dict.invoices.df.data = all.filter(
											(row) => !failed.includes(row)
										);
										grid.refresh();
										return save().then(() => refresh_state());
									},
								})
							),
					})
					.addClass("mb-3")
			);
		}
		$review.append(make_waiting_alerts(all));
		$review.append(make_unread_dates_alert(all));
		$review.append(make_over_alerts(overs));
		if (state.review.length) make_review_box(state);
	};
	// rows whose sheet dates could not be read: one box for all of them, and a way to see just those
	const make_unread_dates_alert = (all) => {
		const rows = all.filter(has_unread_date);
		if (!rows.length) only_unread = false;
		show_only_unread();
		if (!rows.length) return [];
		const kind = is_customer
			? plural(rows.length, __("1 invoice"), __("{0} invoices", [rows.length]))
			: plural(rows.length, __("1 bill"), __("{0} bills", [rows.length]));
		return frappe.ui
			.alert({
				theme: "yellow",
				title: __("{0} with dates we couldn't read", [kind]),
				description: !only_unread
					? __("They're left empty. Type them in, or they're dated {0}.", [day_before])
					: rows.length === 1
					? __("Showing only this one of {0}. Type its dates in, or it's dated {1}.", [
							all.length,
							day_before,
					  ])
					: __("Showing only these {0} of {1}. Type the dates in, or they're dated {2}.", [
							rows.length,
							all.length,
							day_before,
					  ]),
				footer: () =>
					frappe.ui.button({
						label: only_unread ? __("Show all") : __("Show rows"),
						onclick: () => {
							only_unread = !only_unread;
							if (only_unread) {
								// first, so they are on the table's first page
								dialog.fields_dict.invoices.df.data = [
									...rows,
									...all.filter((row) => !rows.includes(row)),
								];
								grid.refresh();
							}
							refresh_state();
						},
					}),
			})
			.addClass("mb-3");
	};
	const is_shown = (row) => !only_unread || has_unread_date(row);
	const show_only_unread = () => {
		for (const grid_row of grid.grid_rows || []) {
			grid_row?.wrapper.toggle(is_shown(grid_row.doc));
		}
	};
	// a ledger's rows only, in the table's own Account filter
	const filter_account = (account) => {
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
	};
	const show_rows_on = (account) => {
		// rows with no ledger set go to the default one: named on them so the filter finds them
		for (const row of dialog.get_value("invoices") || []) {
			if (account && !row.account && account_of(row) === account) row.account = account;
		}
		filter_account(account);
		refresh_state();
	};
	// the ledgers over their trial balance line with the rows as they are in the window, saved or not
	const get_window_overs = (all) => {
		if (accounts.length < 2) return [];
		const moved = {};
		const add = (row, sign) => {
			const account = account_of(row);
			if (
				row.status === "Created" ||
				!account ||
				(!row.party && !row.create_party) ||
				other_currency[row.party] ||
				disabled.has(row.party)
			)
				return;
			moved[account] = (moved[account] || 0) + sign * flt(row.outstanding_amount);
		};
		all.forEach((row) => add(row, 1));
		(frm.doc[party.table] || []).forEach((row) => add(row, -1));
		return get_ledger_overs(frm, party_type, [], disabled, moved);
	};
	const make_over_alerts = (overs) => {
		const shown = grid.filter.account?.value;
		if (accounts.includes(shown) && !overs.some((over) => over.account === shown)) filter_account(null);
		show_only_unread();
		if (!overs.length) return [];
		const all = dialog.get_value("invoices") || [];
		return make_alerts(
			overs.map((over) => {
				const only = over.account === shown;
				const count = grid.data.length;
				const has_rows = all.some(
					(row) => row.status !== "Created" && account_of(row) === over.account
				);
				const posted = flt(over.entered - over.draft, 2);
				return {
					theme: "red",
					title: ledger_over_title(frm, over, currency),
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
										currency
									),
							  ]),
						posted >= 1 &&
							(!has_rows || posted - flt(over.sheet) >= 1) &&
							(is_customer
								? __("{0} is in posted invoices.", [format_currency(posted, currency)])
								: __("{0} is in posted bills.", [format_currency(posted, currency)])),
						has_rows && __("Tick the rows that belong to another ledger, pick it, and Apply."),
					]
						.filter(Boolean)
						.join(" "),
					buttons: has_rows
						? [
								{
									label: only ? __("Show all") : __("Show rows"),
									onclick: () => show_rows_on(only ? null : over.account),
								},
						  ]
						: [],
				};
			})
		).addClass("mb-3");
	};
	// the rows ticked in the table: set their ledger at once
	const $bulk = $(`<div class="flex flex-wrap items-center gap-2 p-2 mb-2 border rounded-lg"></div>`)
		.hide()
		.prependTo(grid.wrapper);
	const $bulk_text = $(`<span class="text-base text-ink-gray-8"></span>`).appendTo($bulk);
	const bulk_account = frappe.ui.form.make_control({
		df: {
			fieldtype: "Link",
			fieldname: "bulk_account",
			options: "Account",
			only_select: 1,
			placeholder: __("Pick a ledger"),
			get_query: () => ({ filters: { name: ["in", accounts] } }),
			filter_description: is_customer ? __("Receivable ledgers") : __("Payable ledgers"),
			change: () => update_bulk(),
		},
		parent: $(`<div class="w-64"></div>`).appendTo($bulk),
		render_input: true,
	});
	const $apply = frappe.ui
		.button({ label: __("Apply"), variant: "solid", onclick: () => apply_bulk() })
		.appendTo($bulk);
	// the table's own Delete is hidden while rows are ticked, so a ledger change deletes nothing
	frappe.ui
		.button({
			label: __("Delete"),
			icon: "trash-2",
			variant: "ghost",
			onclick: () => {
				const ticked = grid.get_selected_children();
				remove_rows((row) => ticked.includes(row), ticked.length);
			},
		})
		.addClass("ml-auto")
		.appendTo($bulk);
	const update_bulk = () => {
		const count = accounts.length > 1 ? grid.get_selected_children().length : 0;
		$bulk.toggle(!!count);
		grid.wrapper.toggleClass("company-setup-bulk-ledger", !!count);
		if (!count) return;
		$bulk_text.text(
			plural(
				count,
				__("1 row selected · Set its ledger to"),
				__("{0} rows selected · Set their ledger to", [count])
			)
		);
		$apply
			.prop("disabled", !bulk_account.get_value())
			.find(".es-button__label")
			.text(plural(count, __("Apply to 1 row"), __("Apply to {0} rows", [count])));
	};
	const apply_bulk = () => {
		const account = bulk_account.get_value();
		const ticked = grid.get_selected_children();
		if (!account || !ticked.length) return;
		for (const row of ticked) {
			Object.assign(row, { account: account, account_by_hand: 1, __checked: 0 });
		}
		grid.wrapper.find(".grid-heading-row .grid-row-check").prop("checked", false);
		grid.refresh();
		refresh_state();
		frappe.ui.toast({
			type: "success",
			message: plural(
				ticked.length,
				__("1 row moved to {0}. Save draft to keep it.", [short_account(frm, account)]),
				__("{0} rows moved to {1}. Save draft to keep it.", [
					ticked.length,
					short_account(frm, account),
				])
			),
		});
	};
	// the header tick takes every row shown, on all pages
	grid.wrapper.on("click", ".grid-row-check", (e) => {
		const $check = $(e.currentTarget);
		if ($check.closest(".grid-heading-row").length) {
			for (const row of grid.data) row.__checked = $check.prop("checked") && is_shown(row) ? 1 : 0;
			for (const grid_row of grid.grid_rows || []) grid_row?.refresh_check();
		}
		update_bulk();
	});
	// rows that stay drafts whatever the names say: a disabled customer's, or in another currency
	const make_waiting_alerts = (all) => {
		const alerts = [];
		const hand = all.filter(is_hand_posted);
		if (hand.length) {
			alerts.push({
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
							[row.party, row.invoice_number, format_currency(row.outstanding_amount, currency)]
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
									save()
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
										.then(() => reload_fresh(frm))
										.then(() => show_opening_invoices(frm, party_type, null, dialog))
							),
					},
				],
			});
		}
		const by_currency = {};
		for (const row of all.filter((row) => kind_of(row) === "other_currency")) {
			(by_currency[other_currency[row.party]] ||= []).push(row);
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
		const off = [...new Set(all.filter((row) => kind_of(row) === "disabled").map((row) => row.party))];
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
								disabled.delete(name);
								post_disabled.add(name);
							});
							grid.refresh();
							refresh_state();
						},
					},
				],
			});
		}
		return alerts.length ? make_alerts(alerts).addClass("mb-3") : [];
	};
	const set_rows_party = (name, values) => {
		const rows = (dialog.get_value("invoices") || []).filter((row) => row.party_name === name);
		rows.forEach((row) => Object.assign(row, values));
		return rows;
	};
	// a customer or supplier linked here gets the account its invoices go to today
	const fill_accounts = () => {
		if (!accounts.length) return;
		const rows = (dialog.get_value("invoices") || []).filter((row) => row.party && !row.account);
		const missing = [...new Set(rows.map((row) => row.party))].filter(
			(name) => !(name in party_accounts)
		);
		Promise.all(
			missing.map((name) =>
				frappe
					.xcall("erpnext.accounts.party.get_party_account", {
						party_type: party_type,
						party: name,
						company: frm.doc.company,
					})
					// an account in another currency isn't one to pick; posting leaves those rows out
					.then((account) => (party_accounts[name] = accounts.includes(account) ? account : null))
			)
		).then(() => {
			rows.forEach((row) => (row.account = row.account || party_accounts[row.party]));
			grid.refresh();
			refresh_state();
		});
	};
	// The names on the sheet are a view of this window, with a way back to the invoices
	const $names = dialog.get_field("names_view").$wrapper.addClass("company-setup-names");
	let names_filter = "review";
	let names_line = null;
	let post_after = false;
	const show_view = (names_view) => {
		for (const fieldname of ["intro", "review", "invoices", "notes"]) {
			dialog.get_field(fieldname).$wrapper.toggle(!names_view);
		}
		// the sections below the table: Where do I find this?
		dialog.$body
			.find(".form-section")
			.filter((i, section) => !section.contains($names[0]) && !$names[0].contains(section))
			.toggle(!names_view);
		dialog.set_df_property("names_view", "hidden", !names_view);
		if (!names_view) {
			post_after = false;
			dialog.set_primary_action(__("Post to books"), post);
			grid.refresh();
			refresh_state();
		}
	};
	const show_names = (line = null) => {
		names_line = line;
		names_filter = "review";
		render_names();
		show_view(true);
	};
	const render_names = () => {
		const state = get_name_state(dialog.get_value("invoices") || [], kind_of);
		const order = { check: 0, new: 1, disabled: 2, other_currency: 3, create: 4, found: 5 };
		const list = state.names.sort((a, b) => order[a.kind] - order[b.kind]);
		const shown = (names_filter === "review" ? state.review : list).map((name) => ({
			...name,
			original_party: name.party,
			party: name.party || (name.kind === "check" ? names.suggestions[name.name_on_sheet] : null),
		}));
		const new_names = state.review.filter((name) => name.kind === "new");
		$names.empty();
		const blocked = state.review.filter((name) => ["disabled", "other_currency"].includes(name.kind));
		const $head = $(`<div class="flex flex-col gap-4 mb-4"></div>`).appendTo($names);
		$head.append(
			$(`<div class="flex flex-wrap items-center justify-between gap-3"></div>`).append(
				$(`<div class="flex items-center gap-2"></div>`).append(
					frappe.ui.button({
						icon: "arrow-left",
						variant: "ghost",
						tooltip: is_customer ? __("Back to invoices") : __("Back to bills"),
						onclick: () => show_view(false),
					}),
					$(`<div class="text-lg-semibold text-ink-gray-9"></div>`).text(
						is_customer ? __("Customer names") : __("Supplier names")
					)
				),
				frappe.ui.tab_buttons({
					label: __("Show"),
					value: names_filter,
					options: [
						{ label: __("To review {0}", [state.review.length]), value: "review" },
						{ label: __("All {0}", [list.length]), value: "all" },
					],
					on_change: (value) => {
						names_filter = value;
						render_names();
					},
				})
			),
			// what to do comes first, then what the badges mean
			$(`<div class="flex flex-col gap-1"></div>`).append(
				$(`<div class="text-base text-ink-gray-8"></div>`).text(
					names_line ||
						(names_filter === "review"
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
		const names_group = new frappe.ui.FieldGroup({
			body: $(`<div></div>`).appendTo($names),
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
								const kind = confirmed.has(value) ? "confirmed" : doc?.kind;
								const badge = get_name_badge(kind, party_type, other_currency[doc?.party]);
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
							onchange: () => update_confirm(),
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
		names_group.make();
		const names_grid = names_group.fields_dict.names.grid;
		setup_dialog_grid(names_grid);
		// without a default group in the settings, the ones created here get the group picked here
		const default_group = frm.doc.__onload?.default_groups?.[party_type];
		const $group = $(`<div class="mt-3 max-w-sm hide"></div>`).appendTo($names);
		const group_form = new frappe.ui.FieldGroup({
			body: $group,
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
		group_form.make();
		const group_control = group_form.get_field("party_group");
		// what confirming a ticked name does: link it to the customer shown, or create it as new
		const get_action = (name) => {
			if (name.party) {
				const changed = name.party !== name.original_party;
				return OPEN_KINDS.includes(name.kind) || changed ? "link" : null;
			}
			return OPEN_KINDS.includes(name.kind) && names.can_create ? "create" : null;
		};
		const update_confirm = () => {
			const actions = names_grid.get_selected_children().map(get_action).filter(Boolean);
			const creates = actions.filter((action) => action === "create").length;
			$group.toggleClass("hide", !creates || !!default_group);
			const territory = frm.doc.__onload?.default_groups?.territory;
			group_control.set_description(
				[
					is_customer
						? __(
								"Your selling settings have no default group. Leave empty to add them without one."
						  )
						: __(
								"Your buying settings have no default group. Leave empty to add them without one."
						  ),
					is_customer && territory && __("Territory: {0}.", [territory]),
				]
					.filter(Boolean)
					.join(" ")
			);
			dialog.set_primary_action(
				!actions.length
					? __("Confirm")
					: creates
					? __("Confirm and create {0}", [creates])
					: __("Confirm {0}", [actions.length]),
				() => confirm_names()
			);
			dialog.get_primary_btn().prop("disabled", !actions.length);
		};
		const confirm_names = () => {
			let done = 0;
			for (const name of names_grid.get_selected_children()) {
				const action = get_action(name);
				if (!action) continue;
				const group = group_control.get_value() || null;
				const rows = set_rows_party(
					name.name_on_sheet,
					action === "link"
						? { party: name.party, create_party: 0 }
						: { party: null, create_party: 1, party_group: group }
				);
				// a ledger the sheet or the user gave stays
				const account =
					action === "link"
						? own_accounts[name.party]
						: names.group_accounts?.[group || default_group];
				rows.filter((row) => !row.account_by_hand).forEach(
					(row) => (row.account = (in_sheet(row) && sheet_account) || account)
				);
				confirmed.add(name.name_on_sheet);
				done += 1;
			}
			if (!done) return;
			fill_accounts();
			const open = (dialog.get_value("invoices") || []).some((row) =>
				OPEN_KINDS.includes(kind_of(row))
			);
			frappe.ui.toast({
				type: "success",
				message: plural(done, __("1 name confirmed."), __("{0} names confirmed.", [done])),
			});
			if (open) {
				render_names();
				return;
			}
			const then_post = post_after;
			show_view(false);
			if (then_post) post();
		};
		names_grid.wrapper.on("click", ".grid-row-check", () => setTimeout(update_confirm));
		dialog.set_secondary_action_label(__("Save draft"));
		update_confirm();
	};

	const download_new_names = () =>
		save().then(() =>
			download_sheet(frm.doc.company, "opening_invoices.download_new_parties", {
				party_type: party_type,
			})
		);
	const post = () => {
		const all = dialog.get_value("invoices") || [];
		const open = new Set(
			all.filter((row) => OPEN_KINDS.includes(kind_of(row))).map((row) => row.party_name)
		);
		if (open.size) {
			post_after = true;
			show_names(
				plural(
					open.size,
					__("Confirm this name first, then post."),
					__("Confirm these {0} names first, then post.", [open.size])
				)
			);
			return;
		}
		const values = dialog.get_values();
		if (!values) return;
		save().then(() =>
			post_opening_invoices(frm, party_type, {
				rows: values.invoices || [],
				names: { ...names, disabled: [...disabled] },
				disabled_parties: [...post_disabled],
				before_post: () => dialog.hide(),
				show_rows: show_rows_on,
			})
		);
	};
	const save_draft = () => {
		if (!dialog.get_values()) return;
		save().then(() => {
			saved_key = get_rows_key(grid);
			if (dialog.get_field("names_view").df.hidden) refresh_state();
			frappe.ui.toast({ type: "success", message: __("Saved as a draft.") });
		});
	};
	let saved_key = get_rows_key(grid);
	ask_before_closing(
		dialog,
		() => !posting && get_rows_key(grid) !== saved_key,
		() => (dialog.get_values() ? save() : Promise.reject())
	);

	if (posting) {
		frm.posting_dialog = Object.assign(dialog, { party_type: party_type });
		dialog.set_df_property("invoices", "read_only", 1);
	}
	show_in_place_of(dialog, replacing);
	refresh_state();
	grid.refresh();
}
