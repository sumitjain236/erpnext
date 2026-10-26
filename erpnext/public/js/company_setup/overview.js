import {
	ORDER_TYPES,
	call_method,
	cheque_words,
	download_sheet,
	get_data,
	grey_line,
	make_alerts,
	make_button,
	make_more_menu,
	make_view_button,
	reload_fresh,
} from "./common";
import { show_migration_date } from "./company";
import {
	REPORTS,
	get_books_reminders,
	has_drafts,
	has_posted,
	report_filters,
	see_books_button,
	show_posting_preview,
} from "./books";
import { get_year_close_summary, make_year_close_alert } from "./close_year";
import { show_go_live } from "./go_live";
import { show_opening_balances } from "./balances";
import {
	UNDONE_TABLES,
	invoice_buttons,
	invoice_menu,
	show_posted,
	show_undo_result,
} from "./invoices_posted";
import { show_opening_invoices } from "./invoices";
import { count_entries, count_invoices, show_posting_result } from "./invoice_posting";
import { import_blocked, import_buttons, items_import_menu, open_party_import } from "./imports";
import { show_chart, show_chart_import } from "./chart";
import { bank_menu, show_bank_accounts, show_cheques_posted } from "./bank";
import { show_taxes } from "./taxes";
import { show_warehouses } from "./warehouses";
import { show_opening_stock, show_stock_corrected } from "./stock";
import { show_job_worker_stock, show_posted_stock } from "./posted";
import {
	bom_buttons,
	bom_menu,
	count_orders,
	order_buttons,
	order_menu,
	show_boms,
	show_boms_made,
	show_open_orders,
} from "./orders";
import { get_hand_after_alerts, show_fixed_assets, show_posted_assets } from "./assets";

export function make_page(frm, fieldname) {
	const $wrapper = frm.get_field(fieldname).$wrapper.empty();
	// 16px above, between and below the cards: the tabs already leave 10px, pt-1.5 adds 6
	$wrapper.closest(".section-body").addClass("pt-0");
	return $(`<div class="flex flex-col gap-4 pt-1.5 mb-4"></div>`).appendTo($wrapper);
}

export function get_steps(frm) {
	const details = frm.doc.__onload?.steps || {};
	// the saved status waits for check_steps; the one worked out on load is the current one
	const status = (row) => details[row.step_key]?.status || row.status;
	return (frm.doc.steps || []).map((row) => ({
		key: row.step_key,
		name: row.step_key.split(".").pop(),
		title: details[row.step_key]?.title || row.step,
		description: details[row.step_key]?.description,
		summary: details[row.step_key]?.summary,
		warnings: details[row.step_key]?.warnings || [],
		blocker: details[row.step_key]?.blocker,
		tab: details[row.step_key]?.tab,
		is_started: details[row.step_key]?.started,
		is_required: details[row.step_key]?.required,
		is_needed: details[row.step_key]?.needed ?? true,
		is_complete: status(row) === "Done",
		is_skipped: status(row) === "Skipped",
	}));
}

// The roles the steps left need that this user lacks, all named at once
export function make_missing_roles(frm) {
	const missing = frm.doc.__onload?.missing_roles || [];
	if (!missing.length) return [];
	return make_alerts([
		{
			theme: "yellow",
			title: __("To finish setup you also need these roles. Ask an Administrator to add them in User."),
			description: missing
				.map(({ role, steps }) => __("{0} for {1}", [__(role), frappe.utils.comma_and(steps)]))
				.join(" · "),
		},
	]);
}

// What the opening books add up to so far; shown once anything is entered
export function make_overview_stats(frm) {
	const sales = frm.doc.opening_sales_invoices || [];
	const purchases = frm.doc.opening_purchase_invoices || [];
	if (!sales.length && !purchases.length && !(frm.doc.opening_balances || []).length) return [];
	const currency = erpnext.get_currency(frm.doc.company);
	const temporary = frm.doc.__onload?.stats?.temporary_opening || 0;
	const posted =
		(frm.doc.opening_balances || []).some((row) => row.status === "Posted") ||
		[...sales, ...purchases].some((row) => row.status === "Created");
	// what is posted, with the opening invoices made by hand, against the trial balance's line;
	// advances are negative rows: they lower the total but aren't invoices
	const owed = (rows, label, count_label, party_type) => {
		const match = frm.doc.__onload?.sheet_match?.[party_type];
		const outside = match?.outside || {};
		const posted_rows = rows.filter((row) => row.status === "Created");
		const total =
			posted_rows.reduce((sum, row) => sum + flt(row.outstanding_amount), 0) + flt(outside.amount);
		const invoices =
			posted_rows.filter((row) => flt(row.outstanding_amount) > 0).length + (outside.count || 0);
		const sheet = match && format_currency(match.sheet, currency, 0);
		const entered = rows.reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
		if (!posted_rows.length && !outside.count) {
			return frappe.ui.stat_card({
				label: label,
				value: match ? sheet : rows.length ? format_currency(entered, currency, 0) : null,
				caption: match
					? __("In your trial balance · 0 posted")
					: rows.length
					? __("Entered · 0 posted")
					: null,
			});
		}
		const count = count_label(format_number(invoices, null, 0));
		return frappe.ui.stat_card({
			label: label,
			value: format_currency(total, currency, 0),
			caption: match ? __("Posted of {0} · {1}", [sheet, count]) : __("Posted · {0}", [count]),
		});
	};
	const waiting = frm.doc.__onload?.temporary_parts || [];
	const $cards = $(`<div class="es-stat-card-grid company-setup-stats"></div>`).append(
		frappe.ui.stat_card({
			label: __("Temporary Opening"),
			value: format_currency(posted ? Math.abs(temporary) : 0, currency),
			caption: !posted
				? __("Not posted yet")
				: !temporary
				? __("Zero · your books match your trial balance")
				: waiting.length
				? get_waiting_text(waiting, currency)
				: __("Not explained yet. Goes to zero once everything is in."),
			onclick: frm.doc.migration_date ? () => show_posting_preview(frm, false) : undefined,
		}),
		owed(sales, __("Customers owe you"), (count) => count_invoices(count, "Customer"), "Customer"),
		owed(purchases, __("You owe suppliers"), (count) => count_invoices(count, "Supplier"), "Supplier")
	);
	if (!posted || !temporary) return $cards;
	return [
		$cards,
		grey_line(
			__("Temporary Opening holds the difference until everything is posted. It must reach zero.")
		),
	];
}

// "Waiting for stock ₹ 18,40,000 and sales invoices ₹ 4,12,000"; past two parts, only their names.
// Invoices, bills or stock over the trial balance come first: they wait for nothing
export function get_waiting_text(parts, currency) {
	const over = parts.filter((part) => part.over);
	const shown = over.length ? over : parts;
	const named =
		shown.length > 2
			? shown.map((part) => part.label)
			: shown.map((part) =>
					__("{0} {1}", [part.label, format_currency(Math.abs(part.amount), currency, 0)])
			  );
	return over.length
		? __("More than your trial balance: {0}", [frappe.utils.comma_and(named)])
		: __("Waiting for {0}", [frappe.utils.comma_and(named)]);
}

export const TABS = [
	"accounts_tab",
	"selling_tab",
	"buying_tab",
	"stock_tab",
	"assets_tab",
	"manufacturing_tab",
	"subcontracting_tab",
];

export const PARTY = {
	Customer: {
		table: "opening_sales_invoices",
		name_field: "customer_name",
		doctype: "Sales Invoice",
		total_label: __("Receivable"),
		report: __("receivables"),
	},
	Supplier: {
		table: "opening_purchase_invoices",
		name_field: "supplier_name",
		doctype: "Purchase Invoice",
		total_label: __("Payable"),
		report: __("payables"),
	},
};

// Every task with its status, in any order. Each row opens its task; rows are grouped by tab.
export function make_checklist(frm, steps) {
	// Go live is what the steps lead to, so it is not counted
	const counted = steps.filter((step) => step.name !== "go_live");
	const finished = (step) => step.is_complete || step.is_skipped;
	const done = counted.filter(finished).length;
	const required = counted.filter((step) => step.is_required);
	const required_done = required.filter(finished).length;
	const $card = $(`<div class="contact-box flex flex-col gap-1 mb-0" data-company-setup-checklist></div>`);
	$card.append(
		frappe.ui.progress({
			label: __("Set up {0}", [frm.doc.company]),
			hint: () =>
				required_done < required.length
					? __("{0} of {1} steps done · {2} of {3} required", [
							done,
							counted.length,
							required_done,
							required.length,
					  ])
					: __("{0} of {1} steps done", [done, counted.length]),
			intervals: true,
			interval_count: counted.length,
			size: "md",
			value: counted.length ? (done / counted.length) * 100 : 0,
		})
	);
	const first_tab = steps.findIndex((step) => step.tab);
	const seen = new Set();
	let group;
	steps.forEach((step, index) => {
		let label = step.tab ? __(frappe.meta.get_label(frm.doctype, step.tab)) : null;
		// a tab's task after other tabs' tasks, like Check your books, closes the list
		if (!label || (label !== group && seen.has(label))) {
			label = index < first_tab ? __("Start") : __("Finish");
		}
		seen.add(label);
		if (label !== group) {
			group = label;
			$(`<div class="text-sm-medium text-ink-gray-5 pt-3"></div>`).text(label).appendTo($card);
		}
		const closing = step.name === "check_books" && frm.year_close && !frm.year_close.error;
		const $task = make_task(
			frm,
			closing
				? { ...step, is_closing: true, summary: get_year_close_summary(frm) }
				: step.is_needed || step.summary
				? step
				: { ...step, summary: __("Optional · not started") },
			[]
		).addClass("grow min-w-0");
		if (closing) {
			const { done } = frm.year_close;
			const total = frm.year_close.steps.length;
			$task
				.find(".flex-col")
				.first()
				.append(
					$(`<div class="pt-1"></div>`).append(
						frappe.ui.progress({
							intervals: true,
							interval_count: total,
							size: "sm",
							value: (done / total) * 100,
						})
					)
				);
		}
		$(
			`<div class="company-setup-step cursor-pointer flex items-center rounded-md mx-n2 p-2 gap-3" role="button" tabindex="0"></div>`
		)
			.append($task)
			.append(
				$(`<span class="shrink-0 text-ink-gray-5"></span>`).html(
					frappe.utils.icon("chevron-right", "sm")
				)
			)
			.on("click", () => open_step(frm, step))
			.on("keydown", (event) => {
				if (event.key !== "Enter" && event.key !== " ") return;
				event.preventDefault();
				open_step(frm, step);
			})
			.appendTo($card);
	});
	return $card;
}

// A checklist row goes to its tab and opens the task's window there
export function open_step(frm, step) {
	const open = STEP_OPENERS[step.name];
	if (step.tab) show_tab(frm, step.tab);
	open?.(frm, step);
}

const STEP_OPENERS = {
	migration_date: (frm) => show_migration_date(frm),
	chart_of_accounts: (frm) => show_chart(frm),
	opening_balances: (frm) => show_opening_balances(frm),
	opening_sales_invoices: (frm) => show_opening_invoices(frm, "Customer"),
	opening_purchase_invoices: (frm) => show_opening_invoices(frm, "Supplier"),
	bank_accounts: (frm) => show_bank_accounts(frm),
	customers: (frm, step) => !step.is_complete && open_party_import(frm, "Customer"),
	suppliers: (frm, step) => !step.is_complete && open_party_import(frm, "Supplier"),
	check_books: (frm) =>
		has_posted(frm) &&
		(has_drafts(frm)
			? show_posting_preview(frm, false)
			: frappe.set_route("query-report", "Balance Sheet", report_filters(frm, "Balance Sheet"))),
	go_live: (frm, step) => !step.is_complete && show_go_live(frm),
};

// A task on its tab: the same buttons whatever its state, the rest in a menu
export function make_task_card(frm, step) {
	const buttons = (STEP_ACTIONS[step.name]?.(frm, step) || []).filter(Boolean);
	const more = [...(STEP_MORE[step.name]?.(frm, step) || []), ...skip_items(frm, step)];
	if (more.length) buttons.push(make_more_menu(more));
	if (step.name === "chart_of_accounts" && !step.is_complete && frm.doc.__onload?.chart_import_blocked) {
		step = { ...step, description: frm.doc.__onload.chart_import_blocked };
	}
	const background =
		{ opening_stock: "Stock", fixed_assets: "Assets", boms: "BOMs" }[step.name] ||
		(ORDER_TYPES[step.name] && "Orders");
	if (
		frm.posting_step === step.name ||
		(background && (frm.doc.__onload?.posting || []).includes(background))
	) {
		step = { ...step, is_posting: true, summary: __("Posting…") };
	}
	if (step.name === "bank_accounts" && frm.doc.__onload?.bank?.posting) {
		step = {
			...step,
			is_posting: true,
			summary: cheque_words(__("Posting uncleared cheques in the background")),
		};
	}
	const posting_party = { opening_sales_invoices: "Customer", opening_purchase_invoices: "Supplier" }[
		step.name
	];
	if (frm.posting?.[posting_party]) {
		step = { ...step, is_posting: true, summary: get_posting_text(frm, posting_party) };
	}
	const $card = $(`<div class="contact-box mb-0"></div>`)
		.attr("data-company-setup-card", step.name)
		.append(make_task(frm, step, buttons));
	if (step.name === "check_books") {
		if (has_posted(frm)) {
			// the reports show only what is posted
			$card.append(
				$(`<div class="flex flex-wrap items-center gap-2 ps-8 pt-2"></div>`).append(
					// a separator wraps with the link after it
					REPORTS.map(([report, label], index) =>
						$(`<span class="flex items-center gap-2 whitespace-nowrap"></span>`).append(
							index ? $(`<span class="text-p-sm text-ink-gray-4">·</span>`) : [],
							$(`<a class="text-p-sm text-ink-gray-7"></a>`)
								.text(label)
								.attr(
									"href",
									frappe.router.make_url(["query-report", report]) +
										"?" +
										$.param(report_filters(frm, report))
								)
						)
					)
				)
			);
		} else {
			$card
				.find(".text-base-medium")
				.first()
				.removeClass("text-ink-gray-8")
				.addClass("text-ink-gray-5");
		}
		const reminders = get_books_reminders(frm);
		if (reminders.length || frm.year_close) {
			$card.append(
				$(`<div class="flex flex-col gap-3 ps-8 pt-3"></div>`).append(
					make_year_close_alert(frm),
					reminders.length ? make_alerts(reminders) : []
				)
			);
		}
	}
	if (step.name === "fixed_assets" && get_hand_after_alerts(frm).length) {
		$card.append($(`<div class="ps-8 pt-3"></div>`).append(make_alerts(get_hand_after_alerts(frm))));
	}
	if (posting_party && !step.is_posting) {
		add_sheet_match(frm, posting_party, $card);
		add_undone_line(frm, posting_party, $card);
	}
	if (step.is_posting && frm.posting?.[posting_party]) {
		$card.find(".text-p-sm").first().attr("data-posting-note", posting_party);
		$card.append($(`<div class="ps-8 pt-2"></div>`).append(make_posting_bar(frm, posting_party)));
	}
	return $card;
}

// Whether the invoices or bills entered add up to what the trial balance says: a badge by the title,
// and the accounts that don't match under it
function add_sheet_match(frm, party_type, $card) {
	const check = get_sheet_check(frm, party_type);
	if (!check) return;
	const { money, short_name, lines, left_out, is_off, off, label, theme } = check;
	$card
		.find(".text-base-medium")
		.first()
		.addClass("flex flex-wrap items-center gap-2")
		.append(
			frappe.ui.badge({
				label: label,
				theme: theme,
				title: label,
				css_class: "company-setup-fit-badge",
			})
		);
	const is_customer = party_type === "Customer";
	const open = {
		label: party_type === "Customer" ? __("Open invoices") : __("Open bills"),
		onclick: () => show_opening_invoices(frm, party_type),
	};
	const notes = [
		...Object.entries(left_out.currencies).map(([code, part]) => ({
			theme: "yellow",
			title: is_customer
				? __("{0} in {1} invoices, not posted", [money(part.amount), code])
				: __("{0} in {1} bills, not posted", [money(part.amount), code]),
			description: __("They are left out of the totals above. Post them to a {0} ledger.", [code]),
			buttons: [open],
		})),
		flt(left_out.disabled?.amount) && {
			theme: "yellow",
			title: is_customer
				? __("{0} for disabled customers, not posted", [money(left_out.disabled.amount)])
				: __("{0} for disabled suppliers, not posted", [money(left_out.disabled.amount)]),
			description: __("They are left out of the totals above. Post them anyway in the window."),
			buttons: [open],
		},
	].filter(Boolean);
	if (!off.length && !notes.length) return;
	const $lines = $(`<div class="flex flex-col gap-2 ps-8 pt-3 mt-3 border-t"></div>`).appendTo($card);
	// each ledger on two lines, so a phone keeps the amount with its badge
	$lines.append(
		(off.length ? lines : []).map((line) => {
			const gap = flt(line.sheet - line.entered, 2);
			return $(`<div class="flex flex-col gap-0.5 min-w-0"></div>`).append(
				$(`<span class="text-p-sm text-ink-gray-8 break-words"></span>`).text(
					short_name(line.account)
				),
				$(`<div class="flex flex-wrap items-center justify-between gap-2"></div>`).append(
					grey_line(
						[
							line.sheet === null
								? __("{0} in {1}", [
										money(line.entered),
										count_invoices(line.count, party_type),
								  ])
								: __("{0} of {1}", [money(line.entered), money(line.sheet)]),
							flt(line.draft, 2) && __("{0} still draft", [money(line.draft)]),
						]
							.filter(Boolean)
							.join(" · ")
					),
					frappe.ui.badge(
						line.sheet === null
							? { label: __("not in your trial balance"), theme: "orange" }
							: !is_off(line)
							? { label: __("Matches"), theme: "green" }
							: gap > 0
							? { label: __("{0} missing", [money(gap)]), theme: "orange" }
							: { label: __("{0} more", [money(-gap)]), theme: "red" }
					)
				)
			);
		}),
		notes.length ? make_alerts(notes) : []
	);
}

// The card's badge, and the figures under it, for the Selling or Buying card and Check your books
export function get_sheet_check(frm, party_type) {
	const match = frm.doc.__onload?.sheet_match?.[party_type];
	if (!match) return null;
	// the symbol stays with its number when the line wraps
	const money = (value) =>
		format_currency(value, erpnext.get_currency(frm.doc.company), 0).replace(" ", "\u00a0");
	const abbr = frm.doc.__onload?.company_details?.abbr;
	const short_name = (account) => (abbr ? account.replace(new RegExp(` - ${abbr}$`), "") : account);
	// drafts in another currency or of a disabled party don't post: left out of the figures
	const lines = match.lines.map((line) => ({
		...line,
		entered: flt(line.entered - flt(line.left_out), 2),
		draft: flt(line.draft - flt(line.left_out), 2),
	}));
	const left_out = match.left_out || { currencies: {}, disabled: {} };
	const left_out_total =
		Object.values(left_out.currencies).reduce((sum, part) => sum + flt(part.amount), 0) +
		flt(left_out.disabled?.amount);
	const is_off = (line) => line.sheet === null || Math.abs(flt(line.sheet - line.entered, 2)) >= 1;
	const off = lines.filter(is_off);
	const gaps = match.by_account
		? off.map((line) => (line.sheet === null ? -line.entered : flt(line.sheet - line.entered, 2)))
		: [flt(match.sheet - match.entered + left_out_total, 2)];
	const missing = flt(
		gaps.filter((gap) => gap > 0).reduce((sum, gap) => sum + gap, 0),
		2
	);
	const more = flt(-gaps.filter((gap) => gap < 0).reduce((sum, gap) => sum + gap, 0), 2);
	// money on the wrong ledger: one over its line, another short
	const names_of = (sign) =>
		off
			.filter((line, index) => Math.sign(gaps[index]) === sign)
			.map((line) => short_name(line.account))
			.join(", ");
	const [label, theme] =
		match.by_account && missing >= 1 && more >= 1
			? [
					__("{0} over on {1} · {2} short on {3}", [
						money(more),
						names_of(-1),
						money(missing),
						names_of(1),
					]),
					"red",
			  ]
			: missing >= 1
			? [__("{0} short of trial balance", [money(missing)]), "orange"]
			: more >= 1
			? [__("{0} more than trial balance", [money(more)]), "red"]
			: off.length
			? [__("Accounts don't match trial balance"), "orange"]
			: (frm.doc[PARTY[party_type].table] || []).some((row) => row.status !== "Created")
			? [__("Matches trial balance once all are posted"), "blue"]
			: [__("Matches trial balance"), "green"];
	return { money, short_name, lines, left_out, is_off, off, label, theme };
}

// Undone invoices wait to be posted again: one line on the card, so they aren't forgotten
function add_undone_line(frm, party_type, $card) {
	const undone = frm.doc[UNDONE_TABLES[party_type]] || [];
	if (!undone.length) return;
	$card.append(
		$(`<div class="flex items-center justify-between gap-3 ps-8 pt-3 mt-3 border-t"></div>`).append(
			grey_line(
				__("{0} undone, not posted again yet", [
					count_entries(
						undone.length,
						undone.filter((row) => flt(row.outstanding_amount) < 0).length,
						party_type
					),
				])
			),
			make_view_button(() => show_posted(frm, party_type, "undone"))
		)
	);
}

// While invoices or bills post in the background: what the card and window say, and the bar
function get_posting_text(frm, party_type) {
	const { done, total } = frm.posting?.[party_type] || {};
	if (!total) {
		return party_type === "Customer" ? __("Posting invoices…") : __("Posting bills…");
	}
	return __("Posting {0} of {1}…", [format_number(done || 0, null, 0), format_number(total, null, 0)]);
}

export function make_posting_bar(frm, party_type, opts = {}) {
	const { done, total } = frm.posting?.[party_type] || {};
	const progress = new frappe.ui.Progress({ value: total ? (done / total) * 100 : 0, ...opts });
	return $(progress.el).attr("data-posting-bar", party_type).data("progress", progress);
}

export function update_posting(frm, party_type) {
	const { done, total } = frm.posting?.[party_type] || {};
	$(`[data-posting-note="${party_type}"]`).text(get_posting_text(frm, party_type));
	$(`[data-posting-bar="${party_type}"]`).each((i, el) => {
		const progress = $(el).data("progress");
		progress?.set_value(total ? (done / total) * 100 : 0);
		progress?.label_el && (progress.label_el.textContent = __("{0} of {1} posted", [done, total]));
	});
	// a posting started elsewhere: the card first shows it here, once the page has what it's drawn from
	if (!$(`[data-posting-note="${party_type}"]`).length && frm.doc.__onload?.steps) {
		frm.events.render(frm);
	}
}

function skip_items(frm, step) {
	if (step.is_skipped) {
		return [{ label: __("Undo skip"), icon: "undo-2", onclick: () => skip_step(frm, step, 0) }];
	}
	if (step.is_complete || step.is_started || step.is_required) return [];
	return [{ label: __("Skip this step"), icon: "circle-minus", onclick: () => skip_step(frm, step, 1) }];
}

function make_task(frm, step, buttons) {
	const [icon, color] = step.is_closing
		? ["loader-circle", "text-ink-blue-7"]
		: step.is_posting
		? ["loader-circle", "text-ink-gray-8"]
		: step.is_complete
		? ["circle-check", "text-ink-gray-8"]
		: step.is_skipped
		? ["circle-minus", "text-ink-gray-5"]
		: step.is_started
		? ["circle-dot", "text-ink-gray-8"]
		: ["circle-dashed", "text-ink-gray-5"];
	const note = step.is_skipped ? __("Skipped for now") : step.summary || step.description;

	// the buttons sit at the right end, and wrap below the text only when the card is too narrow
	const $task = $(`<div class="flex flex-wrap items-center gap-3"></div>`);
	// the icon and the text stay together on the first line
	const $lead = $(`<div class="flex items-center gap-3 grow min-w-0"></div>`).appendTo($task);
	$(`<span class="shrink-0 ${color}"></span>`).html(frappe.utils.icon(icon, "md")).appendTo($lead);
	const $text = $(`<div class="flex flex-col gap-0.5 min-w-0"></div>`).appendTo($lead);
	$(`<div class="text-base-medium text-ink-gray-8"></div>`).text(step.title).appendTo($text);
	if (note) grey_line(note).appendTo($text);
	for (const warning of step.warnings || []) {
		$(`<div class="text-p-sm text-ink-amber-8"></div>`).text(warning).appendTo($text);
	}
	if (step.blocker && !step.is_skipped) {
		$(`<div class="flex items-center gap-1 text-p-sm text-ink-red-7"></div>`)
			.append($(`<span class="shrink-0"></span>`).html(frappe.utils.icon("circle-x", "sm")))
			.append($("<span></span>").text(step.blocker))
			.appendTo($text);
	}

	if (buttons.length) {
		const $actions = $(`<div class="flex flex-wrap items-center justify-end gap-2 ms-auto"></div>`);
		buttons.forEach((opts) =>
			$actions.append(opts.jquery || opts instanceof Element ? opts : make_button(opts))
		);
		$task.append($actions);
	}
	return $task;
}

export function show_tab(frm, fieldname) {
	frm.layout.tabs.find((tab) => tab.df.fieldname === fieldname)?.set_active();
	frappe.utils.scroll_to(0);
}

function skip_step(frm, step, skip) {
	return call_method(frm, "skip_step", { step_key: step.key, skip: skip }).then(() => reload_fresh(frm));
}

// Buttons of each task on its own tab, by the last part of its hook path
const STEP_ACTIONS = {
	chart_of_accounts: (frm, step) => {
		if (step.is_complete) {
			return [{ label: __("Edit chart"), onclick: () => show_chart(frm) }];
		}
		return [
			{
				label: __("Keep this chart"),
				onclick: () => call_method(frm, "use_existing_chart").then(() => reload_fresh(frm)),
			},
			!frm.doc.__onload?.chart_import_blocked && {
				label: __("Import my chart"),
				onclick: () => show_chart_import(frm),
			},
		].filter(Boolean);
	},
	opening_balances: (frm) => {
		const drafts = (frm.doc.opening_balances || []).filter((row) => row.status !== "Posted");
		const blocker = !frm.doc.migration_date && __("Set the migration date first.");
		return [
			{
				label: frm.doc.opening_balances?.length ? __("Edit balances") : __("Enter balances"),
				onclick: () => show_opening_balances(frm),
			},
			// only a draft has something to preview and post
			drafts.length && {
				label: __("Preview and post"),
				disabled: !!blocker,
				tooltip: blocker || undefined,
				onclick: () => show_posting_preview(frm),
			},
		];
	},
	opening_sales_invoices: (frm) => invoice_buttons(frm, "Customer"),
	opening_purchase_invoices: (frm) => invoice_buttons(frm, "Supplier"),
	bank_accounts: (frm, step) => [
		{
			label: step.is_complete ? __("Edit bank details") : __("Add bank details"),
			onclick: () => show_bank_accounts(frm),
		},
	],
	customers: (frm, step) => import_buttons(frm, "Customer", step),
	suppliers: (frm, step) => import_buttons(frm, "Supplier", step),
	// nothing to check before anything is posted: the balances step has the preview
	check_books: (frm) => [has_posted(frm) && has_drafts(frm) && see_books_button(frm)],
};

// What else a task can do, in its ⋯ menu
const STEP_MORE = {
	chart_of_accounts: (frm, step) =>
		step.is_complete
			? [
					!frm.doc.__onload?.chart_import_blocked && {
						label: __("Change chart"),
						icon: "replace",
						onclick: () => show_chart_import(frm),
					},
					{
						label: __("Download chart"),
						icon: "download",
						onclick: () =>
							download_sheet(frm.doc.company, "chart_sheet.download_chart_template", {
								template: "Current Chart",
							}),
					},
			  ].filter(Boolean)
			: [{ label: __("View chart"), icon: "eye", onclick: () => show_chart(frm) }],
	taxes: (frm) => [
		{
			label: __("Sales tax templates"),
			icon: "arrow-up-right",
			onclick: () =>
				frappe.set_route("List", "Sales Taxes and Charges Template", { company: frm.doc.company }),
		},
		{
			label: __("Purchase tax templates"),
			icon: "arrow-up-right",
			onclick: () =>
				frappe.set_route("List", "Purchase Taxes and Charges Template", {
					company: frm.doc.company,
				}),
		},
	],
	opening_balances: (frm) =>
		(frm.doc.opening_balances || []).some((row) => row.status === "Posted")
			? [{ label: __("Posted balances"), icon: "list", onclick: () => show_posted(frm, "balances") }]
			: [],
	opening_sales_invoices: (frm) => invoice_menu(frm, "Customer"),
	opening_purchase_invoices: (frm) => invoice_menu(frm, "Supplier"),
	bank_accounts: (frm) => bank_menu(frm),
	customers: () => [party_list_item("Customer")],
	suppliers: () => [party_list_item("Supplier")],
};

function party_list_item(doctype) {
	return {
		label: __("{0} list", [__(doctype)]),
		icon: "arrow-up-right",
		onclick: () => frappe.set_route("List", doctype),
	};
}

export function has_rows(rows, posted) {
	return (rows || []).some((row) => (row.status === "Posted") === posted);
}

// Card buttons and ⋯ items of the steps added after the bank accounts
Object.assign(STEP_ACTIONS, {
	taxes: (frm, step) => [
		{
			label: step.is_complete ? __("Edit taxes") : __("Set up taxes"),
			onclick: () => show_taxes(frm),
		},
	],
	items: (frm) => items_import_menu(frm),
	warehouses: (frm, step) => [
		{
			label: step.is_complete ? __("Edit warehouses") : __("Add warehouses"),
			onclick: () => show_warehouses(frm),
		},
	],
	opening_stock: (frm) => [
		{
			label: has_rows(frm.doc.opening_stock, false) ? __("Edit stock") : __("Add stock"),
			onclick: () => show_opening_stock(frm),
		},
	],
	job_workers: (frm) => [
		{
			label: __("Edit in Warehouses"),
			onclick: () => show_warehouses(frm, { job_workers: true }),
		},
	],
	open_sales_orders: order_buttons,
	open_purchase_orders: order_buttons,
	open_work_orders: order_buttons,
	open_subcontracting_orders: order_buttons,
	fixed_assets: (frm) => [
		{
			label: has_rows(frm.doc.fixed_assets, false) ? __("Edit assets") : __("Add assets"),
			onclick: () => show_fixed_assets(frm),
		},
	],
	boms: bom_buttons,
});

Object.assign(STEP_MORE, {
	warehouses: () => [
		{
			label: __("Warehouse list"),
			icon: "arrow-up-right",
			onclick: () => frappe.set_route("List", "Warehouse"),
		},
	],
	opening_stock: (frm) =>
		[
			has_rows(frm.doc.opening_stock, true) && {
				label: __("Posted stock"),
				icon: "list",
				onclick: () => show_posted_stock(frm),
			},
			{
				label: __("Download sheet"),
				icon: "download",
				onclick: () => download_sheet(frm.doc.company, "opening_stock.download_opening_stock"),
			},
		].filter(Boolean),
	job_workers: (frm) => [
		{ label: __("Stock at job workers"), icon: "list", onclick: () => show_job_worker_stock(frm) },
		party_list_item("Supplier"),
	],
	open_sales_orders: order_menu,
	open_purchase_orders: order_menu,
	open_work_orders: order_menu,
	open_subcontracting_orders: order_menu,
	fixed_assets: (frm) =>
		[
			(has_rows(frm.doc.fixed_assets, true) || (frm.doc.undone_assets || []).length) && {
				label: __("Posted assets"),
				icon: "list",
				onclick: () => show_posted_assets(frm),
			},
			{
				label: __("Download sheet"),
				icon: "download",
				onclick: () => download_sheet(frm.doc.company, "fixed_assets.download_fixed_assets"),
			},
		].filter(Boolean),
	boms: bom_menu,
});

Object.assign(STEP_OPENERS, {
	taxes: (frm) => show_taxes(frm),
	items: (frm, step) => !step.is_complete && open_party_import(frm, "Item"),
	warehouses: (frm) => show_warehouses(frm),
	opening_stock: (frm) => show_opening_stock(frm),
	job_workers: (frm) => show_warehouses(frm, { job_workers: true }),
	open_sales_orders: (frm) => show_open_orders(frm, "Sales Order"),
	open_purchase_orders: (frm) => show_open_orders(frm, "Purchase Order"),
	open_work_orders: (frm) => show_open_orders(frm, "Work Order"),
	open_subcontracting_orders: (frm) => show_open_orders(frm, "Subcontracting Order"),
	fixed_assets: (frm) => show_fixed_assets(frm),
	boms: (frm) => show_boms(frm),
});

// ---------- realtime ----------

// tabs that run past a phone's width fade at the end, so the rest are seen to be there
export function show_tabs_cue(frm) {
	const $tabs = frm.page.wrapper.find(".form-tabs");
	const update = () => {
		const el = $tabs[0];
		if (!el) return;
		$tabs.toggleClass("company-setup-tabs-more", el.scrollWidth - el.scrollLeft - el.clientWidth > 4);
	};
	$tabs.off("scroll.company_setup").on("scroll.company_setup", update);
	$(window).off("resize.company_setup").on("resize.company_setup", update);
	update();
}

// the realtime event can be missed: while stock posts in the background, look again now and then
export function watch_stock_posting(frm) {
	if (!(frm.doc.__onload?.posting || []).includes("Stock")) return;
	if (frm.stock_watch) return;
	frm.stock_watch = setTimeout(() => {
		get_data(frm, "get_stock_status").then((r) => {
			frm.stock_watch = null;
			if (cur_frm !== frm) return;
			if (r.message.posting) return watch_stock_posting(frm);
			if (!frm.is_dirty()) frm.reload_doc();
		});
	}, 15000);
}

// A job in the background says when it is done: the page reloads, then says how it went
export function listen_for_postings(frm) {
	const toast = (message) => () => frappe.ui.toast({ type: "success", message: message });
	const jobs = {
		company_setup_cheques: {
			title: () => cheque_words(__("Cheques not posted")),
			done: () => show_cheques_posted(frm),
		},
		company_setup_invoices: {
			title: (data) =>
				data.party_type === "Supplier" ? __("Bills not posted") : __("Invoices not posted"),
			// the posting bar and its dialog go before the reload
			before: (data) => {
				if (frm.posting) delete frm.posting[data.party_type];
				if (frm.posting_dialog?.party_type === data.party_type) frm.posting_dialog.hide();
			},
			done: (data) => show_posting_result(frm, data.party_type),
		},
		company_setup_undo: {
			title: () => __("Not undone"),
			done: (data) => data.result && show_undo_result(frm, data.party_type, data.result, data.source),
		},
		company_setup_stock: { done: toast(__("Opening stock posted.")) },
		company_setup_assets: { done: toast(__("Fixed assets posted.")) },
		company_setup_stock_correction: { done: (data) => show_stock_corrected(frm, data.result) },
		company_setup_boms: { title: () => __("BOMs not made"), done: (data) => show_boms_made(frm, data) },
		company_setup_orders: {
			done: (data) =>
				frappe.ui.toast({
					type: "success",
					message: __("{0} made.", [count_orders(data.order_type, data.made)]),
				}),
		},
	};
	for (const [event, job] of Object.entries(jobs)) {
		frappe.realtime.off(event);
		frappe.realtime.on(event, (data) => {
			if (data.company !== frm.doc.company) return;
			job.before?.(data);
			if (data.error) {
				frappe.msgprint({
					title: job.title?.(data) || __("Not done"),
					message: data.error,
					indicator: "red",
				});
			}
			reload_fresh(frm).then(() => !data.error && job.done(data));
		});
	}
}
