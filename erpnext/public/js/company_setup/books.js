import {
	call_method,
	count_balances,
	get_data,
	grey_line,
	make_alerts,
	make_posting_reds,
	make_title_cell,
	money,
	plural,
	reload_fresh,
	short_account,
} from "./common";
import { get_sheet_check, get_steps, get_waiting_text, show_tab } from "./overview";
import { close_year, get_year_close_steps } from "./close_year";
import { fiscal_year_text } from "./balances";
import { show_opening_invoices } from "./invoices";
import { get_left_out_text } from "./invoice_posting";
import { show_bank_accounts } from "./bank";
import { show_opening_stock } from "./stock";
import { HAND_AFTER_TEXT, show_fixed_assets } from "./assets";

export const REPORTS = [
	["Balance Sheet", __("Balance Sheet")],
	["Profit and Loss Statement", __("Profit and Loss Statement")],
	["Trial Balance", __("Trial Balance")],
	["General Ledger", __("General Ledger")],
];

// The opening entries are dated the day before migration, in last year: the reports open on that year
export function report_filters(frm, report) {
	const period = frm.doc.__onload?.report_period;
	const filters = { company: frm.doc.company };
	if (!period) return filters;
	if (report === "Trial Balance") {
		return {
			...filters,
			fiscal_year: period.fiscal_year,
			from_date: period.from_date,
			to_date: period.to_date,
		};
	}
	if (report === "General Ledger") {
		return { ...filters, from_date: period.from_date, to_date: period.to_date, show_opening_entries: 1 };
	}
	return {
		...filters,
		filter_based_on: "Fiscal Year",
		from_fiscal_year: period.fiscal_year,
		to_fiscal_year: period.fiscal_year,
		periodicity: "Yearly",
	};
}

export function has_posted(frm) {
	return (
		(frm.doc.opening_balances || []).some((row) => row.status === "Posted") ||
		[...(frm.doc.opening_sales_invoices || []), ...(frm.doc.opening_purchase_invoices || [])].some(
			(row) => row.status === "Created"
		)
	);
}

// Anything entered but not posted yet: only then is there a preview to see
export function has_drafts(frm) {
	return (
		(frm.doc.opening_balances || []).some((row) => row.status !== "Posted") ||
		[...(frm.doc.opening_sales_invoices || []), ...(frm.doc.opening_purchase_invoices || [])].some(
			(row) => row.status !== "Created"
		)
	);
}

export function see_books_button(frm) {
	return {
		label: __("See your books"),
		disabled: !frm.doc.migration_date,
		tooltip: frm.doc.migration_date ? undefined : __("Set the migration date first."),
		onclick: () => show_posting_preview(frm, false),
	};
}

// What the books need beyond the steps: last year's profit closed, and the Company's default
// accounts set; for Check your books and Go live
export function get_books_reminders(frm) {
	const reminders = frm.doc.__onload?.books_reminders || {};
	// ERPNext closes a year only after the one before it: the years with a profit each get a line,
	// and the button on the oldest closes the years before it with entries but no profit too
	const years = frm.year_close ? [] : reminders.unclosed_years || [];
	const first = years.findIndex((year) => flt(year.profit, 2));
	const alerts = years
		.filter((year) => flt(year.profit, 2))
		.map((year, index) => {
			const steps = index ? [] : get_year_close_steps(years.slice(0, first + 1));
			const blocker = years.slice(0, first + 1).find((y) => y.stock?.needed && !y.stock.done)
				?.stock.blocker;
			const drafts = year.drafts?.length
				? __("Can't close yet: {0} still drafts.", [year.drafts.join(" · ")])
				: null;
			return {
				theme: "yellow",
				title:
					year.profit > 0
						? __("Last year's profit is not closed")
						: __("Last year's loss is not closed"),
				description: [
					__(
						"{0} has {1} still in income and expenses, so the Balance Sheet shows the year as not closed. Close it once every opening entry is posted: entries on or before {2} can't be posted or undone after.",
						[
							year.fiscal_year,
							profit_words(frm, year.profit),
							frappe.datetime.str_to_user(year.year_end_date),
						]
					),
					index ? null : blocker,
					index ? null : drafts,
				]
					.filter(Boolean)
					.join(" "),
				buttons:
					index || blocker
						? []
						: [
								{
									label: __("Close {0}", [year.fiscal_year]),
									disabled: Boolean(drafts),
									tooltip: drafts,
									onclick: () => close_year(frm, year, steps),
								},
						  ],
			};
		});
	const gaps = reminders.default_account_gaps || [];
	if (gaps.length) {
		alerts.push({
			theme: "yellow",
			title: plural(
				gaps.length,
				__("1 default account of the company needs a look"),
				__("{0} default accounts of the company need a look", [gaps.length])
			),
			description: gaps.map((gap) => __("{0}: {1}", [gap.label, gap.reason])).join(" · "),
			buttons: [
				{
					label: __("Open company"),
					onclick: () => frappe.set_route("Form", "Company", frm.doc.company),
				},
			],
		});
	}
	for (const line of frm.doc.__onload?.bank?.statement_off_short || []) {
		alerts.push({ theme: "yellow", title: line });
	}
	return alerts;
}

// A year's profit or loss as words, like "a loss of ₹ 20,66,837"
export function profit_words(frm, profit) {
	return profit > 0
		? __("a profit of {0}", [money(frm, profit)])
		: __("a loss of {0}", [money(frm, Math.abs(profit))]);
}

// The books on the day before the migration date; with can_post, as they will be after posting
// on_close: called when the window closes, unless it closed to open another step's window
export function show_posting_preview(frm, can_post = true, on_close = null) {
	get_data(frm, "preview_opening_balances").then((r) => {
		const preview = r.message;
		const currency = erpnext.get_currency(frm.doc.company);
		const dialog = new frappe.ui.Dialog({
			title: __("Your books on {0}", [frappe.datetime.str_to_user(preview.posting_date)]),
			size: "extra-large",
			fields: [{ fieldname: "preview", fieldtype: "HTML" }],
			primary_action_label: can_post ? __("Post to books") : __("Done"),
			primary_action: () =>
				can_post
					? call_method(frm, "post_opening_balances", null, { freeze: true }).then(() =>
							reload_fresh(frm).then(() => dialog.hide())
					  )
					: dialog.hide(),
			secondary_action_label: can_post ? __("Back") : undefined,
			secondary_action: can_post ? () => dialog.hide() : undefined,
		});
		if (can_post) {
			dialog
				.get_field("preview")
				.$wrapper.append(make_posting_reds(frm, frm.doc.__onload?.posting_reds?.balances));
		}
		if (!can_post) dialog.get_field("preview").$wrapper.append(make_books_checks(frm, dialog));
		if (on_close) dialog.onhide = () => !dialog.opened_other && on_close();
		dialog
			.get_field("preview")
			.$wrapper.append(make_posting_preview(preview, currency, frm, can_post ? null : dialog));
		dialog.show();
	});
}

// What waits on Temporary Opening: customers, suppliers and stock still to come explain it, or a
// balance may be missing from the sheet
function make_temporary_sum(preview, temporary, money) {
	const waiting = preview.waiting || {};
	const expected = flt(waiting.Receivable) - flt(waiting.Payable) + flt(waiting.Stock);
	const unexplained = flt(temporary.balance - expected, 2);
	if (Math.abs(unexplained) >= 1) {
		return frappe.ui.alert({
			theme: "yellow",
			title:
				unexplained > 0
					? __("{0} more than customers, suppliers and stock explain", [money(unexplained)])
					: __("{0} less than customers, suppliers and stock explain", [money(-unexplained)]),
			description: __(
				"A balance may be missing from your sheet. Check the trial balance, or post anyway: it waits in Temporary Opening."
			),
		});
	}
	// below zero, more is posted than the trial balance has
	const lines = [
		[__("Customers owe you"), __("Customer invoices more than the trial balance"), waiting.Receivable, 1],
		[__("You owe suppliers"), __("Supplier bills more than the trial balance"), waiting.Payable, -1],
		[__("Stock"), __("Stock more than the trial balance"), waiting.Stock, 1],
	]
		.filter(([, , amount]) => flt(amount, 2))
		.map(([label, over_label, amount, sign]) => [
			amount < 0 ? over_label : label,
			Math.abs(amount),
			amount < 0 ? 1 : sign,
		]);
	const amounts = [waiting.Receivable, waiting.Payable, waiting.Stock].filter((amount) => flt(amount, 2));
	const over = amounts.some((amount) => amount < 0);
	const all_over = amounts.length && amounts.every((amount) => amount < 0);
	const $table = $(`<div class="flex flex-col border rounded-lg"></div>`).append(
		lines.map(([label, amount, sign], index) =>
			$(
				`<div class="flex justify-between gap-3 px-3 py-2 text-base ${
					index ? "border-t" : ""
				}"></div>`
			).append(
				$(`<span class="text-ink-gray-6"></span>`).text(label),
				$(`<span class="text-ink-gray-8"></span>`).text(
					sign < 0 ? `− ${money(amount)}` : money(amount)
				)
			)
		),
		$(
			`<div class="flex justify-between gap-3 px-3 py-2 text-base-medium text-ink-gray-8 border-t"></div>`
		).append(
			$(`<span></span>`).text(
				all_over ? __("More than the trial balance") : __("Waits in Temporary Opening")
			),
			$(`<span class="flex items-center gap-1"></span>`).append(
				$(`<span></span>`).text(money(Math.abs(temporary.balance))),
				$(`<span class="flex text-ink-green-7"></span>`)
					.attr("title", __("As expected"))
					.html(frappe.utils.icon("check", "sm"))
			)
		)
	);
	return $(`<div class="flex flex-col gap-2"></div>`).append(
		$table,
		grey_line(
			over
				? __("Check the invoices, bills or stock over your trial balance, or the trial balance.")
				: __("It goes to zero once the opening invoices, bills and stock are posted.")
		)
	);
}

// With the Check your books dialog, Temporary Opening is the Overview card's figure
function make_posting_preview(preview, currency, frm, books_dialog) {
	const company = frm.doc.company;
	const temporary = preview.rows.find((row) => row.account === preview.temporary_account);
	const rows = preview.rows.filter((row) => row !== temporary);
	const of = (...root_types) => rows.filter((row) => root_types.includes(row.root_type));
	const sum = (list, sign = 1) => list.reduce((total, row) => total + sign * row.balance, 0);
	const income = sum(of("Income"), -1);
	const expenses = sum(of("Expense"));
	const profit = income - expenses;
	const money = (value) => format_currency(value, currency);
	// each account on its own side, so depreciation and provisions show as minus lines under assets
	const debits = of("Asset");
	const credits = of("Liability", "Equity");

	const $body = $(`<div class="flex flex-col gap-3"></div>`);
	$(`<div class="text-p-sm text-ink-gray-6"></div>`)
		.text(
			preview.drafts && preview.draft_invoices
				? __("{0} and {1} aren't posted yet. This is how your books will look once they are.", [
						count_balances(preview.drafts),
						plural(
							preview.draft_invoices,
							__("1 invoice or bill"),
							__("{0} invoices and bills", [preview.draft_invoices])
						),
				  ])
				: preview.drafts
				? preview.drafts === 1
					? __("1 balance isn't posted yet. This is how your books will look once it is.")
					: __("{0} balances aren't posted yet. This is how your books will look once they are.", [
							preview.drafts,
					  ])
				: preview.draft_invoices === 1
				? __("1 invoice or bill isn't posted yet. This is how your books will look once it is.")
				: preview.draft_invoices
				? __(
						"{0} invoices and bills aren't posted yet. This is how your books will look once they are.",
						[preview.draft_invoices]
				  )
				: __("Your books as they are, with everything posted so far.")
		)
		.append(books_dialog ? "" : ` ${fiscal_year_text(frm)}`)
		.appendTo($body);
	const left_out = preview.left_out?.count && get_left_out_text(preview.left_out, money);
	const party_type = preview.left_out?.party_types?.[0];
	const open_invoices = {
		label: party_type === "Supplier" ? __("Open bills") : __("Open invoices"),
		onclick: () => {
			if (books_dialog) books_dialog.opened_other = true;
			books_dialog?.hide();
			show_tab(frm, party_type === "Customer" ? "selling_tab" : "buying_tab");
			show_opening_invoices(frm, party_type);
		},
	};
	const posted_temporary = flt(frm.doc.__onload?.stats?.temporary_opening);
	const over = (frm.doc.__onload?.temporary_parts || []).filter((part) => part.over);
	if (books_dialog && posted_temporary) {
		$body.append(
			make_alerts([
				{
					theme: "yellow",
					title: __("Temporary Opening is {0}", [money(Math.abs(posted_temporary))]),
					description:
						left_out ||
						(over.length
							? __("{0}. Check them or the trial balance.", [get_waiting_text(over, currency)])
							: __(
									"It waits there until your customer invoices, supplier bills and opening stock, or a balance missing from your sheet, are in. Then it goes to zero."
							  )),
					buttons: left_out ? [open_invoices] : [],
				},
			])
		);
	} else if (left_out) {
		$body.append(
			make_alerts([{ theme: "yellow", title: left_out, buttons: books_dialog ? [open_invoices] : [] }])
		);
	}
	if (temporary && !books_dialog) $body.append(make_temporary_sum(preview, temporary, money));

	const tabs = [
		{
			label: __("Balance Sheet"),
			report: "Balance Sheet",
			content: () =>
				$(`<div class="flex gap-6 pt-3"></div>`).append(
					make_statement_side(
						__("Assets"),
						debits,
						1,
						money,
						temporary?.balance > 0 ? [[__("Temporary Opening"), temporary.balance, true]] : []
					),
					make_statement_side(
						__("Liabilities and Equity"),
						credits,
						-1,
						money,
						[
							profit && [__("Profit so far"), profit],
							temporary?.balance < 0 && [__("Temporary Opening"), -temporary.balance, true],
						].filter(Boolean)
					)
				),
		},
		{
			label: __("Trial Balance"),
			report: "Trial Balance",
			content: () => make_trial_balance(preview.rows, money),
		},
	];
	if (of("Income", "Expense").length) {
		tabs.splice(1, 0, {
			label: __("Profit and Loss"),
			report: "Profit and Loss Statement",
			content: () =>
				$(`<div class="flex gap-6 pt-3"></div>`).append(
					make_statement_side(__("Income"), of("Income"), -1, money),
					make_statement_side(__("Expenses"), of("Expense"), 1, money, [
						[profit >= 0 ? __("Profit") : __("Loss"), Math.abs(profit)],
					])
				),
		});
	}
	// each statement links to the full report it stands for, which shows only what is posted
	const all_posted = !preview.drafts && !preview.draft_invoices;
	for (const tab of all_posted ? tabs : []) {
		const make_content = tab.content;
		tab.content = () =>
			$(`<div class="flex flex-col"></div>`).append(
				$(`<div class="flex justify-end pt-2"></div>`).append(
					frappe.ui.button({
						label: __("Open full report"),
						icon_right: "arrow-up-right",
						variant: "ghost",
						size: "sm",
						onclick: () => {
							frappe.set_route("query-report", tab.report, report_filters(frm, tab.report));
						},
					})
				),
				make_content()
			);
	}
	return $body.append(frappe.ui.tabs({ tabs: tabs }));
}

// One side of a statement: accounts under their parent group, extra lines, then the total
function make_statement_side(title, rows, sign, money, extra_lines = []) {
	const $side = $(`<div class="flex flex-col min-w-0 flex-1"></div>`);
	$(`<div class="text-sm-medium text-ink-gray-8 pb-2 border-b"></div>`).text(title).appendTo($side);
	// a group's accounts together, even when the chart puts a subgroup between them
	const groups = new Map();
	for (const row of rows) {
		if (!groups.has(row.parent)) groups.set(row.parent, []);
		groups.get(row.parent).push(row);
	}
	let total = 0;
	for (const [parent, group_rows] of groups) {
		grey_line(parent || "")
			.addClass("pt-2")
			.appendTo($side);
		for (const row of group_rows) {
			total += sign * row.balance;
			$side.append(make_statement_line(row.account_name, money(sign * row.balance)));
		}
	}
	for (const [label, value, warn] of extra_lines) {
		total += value;
		$side.append(make_statement_line(label, money(value), warn));
	}
	$side.append(make_statement_line(__("Total"), money(total)).addClass("border-t text-sm-medium mt-2"));
	return $side;
}

function make_statement_line(label, value, warn) {
	const $line = $(`<div class="flex justify-between gap-3 py-1.5 text-p-sm text-ink-gray-8"></div>`);
	if (warn) $line.addClass("text-ink-amber-8");
	// a contra account, like depreciation under assets, reads as a minus line
	else if (value.includes("-")) $line.addClass("text-ink-gray-6");
	return $line.append(
		$(`<span class="truncate"></span>`).text(label),
		$(`<span class="shrink-0"></span>`).text(value)
	);
}

function make_trial_balance(rows, money) {
	const $table = $(`<div class="flex flex-col pt-3"></div>`);
	const $head = make_trial_balance_line(__("Account"), __("Debit"), __("Credit")).appendTo($table);
	$head.addClass("text-ink-gray-5 border-b pb-2");
	let debit = 0;
	let credit = 0;
	for (const row of rows) {
		const [dr, cr] = row.balance > 0 ? [row.balance, 0] : [0, -row.balance];
		debit += dr;
		credit += cr;
		const name = row.account_number ? `${row.account_number} - ${row.account_name}` : row.account_name;
		$table.append(make_trial_balance_line(name, dr ? money(dr) : "", cr ? money(cr) : ""));
	}
	$table.append(
		make_trial_balance_line(__("Total"), money(debit), money(credit)).addClass(
			"border-t text-sm-medium mt-2"
		)
	);
	return $table;
}

function make_trial_balance_line(account, debit, credit) {
	return $(`<div class="flex gap-3 py-1.5 text-p-sm text-ink-gray-8"></div>`).append(
		$(`<span class="grow truncate"></span>`).text(account),
		$(`<span class="company-setup-amount"></span>`).text(debit),
		$(`<span class="company-setup-amount"></span>`).text(credit)
	);
}

// Check your books: each bank against its line, stock by line, and the asset register
function make_books_checks(frm, dialog) {
	const onload = frm.doc.__onload || {};
	const currency = erpnext.get_currency(frm.doc.company);
	const rows = [];
	const bank = onload.bank_match;
	if (bank?.total) {
		const lines_match = bank.matched === bank.total;
		// the lines match, but a statement balance doesn't: a yellow, as on the Bank accounts card
		const statement_off = lines_match ? bank.statement_off || [] : [];
		// as Go live says: the lines match, but the Bank accounts step is not done
		const step = get_steps(frm).find((step) => step.name === "bank_accounts");
		const step_open = lines_match && !statement_off.length && step && !step.is_complete;
		const matches = lines_match && !statement_off.length && !step_open;
		rows.push({
			title: __("Bank"),
			line: step_open
				? step.summary || __("Bank accounts is not done")
				: statement_off.length
				? statement_off.length === 1
					? __("{0} of {1} off by {2} from its statement", [
							1,
							bank.total,
							format_currency(Math.abs(statement_off[0].off), statement_off[0].currency, 0),
					  ])
					: __("{0} of {1} off from their statements", [statement_off.length, bank.total])
				: bank.total === 1
				? lines_match
					? __("1 of 1 account matches its line")
					: __("0 of 1 account matches its line")
				: __("{0} of {1} accounts match their lines", [bank.matched, bank.total]),
			badge: matches
				? { label: __("Matches"), theme: "green" }
				: step_open
				? { label: __("Not done"), theme: "amber" }
				: {
						label: __("{0} off", [statement_off.length || bank.total - bank.matched]),
						theme: "amber",
				  },
			open: !matches && (() => (show_tab(frm, "accounts_tab"), show_bank_accounts(frm))),
		});
	}
	const stock = onload.stock_match;
	if (stock?.line) {
		rows.push({
			title: __("Stock"),
			line: [
				stock.difference_posted
					? __("Books {0} + difference {1}", [
							money(frm, stock.books),
							money(frm, stock.difference_posted),
					  ])
					: __("Books {0}", [money(frm, stock.books)]),
				__("trial balance {0}", [money(frm, stock.line)]),
				!stock.matches &&
					stock.lines_off?.length &&
					__("off on {0}", [
						stock.lines_off
							.map((account) => short_account(frm, account) || __("Stock"))
							.join(", "),
					]),
			]
				.filter(Boolean)
				.join(" · "),
			badge: stock.matches
				? { label: __("Matches"), theme: "green" }
				: {
						label: __("Off by {0}", [format_currency(Math.abs(stock.off), currency, 0)]),
						theme: "amber",
				  },
			open: !stock.matches && (() => (show_tab(frm, "stock_tab"), show_opening_stock(frm))),
		});
	}
	if (get_steps(frm).some((step) => step.name === "fixed_assets")) {
		const assets = onload.asset_match;
		const hand_after = assets?.hand_after;
		rows.push({
			title: __("Fixed assets"),
			line: hand_after
				? [HAND_AFTER_TEXT(), assets.hand_after_entries.map((entry) => entry.name).join(", ")].join(
						" "
				  )
				: assets
				? assets.matches
					? __("Register and lines match, as on today · cost {0} · depreciation {1}", [
							money(frm, assets.cost),
							money(frm, assets.depreciation),
					  ])
					: __("Register {0} · lines {1}, as on today", [
							money(frm, assets.cost),
							money(frm, assets.books_cost),
					  ])
				: __("Not added yet"),
			badge: !assets
				? { label: __("Not added"), theme: "gray" }
				: assets.matches && !hand_after
				? { label: __("Matches"), theme: "green" }
				: { label: __("Off"), theme: "amber" },
			open:
				!(assets && assets.matches && !hand_after) &&
				(() => (show_tab(frm, "assets_tab"), show_fixed_assets(frm))),
		});
	}
	// each ledger as the Selling and Buying cards check it
	for (const [party_type, title, tab] of [
		["Customer", __("Customers owe you"), "selling_tab"],
		["Supplier", __("You owe suppliers"), "buying_tab"],
	]) {
		const check = get_sheet_check(frm, party_type);
		if (!check) continue;
		const matches = ["green", "blue"].includes(check.theme);
		rows.push({
			title: title,
			line: check.lines.length
				? check.lines
						.map((line) => {
							const gap = flt(line.sheet - line.entered, 2);
							return !check.is_off(line)
								? check.short_name(line.account)
								: line.sheet === null
								? __("{0} not in your trial balance", [check.short_name(line.account)])
								: gap > 0
								? __("{0} {1} missing", [check.short_name(line.account), check.money(gap)])
								: __("{0} {1} more", [check.short_name(line.account), check.money(-gap)]);
						})
						.join(" · ")
				: __("Checked in total: your trial balance has one line for them"),
			badge: { label: check.theme === "green" ? __("Matches") : check.label, theme: check.theme },
			open: !matches && (() => (show_tab(frm, tab), show_opening_invoices(frm, party_type))),
		});
	}
	if (!rows.length) return [];
	return $(`<div class="flex flex-col border rounded-lg mb-3"></div>`).append(
		rows.map((row, index) => {
			const $row = $(`<div class="flex items-center gap-3 px-3 py-2.5"></div>`)
				.toggleClass("border-t", index > 0)
				.append(make_title_cell(row.title, row.line, "text-ink-gray-8"), frappe.ui.badge(row.badge));
			if (row.open) {
				$row.addClass("company-setup-step cursor-pointer rounded-lg")
					.attr({ role: "button", tabindex: 0 })
					.append(
						$(`<span class="shrink-0 text-ink-gray-5"></span>`).html(
							frappe.utils.icon("chevron-right", "sm")
						)
					)
					.on("click", () => {
						dialog.opened_other = true;
						dialog.hide();
						row.open();
					});
			}
			return $row;
		})
	);
}
