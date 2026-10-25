import {
	after_run,
	call_method,
	count_advances,
	day_before_text,
	get_data,
	grey_line,
	make_alerts,
	make_posting_reds,
	plural,
	short_account,
	source_name,
	upload_sheet,
} from "./common";
import { PARTY } from "./overview";
import { show_opening_invoices } from "./invoices";

export function make_opening_invoice_stats(frm, party_type, state, currency) {
	const hand = state.names.filter((name) => name.kind === "hand_posted").length;
	const is_customer = party_type === "Customer";
	const total = state.invoices.reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
	const day_before = frm.doc.migration_date && day_before_text(frm);
	const count = state.names.length;
	const ready = state.ready_rows.length;
	const credits = state.ready_rows.filter((row) => flt(row.outstanding_amount) < 0).length;
	const disabled = state.names.filter((name) => name.kind === "disabled").length;
	return [
		frappe.ui.stat_card({
			label: __("On your sheet"),
			value: format_currency(total, currency, 0),
			caption: state.advance
				? is_customer
					? __("Less {0} in credits or advances", [format_currency(state.advance, currency, 0)])
					: __("Less {0} in debits or advances", [format_currency(state.advance, currency, 0)])
				: day_before && __("On {0}", [day_before]),
		}),
		frappe.ui.stat_card({
			label: __("Ready to post"),
			value: credits
				? __("{0} + {1}", [format_number(ready - credits, null, 0), format_number(credits, null, 0)])
				: format_number(ready, null, 0),
			caption: ready ? count_entries(ready, credits, party_type) : null,
		}),
		frappe.ui.stat_card({
			label: is_customer ? __("Customers") : __("Suppliers"),
			value: format_number(count, null, 0),
			caption: !count
				? null
				: disabled
				? __("{0} disabled", [disabled])
				: state.review.length
				? __("{0} to review", [state.review.length])
				: hand
				? __("{0} posted by hand", [hand])
				: __("All ready"),
		}),
	];
}

export function upload_opening_invoices(frm, party_type, dialog) {
	const drafts = (dialog.fields_dict.invoices.df.data || []).filter(
		(row) => row.party_name || flt(row.outstanding_amount)
	).length;
	upload_sheet(
		frm,
		"import_opening_invoices",
		{ party_type: party_type },
		{
			replaces:
				drafts &&
				(party_type === "Customer"
					? __(
							"The sheet replaces the {0} in this table, unsaved changes included. Posted invoices stay as they are. Continue?",
							[count_invoices(drafts, party_type)]
					  )
					: __(
							"The sheet replaces the {0} in this table, unsaved changes included. Posted bills stay as they are. Continue?",
							[count_invoices(drafts, party_type)]
					  )),
			after: (result) => show_opening_invoices(frm, party_type, result, dialog),
		}
	);
}

// Ask before posting, saying what is posted and what stays a draft, then post and say how it went
export function post_opening_invoices(
	frm,
	party_type,
	{ rows, names = {}, disabled_parties = [], before_post, show_rows }
) {
	const is_customer = party_type === "Customer";
	const currency = erpnext.get_currency(frm.doc.company);
	const disabled = new Set(names.disabled || []);
	const other_currency = names.other_currency || {};
	const money = (list) =>
		format_currency(
			list.reduce((sum, row) => sum + flt(row.outstanding_amount), 0),
			currency
		);
	const named = rows.filter((row) => row.status !== "Created" && (row.party_name || row.party));
	const is_hand_posted = make_hand_posted_test(names);
	const why_waiting = (row) =>
		is_hand_posted(row)
			? "hand_posted"
			: !row.party && !row.create_party
			? "unconfirmed"
			: disabled.has(row.party)
			? "disabled"
			: other_currency[row.party]
			? "other_currency"
			: null;
	const posting = named.filter((row) => !why_waiting(row));
	const waiting = named.filter(why_waiting);
	const unique = (list) => [...new Set(list)];
	const new_names = unique(
		named.filter((row) => !row.party && row.create_party).map((row) => row.party_name)
	);
	if (!posting.length) {
		frappe.msgprint(
			named.every((row) => why_waiting(row) === "hand_posted")
				? is_customer
					? __(
							"These invoices look already posted by hand. Remove them from the window, or add the ones still to post."
					  )
					: __(
							"These bills look already posted by hand. Remove them from the window, or add the ones still to post."
					  )
				: is_customer
				? __("None of these invoices can be posted yet: their customers need a look first.")
				: __("None of these bills can be posted yet: their suppliers need a look first.")
		);
		return;
	}
	const advances = posting.filter((row) => flt(row.outstanding_amount) < 0);
	const day_before = day_before_text(frm);
	const list = (items) =>
		items.length > 3
			? __("{0} and {1} more", [items.slice(0, 3).join(", "), items.length - 3])
			: items.join(", ");
	const reasons = [
		["hand_posted", (parties) => __("{0} posted by hand", [list(parties)])],
		["disabled", (parties) => __("{0} disabled", [list(parties)])],
		["other_currency", (parties) => __("{0} in another currency", [list(parties)])],
		[
			"unconfirmed",
			(parties) =>
				plural(
					parties.length,
					__("1 name not confirmed"),
					__("{0} names not confirmed", [parties.length])
				),
		],
	]
		.map(([reason, text]) => {
			const parties = unique(
				waiting.filter((row) => why_waiting(row) === reason).map((row) => row.party || row.party_name)
			);
			return parties.length && text(parties);
		})
		.filter(Boolean);
	const lines = [
		[
			__("To post"),
			__("{0} · {1}", [count_entries(posting.length, advances.length, party_type), money(posting)]),
		],
		advances.length && [
			credit_rows_label(is_customer),
			__("{0} · {1}", [
				advances.length,
				format_currency(
					-advances.reduce((sum, row) => sum + flt(row.outstanding_amount), 0),
					currency
				),
			]),
		],
		new_names.length && [
			is_customer ? __("New customers") : __("New suppliers"),
			__("{0} · {1}", [new_names.length, list(new_names)]),
		],
		waiting.length && [__("Stay as drafts"), __("{0} · {1}", [waiting.length, reasons.join("; ")])],
	].filter(Boolean);
	const missing_year = frm.doc.__onload?.missing_fiscal_year;
	const can_add_year = frappe.model.can_create("Fiscal Year");
	// where the drafts are dated: their own dates where ERPNext has the year, else the day before
	// migration; invoices in a missing year are the choice above it
	const dated = (dates) => {
		const user = (date) => frappe.datetime.str_to_user(date);
		const lines = [
			dates.kept && [
				__("Dated as in your old books"),
				__("{0} · {1}", [
					__("{0} of {1} rows", [dates.kept, dates.kept + dates.no_year + dates.undated]),
					dates.first === dates.last
						? user(dates.first)
						: __("{0} to {1}", [user(dates.first), user(dates.last)]),
				]),
			],
			dates.undated && [__("No date"), __("{0} · posted on {1}", [dates.undated, day_before])],
		].filter(Boolean);
		return $(`<div class="flex flex-col"></div>`).append(
			lines.map(([label, value]) =>
				$(`<div class="flex justify-between gap-4 py-1.5 border-b text-p-sm"></div>`).append(
					$(`<span class="text-ink-gray-6"></span>`).text(label),
					$(`<span class="text-ink-gray-8 text-right"></span>`).text(value)
				)
			),
			[
				missing_year &&
					can_add_year &&
					__("The financial year {0} is added when you post.", [missing_year.year]),
				is_customer ? __("Nothing is sent to customers.") : __("Nothing is sent to suppliers."),
			]
				.filter(Boolean)
				.map((text) => grey_line(text).addClass("mt-3"))
		);
	};
	const fixes = [];
	let keep_old_dates = false;
	let old_years = [];
	const confirm = new frappe.ui.Dialog({
		title: is_customer ? __("Post opening invoices?") : __("Post opening bills?"),
		fields: [{ fieldname: "summary", fieldtype: "HTML" }],
		primary_action_label: __("Post to books"),
		primary_action: () => {
			confirm.hide();
			before_post?.();
			const account_fixes = fixes
				.filter(({ check }) => check.get_value())
				.map(({ clash }) => ({
					kind: clash.kind,
					account: clash.account,
					from_account: clash.from_account,
				}));
			call_method(
				frm,
				"create_opening_invoices",
				{
					party_type: party_type,
					account_fixes: account_fixes,
					keep_old_dates: keep_old_dates ? 1 : 0,
					disabled_parties: disabled_parties,
				},
				{ freeze: true, freeze_message: __("Posting…") }
			).then((r) => {
				if (r.message === "queued") {
					frm.posting = { ...frm.posting, [party_type]: { done: 0, total: posting.length } };
				}
				after_run(
					frm,
					r.message,
					is_customer
						? __("Posting the invoices in the background. This page updates when they are done.")
						: __("Posting the bills in the background. This page updates when they are done."),
					() => show_posting_result(frm, party_type, new_names)
				);
			});
		},
		secondary_action_label: __("Cancel"),
		secondary_action: () => confirm.hide(),
	});
	const $summary = $(`<div class="flex flex-col"></div>`);
	for (const [label, value] of lines) {
		$(`<div class="flex justify-between gap-4 py-2 border-b text-base"></div>`)
			.append(
				$(`<span class="text-ink-gray-6"></span>`).text(label),
				$(`<span class="text-base-medium text-ink-gray-8 text-right"></span>`).text(value)
			)
			.appendTo($summary);
	}
	const $ledgers = $(`<div></div>`).prependTo($summary);
	const $years = $(`<div></div>`).prependTo($summary);
	const $clashes = $(`<div class="flex flex-col gap-3"></div>`).appendTo($summary);
	const $note = $(`<div></div>`).appendTo($summary);
	// a ledger these would put above its trial balance line while another is short: not posted
	const check_ledgers = () => {
		const overs = get_ledger_overs(
			frm,
			party_type,
			fixes.filter(({ check }) => check.get_value()).map(({ clash }) => clash),
			disabled
		);
		$ledgers.empty().append(
			overs.length
				? make_alerts(
						overs.map((over) => ({
							theme: "red",
							title: ledger_over_title(frm, over, currency),
							description: [
								__("{0} is {1} short.", [
									over.short.map((line) => short_account(frm, line.account)).join(", "),
									format_currency(
										over.short.reduce((sum, line) => sum + line.sheet - line.entered, 0),
										currency
									),
								]),
								ledger_over_hint(frm, party_type, over.account, fixes),
							].join(" "),
							buttons: show_rows
								? [
										{
											label: __("Show rows"),
											onclick: () => {
												confirm.hide();
												show_rows(over.account);
											},
										},
								  ]
								: [],
						}))
				  ).addClass("mb-3")
				: []
		);
		// a missing year this user can't add stops the post
		const years = can_add_year
			? []
			: [...new Set([missing_year?.year, ...(keep_old_dates ? old_years : [])].filter(Boolean))];
		$years.empty().append(
			years.length
				? make_posting_reds(frm, [
						{
							text: __("Ask a System Manager to add the financial year {0} first.", [
								years.join(", "),
							]),
							fix: { label: __("New Fiscal Year"), doctype: "Fiscal Year", name: "new" },
						},
				  ])
				: []
		);
		confirm.get_primary_btn().prop("disabled", !!overs.length || !!years.length);
	};
	confirm.get_field("summary").$wrapper.append($summary);
	Promise.all([
		get_data(frm, "get_account_clashes", { party_type: party_type }),
		get_data(frm, "get_posting_dates", { party_type: party_type }),
	]).then(([r, dates]) => {
		$note.append(dated(dates.message));
		if (dates.message.no_year) {
			keep_old_dates = true;
			old_years = dates.message.missing_years;
			$note.before(
				make_old_dates_choice(dates.message, party_type, currency, day_before, (keep) => {
					keep_old_dates = keep;
					check_ledgers();
				})
			);
		}
		// a clash of drafts that stay drafts is not shown: they don't post
		const waiting_parties = new Set(waiting.map((row) => row.party).filter(Boolean));
		const clashes = (r.message || [])
			.map((clash) =>
				clash.kind === "draft"
					? {
							...clash,
							parties: clash.parties.filter((party) => !waiting_parties.has(party.party)),
					  }
					: clash
			)
			.filter((clash) => clash.parties.length);
		for (const clash of clashes) {
			const $box = make_account_clash(clash, party_type, currency);
			const check = frappe.ui.form.make_control({
				df: {
					fieldtype: "Check",
					fieldname: "fix",
					label: clash_fix_label(clash, party_type),
					description: clash_fix_note(clash, party_type, currency),
				},
				parent: $box,
				render_input: true,
			});
			check.$wrapper.addClass("mb-0 mt-1");
			check.$input.on("change", () => {
				// moving posted invoices cancels them: the button says so
				const moving = fixes
					.filter((fix) => fix.clash.kind === "posted" && fix.check.get_value())
					.reduce(
						(sum, fix) => sum + fix.clash.parties.reduce((n, party) => n + party.count, 0),
						0
					);
				confirm
					.get_primary_btn()
					.text(moving ? __("Post {0}, move {1}", [posting.length, moving]) : __("Post to books"));
			});
			check.$input.on("change", check_ledgers);
			fixes.push({ clash, check });
			$clashes.addClass("mt-4").append($box);
		}
		confirm.show();
		check_ledgers();
	});
}

// Customers or suppliers that something the user said puts on one account and their other invoices
// on another, where the trial balance agrees with one: shown with a tick to follow it
function make_account_clash(clash, party_type, currency) {
	const is_customer = party_type === "Customer";
	const count = clash.parties.reduce((sum, party) => sum + party.count, 0);
	const invoices = count_invoices(count, party_type);
	clash = { ...clash, account: clash.account_name, from_account: clash.from_account_name };
	const sheet = clash.parties[0]?.sources[0];
	const record_uses =
		clash.parties.length === 1
			? __("{0}'s record uses {1}.", [clash.parties[0].party, clash.account])
			: __("Their records use {0}.", [clash.account]);
	const goes_to =
		clash.kind === "split"
			? __("The rest of {0} is on {1}, where your trial balance is short.", [sheet, clash.account])
			: clash.kind === "posted"
			? clash.said
				? __("This sheet says {0}.", [clash.account])
				: record_uses
			: clash.said
			? __("This sheet says {0}.", [clash.from_account])
			: plural(
					count,
					__("It would go to {0}.", [clash.from_account]),
					__("These would go to {0}.", [clash.from_account])
			  );
	const before =
		clash.kind === "split"
			? plural(
					count,
					__("It went to {0}, which is over its trial balance line.", [clash.from_account]),
					__("These went to {0}, which is over its trial balance line.", [clash.from_account])
			  )
			: clash.kind === "posted"
			? plural(
					count,
					__("Earlier sheets put it on {0}.", [clash.from_account]),
					__("Earlier sheets put them on {0}.", [clash.from_account])
			  )
			: clash.earlier && clash.saved
			? __("Their earlier invoices and saved account are on {0}.", [clash.account])
			: clash.saved
			? record_uses
			: __("Their earlier invoices are on {0}.", [clash.account]);
	return $(`<div class="flex flex-col gap-2"></div>`).append(
		$(`<div class="text-base-medium text-ink-gray-8"></div>`).text(
			clash.kind === "split"
				? count === 1
					? __("{0} of {1} is on {2}", [invoices, sheet, clash.from_account])
					: __("{0} of {1} are on {2}", [invoices, sheet, clash.from_account])
				: clash.kind === "posted"
				? count === 1
					? __("{0} already posted is on another account", [invoices])
					: __("{0} already posted are on another account", [invoices])
				: count === 1
				? __("{0} in this sheet goes to a different account than before", [invoices])
				: __("{0} in this sheet go to a different account than before", [invoices])
		),
		$(`<div class="text-p-sm text-ink-gray-6"></div>`).text(goes_to + " " + before),
		$(`<div class="flex flex-col"></div>`).append(
			clash.parties.map((party) =>
				$(`<div class="flex flex-col gap-0.5 py-1.5 border-b text-p-sm"></div>`).append(
					$(`<div class="flex items-center justify-between gap-3"></div>`).append(
						$(`<span class="text-ink-gray-8 truncate"></span>`).text(party.party),
						$(`<span class="text-ink-gray-8 shrink-0"></span>`).text(
							format_currency(party.amount, currency)
						)
					),
					$(`<span class="text-ink-gray-5 truncate"></span>`).text(
						[
							count_invoices(party.count, party_type),
							clash.kind !== "draft" && party.sources.map(source_name).join(", "),
						]
							.filter(Boolean)
							.join(" · ")
					)
				)
			)
		)
	);
}

// Invoices dated in a year ERPNext doesn't have: add that year and keep their dates, or post them on
// the day before migration, where their due dates move too
function make_old_dates_choice(dates, party_type, currency, day_before, on_change) {
	const user = (date) => frappe.datetime.str_to_user(date);
	const rows = dates.no_year_rows;
	const one = dates.no_year === 1;
	const years = dates.missing_years.join(", ");
	const lines = {
		keep: one
			? __(
					"Adds the financial year {0}. It keeps its own date and due date, so ageing shows how overdue it is.",
					[years]
			  )
			: __(
					"Adds the financial years {0}. Each keeps its own date and due date, so ageing shows how overdue it is.",
					[years]
			  ),
		move: one
			? __("It goes on {0} and its due date moves there too, so it looks current in ageing.", [
					day_before,
			  ])
			: __("They go on {0} and their due dates move there too, so they look current in ageing.", [
					day_before,
			  ]),
	};
	const $line = grey_line(lines.keep);
	const pick = frappe.ui.tab_buttons({
		label: __("Date"),
		options: [
			{ label: one ? __("Keep its own date") : __("Keep their own dates"), value: "keep" },
			{ label: __("Post on {0}", [day_before]), value: "move" },
		],
		value: "keep",
		on_change: (value) => {
			$line.text(lines[value]);
			on_change(value === "keep");
		},
	});
	const shown = rows.slice(0, 3);
	return $(`<div class="flex flex-col gap-2 mt-4"></div>`).append(
		$(`<div class="text-base-medium text-ink-gray-8"></div>`).text(
			one
				? __("{0} is from a financial year not set up yet ({1})", [
						count_invoices(1, party_type),
						dates.missing_years.join(", "),
				  ])
				: __("{0} are from financial years not set up yet ({1})", [
						count_invoices(dates.no_year, party_type),
						dates.missing_years.join(", "),
				  ])
		),
		one
			? []
			: $(`<div class="text-p-sm text-ink-gray-6"></div>`).text(
					__("{0} · dated {1} to {2}", [
						format_currency(dates.no_year_amount, currency),
						user(rows[0].invoice_date),
						user(rows[rows.length - 1].invoice_date),
					])
			  ),
		$(`<div class="flex flex-col"></div>`).append(
			shown.map((row) =>
				$(
					`<div class="flex items-center justify-between gap-3 py-1.5 border-b text-p-sm"></div>`
				).append(
					$(`<span class="text-ink-gray-8 truncate"></span>`).text(
						[row.party, row.invoice_number].filter(Boolean).join(" · ")
					),
					$(`<span class="text-ink-gray-5 shrink-0"></span>`).text(
						user(row.invoice_date) + " · " + format_currency(row.amount, currency)
					)
				)
			),
			rows.length > shown.length
				? grey_line(__("and {0} more, the oldest first", [rows.length - shown.length])).addClass(
						"py-1.5"
				  )
				: []
		),
		$(`<div class="mt-1"></div>`).append(pick.$el || pick),
		$line
	);
}

// What to do about a ledger going over its line: the tick below that moves rows off it, else moving
// its rows in the window
export function ledger_over_title(frm, over, currency) {
	return over.sheet === null
		? __("{0} has no line in your trial balance", [short_account(frm, over.account)])
		: __("{0} would be {1} over its trial balance line", [
				short_account(frm, over.account),
				format_currency(over.entered - flt(over.sheet), currency),
		  ]);
}

function ledger_over_hint(frm, party_type, account, fixes) {
	const tick = fixes.find(
		({ clash, check }) => clash.from_account === account && clash.kind !== "posted" && !check.get_value()
	);
	if (tick) return __('Tick "{0}" below.', [clash_fix_label(tick.clash, party_type)]);
	return __("Tick the rows that belong to another ledger, pick it, and Apply.");
}

// The ledgers the drafts would put above their trial balance line while another is short, as the
// server checks it, with the account fixes ticked moving their amounts and moved ({account: amount})
// the window's changes not saved yet
export function get_ledger_overs(frm, party_type, ticked = [], disabled = new Set(), moved = {}) {
	const match = frm.doc.__onload?.sheet_match?.[party_type];
	if (!match?.by_account) return [];
	const lines = {};
	// only the drafts that post count: not those that stay drafts, as the server leaves them out
	for (const line of match.lines) {
		const stays =
			flt(line.waiting) +
			Object.entries(line.disabled || {})
				.filter(([party]) => disabled.has(party))
				.reduce((sum, [, amount]) => sum + flt(amount), 0);
		lines[line.account] = { ...line, entered: line.entered - stays, draft: flt(line.draft) - stays };
	}
	const line_of = (account) => (lines[account] ||= { account: account, sheet: null, entered: 0, draft: 0 });
	for (const clash of ticked) {
		const amount = clash.parties.reduce((sum, party) => sum + flt(party.amount), 0);
		const from = line_of(clash.from_account);
		const to = line_of(clash.account);
		from.entered -= amount;
		if (clash.kind === "draft") from.draft -= amount;
		to.entered += amount;
		to.draft += amount;
	}
	for (const [account, amount] of Object.entries(moved)) {
		line_of(account).entered += amount;
		line_of(account).draft += amount;
	}
	const all = Object.values(lines);
	const short = all.filter((line) => line.sheet !== null && line.sheet - line.entered >= 1);
	if (!short.length) return [];
	return all
		.filter((line) => Math.abs(flt(line.draft)) >= 0.005 && line.entered - flt(line.sheet) >= 1)
		.map((line) => ({ ...line, short: short }));
}

// a minus row is an advance, or a credit the party is owed back, worded as the country says it
function credit_rows_label(is_customer) {
	if (is_customer) return __("Credits or advances");
	return __("Debits or advances");
}

// Whether a draft row is one an opening invoice made by hand stands for, as the server found it
export function make_hand_posted_test(names) {
	const key = (party, number, amount) => [party || "", number || "", flt(amount, 2)].join("\u0001");
	const keys = new Set(
		(names.hand_posted || []).map((row) => key(row.party, row.invoice_number, row.amount))
	);
	return (row) => keys.has(key(row.party, row.invoice_number, row.outstanding_amount));
}

export function invoice_word(party_type) {
	return party_type === "Customer" ? __("invoice") : __("bill");
}

export function count_invoices(count, party_type) {
	if (party_type === "Customer") {
		return plural(count, __("1 invoice"), __("{0} invoices", [count]));
	}
	return plural(count, __("1 bill"), __("{0} bills", [count]));
}

// Like "4 invoices stay as drafts (₹ 65,52,970): 2 in USD, 2 for a disabled customer"
export function get_left_out_text(left_out, money) {
	const party_type = left_out.party_types.length === 1 ? left_out.party_types[0] : null;
	const what = party_type
		? count_invoices(left_out.count, party_type)
		: __("{0} invoices and bills", [left_out.count]);
	const reasons = [
		...Object.entries(left_out.currencies).map(([code, count]) => __("{0} in {1}", [count, code])),
		left_out.disabled &&
			(party_type === "Customer"
				? __("{0} for a disabled customer", [left_out.disabled])
				: party_type === "Supplier"
				? __("{0} for a disabled supplier", [left_out.disabled])
				: __("{0} for a disabled customer or supplier", [left_out.disabled])),
		left_out.unconfirmed &&
			(party_type === "Customer"
				? __("{0} with no customer confirmed yet", [left_out.unconfirmed])
				: party_type === "Supplier"
				? __("{0} with no supplier confirmed yet", [left_out.unconfirmed])
				: __("{0} with no customer or supplier confirmed yet", [left_out.unconfirmed])),
	].filter(Boolean);
	return plural(
		left_out.count,
		__("{0} stays as a draft ({1}): {2}.", [what, money(left_out.amount), reasons.join(", ")]),
		__("{0} stay as drafts ({1}): {2}.", [what, money(left_out.amount), reasons.join(", ")])
	);
}

// Like "39 invoices + 1 credit": count is every row, the rows below zero among them
export function count_entries(count, advances, party_type) {
	const parts = [
		count - advances && count_invoices(count - advances, party_type),
		advances && count_advances(advances, party_type),
	].filter(Boolean);
	return parts.length === 2 ? __("{0} + {1}", parts) : parts[0] || count_invoices(0, party_type);
}

function clash_fix_label(clash, party_type) {
	const count = clash.parties.reduce((sum, party) => sum + party.count, 0);
	const invoices = count_invoices(count, party_type);
	if (clash.kind !== "draft") {
		return plural(
			count,
			__("Also move this {0} to {1}", [invoices, clash.account_name]),
			__("Also move these {0} to {1}", [invoices, clash.account_name])
		);
	}
	return plural(
		count,
		__("Post this {0} on {1} instead", [invoices, clash.account_name]),
		__("Post these {0} on {1} instead", [invoices, clash.account_name])
	);
}

function clash_fix_note(clash, party_type, currency) {
	const count = clash.parties.reduce((sum, party) => sum + party.count, 0);
	const short = __("Your trial balance is {0} short there.", [format_currency(clash.short, currency)]);
	const moved = plural(
		count,
		__("It's taken out and posted again on {0} under the same number.", [clash.account_name]),
		__("They're taken out and posted again on {0} under the same numbers.", [clash.account_name])
	);
	if (clash.kind === "split") return moved + " " + short;
	const parties = clash.parties.length;
	return clash.kind === "posted"
		? moved +
				" " +
				short +
				" " +
				(party_type === "Customer"
					? plural(
							parties,
							__("If you leave this off, this customer stays on two accounts."),
							__("If you leave this off, these customers stay on two accounts.")
					  )
					: plural(
							parties,
							__("If you leave this off, this supplier stays on two accounts."),
							__("If you leave this off, these suppliers stay on two accounts.")
					  ))
		: short +
				" " +
				plural(
					count,
					__("If you leave this off, it goes to {0}.", [clash.from_account_name]),
					__("If you leave this off, they go to {0}.", [clash.from_account_name])
				);
}

// Say which new names were added and why any row stays a draft, not only in the table
export function show_posting_result(frm, party_type, new_names = []) {
	const rows = frm.doc[PARTY[party_type].table] || [];
	const failed = rows.filter((row) => row.status === "Failed");
	const added = new_names.filter((name) => rows.some((row) => row.party_name === name && row.party));
	if (!failed.length) {
		frappe.ui.toast({
			type: "success",
			message: added.length
				? __("Posted. Added {0}.", [added.join(", ")])
				: party_type === "Customer"
				? __("Invoices posted.")
				: __("Bills posted."),
		});
		return;
	}
	const $message = $("<div></div>");
	if (added.length) {
		$(`<p></p>`)
			.text(
				party_type === "Customer"
					? __("New customers added: {0}.", [added.join(", ")])
					: __("New suppliers added: {0}.", [added.join(", ")])
			)
			.appendTo($message);
	}
	$(`<p></p>`)
		.text(__("The rest are posted. Fix the reason below, then post again: only these rows are tried."))
		.appendTo($message);
	const $list = $(`<ul class="mb-0"></ul>`).appendTo($message);
	for (const row of failed) {
		$("<li></li>")
			.text(`${row.party_name}${row.invoice_number ? ` (${row.invoice_number})` : ""}: ${row.error}`)
			.appendTo($list);
	}
	frappe.msgprint({
		title:
			party_type === "Customer"
				? plural(
						failed.length,
						__("1 invoice could not be posted"),
						__("{0} invoices could not be posted", [failed.length])
				  )
				: plural(
						failed.length,
						__("1 bill could not be posted"),
						__("{0} bills could not be posted", [failed.length])
				  ),
		message: $message.html(),
		indicator: "red",
	});
}
