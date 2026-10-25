frappe.ui.form.on("Company Setup", {
	setup(frm) {
		// invoices made in the background: how far they are, then done
		frappe.realtime.off("company_setup_invoices_progress");
		frappe.realtime.on("company_setup_invoices_progress", (data) => {
			if (data.company !== frm.doc.company) return;
			frm.posting = { ...frm.posting, [data.party_type]: { done: data.done, total: data.total } };
			update_posting(frm, data.party_type);
		});
		listen_for_postings(frm);
		frappe.realtime.off("company_setup_year_close");
		frappe.realtime.on("company_setup_year_close", (data) => {
			if (data.company === frm.doc.company) update_year_close(frm, data.state);
		});
	},

	refresh(frm) {
		if (frm.is_new()) {
			// one setup per company: only companies without one can be picked
			frappe.db.get_list("Company Setup", { fields: ["company"], limit: 0 }).then((rows) => {
				const taken = rows.map((row) => row.company);
				frm.set_query("company", () => ({ filters: { name: ["not in", taken] } }));
				if (taken.includes(frm.doc.company)) frm.set_value("company", null);
			});
			return;
		}

		says_check = CHECK_COUNTRIES.includes(frm.doc.__onload?.company_details?.country);
		// every step saves from its own window
		frm.disable_save();
		// one setup per company: nothing to page through
		frm.page.clear_icons();
		frm.posting = Object.fromEntries(
			(frm.doc.__onload?.posting || []).map((party_type) => [
				party_type,
				frm.posting?.[party_type] || {},
			])
		);
		frm.year_close = frm.doc.__onload?.year_close;
		frm.events.render(frm);
		watch_stock_posting(frm);
		watch_year_close(frm);
		show_tabs_cue(frm);
		// a step done or undone since the last save, from the company's data, is saved now
		// once at a time: two reloads close together would save the steps twice over each other
		if (frm.doc.__onload?.steps_changed && !frm.checking_steps) {
			frm.checking_steps = true;
			call_method(frm, "check_steps")
				.then((r) => r.message && frm.reload_doc())
				.always(() => (frm.checking_steps = false));
		}
	},

	async render(frm) {
		await frappe.require(["desk_charts.bundle.js", "desk_charts.bundle.css"]);
		const steps = get_steps(frm);
		make_page(frm, "onboarding_html").append(
			make_company_card(frm),
			make_missing_roles(frm),
			make_overview_stats(frm),
			frm.doc.status === "Live"
				? $(`<div class="contact-box mb-0"></div>`).append(make_live_note(frm))
				: [],
			make_checklist(frm, steps)
		);
		for (const tab of TABS) {
			const field = tab.replace("_tab", "_html");
			make_page(frm, field).append(
				steps.filter((step) => step.tab === tab).map((step) => make_task_card(frm, step))
			);
		}
	},
});

function make_page(frm, fieldname) {
	const $wrapper = frm.get_field(fieldname).$wrapper.empty();
	// 16px above, between and below the cards: the tabs already leave 10px, pt-1.5 adds 6
	$wrapper.closest(".section-body").addClass("pt-0");
	return $(`<div class="flex flex-col gap-4 pt-1.5 mb-4"></div>`).appendTo($wrapper);
}

function get_steps(frm) {
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
function make_missing_roles(frm) {
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
function make_overview_stats(frm) {
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
function get_waiting_text(parts, currency) {
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

const TABS = [
	"accounts_tab",
	"selling_tab",
	"buying_tab",
	"stock_tab",
	"assets_tab",
	"manufacturing_tab",
	"subcontracting_tab",
];

const PARTY = {
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
function make_checklist(frm, steps) {
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
function open_step(frm, step) {
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
function make_task_card(frm, step) {
	const buttons = (STEP_ACTIONS[step.name]?.(frm, step) || []).filter(Boolean);
	const more = [...(STEP_MORE[step.name]?.(frm, step) || []), ...skip_items(frm, step)];
	if (more.length) buttons.push(make_more_menu(more));
	if (step.name === "chart_of_accounts" && !step.is_complete && frm.doc.__onload?.chart_import_blocked) {
		step = { ...step, description: frm.doc.__onload.chart_import_blocked };
	}
	const background =
		{ opening_stock: "Stock", fixed_assets: "Assets" }[step.name] || (ORDER_TYPES[step.name] && "Orders");
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
function get_sheet_check(frm, party_type) {
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

function make_posting_bar(frm, party_type, opts = {}) {
	const { done, total } = frm.posting?.[party_type] || {};
	const progress = new frappe.ui.Progress({ value: total ? (done / total) * 100 : 0, ...opts });
	return $(progress.el).attr("data-posting-bar", party_type).data("progress", progress);
}

function update_posting(frm, party_type) {
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

// Where each report or list is in the software people move from
const WHERE_TO_FIND = {
	chart_of_accounts: [
		[
			"Tally",
			__(
				"Display More Reports › List of Accounts, then Alt+E to export (Tally ERP 9: Display › List of Accounts)"
			),
		],
		["Busy", __("Administration › Masters › Account › List, choose All Accounts, then Alt+E")],
		["Zoho Books", __("Accountant › Chart of Accounts › Export")],
		["QuickBooks", __("Reports › Account List, then Export to Excel")],
		["Xero", __("Accounting › Chart of accounts › Export")],
		["Marg", __("Books › All Ledgers, then the Excel icon and Format-2")],
		["Excel / other", __("Download the sample chart and fill it in.")],
	],
	opening_balances: [
		[
			"Tally",
			__(
				"Display More Reports › Trial Balance, Alt+F5 for every ledger, then Alt+E (Tally ERP 9: Display › Trial Balance)"
			),
		],
		["Busy", __("Display › Trial Balance › Closing Trial - Alphabetical, Balance Only, then Alt+E")],
		["Zoho Books", __("Reports › Trial Balance")],
		["QuickBooks", __("Reports › Trial Balance")],
		["Xero", __("Accounting › Reports › Trial Balance, then Export to Excel")],
		["Marg", __("Final Reports › Trial Balance, Alt+P, A/C Group Wise: No, then Excel")],
		["Excel / other", __("One row per account with its debit or credit.")],
	],
	opening_sales_invoices: [
		[
			"Tally",
			__(
				"Display More Reports › Statements of Accounts › Outstandings › Receivables (Tally ERP 9: Display › Statements of Accounts › Outstandings)"
			),
		],
		[
			"Busy",
			__("Display › Outstanding Analysis › Bills Receivable, Sundry Debtors, Bill-wise, then Alt+E"),
		],
		["Zoho Books", __("Reports › AR Aging Details")],
		["QuickBooks", __("Reports › A/R Aging Detail, then Export to Excel")],
		["Xero", __("Accounting › Reports › Aged Receivables Detail, then Export to Excel")],
		["Marg", __("Books › Outstanding › Whole, Alt+P › Outstanding List Party Wise, then the Excel icon")],
		["Excel / other", __("One row per unpaid invoice: customer, invoice number, date, amount.")],
	],
	opening_purchase_invoices: [
		[
			"Tally",
			__(
				"Display More Reports › Statements of Accounts › Outstandings › Payables (Tally ERP 9: Display › Statements of Accounts › Outstandings)"
			),
		],
		[
			"Busy",
			__("Display › Outstanding Analysis › Bills Payable, Sundry Creditors, Bill-wise, then Alt+E"),
		],
		["Zoho Books", __("Reports › AP Aging Details")],
		["QuickBooks", __("Reports › A/P Aging Detail, then Export to Excel")],
		["Xero", __("Accounting › Reports › Aged Payables Detail, then Export to Excel")],
		[
			"Marg",
			__(
				"Books › Outstanding › Creditors: Whole, Alt+P › Outstanding List Party Wise, then the Excel icon"
			),
		],
		["Excel / other", __("One row per unpaid bill: supplier, bill number, date, amount.")],
	],
	opening_stock: [
		[
			"Tally",
			__(
				"Display More Reports › Inventory Books › Godown Summary, then Alt+E for each godown (Tally ERP 9: Display › Inventory Books › Godown Summary)"
			),
		],
		[
			"Busy",
			__(
				"Display › Stock Status › Material Centre-wise, with closing value, then Alt+E (Busy calls a warehouse a Material Centre)"
			),
		],
		[
			"Zoho Books",
			__("Reports › Inventory › Inventory Valuation Summary, one warehouse at a time, then Export"),
		],
		["QuickBooks", __("Reports › Inventory Valuation Summary, then Export to Excel")],
		[
			"Excel / other",
			__(
				"One row per item per warehouse: item, warehouse, qty, unit, value. Job worker rows add the challan."
			),
		],
	],
	fixed_assets: [
		["Busy", __("Display › Final Results › Depreciation Chart, then Alt+E")],
		["QuickBooks", __("Reports › Fixed Asset List, then Export to Excel")],
		[
			"Excel / other",
			__("One row per asset: name, asset account, purchase date, cost, depreciation till date."),
		],
	],
	open_orders: [
		[
			"Tally",
			__(
				"Display More Reports › Statements of Inventory › Sales Order Outstandings, and Purchase Order Outstandings, then Alt+E"
			),
		],
		["Excel / other", __("One row per order line: order number, party, item, qty left, rate, date.")],
	],
	boms: [
		["Tally", __("Tally keeps the BOM on each stock item: fill the BOM import sheet from them.")],
		["Excel / other", __("Import them from the BOMs step with the BOM import sheet.")],
	],
	job_work_orders: [
		[
			"Tally",
			__(
				"Display More Reports › Statements of Inventory › Job Work Out Order Outstandings, then Alt+E"
			),
		],
		[
			"Excel / other",
			__("One row per order line: order number, job worker, item, qty left, rate, date."),
		],
	],
	uncleared_cheques: [
		[
			"Tally",
			__("Banking › Bank Reconciliation, pick the bank, then Alt+E to export what is not cleared"),
		],
		["Busy", __("Display › Bank Reconciliation, pick the bank, show unreconciled entries, then Alt+E")],
		[
			"QuickBooks",
			__("Accounting › Reconcile › History by account, open the last report, then Export to Excel"),
		],
		[
			"Excel / other",
			__(
				"One row per cheque or deposit: bank account, issued or deposited, number, date, party, amount."
			),
		],
	],
};

// in the order people in the country use them
const SOFTWARE =
	frappe.sys_defaults?.country === "India"
		? ["Tally", "Busy", "Zoho Books", "QuickBooks", "Xero", "Marg", "Excel / other"]
		: ["QuickBooks", "Xero", "Zoho Books", "Tally", "Busy", "Marg", "Excel / other"];

// Each task's report comes from the software picked for it; the chart from where the trial balance is
const SOURCES = {
	chart_of_accounts: ["accounts_source", __("Your chart comes from")],
	opening_balances: ["accounts_source", __("Your trial balance comes from")],
	opening_sales_invoices: ["receivables_source", __("Your unpaid invoices come from")],
	opening_purchase_invoices: ["payables_source", __("Your unpaid bills come from")],
	uncleared_cheques: ["cheques_source", __("Your bank book comes from")],
	opening_stock: ["stock_source", __("Your stock summary comes from")],
	fixed_assets: ["assets_source", __("Your asset register comes from")],
	open_orders: ["orders_source", __("Your open orders come from")],
	boms: ["boms_source", __("Your BOMs come from")],
	job_work_orders: ["job_work_source", __("Your job work orders come from")],
};
// reports that come from the trial balance's software until picked on their own
const FOLLOWS_TRIAL_BALANCE = [
	"uncleared_cheques",
	"opening_stock",
	"fixed_assets",
	"open_orders",
	"boms",
	"job_work_orders",
];

// the trial balance's software, or an Excel sheet when that software has no such report
function follow_trial_balance(task_name, accounts_source) {
	return !accounts_source || WHERE_TO_FIND[task_name].some(([software]) => software === accounts_source)
		? accounts_source || ""
		: "Excel / other";
}

function get_source(frm, task_name) {
	const value = frm.doc[SOURCES[task_name][0]];
	return value || !FOLLOWS_TRIAL_BALANCE.includes(task_name)
		? value || ""
		: follow_trial_balance(task_name, frm.doc.accounts_source);
}

// a software without its own steps for this report gets the sheet's columns
function get_places(task_name, source) {
	const places = WHERE_TO_FIND[task_name]
		.filter(([software]) => !source || software === source)
		.sort(([a], [b]) => SOFTWARE.indexOf(a) - SOFTWARE.indexOf(b));
	return places.length
		? places
		: WHERE_TO_FIND[task_name].filter(([software]) => software === "Excel / other");
}

function make_source_select(value, onchange) {
	const $parent = $(`<div class="shrink-0"></div>`);
	const control = frappe.ui.form.make_control({
		df: {
			fieldtype: "Select",
			fieldname: "source",
			options: ["", ...SOFTWARE].map((software) => ({ value: software, label: __(software) })),
			placeholder: __("Pick one"),
			change: () => onchange(control.get_value()),
		},
		parent: $parent,
		only_input: true,
		render_input: true,
	});
	control.set_value(value || "");
	$parent.find(".form-group").addClass("mb-0");
	return [$parent, control];
}

// The "Where do I find this?" section at the end of a dialog: open while nothing is entered yet,
// folded once something is, and left out once anything is posted. fill_where draws it.
function where_to_find_fields(frm, task_name, { has_data, posted }) {
	if (posted) return [];
	return [
		{
			fieldname: "where_section",
			fieldtype: "Section Break",
			label: __("Where do I find this?"),
			collapsible: 1,
			collapsible_depends_on: has_data ? "eval:0" : "eval:1",
		},
		{ fieldname: "where_to_find", fieldtype: "HTML" },
	];
}

// The steps for the software this task's report comes from, and a way to pick another
function fill_where(frm, dialog, task_name) {
	const field = dialog.fields_dict.where_to_find;
	if (!field) return;
	const [source_field, says] = SOURCES[task_name];
	const source = get_source(frm, task_name);
	const [$select] = make_source_select(source, (value) => {
		if (!value || value === source) return;
		frm.set_value(source_field, value)
			.then(() => frm.save())
			.then(() => fill_where(frm, dialog, task_name));
	});
	const $line = $(`<div class="flex items-center justify-between gap-2"></div>`).append(
		$(`<div class="text-p-sm text-ink-gray-7"></div>`).text(says),
		$select
	);
	field.$wrapper
		.empty()
		.append($line.addClass("mb-2"), make_where_table(frm, get_places(task_name, source)));
}

function make_where_table(frm, places) {
	// one software picked: the picker above names it, so only its steps
	const $box =
		places.length === 1
			? $(`<div class="border rounded-lg px-3 py-2 text-p-sm text-ink-gray-8"></div>`).text(
					cheque_words(places[0][1])
			  )
			: make_where_box(places);
	const $table = $(`<div class="flex flex-col gap-2"></div>`).append($box);
	if (frm.doc.migration_date) {
		grey_line(__("Take it as on {0}, the day before you start.", [day_before_text(frm)])).appendTo(
			$table
		);
	}
	return $table[0];
}

// A box with each software beside where its report is, under the report's name when given
function make_where_box(places, title) {
	return $(`<div class="flex flex-col border rounded-lg overflow-hidden"></div>`).append(
		title
			? $(`<div class="px-3 py-2 text-sm-medium text-ink-gray-8 bg-surface-gray-1"></div>`).text(title)
			: [],
		places.map(([software, where], index) =>
			$(
				`<div class="company-setup-where gap-4 px-3 py-2 ${title || index ? "border-t" : ""}"></div>`
			).append(
				grey_line(software),
				$(`<div class="text-p-sm text-ink-gray-8"></div>`).text(cheque_words(where))
			)
		)
	);
}

// Status icon, title, one line on what's done or left, and the buttons
// a disabled button takes no pointer, so the reason it is disabled shows from a wrapper
function make_button(opts) {
	if (!opts.disabled || !opts.tooltip) return frappe.ui.button(opts);
	return frappe.ui.tooltip(
		$(`<span class="inline-flex" tabindex="0"></span>`)
			.attr("aria-label", opts.tooltip)
			.append(frappe.ui.button({ ...opts, tooltip: undefined })),
		{ text: opts.tooltip }
	);
}

// A window closed by its X, Esc or a click outside while is_dirty() asks first: Save draft keeps
// the changes with save() and closes, Discard closes without them. Closing from code isn't asked.
function ask_before_closing(dialog, is_dirty, save) {
	const ask = (e) => {
		if (e.isDefaultPrevented() || !is_dirty()) return;
		e.preventDefault();
		e.stopImmediatePropagation();
		const confirm = new frappe.ui.Dialog({
			title: __("You have unsaved changes"),
			size: "small",
			primary_action_label: __("Save draft"),
			primary_action: () =>
				Promise.resolve(save()).then(() => {
					confirm.hide();
					dialog.hide();
				}),
			secondary_action_label: __("Discard"),
			secondary_action: () => {
				confirm.hide();
				dialog.hide();
			},
		});
		confirm.show();
	};
	dialog.get_close_btn().on("click", ask);
	dialog.$wrapper.on("keydown", (e) => {
		if (e.key === "Escape" && !dialog.$wrapper.find(".grid-row-open").length) ask(e);
	});
	dialog.$wrapper.on("click", (e) => e.target === e.currentTarget && ask(e));
}

// A table's rows as text, to tell whether they changed since: the order of the rows doesn't count
function get_rows_key(grid, rows = grid.df.data || []) {
	const fields = grid.docfields.map((df) => df.fieldname);
	return JSON.stringify(rows.map((row) => JSON.stringify(fields.map((f) => row[f] ?? null))).sort());
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

function show_tab(frm, fieldname) {
	frm.layout.tabs.find((tab) => tab.df.fieldname === fieldname)?.set_active();
	frappe.utils.scroll_to(0);
}

function skip_step(frm, step, skip) {
	return call_method(frm, "skip_step", { step_key: step.key, skip: skip }).then(() => reload_fresh(frm));
}

function show_migration_date(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("When do you move to ERPNext?"),
		fields: [
			{
				fieldname: "migration_date",
				fieldtype: "Date",
				label: __("Migration date"),
				reqd: 1,
				default: frm.doc.migration_date,
				description: __("Your balances come in as they stood at the end of the day before."),
			},
			{ fieldname: "reports", fieldtype: "HTML" },
			{
				fieldtype: "Section Break",
				label: __("Where is each report?"),
				collapsible: 1,
			},
			{ fieldname: "where", fieldtype: "HTML" },
		],
		primary_action_label: __("Save"),
		primary_action: (values) =>
			save_migration_date(frm, dialog, { migration_date: values.migration_date, ...sources }),
	});

	const reports = [
		[__("Trial balance"), __("Every account with its balance"), "opening_balances"],
		[__("Unpaid customer invoices"), __("Who owes you, invoice by invoice"), "opening_sales_invoices"],
		[__("Unpaid supplier bills"), __("Whom you owe, bill by bill"), "opening_purchase_invoices"],
		[__("Stock summary"), __("If you keep stock: item by item, in each warehouse"), "opening_stock"],
		[__("Fixed asset register"), __("If you have fixed assets: asset by asset"), "fixed_assets"],
		[__("Bank reconciliation"), __("Cheques the bank had not cleared"), "uncleared_cheques"],
		[__("Open orders"), __("Sales and purchase orders not yet delivered or received"), "open_orders"],
		[__("BOMs"), __("If you make goods: the materials of each item you make"), "boms"],
		[
			__("Job work orders"),
			__("If you send work out: orders with goods still at job workers"),
			"job_work_orders",
		],
	];
	const sources = Object.fromEntries(
		reports.map(([, , task_name]) => [SOURCES[task_name][0], get_source(frm, task_name)])
	);
	const $where = $(`<div class="flex flex-col gap-3"></div>`);
	const show_where = () => {
		$where
			.empty()
			.append(
				reports.map(([title, , task_name]) =>
					make_where_box(get_places(task_name, sources[SOURCES[task_name][0]]), title)
				)
			);
	};

	const $reports = $(`<div class="flex flex-col gap-2 pt-2"></div>`);
	const values = ["accounts_source", "receivables_source", "payables_source"].map(
		(field) => sources[field]
	);
	const controls = {};
	// "Mostly from" fills the reports with a software of their own; the others follow the trial
	// balance. Any report can then be changed on its own.
	const [$mostly] = make_source_select(
		values.every((value) => value === values[0]) ? values[0] : "",
		(value) => {
			if (!value) return;
			for (const task_name of [
				"opening_balances",
				"opening_sales_invoices",
				"opening_purchase_invoices",
			]) {
				controls[task_name].set_value(value);
			}
		}
	);
	$(`<div class="flex items-center justify-between gap-3"></div>`)
		.append(
			$(`<div class="text-sm-medium text-ink-gray-7"></div>`).text(
				__("Keep these ready, as on the day before you start")
			),
			$(`<div class="flex items-center gap-2"></div>`).append(
				$(`<span class="text-sm text-ink-gray-5"></span>`).text(__("Mostly from")),
				$mostly
			)
		)
		.appendTo($reports);
	const $list = $(`<div class="flex flex-col border rounded-lg"></div>`).appendTo($reports);
	reports.forEach(([title, note, task_name], index) => {
		const source_field = SOURCES[task_name][0];
		const [$select, control] = make_source_select(sources[source_field], (value) => {
			const before = sources[source_field];
			sources[source_field] = value || "";
			if (task_name === "opening_balances") {
				for (const follower of FOLLOWS_TRIAL_BALANCE) {
					const field = SOURCES[follower][0];
					// one picked on its own stays
					if (sources[field] === follow_trial_balance(follower, before)) {
						controls[follower].set_value(follow_trial_balance(follower, value));
					}
				}
			}
			show_where();
		});
		controls[task_name] = control;
		$(`<div class="flex items-center gap-3 px-3 py-2.5 ${index ? "border-t" : ""}"></div>`)
			.append(
				$(`<span class="flex shrink-0 text-ink-gray-5"></span>`).html(
					frappe.utils.icon("file-spreadsheet", "sm")
				),
				$(`<div class="flex flex-col flex-1 gap-0.5 min-w-0"></div>`).append(
					$(`<div class="text-base text-ink-gray-8"></div>`).text(title),
					grey_line(note)
				),
				$select
			)
			.appendTo($list);
	});
	grey_line(
		__("Customer and supplier lists help too. Without them, you confirm each new name before posting.")
	).appendTo($reports);
	dialog.get_field("reports").$wrapper.append($reports);

	show_where();
	dialog.get_field("where").$wrapper.append($where);
	// Escape closes an open calendar, not the window: before the calendar's own handler hides it
	dialog.$wrapper[0].addEventListener(
		"keydown",
		(event) => {
			const picker = dialog.get_field("migration_date").datepicker;
			if (event.key !== "Escape" || !picker?.visible) return;
			picker.hide();
			event.stopPropagation();
		},
		true
	);
	dialog.show();
}

function save_migration_date(frm, dialog, values) {
	// set_value applies the values asynchronously, so check for changes once it is done
	return frm
		.set_value(values)
		.then(() => (frm.is_dirty() ? frm.save() : null))
		.then(() => dialog.hide());
}

const REPORTS = [
	["Balance Sheet", __("Balance Sheet")],
	["Profit and Loss Statement", __("Profit and Loss Statement")],
	["Trial Balance", __("Trial Balance")],
	["General Ledger", __("General Ledger")],
];

// The opening entries are dated the day before migration, in last year: the reports open on that year
function report_filters(frm, report) {
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

function has_posted(frm) {
	return (
		(frm.doc.opening_balances || []).some((row) => row.status === "Posted") ||
		[...(frm.doc.opening_sales_invoices || []), ...(frm.doc.opening_purchase_invoices || [])].some(
			(row) => row.status === "Created"
		)
	);
}

// Anything entered but not posted yet: only then is there a preview to see
function has_drafts(frm) {
	return (
		(frm.doc.opening_balances || []).some((row) => row.status !== "Posted") ||
		[...(frm.doc.opening_sales_invoices || []), ...(frm.doc.opening_purchase_invoices || [])].some(
			(row) => row.status !== "Created"
		)
	);
}

function see_books_button(frm) {
	return {
		label: __("See your books"),
		disabled: !frm.doc.migration_date,
		tooltip: frm.doc.migration_date ? undefined : __("Set the migration date first."),
		onclick: () => show_posting_preview(frm, false),
	};
}

// What the books need beyond the steps: last year's profit closed, and the Company's default
// accounts set; for Check your books and Go live
function get_books_reminders(frm) {
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
function profit_words(frm, profit) {
	return profit > 0
		? __("a profit of {0}", [money(frm, profit)])
		: __("a loss of {0}", [money(frm, Math.abs(profit))]);
}

// What closing the last of years takes, in order, as the server's get_year_close_steps gives it:
// each year's stock to its end when it isn't closed yet, then the year
function get_year_close_steps(years) {
	return years.flatMap((year) => [
		...(year.stock?.needed && !year.stock.done ? [{ stock: year.year_end_date }] : []),
		{ fiscal_year: year.fiscal_year },
	]);
}

// Close a year from Check your books: one click closes the stock and the years before it too, in
// order, in the background
function close_year(frm, year, steps) {
	const year_end = frappe.datetime.str_to_user(year.year_end_date);
	const label = year.profit > 0 ? __("Profit goes to") : __("Loss goes to");
	const dialog = new frappe.ui.Dialog({
		title: __("Close {0}", [year.fiscal_year]),
		fields: [
			{ fieldname: "note", fieldtype: "HTML" },
			{
				fieldname: "closing_account",
				fieldtype: "Link",
				options: "Account",
				label: label,
				reqd: 1,
				only_select: 1,
				description: __("An equity account, usually Reserves & Surplus."),
				get_query: () => ({
					filters: {
						company: frm.doc.company,
						is_group: 0,
						disabled: 0,
						root_type: ["in", ["Equity", "Liability"]],
					},
				}),
			},
		],
		primary_action_label: __("Close {0}", [year.fiscal_year]),
		primary_action: (values) =>
			call_method(
				frm,
				"close_year",
				{ fiscal_year: year.fiscal_year, closing_account: values.closing_account },
				{ freeze: true }
			).then(() => close_and_reload(frm, dialog)),
	});
	const years = Object.fromEntries(
		(frm.doc.__onload?.books_reminders?.unclosed_years || []).map((row) => [row.fiscal_year, row])
	);
	const note = (step) =>
		step.stock
			? __("ERPNext needs this before a year with stock is closed.")
			: step.fiscal_year === year.fiscal_year
			? year.profit > 0
				? __("The profit goes to the account below.")
				: __("The loss goes to the account below.")
			: flt(years[step.fiscal_year]?.profit, 2)
			? __("It has {0}; years close in order.", [profit_words(frm, years[step.fiscal_year].profit)])
			: __("It has entries, like old invoices; years close in order.");
	dialog
		.get_field("note")
		.$wrapper.append(
			$(`<div class="text-base text-ink-gray-8 mb-2"></div>`).text(
				__(
					"{0} has {1}. Closing moves it out of income and expenses, dated {2}. One click does all of this, in order, in the background:",
					[year.fiscal_year, profit_words(frm, year.profit), year_end]
				)
			),
			$(`<div class="flex flex-col gap-2 border rounded-md p-3 mb-2"></div>`).append(
				steps.map((step, index) =>
					make_year_close_step(
						`${index + 1}. ${year_close_step_label(step, "waiting")}`,
						"waiting",
						note(step)
					)
				)
			),
			grey_line(
				__("After this, opening entries on or before {0} can't be posted or undone.", [year_end])
			).addClass("mb-3")
		);
	if (year.drafts?.length) {
		dialog.get_field("note").$wrapper.prepend(
			make_alerts([
				{
					theme: "red",
					title: __("Post or remove the opening drafts first"),
					description: __("{0}. After the close, entries on or before {1} can't be posted.", [
						year.drafts.join(" · "),
						year_end,
					]),
				},
			]).addClass("mb-3")
		);
		dialog.get_primary_btn().prop("disabled", true);
	}
	dialog.show();
	dialog.set_value("closing_account", year.closing_account || "");
	// typing replaces the suggested account instead of adding to it
	const $closing = dialog.get_field("closing_account").$input;
	$closing?.on("focus", () => setTimeout(() => $closing.select()));
}

// A step of a year close as it reads done, running or waiting
function year_close_step_label(step, status) {
	const date = step.stock && frappe.datetime.str_to_user(step.stock);
	const labels = step.stock
		? {
				done: __("Stock closed to {0}", [date]),
				running: __("Closing stock to {0}", [date]),
				waiting: __("Close stock to {0}", [date]),
		  }
		: {
				done: __("{0} closed", [step.fiscal_year]),
				running: __("Closing {0}", [step.fiscal_year]),
				waiting: __("Close {0}", [step.fiscal_year]),
		  };
	return labels[status];
}

function make_year_close_step(label, status, note) {
	const [icon, color] = {
		done: ["circle-check", "text-ink-green-6"],
		running: ["loader-circle", "text-ink-blue-7"],
		waiting: ["circle-dashed", "text-ink-gray-5"],
	}[status];
	return $(`<div class="flex items-start gap-2"></div>`).append(
		$(`<span class="shrink-0 ${color}"></span>`).html(frappe.utils.icon(icon, "sm")),
		$(`<div class="flex flex-col min-w-0"></div>`).append(
			$(`<span class="text-base text-ink-gray-8"></span>`).text(label),
			note ? grey_line(note) : []
		)
	);
}

// A year closing in the background, on Check your books: each step ticks as it is done; one that
// failed says why and tries again from there
function make_year_close_alert(frm) {
	const state = frm.year_close;
	if (!state) return [];
	const year = (frm.doc.__onload?.books_reminders?.unclosed_years || []).find(
		(row) => row.fiscal_year === state.fiscal_year
	);
	const steps = state.steps.map((step, index) => {
		const status = index < state.done ? "done" : index === state.done ? "running" : "waiting";
		const failed = state.error && index === state.done;
		const label = year_close_step_label(step, failed ? "waiting" : status);
		const last = step.fiscal_year === state.fiscal_year && status !== "done" && year;
		return make_year_close_step(
			last
				? __("{0} · {1} to {2}", [label, profit_words(frm, year.profit), state.closing_account])
				: label,
			failed ? "waiting" : status
		);
	});
	return frappe.ui.alert({
		theme: state.error ? "red" : "blue",
		icon: state.error ? undefined : "loader-circle",
		title: state.error
			? __("Closing {0} stopped", [state.fiscal_year])
			: __("Closing {0} in the background", [state.fiscal_year]),
		description: state.error || __("You can keep working; this updates by itself."),
		footer: () =>
			$(`<div class="flex flex-col gap-2"></div>`).append(
				steps,
				state.error
					? $(`<div class="flex gap-2 pt-1"></div>`).append(
							frappe.ui.button({
								label: __("Try again"),
								onclick: () =>
									call_method(
										frm,
										"close_year",
										{
											fiscal_year: state.fiscal_year,
											closing_account: state.closing_account,
										},
										{ freeze: true }
									).then(() => reload_fresh(frm)),
							})
					  )
					: []
			),
	});
}

// The Overview row of Check your books while a year closes: which step of how many
function get_year_close_summary(frm) {
	const state = frm.year_close;
	const step = state.steps[state.done];
	return __("Closing {0} in the background · step {1} of {2}: {3}", [
		state.fiscal_year,
		state.done + 1,
		state.steps.length,
		step ? year_close_step_label(step, "running").toLowerCase() : "",
	]);
}

// The year close as the server has it now: the card and the Overview row show it in place, and
// the page reloads once it is done
function update_year_close(frm, state) {
	if (cur_frm !== frm) return;
	const was = frm.year_close;
	frm.year_close = state;
	if (!state) {
		frappe.ui.toast({
			type: "success",
			message: __("{0} is closed. The Balance Sheet shows it in a few minutes.", [was?.fiscal_year]),
		});
		return frm.reload_doc();
	}
	const steps = get_steps(frm);
	$("[data-company-setup-checklist]").replaceWith(make_checklist(frm, steps));
	const books = steps.find((step) => step.name === "check_books");
	if (books) $("[data-company-setup-card='check_books']").replaceWith(make_task_card(frm, books));
	watch_year_close(frm);
}

// the realtime event can be missed: while a year closes, look again now and then
function watch_year_close(frm) {
	if (!frm.year_close || frm.year_close.error || frm.year_close_timer) return;
	frm.year_close_timer = setTimeout(() => {
		get_data(frm, "get_year_close").then((r) => {
			frm.year_close_timer = null;
			if (cur_frm !== frm || !frm.year_close) return;
			if (JSON.stringify(r.message) !== JSON.stringify(frm.year_close)) {
				return update_year_close(frm, r.message);
			}
			watch_year_close(frm);
		});
	}, 15000);
}

// What is left, as reminders only: nothing here stops going live
function show_go_live(frm) {
	const reminders = frm.doc.__onload?.go_live_reminders || {};
	const currency = erpnext.get_currency(frm.doc.company);
	const temporary = flt(reminders.temporary_opening);
	const steps = get_steps(frm);
	const by_key = (key) => steps.find((step) => step.key === key);
	// money left in Temporary Opening already says Check your books is not done
	const open_steps = (reminders.steps || [])
		.map(by_key)
		.filter((step) => step && !(step.name === "check_books" && temporary));
	// a step left while the trial balance has money for it, like fixed assets, is a gap; other
	// skipped steps only get a grey line
	const gaps = (reminders.gaps || []).map(by_key).filter((step) => step && !open_steps.includes(step));
	const skipped = (reminders.skipped || []).map(by_key).filter((step) => step && !gaps.includes(step));
	const open = (step) => () => {
		dialog.hide();
		open_step(frm, step);
	};
	const alerts = [
		reminders.closed_drafts?.length && {
			theme: "red",
			title: __("These opening drafts can no longer be posted"),
			description: __(
				"{0}. They are dated on or before {1}, and that year is closed. Remove them, or enter them as normal entries after the migration date.",
				[reminders.closed_drafts.join(" · "), frappe.datetime.str_to_user(reminders.closed_to)]
			),
			buttons: [
				{
					label: __("Remove drafts"),
					onclick: () =>
						frappe.confirm(
							__("Remove these opening drafts: {0}?", [
								frappe.utils.escape_html(reminders.closed_drafts.join(" · ")),
							]),
							() =>
								call_method(frm, "remove_closed_drafts", {}, { freeze: true }).then(() => {
									dialog.hide();
									reload_fresh(frm).then(() => show_go_live(frm));
								})
						),
				},
			],
		},
		temporary && {
			theme: "red",
			title: __("Temporary Opening is {0}, so it is not zero.", [
				format_currency(Math.abs(temporary), currency),
			]),
			buttons: [
				{
					label: __("See your books"),
					onclick: () => {
						dialog.hide();
						show_posting_preview(frm, false, () => show_go_live(frm));
					},
				},
			],
		},
		...open_steps.map((step) => {
			const party_type = {
				opening_sales_invoices: "Customer",
				opening_purchase_invoices: "Supplier",
			}[step.name];
			const table = party_type && PARTY[party_type].table;
			const drafts =
				party_type &&
				(frm.doc[table] || []).filter((row) => row.status !== "Created").length -
					flt(reminders.closed_draft_counts?.[table]);
			if (party_type && !drafts && reminders.closed_draft_counts?.[table]) return null;
			if (!drafts) {
				return {
					title: __("{0} is not done.", [step.title]),
					description: step.summary || undefined,
					buttons: [{ label: __("Open"), onclick: open(step) }],
				};
			}
			return party_type === "Customer"
				? {
						title: plural(
							drafts,
							__("1 opening invoice is still a draft."),
							__("{0} opening invoices are still drafts.", [drafts])
						),
						buttons: [{ label: __("Open invoices"), onclick: open(step) }],
				  }
				: {
						title: plural(
							drafts,
							__("1 opening bill is still a draft."),
							__("{0} opening bills are still drafts.", [drafts])
						),
						buttons: [{ label: __("Open bills"), onclick: open(step) }],
				  };
		}),
		...gaps.map((step) => ({
			title: step.is_skipped
				? __("{0} is skipped, but your trial balance has lines for it.", [step.title])
				: __("{0} is not done, and your trial balance has lines for it.", [step.title]),
			description: step.summary || undefined,
			buttons: [{ label: __("Open"), onclick: open(step) }],
		})),
		...get_books_reminders(frm),
		reminders.stock_reposts?.running && {
			title: plural(
				reminders.stock_reposts.running,
				__("1 stock cost update is still running."),
				__("{0} stock cost updates are still running.", [reminders.stock_reposts.running])
			),
			description: __(
				"They usually finish within the hour. Stock values and closing the year wait for them."
			),
		},
		reminders.stock_reposts?.failed && {
			title: plural(
				reminders.stock_reposts.failed,
				__("1 stock cost update failed."),
				__("{0} stock cost updates failed.", [reminders.stock_reposts.failed])
			),
			description: __("Stock values after the opening may be off until it runs again."),
			buttons: [
				{
					label: __("Open"),
					onclick: () => frappe.set_route("List", "Repost Item Valuation", { status: "Failed" }),
				},
			],
		},
		reminders.bank_accounts_without_number && {
			title:
				reminders.bank_accounts_without_number === 1
					? __("1 bank account has no account number.")
					: __("{0} bank accounts have no account number.", [
							reminders.bank_accounts_without_number,
					  ]),
			description: __("Add it before your first bank payment."),
			buttons: [
				{
					label: __("Add bank details"),
					onclick: () => {
						dialog.hide();
						show_tab(frm, "accounts_tab");
						show_bank_accounts(frm);
					},
				},
			],
		},
	]
		.filter(Boolean)
		.map((alert) => ({ theme: "yellow", ...alert }));
	const left = Boolean(temporary || open_steps.length || gaps.length);
	const notes = [
		skipped.length &&
			grey_line(
				__("Skipped: {0}. Your trial balance has nothing waiting for them.", [
					skipped.map((step) => step.title).join(", "),
				])
			),
		reminders.missing_years?.length &&
			(frappe.model.can_create("Fiscal Year")
				? frappe.ui.alert({
						theme: "blue",
						title: __("Going live adds the financial year {0}", [
							reminders.missing_years.join(", "),
						]),
						description: __("Your entries from the migration date on go in it."),
				  })
				: make_alerts([
						{
							theme: "yellow",
							title: __("Ask a System Manager to add the financial year {0}", [
								reminders.missing_years.join(", "),
							]),
							description: __("Your entries from the migration date on go in it."),
							buttons: [
								fix_button(
									{ label: __("New Fiscal Year"), doctype: "Fiscal Year", name: "new" },
									frm
								),
							],
						},
				  ])),
	].filter(Boolean);

	const dialog = new frappe.ui.Dialog({
		title: __("Go live"),
		fields: [{ fieldname: "body", fieldtype: "HTML" }],
		primary_action_label: __("Go live"),
		primary_action: () => show_confirm(),
		secondary_action_label: __("Close"),
		secondary_action: () => dialog.hide(),
	});
	const $body = dialog.get_field("body").$wrapper;

	const show_checks = () => {
		dialog.set_title(__("Go live"));
		$body
			.empty()
			.append(
				$(`<div class="flex flex-col gap-3"></div>`).append(
					grey_line(__("Start using ERPNext. We point out anything left, but nothing stops you.")),
					make_alerts(alerts),
					notes
				)
			);
		dialog.set_primary_action(__("Go live"), () => show_confirm());
		dialog.set_secondary_action_label(__("Close"));
		dialog.set_secondary_action(() => dialog.hide());
	};
	const show_confirm = () => {
		dialog.set_title(__("Go live?"));
		$body
			.empty()
			.append(
				$(`<div class="flex flex-col gap-3"></div>`).append(
					$(`<div class="text-p-base text-ink-gray-8"></div>`).text(
						__("After you go live you can't undo opening entries here.")
					),
					alerts.length
						? [
								$(`<div class="text-sm-medium text-ink-gray-5"></div>`).text(
									__("Still open")
								),
								$(`<div class="flex flex-col gap-2"></div>`).append(
									alerts.map((alert) =>
										$(`<div class="flex items-start gap-2"></div>`).append(
											frappe.ui.badge(
												alert.theme === "red"
													? { label: __("Not done"), theme: "red" }
													: { label: __("Check"), theme: "amber" }
											),
											$(`<div class="text-p-sm text-ink-gray-7 min-w-0"></div>`).text(
												alert.title
											)
										)
									)
								),
						  ]
						: grey_line(__("Your books match your trial balance.")),
					grey_line(__("Nothing changes until you confirm."))
				)
			);
		dialog.set_primary_action(left ? __("Go live anyway") : __("Go live"), () =>
			call_method(frm, "go_live", { accept_temporary_opening: temporary ? 1 : 0 }).then(() =>
				reload_fresh(frm).then(() => show_live())
			)
		);
		dialog.set_secondary_action_label(__("Back to checks"));
		dialog.set_secondary_action(() => show_checks());
	};
	const show_live = () => {
		dialog.set_title(__("Go live"));
		$body.empty().append(make_live_note(frm, () => dialog.hide()));
		dialog.set_primary_action(__("Close"), () => dialog.hide());
		dialog.get_secondary_btn().addClass("hide");
	};
	show_checks();
	dialog.show();
}

// After going live: the company is live, and the first things to do in ERPNext
function make_live_note(frm, before_open) {
	const go = (open) => () => {
		before_open?.();
		open();
	};
	const has_bank = Boolean(frm.doc.__onload?.bank?.accounts);
	const from = frappe.datetime.str_to_user(frm.doc.migration_date);
	return $(`<div class="flex flex-col items-center text-center gap-2 py-3"></div>`).append(
		$(`<div class="text-lg-medium text-ink-gray-8"></div>`).text(__("{0} is live", [frm.doc.company])),
		grey_line(
			has_bank
				? __("Next: make your first invoice · reconcile banks from {0}", [from])
				: __("Next: make your first invoice")
		),
		$(`<div class="flex flex-wrap justify-center gap-2 pt-1"></div>`).append(
			frappe.ui.button({
				label: __("New Sales Invoice"),
				size: "sm",
				onclick: go(() => frappe.new_doc("Sales Invoice", { company: frm.doc.company })),
			}),
			has_bank &&
				frappe.ui.button({
					label: __("Bank Reconciliation"),
					size: "sm",
					onclick: go(() => frappe.set_route("Form", "Bank Reconciliation Tool")),
				})
		)
	);
}

// A grey line of text, as under a window's cards or in a confirm
function grey_line(text) {
	return $(`<div class="text-p-sm text-ink-gray-5"></div>`).text(text);
}

// In an undo confirm: what Accounts Settings does with a deleted document's ledger entries, and a
// choice for this undo, starting from that setting, shown only when a document has ledger entries.
// what: like "5 invoices"; one: like "invoice"; example: a re-post's name; kind, source and names:
// the undo, for the server to say which of its documents have ledger entries. delete_entries()
// gives the choice, null until it shows.
function make_delete_choice(
	frm,
	{ count, what, one = what, example, has_ledger = true, kind, source, names }
) {
	const no_ledger = () =>
		plural(
			count,
			__("This {0} has no ledger entries, so it is deleted.", [one]),
			__("These {0} have no ledger entries, so they are deleted.", [what])
		);
	const $choice = $(`<div class="flex flex-col gap-2"></div>`);
	$choice.delete_entries = () => null;
	if (!has_ledger) return $choice.append(grey_line(no_ledger()));
	const $line = grey_line(__("Reading Accounts Settings…")).appendTo($choice);
	const $options = $(`<div class="flex flex-col gap-2"></div>`).appendTo($choice);
	const $result = $(`<div class="text-p-sm text-ink-gray-7"></div>`).appendTo($choice);
	const group = frappe.utils.get_random(8);
	$choice.delete_entries = () => {
		const picked = $options.find("input:checked").val();
		return picked === undefined ? null : picked === "1";
	};
	const new_numbers = () =>
		example
			? plural(
					count,
					__("Posting again gives it a new number, like {0}.", [example]),
					__("Posting again gives them new numbers, like {0}.", [example])
			  )
			: plural(
					count,
					__("Posting again gives it a new number."),
					__("Posting again gives them new numbers.")
			  );
	// unticked, the documents with no ledger entries are still deleted
	const kept_text = (split) => {
		const kept = count_documents(split.with_ledger);
		const deleted = count_documents(split.without_ledger);
		if (!kept) return no_ledger();
		if (deleted) {
			return (
				__(
					"{0} will be deleted (no ledger entries). {1} will stay cancelled with their ledger entries.",
					[deleted, kept]
				) +
				" " +
				__("Posting again gives them new numbers.")
			);
		}
		return (
			plural(
				count,
				__("This {0} will stay as a cancelled document with its ledger entries.", [one]),
				__("These {0} will stay as cancelled documents with their ledger entries.", [what])
			) +
			" " +
			new_numbers()
		);
	};
	// ticked, every document goes; named by kind when there is more than one kind
	const deleted_text = (split) => {
		const all = { ...split.without_ledger };
		for (const [doctype, n] of Object.entries(split.with_ledger)) all[doctype] = (all[doctype] || 0) + n;
		if (Object.keys(all).length < 2) return null;
		return __("{0} will be deleted, with their ledger entries. Posting again keeps their numbers.", [
			count_documents(all),
		]);
	};
	const say = (deleting, split) =>
		$result.text(
			deleting
				? deleted_text(split) ||
						plural(
							count,
							__(
								"This {0} and its ledger entries will be deleted. Posting again keeps its number.",
								[one]
							),
							__(
								"These {0} and their ledger entries will be deleted. Posting again keeps their numbers.",
								[what]
							)
						)
				: kept_text(split)
		);
	Promise.all([
		get_data(frm, "get_delete_setting"),
		get_data(frm, "get_undo_split", { kind: kind, source: source || "", names: names || null }),
	]).then(([{ message: setting }, { message: split }]) => {
		if (!Object.keys(split.with_ledger).length) {
			$line.text(no_ledger());
			return;
		}
		$line.text(
			setting.delete
				? __("Your Accounts Settings delete ledger entries with the document.")
				: __("Your Accounts Settings keep cancelled copies of deleted entries.")
		);
		const option = (value, title, line) =>
			$(
				`<label class="flex items-start gap-2 border rounded-lg px-3 py-2 mb-0 cursor-pointer"></label>`
			)
				.append(
					$(`<input type="radio" class="mt-1">`)
						.attr({ name: group, value: value })
						.prop("checked", Boolean(value) === Boolean(setting.delete)),
					$(`<span class="flex flex-col"></span>`).append(
						$(`<span class="text-base-medium text-ink-gray-8"></span>`)
							.text(title)
							.append(
								Boolean(value) === Boolean(setting.delete)
									? $(`<span class="text-ink-gray-5"></span>`).text(
											" " + __("(your setting)")
									  )
									: []
							),
						grey_line(line)
					)
				)
				.appendTo($options);
		option(1, __("Remove completely"), __("The numbers can be used again"));
		option(
			0,
			__("Keep a cancelled copy"),
			example ? __("New numbers like {0}", [example]) : __("New numbers when posted again")
		);
		$options.on("change", "input", () => say($choice.delete_entries(), split));
		say(Boolean(setting.delete), split);
	});
	return $choice;
}

// Like "2 assets and 1 journal entry", from counts by doctype
function count_documents(counts) {
	const words = {
		"Sales Invoice": [__("invoice"), __("invoices")],
		"Purchase Invoice": [__("bill"), __("bills")],
		"Journal Entry": [__("journal entry"), __("journal entries")],
		"Stock Reconciliation": [__("stock entry"), __("stock entries")],
		Asset: [__("asset"), __("assets")],
	};
	return frappe.utils.comma_and(
		Object.entries(counts || {}).map(([doctype, n]) => {
			const [single, many] = words[doctype] || [__(doctype), __(doctype)];
			return __("{0} {1}", [n, n === 1 ? single : many]);
		})
	);
}

function make_text(value, classes = "text-ink-gray-8") {
	return $(`<span class="text-base"></span>`).addClass(classes).text(value);
}

// Rows of cells in one bordered box, a line between rows
function make_box(rows) {
	return $(`<div class="flex flex-col border rounded-lg"></div>`).append(
		rows.map((cells, index) =>
			$(`<div class="flex items-center gap-4 px-3 py-2.5"></div>`)
				.toggleClass("border-t", index > 0)
				.append(cells)
		)
	);
}

// A box row's left side: the title, and a grey line under it
function make_title_cell(title, line, title_classes = "text-ink-gray-8 truncate") {
	return $(`<div class="flex flex-col gap-0.5 grow min-w-0"></div>`).append(
		make_text(title, title_classes),
		grey_line(line)
	);
}

function make_view_button(onclick) {
	return frappe.ui.button({ label: __("View"), variant: "ghost", size: "sm", onclick });
}

// The ⋯ menu of a card or window, when it has options
function make_more_menu(options) {
	return options.length
		? frappe.ui.dropdown({
				button: { icon: "ellipsis", variant: "ghost", attrs: { "aria-label": __("More") } },
				options: options,
				align: "end",
		  })
		: [];
}

function make_sheet_buttons(upload, menu = []) {
	return $(`<div class="flex gap-1 shrink-0"></div>`).append(
		upload ? frappe.ui.button({ label: __("Upload sheet"), icon: "upload", onclick: upload }) : [],
		make_more_menu(menu)
	);
}

// A window's head line: what goes in it, with Upload sheet and the ⋯ menu
function make_sheet_head(intro, upload, menu) {
	return $(`<div class="flex items-center justify-between gap-3 mb-3"></div>`).append(
		$(`<div class="text-p-base text-ink-gray-7"></div>`).text(intro),
		make_sheet_buttons(upload, menu)
	);
}

// An amount in the company's currency
function money(frm, value, decimals) {
	return format_currency(value, erpnext.get_currency(frm.doc.company), decimals);
}

// A sheet of the company's rows from one of our modules; undone_from: the rows undone from that source
function download_sheet(company, method, args = {}) {
	open_url_post(`/api/method/erpnext.setup.doctype.company_setup.${method}`, {
		company: company,
		...Object.fromEntries(Object.entries(args).filter(([, value]) => value !== undefined)),
	});
}

// A grid cell: the name, cut short when long, with its badge
function name_with_badge(name_html, badge_html) {
	return `<span class="flex items-center gap-2 min-w-0">
		<span class="truncate">${name_html}</span>
		${badge_html ? `<span class="shrink-0">${badge_html}</span>` : ""}
	</span>`;
}

function new_badge() {
	return frappe.ui.badge.html({ label: __("New"), theme: "blue" });
}

// where rows came from: the sheet's name, or typed in
function source_name(source) {
	return source || __("Typed in");
}

// The first line of a confirm that replaced a window's body: a back arrow and what confirming does
function make_back_line(text, onclick) {
	return $(`<div class="flex items-center gap-2"></div>`).append(
		frappe.ui.button({ icon: "arrow-left", variant: "ghost", size: "sm", tooltip: __("Back"), onclick }),
		$(`<div class="text-p-base text-ink-gray-7"></div>`).text(text)
	);
}

// A sub-view's head: back arrow, title and line, and its buttons on the right
function make_back_header(title, line, onclick, actions) {
	return $(`<div class="flex items-center justify-between gap-3"></div>`).append(
		$(`<div class="flex items-center gap-2 min-w-0"></div>`).append(
			frappe.ui.button({
				icon: "arrow-left",
				variant: "ghost",
				size: "sm",
				tooltip: __("Back"),
				onclick,
			}),
			$(`<div class="flex flex-col min-w-0"></div>`).append(
				$(`<span class="text-base-medium text-ink-gray-8 truncate"></span>`).text(title),
				line ? grey_line(line) : []
			)
		),
		$(`<div class="flex items-center gap-2 shrink-0"></div>`).append(actions || [])
	);
}

// Red and yellow messages, each with its buttons; past three, the rest wait behind "Show N more"
function make_alerts(alerts) {
	const make = (alert) =>
		frappe.ui.alert({
			theme: alert.theme,
			title: alert.title,
			description: alert.description,
			footer: alert.buttons?.length
				? () =>
						$(`<div class="flex gap-2"></div>`).append(
							alert.buttons.map((button) => make_button(button))
						)
				: undefined,
		});
	const $alerts = $(`<div class="flex flex-col gap-3"></div>`).append(alerts.slice(0, 3).map(make));
	if (alerts.length > 3) {
		const $more = $(`<div></div>`)
			.append(
				frappe.ui.button({
					label: __("Show {0} more", [alerts.length - 3]),
					variant: "ghost",
					onclick: () => {
						$more.remove();
						$alerts.append(alerts.slice(3).map(make));
					},
				})
			)
			.appendTo($alerts);
	}
	return $alerts.toggleClass("hide", !alerts.length);
}

// A window's primary button, disabled while it can't be used yet: what stops it shows above
function set_primary_disabled(dialog, disabled) {
	disabled ? dialog.disable_primary_action() : dialog.enable_primary_action();
}

// Label and value lines, as in a confirm
function make_kv_lines(lines) {
	return $(`<div class="flex flex-col"></div>`).append(
		lines.map(([label, value]) =>
			$(`<div class="flex justify-between gap-4 py-2 border-b text-base"></div>`).append(
				$(`<span class="text-ink-gray-6"></span>`).text(label),
				$(`<span class="text-base-medium text-ink-gray-8 text-right"></span>`).text(value)
			)
		)
	);
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

function show_opening_balances(frm, result, replacing) {
	const company = frm.doc.company;
	const abbr = frm.doc.__onload?.company_details?.abbr;
	const short_name = (account) => (abbr ? account.replace(new RegExp(` - ${abbr}$`), "") : account);
	const posted = (frm.doc.opening_balances || []).filter((row) => row.status === "Posted");
	const get_drafts = () => (frm.doc.opening_balances || []).filter((row) => row.status !== "Posted");
	const drafts = get_drafts();
	// a zero is left blank, so the side with the amount stands out
	const amount = (value, df, options, doc) =>
		flt(value) ? frappe.form.formatters.Currency(value, df, options, doc) : "";
	const dialog = new frappe.ui.Dialog({
		title: __("Opening balances"),
		size: "extra-large",
		fields: [
			{ fieldname: "intro", fieldtype: "HTML" },
			{
				fieldname: "balances",
				fieldtype: "Table",
				label: __("Balances"),
				cannot_add_rows: false,
				in_place_edit: true,
				data: drafts.map((row) => ({ ...row, name: undefined })),
				fields: [
					{
						fieldname: "original_account",
						fieldtype: "Data",
						label: __("From Sheet"),
						read_only: 1,
						in_list_view: 1,
						columns: 2,
					},
					{
						fieldname: "account",
						fieldtype: "Link",
						options: "Account",
						label: __("Account"),
						in_list_view: 1,
						columns: 2,
						formatter: (value) => frappe.utils.escape_html(short_name(value || "")),
						// a new account is added with this company's Add account, not the full form
						only_select: 1,
						filter_description: __(
							"Customer, supplier, stock and temporary accounts aren't listed: they come in their own way."
						),
						get_query: () => ({
							filters: [
								["company", "=", company],
								["is_group", "=", 0],
								["disabled", "=", 0],
								["account_type", "not in", OPENING_EXCLUDED_TYPES],
							],
						}),
						onchange() {
							const row = this.doc;
							if (!row || !row.account) return;
							frappe.db
								.get_value("Account", row.account, ["account_type", "root_type"])
								.then((r) => {
									Object.assign(row, r.message || {});
									// the row being edited isn't redrawn by a grid refresh
									for (const fieldname of ["account_type", "root_type"]) {
										this.grid_row?.refresh_field(fieldname);
									}
									update_totals();
								});
						},
					},
					{
						fieldname: "account_type",
						fieldtype: "Data",
						label: __("Account Type"),
						read_only: 1,
						in_list_view: drafts.some((row) => row.account_type) ? 1 : 0,
						columns: 1,
					},
					{
						fieldname: "root_type",
						fieldtype: "Data",
						label: __("Category"),
						read_only: 1,
						in_list_view: 1,
						columns: 1,
					},
					{
						fieldname: "debit",
						fieldtype: "Currency",
						label: __("Debit"),
						in_list_view: 1,
						columns: 2,
						formatter: amount,
						onchange: () => update_totals(),
					},
					{
						fieldname: "credit",
						fieldtype: "Currency",
						label: __("Credit"),
						in_list_view: 1,
						columns: 2,
						formatter: amount,
						onchange: () => update_totals(),
					},
				],
			},
			...where_to_find_fields(frm, "opening_balances", {
				has_data: (frm.doc.opening_balances || []).length > posted.length,
				posted: posted.length,
			}),
		],
		primary_action_label: __("Preview and post"),
		primary_action: () => save().then(() => show_posting_preview(frm)),
		secondary_action_label: __("Save draft"),
		secondary_action: () => save_draft(),
	});
	fill_where(frm, dialog, "opening_balances");
	const save_rows = () =>
		call_method(frm, "save_opening_balances", { rows: dialog.get_values().balances || [] });
	const save = () => save_rows().then(() => close_and_reload(frm, dialog));
	const save_draft = () =>
		save_rows()
			.then(() => reload_fresh(frm))
			.then(() => {
				const grid = dialog.fields_dict.balances.grid;
				grid.df.data = get_drafts().map((row) => ({ ...row, name: undefined }));
				grid.refresh();
				saved_key = get_rows_key(grid);
				update_totals();
				frappe.ui.toast({ type: "success", message: __("Saved as a draft.") });
			});
	let saved_key = get_rows_key(dialog.fields_dict.balances.grid);
	ask_before_closing(
		dialog,
		() => get_rows_key(dialog.fields_dict.balances.grid) !== saved_key,
		() => save_rows().then(() => reload_fresh(frm))
	);

	const $intro = dialog.get_field("intro").$wrapper;
	$(`<div class="flex flex-wrap items-center justify-between gap-2 mb-3"></div>`)
		.append(
			$(`<div class="text-p-sm text-ink-gray-6"></div>`).text(
				__(
					"Bank, cash, loans, capital, tax and the rest, as on {0}. Customers, suppliers and stock come in their own way.",
					[day_before_text(frm)]
				)
			),
			make_sheet_buttons(
				() => upload_opening_balances(frm, dialog),
				[
					{
						label: __("Download sheet"),
						icon: "download",
						onclick: () => download_sheet(company, "chart_sheet.download_opening_balances"),
					},
					{
						label: __("Add account"),
						icon: "plus",
						onclick: () =>
							frappe.model.with_doctype("Account", () => add_balance_account(frm, dialog)),
					},
				]
			)
		)
		.appendTo($intro);
	$intro.append(
		make_posting_reds(frm, frm.doc.__onload?.posting_reds?.balances, () =>
			show_opening_balances(frm, null, dialog)
		)
	);
	const $cards = $(`<div class="es-stat-card-grid company-setup-stats mb-3"></div>`).appendTo($intro);
	// nothing entered yet: the table's place says how to start, and the cards wait for rows
	let typing = false;
	const $empty = $(`<div class="border rounded-lg py-10 mb-3"></div>`)
		.append(
			frappe.ui.empty_state({
				icon: "file-spreadsheet",
				title: __("Upload your trial balance"),
				description: __("Excel or CSV. Each account is read with its debit or credit."),
				actions: [
					{
						label: __("Upload sheet"),
						icon: "upload",
						variant: "solid",
						onclick: () => upload_opening_balances(frm, dialog),
					},
					{
						label: __("Add row"),
						onclick: () => {
							typing = true;
							update_totals();
							dialog.fields_dict.balances.grid.add_new_row();
						},
					},
					{
						label: __("Download sample sheet"),
						variant: "ghost",
						onclick: () => download_sheet(company, "chart_sheet.download_opening_balances"),
					},
				],
			})
		)
		.appendTo($intro);
	if (result) $intro.append(make_upload_result(frm, result));
	// drawn from the rows as they change: see update_totals
	$intro.append($(`<div data-unmatched></div>`));
	// accounts suggested for rows without one, applied only when the user says so
	let suggested = result?.suggestions || null;
	// rows no ledger fits: the ledger to add, with its type and group
	let new_accounts = result?.new_accounts || {};
	$intro.append(make_balance_yellows(frm, dialog));
	if (posted.length) {
		grey_line(
			plural(
				posted.length,
				__("1 balance is posted already and stays as it is."),
				__("{0} balances are posted already and stay as they are.", [posted.length])
			)
		)
			.addClass("mb-3")
			.appendTo($intro);
	}

	const currency = erpnext.get_currency(company);
	const update_totals = () => {
		const rows = dialog.fields_dict.balances.df.data || [];
		const empty = !rows.length && !typing;
		$empty.toggleClass("hide", !empty);
		$cards.toggleClass("hide", empty);
		dialog.set_df_property("balances", "hidden", empty ? 1 : 0);
		const waiting = rows.filter((row) => !row.account && row.original_account);
		if (waiting.length && !suggested) {
			suggested = {};
			frappe
				.call("erpnext.setup.doctype.company_setup.company_setup.suggest_balance_accounts", {
					company: company,
					labels: waiting.map((row) => row.original_account),
					root_types: Object.fromEntries(
						waiting.map((row) => [row.original_account, row.root_type])
					),
					sides: Object.fromEntries(
						waiting.map((row) => [
							row.original_account,
							flt(row.debit) >= flt(row.credit) ? "debit" : "credit",
						])
					),
				})
				.then((r) => {
					suggested = r.message?.suggestions || {};
					new_accounts = r.message?.new_accounts || {};
					update_totals();
				});
		}
		const suggestions = waiting.filter((row) => suggested?.[row.original_account]);
		const to_add = waiting.filter(
			(row) => !suggested?.[row.original_account] && new_accounts[row.original_account]
		);
		const pairs = suggestions.map(
			(row) => `${row.original_account} → ${short_name(suggested[row.original_account])}`
		);
		$intro
			.find("[data-unmatched]")
			.empty()
			.append(
				waiting.length
					? $(`<div class="mb-2"></div>`).append(
							frappe.ui.alert({
								theme: "yellow",
								title: plural(
									waiting.length,
									__("Pick an account for 1 row"),
									__("Pick an account for {0} rows", [waiting.length])
								),
								description:
									[
										suggestions.length === 1 &&
											__("1 close match: {0}. Check it before you use it.", [pairs[0]]),
										suggestions.length > 1 &&
											__("{0} close matches: {1}. Check them before you use them.", [
												suggestions.length,
												pairs.join(", "),
											]),
										to_add.length &&
											plural(
												to_add.length,
												__(
													"No ledger yet for {0}: add it with its type and group filled in.",
													[to_add[0].original_account]
												),
												__(
													"No ledger yet for {0}: add each with its type and group filled in.",
													[to_add.map((row) => row.original_account).join(", ")]
												)
											),
									]
										.filter(Boolean)
										.join(" ") || undefined,
								footer: $(`<div class="flex flex-wrap items-center gap-2"></div>`).append(
									suggestions.length
										? frappe.ui.button({
												label:
													suggestions.length === 1
														? __("Use the match: {0}", [pairs[0]])
														: __("Use these {0} matches", [suggestions.length]),
												size: "sm",
												onclick: () => use_suggestions(suggestions),
										  })
										: [],
									to_add.length
										? frappe.ui.button({
												label: __("Add account"),
												size: "sm",
												onclick: () =>
													frappe.model.with_doctype("Account", () =>
														add_balance_account(frm, dialog, new_accounts)
													),
										  })
										: [],
									frappe.ui.button({
										label: __("Go to next empty row"),
										variant: "outline",
										size: "sm",
										onclick: () => go_to_empty_row(),
									})
								),
							})
					  )
					: []
			);
		const sum = (field) => rows.reduce((total, row) => total + flt(row[field]), 0);
		// what the rows of these categories add up to, on the side they usually sit; posted ones too
		const net = (root_types, sign) =>
			[...rows, ...posted]
				.filter((row) => root_types.includes(row.root_type))
				.reduce((total, row) => total + sign * (flt(row.debit) - flt(row.credit)), 0);
		const debit = sum("debit");
		const credit = sum("credit");
		$cards.empty().append(
			make_lines_card(
				__("Debit and credit"),
				[
					[__("Debit"), money(frm, debit, 0)],
					[__("Credit"), money(frm, credit, 0)],
					[__("Difference"), money(frm, Math.abs(debit - credit), 0), true],
				],
				get_difference_caption(frm, debit - credit)
			),
			// what customers owe, stock and what suppliers are owed are in, so the sides add up
			make_lines_card(
				__("Your books"),
				[
					[
						__("Assets"),
						money(
							frm,
							net(["Asset"], 1) + flt(frm.doc.sheet_receivables) + flt(frm.doc.sheet_stock)
						),
					],
					[
						__("Liabilities and capital"),
						money(frm, net(["Liability", "Equity"], -1) + flt(frm.doc.sheet_payables), 0),
					],
					[__("Profit"), money(frm, net(["Income"], -1) - net(["Expense"], 1), 0), true],
				],
				get_profit_caption(frm)
			),
			get_left_out_card(frm)
		);
	};
	const use_suggestions = (suggestions) =>
		frappe.db
			.get_list("Account", {
				filters: { name: ["in", suggestions.map((row) => suggested[row.original_account])] },
				fields: ["name", "account_type", "root_type"],
				limit: 0,
			})
			.then((accounts) => {
				const by_name = Object.fromEntries(accounts.map((account) => [account.name, account]));
				for (const row of suggestions) {
					const account = by_name[suggested[row.original_account]];
					if (account) {
						Object.assign(row, {
							account: account.name,
							account_type: account.account_type,
							root_type: account.root_type,
						});
					}
				}
				dialog.fields_dict.balances.grid.refresh();
				update_totals();
				frappe.ui.toast({
					type: "success",
					message:
						suggestions.length === 1
							? __("1 account matched: {0}", [
									`${suggestions[0].original_account} → ${short_name(
										suggestions[0].account
									)}`,
							  ])
							: __("{0} accounts matched.", [suggestions.length]),
					duration: 8000,
				});
			});
	// the next row without an account, on its page, with its account field open
	const go_to_empty_row = () => {
		const grid = dialog.fields_dict.balances.grid;
		const index = (grid.df.data || []).findIndex((row) => !row.account && row.original_account);
		if (index < 0) return;
		const pagination = grid.grid_pagination;
		const page = Math.floor(index / (pagination?.page_length || 50)) + 1;
		if (pagination && pagination.page_index !== page) pagination.go_to_page(page);
		const grid_row = grid.grid_rows[index];
		if (!grid_row) return;
		grid_row.row[0].scrollIntoView({ block: "center" });
		grid_row.toggle_editable_row(true);
		grid_row.on_grid_fields_dict?.account?.$input?.focus();
	};
	// the window's own buttons, put back after a view inside it
	dialog.restore_actions = () => {
		dialog.set_primary_action(__("Preview and post"), () => save().then(() => show_posting_preview(frm)));
		dialog.set_secondary_action_label(__("Save draft"));
		dialog.set_secondary_action(() => save_draft());
	};
	dialog.update_totals = update_totals;
	setup_dialog_grid(dialog.fields_dict.balances.grid, update_totals);
	show_in_place_of(dialog, replacing);
	update_totals();
	// an account's type or category may have changed in the chart since the rows were saved
	const picked = (dialog.fields_dict.balances.df.data || []).filter((row) => row.account);
	if (picked.length) {
		frappe.db
			.get_list("Account", {
				filters: { name: ["in", [...new Set(picked.map((row) => row.account))]] },
				fields: ["name", "account_type", "root_type"],
				limit: 0,
			})
			.then((accounts) => {
				const by_name = Object.fromEntries(accounts.map((account) => [account.name, account]));
				for (const row of picked) {
					const account = by_name[row.account];
					if (account) {
						row.account_type = account.account_type;
						row.root_type = account.root_type;
					}
				}
				dialog.fields_dict.balances.grid.refresh();
				update_totals();
			});
	}
}

// What the profit in the balances is: of the year so far, or of a whole year that then needs closing
function get_profit_caption(frm) {
	const period = frm.doc.__onload?.report_period;
	if (!period) return __("Income minus expenses this year. Assets include customers and stock.");
	const from = frappe.datetime.str_to_user(period.from_date);
	const to = frappe.datetime.str_to_user(period.to_date);
	return period.to_date === period.year_end
		? __(
				"Income minus expenses for the year {0} to {1}. Close that year once everything is posted. Assets include customers and stock.",
				[from, to]
		  )
		: __("Income minus expenses from {0} to {1}. Assets include customers and stock.", [from, to]);
}

// A stat card with a few amounts that belong together, the one to read first in bold
function make_lines_card(label, lines, caption, info) {
	const $card = frappe.ui.stat_card({
		label: label,
		value: $(`<div class="flex flex-col gap-3 w-full pt-2 pb-1"></div>`).append(
			lines.map(([name, amount, strong]) =>
				$(`<div class="flex items-baseline justify-between gap-3"></div>`).append(
					$(`<span></span>`)
						.addClass(strong ? "text-base-medium text-ink-gray-8" : "text-base text-ink-gray-6")
						.text(name),
					$(`<span></span>`)
						.addClass(strong ? "text-base-medium text-ink-gray-8" : "text-base text-ink-gray-8")
						.text(amount)
				)
			)
		),
		caption: caption,
	});
	$card.find(".es-stat-card__caption").addClass("border-t pt-3 mt-2");
	if (info) {
		$card
			.find(".es-stat-card__head")
			.append(
				frappe.ui.tooltip(
					$(`<span class="flex text-ink-gray-5" tabindex="0"></span>`)
						.attr("aria-label", info)
						.html(frappe.utils.icon("info", "sm")),
					{ text: info }
				)
			);
	}
	return $card;
}

const OPENING_EXCLUDED_TYPES = ["Receivable", "Payable", "Stock", "Temporary"];

// A new account, in a view inside the balances window; the row picked gets it
function add_balance_account(frm, balances_dialog, new_accounts = {}) {
	const company = frm.doc.company;
	const currency = erpnext.get_currency(company);
	const types = frappe.meta
		.get_docfield("Account", "account_type")
		.options.split("\n")
		.filter((type) => !OPENING_EXCLUDED_TYPES.includes(type));
	const grid = balances_dialog.fields_dict.balances.grid;
	// the rows that can get the new account; the one being edited is picked first
	const waiting = grid.df.data.filter((row) => !row.account && row.original_account);
	const editing = frappe.ui.form.editable_row?.doc;
	let row =
		(waiting.includes(editing) && editing) ||
		waiting.find((option) => new_accounts[option.original_account]) ||
		waiting[0];
	const type_of = (row) => {
		const hint = row && new_accounts[row.original_account];
		return hint ? hint.account_type || "Other" : undefined;
	};
	// the sheet's label may carry the account number, "1001 - Rent Deposit", but "Security
	// Deposit - Office Rent" is all name; "Payroll:Taxes" is a QuickBooks sub-account
	const name_of = (label) => {
		const [number, ...rest] = cstr(label).split(" - ");
		const name = rest.length && /^[\w.]*\d[\w.]*$/.test(number) ? rest.join(" - ") : cstr(label);
		return name.split(":").pop().trim();
	};
	const $layout = balances_dialog.$body.children().first();
	const $view = $(`<div class="flex flex-col gap-3"></div>`).insertBefore($layout);
	const close = () => {
		$view.remove();
		$layout.show();
		balances_dialog.restore_actions();
	};
	$layout.hide();
	$view.append(
		$(`<div class="flex items-center gap-2"></div>`).append(
			frappe.ui.button({
				icon: "arrow-left",
				variant: "ghost",
				size: "sm",
				tooltip: __("Back"),
				onclick: close,
			}),
			$(`<div class="flex flex-col min-w-0"></div>`).append(
				$(`<span class="text-base-medium text-ink-gray-8 truncate"></span>`).text(
					__("Add an account")
				),
				waiting.length ? grey_line(__("The row you pick gets the new account.")) : []
			)
		)
	);
	const no_row = __("No row, only add the account");
	const label_of = (row) =>
		`${row.original_account} · ${format_currency(flt(row.debit) || flt(row.credit), currency)}`;
	const form = new frappe.ui.FieldGroup({
		body: $(`<div></div>`).appendTo($view),
		fields: [
			{ fieldname: "group_note", fieldtype: "HTML" },
			{
				fieldname: "for_row",
				fieldtype: "Select",
				label: __("For row"),
				options: [...waiting.map(label_of), no_row],
				default: row ? label_of(row) : no_row,
				hidden: !waiting.length,
				change: () => {
					row = waiting.find((option) => label_of(option) === form.get_value("for_row"));
					form.set_value("account_name", row ? name_of(row.original_account) : "");
					if (type_of(row)) form.set_value("account_type", type_of(row));
					suggest_parent();
				},
			},
			{
				fieldname: "account_name",
				fieldtype: "Data",
				label: __("Account Name"),
				reqd: 1,
				default: row ? name_of(row.original_account) : "",
				change: () => !row && suggest_parent(),
			},
			{
				fieldname: "account_type",
				fieldtype: "Select",
				label: __("Account Type"),
				reqd: 1,
				// "Other" is an account of no type, like a deposit or a provision
				options: [
					...types.filter(Boolean).map((type) => ({ value: type, label: __(type) })),
					{ value: "Other", label: __("Other") },
				],
				description: __("Bank or Cash for money accounts; Other for most others."),
				default: type_of(row),
				change: () => suggest_parent(),
			},
			{
				fieldname: "parent_account",
				fieldtype: "Link",
				options: "Account",
				label: __("Under"),
				reqd: 1,
				only_select: 1,
				get_query: () => ({ filters: { company: company, is_group: 1 } }),
			},
		],
	});
	form.make();
	// the group the account goes under is suggested from the row's kind and the type picked, never
	// over what the user typed or picked there
	let picked_parent = false;
	const $parent = form.get_field("parent_account").$input;
	$parent.on("input", () => (picked_parent = true));
	// typing replaces the suggested group instead of adding to it
	$parent.on("focus", () => setTimeout(() => $parent.select()));
	// the group of the type picked, on the row's side, like Bank Accounts or Duties and Taxes
	const suggest_parent = () => {
		const account_type = form.get_value("account_type");
		const side = !row ? null : flt(row.debit) >= flt(row.credit) ? "Asset" : "Liability";
		frappe
			.call("erpnext.setup.doctype.company_setup.company_setup.get_account_parent", {
				company: company,
				account_type: account_type === "Other" ? null : account_type,
				root_type: row?.root_type || side,
				account_name: form.get_value("account_name") || "",
			})
			.then((r) => {
				if (!picked_parent) form.set_value("parent_account", r.message || "");
			});
	};
	suggest_parent();
	balances_dialog.set_secondary_action_label(__("Back"));
	balances_dialog.set_secondary_action(close);
	balances_dialog.set_primary_action(__("Add"), async () => {
		const values = form.get_values();
		if (!values) return;
		// an asset line can't go under liabilities, nor the other way
		const parent_root = (await frappe.db.get_value("Account", values.parent_account, "root_type")).message
			?.root_type;
		if (row?.root_type && parent_root && parent_root !== row.root_type) {
			frappe.msgprint({
				title: __("Wrong group"),
				message: __("{0} is on the {1} side, but {2} is under {3}. Pick a group under {1}.", [
					frappe.utils.escape_html(row.original_account),
					__(row.root_type),
					frappe.utils.escape_html(values.parent_account),
					__(parent_root),
				]),
				indicator: "red",
			});
			return;
		}
		frappe
			.call({
				method: "erpnext.accounts.utils.add_ac",
				args: {
					doctype: "Account",
					company: company,
					account_name: values.account_name,
					account_type: values.account_type === "Other" ? "" : values.account_type,
					parent: values.parent_account,
					is_group: 0,
					is_root: 0,
				},
				freeze: true,
			})
			.then((r) =>
				frappe.db.get_value("Account", r.message, ["account_type", "root_type"]).then((info) => {
					if (row) Object.assign(row, { account: r.message, ...info.message });
					else grid.df.data.push({ account: r.message, ...info.message });
					close();
					grid.refresh();
					balances_dialog.update_totals?.();
					frappe.ui.toast({
						type: "success",
						message: row
							? __("{0} now uses {1}.", [row.original_account, values.account_name])
							: __("{0} added.", [values.account_name]),
						duration: 8000,
					});
				})
			);
	});
	show_group_chart_note(company, form.get_field("group_note").$wrapper);
}

// ERPNext keeps one chart for a group of companies: accounts are added in the parent and copied to
// its subsidiaries, and a subsidiary can't add one of its own unless the company allows it
function show_group_chart_note(company, $wrapper) {
	Promise.all([
		frappe.db.get_list("Company", { filters: { parent_company: company }, fields: ["name"] }),
		frappe.db.get_value("Company", company, [
			"parent_company",
			"allow_account_creation_against_child_company",
		]),
	]).then(([subsidiaries, r]) => {
		const { parent_company, allow_account_creation_against_child_company } = r.message || {};
		let note;
		if (subsidiaries.length) {
			note = frappe.ui.alert({
				theme: "blue",
				title: __("Also added to {0}", [subsidiaries.map((row) => row.name).join(", ")]),
				description: __(
					"ERPNext keeps the chart of a group of companies the same, so an account added here is added to its subsidiaries too."
				),
			});
		} else if (parent_company && !allow_account_creation_against_child_company) {
			note = frappe.ui.alert({
				theme: "yellow",
				title: __("Accounts of {0} are added in {1}", [company, parent_company]),
				description: __(
					"ERPNext keeps a group's chart in its parent company. To add accounts here instead, turn on Allow Account Creation Against Child Company on this company."
				),
			});
		}
		if (note) $wrapper.append($(`<div class="mb-3"></div>`).append(note));
	});
}

function upload_opening_balances(frm, dialog) {
	const drafts = (dialog.fields_dict.balances.df.data || []).filter(
		(row) => row.account || flt(row.debit) || flt(row.credit)
	).length;
	upload_sheet(
		frm,
		"import_opening_balances",
		{},
		{
			replaces:
				drafts &&
				__(
					"The sheet replaces the {0} balances in this table, unsaved changes included. Posted balances stay as they are. Continue?",
					[drafts]
				),
			after: (result) => show_opening_balances(frm, result, dialog),
		}
	);
}

// Pick a sheet and read it with method, then draw the window again on what it read. replaces: the
// confirm to ask first, as the sheet replaces drafts.
// The uploader for a sheet: a .xlsx or .csv from this device only
function pick_sheet(on_success) {
	return new frappe.ui.FileUploader({
		restrictions: { allowed_file_types: [".xlsx", ".csv"] },
		disable_file_browser: true,
		allow_web_link: false,
		allow_take_photo: false,
		allow_google_drive: false,
		on_success: on_success,
	});
}

function upload_sheet(frm, method, args, { replaces, after }) {
	const choose = () => {
		// the window is drawn again once the upload's own dialog is gone, so it ends on top
		let closed;
		const uploader = pick_sheet((file) =>
			call_method(frm, method, { ...args, file_url: file.file_url }).then((r) =>
				Promise.all([reload_fresh(frm), closed]).then(() => after(r.message))
			)
		);
		closed = new Promise((resolve) => uploader.dialog.$wrapper.one("hidden.bs.modal", resolve));
	};
	if (!replaces) return choose();
	frappe.confirm(replaces, choose);
}

// What the upload did, and where what it left out goes
function make_upload_result(frm, result) {
	const $result = $(`<div class="flex flex-col gap-1 mb-3"></div>`);
	if (result.skipped) {
		$result.append(
			$(`<div class="mb-2"></div>`).append(
				frappe.ui.alert({
					theme: "yellow",
					title: result.added
						? __("{0} of {1} balances are posted already", [
								result.skipped,
								result.skipped + result.added,
						  ])
						: plural(
								result.skipped,
								__("The balance on this sheet is posted already"),
								__("All {0} balances on this sheet are posted already", [result.skipped])
						  ),
					description: __(
						"They were left out, so nothing counts twice. To change a posted balance, undo the posting first."
					),
				})
			)
		);
	}

	return $result;
}

// What the trial balance holds that comes in on other tabs, each with how far it is
function get_left_out_card(frm) {
	const lines = [
		[__("Customers"), frm.doc.sheet_receivables, __("Selling")],
		[__("Suppliers"), frm.doc.sheet_payables, __("Buying")],
		[__("Stock"), frm.doc.sheet_stock, __("Stock")],
	].filter(([, amount]) => flt(amount, 2));
	if (!lines.length) return [];
	return make_lines_card(
		__("Left out of these balances"),
		lines.map(([label, amount]) => [label, money(frm, amount, 0)]),
		__("They come in on the {0} tabs.", [frappe.utils.comma_and(lines.map(([, , tab]) => tab))])
	);
}

// What the balances' difference waits for: the customer invoices and supplier bills posted so far
// cover none of it, all of it, or part of it
function get_difference_caption(frm, difference) {
	if (!flt(difference, 2)) return __("Debit and credit match.");
	const posted = (table) =>
		(frm.doc[table] || [])
			.filter((row) => row.status === "Created")
			.reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
	const invoices = posted("opening_sales_invoices") - posted("opening_purchase_invoices");
	if (flt(frm.doc.sheet_stock)) {
		const stock = frm.doc.__onload?.stock_match;
		const covered = invoices + flt(stock?.books) + flt(stock?.difference_posted);
		if (!covered) return __("Waiting for your customer invoices, supplier bills and stock to be posted.");
		const left = flt(difference + covered, 2);
		if (Math.abs(left) < 1) return __("Covered by the invoices, bills and stock posted.");
		if (Math.sign(left) !== Math.sign(difference)) {
			return __(
				"The invoices, bills and stock posted are {0} more than this. Check them or the trial balance.",
				[money(frm, Math.abs(left))]
			);
		}
		return __("{0} not covered yet by the invoices, bills and stock posted.", [
			money(frm, Math.abs(left)),
		]);
	}
	if (!invoices) return __("Waiting for your customer invoices and supplier bills to be posted.");
	const left = flt(difference + invoices, 2);
	if (Math.abs(left) < 1) return __("Covered by the customer invoices and supplier bills posted.");
	if (Math.sign(left) !== Math.sign(difference)) {
		return __("The invoices and bills posted are {0} more than this. Check them or the trial balance.", [
			money(frm, Math.abs(left)),
		]);
	}
	return __("{0} not covered yet by the invoices and bills posted.", [money(frm, Math.abs(left))]);
}

// What a sheet held for customers, suppliers or stock, which come in their own way, against the
// invoices and bills entered so far
function left_out_text(frm, left_out = {}) {
	const currency = erpnext.get_currency(frm.doc.company);
	const money = (amount) => format_currency(Math.abs(amount), currency);
	const entered = (table) =>
		(frm.doc[table] || []).reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
	const party_line = (amount, table, is_customer) => {
		const total = entered(table);
		if (!(frm.doc[table] || []).length) {
			return is_customer
				? __("Customers {0} come in as opening invoices in Selling.", [money(amount)])
				: __("Suppliers {0} come in as opening bills in Buying.", [money(amount)]);
		}
		if (Math.abs(Math.abs(total) - Math.abs(amount)) < 1) {
			return is_customer
				? __("Customers {0} match your opening invoices.", [money(amount)])
				: __("Suppliers {0} match your opening bills.", [money(amount)]);
		}
		return is_customer
			? __("Customers {0}: your opening invoices so far add up to {1}.", [money(amount), money(total)])
			: __("Suppliers {0}: your opening bills so far add up to {1}.", [money(amount), money(total)]);
	};
	const lines = [
		left_out.Receivable && party_line(left_out.Receivable, "opening_sales_invoices", true),
		left_out.Payable && party_line(left_out.Payable, "opening_purchase_invoices", false),
		left_out.Stock && __("Stock {0} needs an opening stock entry.", [money(left_out.Stock)]),
	].filter(Boolean);
	if (!lines.length) return "";
	return [__("Not added as balances:"), ...lines].join(" ");
}

// Opening entries are dated the day before the migration date, in a year a new site may not have
function fiscal_year_text(frm) {
	const missing = frm.doc.__onload?.missing_fiscal_year;
	// without the right to add it, the posting reds ask for it instead
	if (!missing || !frappe.model.can_create("Fiscal Year")) return "";
	return __("They're dated {0}; the financial year {1} is added when you post.", [
		frappe.datetime.str_to_user(missing.date),
		missing.year,
	]);
}

// A button that opens where a red is fixed: a closing entry, the company, a dimension. after:
// draws the window again once a dimension's default is set; without it the window closes.
function fix_button(fix, frm, after) {
	if (fix.doctype === "Accounting Dimension" && fix.document_type) {
		return {
			label: fix.label,
			onclick: () =>
				frappe.prompt(
					[
						{
							fieldname: "value",
							fieldtype: "Link",
							options: fix.document_type,
							label: __(fix.document_type),
							reqd: 1,
						},
					],
					(values) =>
						call_method(frm, "set_dimension_default", {
							dimension: fix.name,
							value: values.value,
						}).then(() =>
							after ? reload_fresh(frm).then(after) : close_and_reload(frm, window.cur_dialog)
						),
					fix.label,
					__("Set")
				),
		};
	}
	return {
		label: fix.label,
		onclick: () => {
			// the window would stay over the form it opens
			window.cur_dialog?.hide();
			frappe.set_route("Form", fix.doctype, fix.name);
		},
	};
}

// What stops posting or undoing, as the server's get_posting_reds gives it, shown as a window opens
function make_posting_reds(frm, reds, after) {
	if (!reds?.length) return [];
	return $(`<div class="mb-3"></div>`).append(
		make_alerts(
			reds.map((red) => ({
				theme: "red",
				title: red.text,
				buttons: red.fix ? [fix_button(red.fix, frm, after)] : [],
			}))
		)
	);
}

// After a post or undo: the cards are drawn again from the server before anything else shows,
// so none reads as before the post meanwhile
function close_and_reload(frm, dialog) {
	dialog?.hide();
	return reload_fresh(frm);
}

// A window drawn again shows where the old one was, at its scroll, with no close and open between
function show_in_place_of(dialog, old) {
	if (!old?.display) return dialog.show();
	const scroll = old.$wrapper.scrollTop();
	dialog.animate = false;
	old.$wrapper.removeClass("fade");
	old.hide();
	dialog.show();
	dialog.$wrapper.scrollTop(scroll);
}

function reload_fresh(frm) {
	frm.posting_step = null;
	frappe.dom.freeze(__("Updating…"));
	return frm.reload_doc().then(
		() => frappe.dom.unfreeze(),
		() => frappe.dom.unfreeze()
	);
}

const UNDOING_TEXT = () => __("Undoing in the background. This page updates when it's done.");

// After a post or undo: a job queued says so, and the page updates again when it ends
function after_run(frm, result, queued_text, done) {
	if (result === "queued") {
		frappe.ui.toast({ type: "info", message: queued_text });
		return reload_fresh(frm);
	}
	return reload_fresh(frm).then(() => done?.(result));
}

// Read-only data for a window: unlike call_method, the document does not come back with it
function get_data(frm, method, args = {}, opts = {}) {
	return frappe.call({
		method: "erpnext.setup.doctype.company_setup.company_setup_api." + method,
		args: { company: frm.doc.company, ...args },
		...opts,
	});
}

// The step each posting call posts for
const POSTING_STEPS = {
	post_opening_balances: () => "opening_balances",
	create_opening_invoices: (args) =>
		args.party_type === "Supplier" ? "opening_purchase_invoices" : "opening_sales_invoices",
	post_uncleared_cheques: () => "bank_accounts",
	post_opening_stock: () => "opening_stock",
	post_stock_difference: () => "opening_stock",
	post_fixed_assets: () => "fixed_assets",
	make_open_orders: (args) =>
		Object.keys(ORDER_TYPES).find((name) => ORDER_TYPES[name] === args.order_type),
};

// by name: the form reloads itself after a save, and a call sent mid-reload has no doc to send
function call_method(frm, method, args, opts = {}) {
	const onload = frm.doc.__onload;
	// the card says "Posting…" until the page reloads, not what the drafts add up to
	frm.posting_step = POSTING_STEPS[method]?.(args || {}) || frm.posting_step;
	return frappe
		.call({
			method: "run_doc_method",
			args: { dt: frm.doctype, dn: frm.docname, method: method, args: args },
			...opts,
		})
		.then(
			(r) => {
				// the doc comes back without __onload and the sync drops it from the form
				frm.doc.__onload ??= onload;
				return r;
			},
			(error) => {
				frm.posting_step = null;
				throw error;
			}
		);
}

// When something was posted, as the user reads dates and times, to the minute
function format_time_posted(datetime) {
	if (!datetime) return "";
	return frappe.datetime.str_to_user(datetime).replace(/(\d{1,2}:\d{2}):\d{2}.*$/, "$1");
}

// The books on the day before the migration date; with can_post, as they will be after posting
// on_close: called when the window closes, unless it closed to open another step's window
function show_posting_preview(frm, can_post = true, on_close = null) {
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

function invoice_buttons(frm, party_type) {
	const rows = frm.doc[PARTY[party_type].table] || [];
	// once all are posted there is nothing to edit, only more to add
	const all_posted = rows.length && rows.every((row) => row.status === "Created");
	return [
		{
			label:
				party_type === "Customer"
					? all_posted
						? __("Add invoices")
						: rows.length
						? __("Edit invoices")
						: __("Enter invoices")
					: all_posted
					? __("Add bills")
					: rows.length
					? __("Edit bills")
					: __("Enter bills"),
			onclick: () => show_opening_invoices(frm, party_type),
		},
	];
}

function invoice_menu(frm, party_type) {
	const rows = frm.doc[PARTY[party_type].table] || [];
	return [
		rows.some((row) => row.status === "Created") && {
			label: party_type === "Supplier" ? __("Posted bills") : __("Posted invoices"),
			icon: "list",
			onclick: () => show_posted(frm, party_type),
		},
		rows.length && {
			label: __("Download sheet"),
			icon: "download",
			onclick: () =>
				download_sheet(frm.doc.company, "opening_invoices.download_opening_invoices", {
					party_type: party_type,
				}),
		},
	].filter(Boolean);
}

const POSTED_KINDS = {
	balances: {
		title: __("Posted balances"),
		table: "opening_balances",
		posted: "Posted",
		undo: __("Undo this posting"),
	},
	Customer: {
		title: __("Posted invoices"),
		table: "opening_sales_invoices",
		posted: "Created",
		undo: __("Undo this upload"),
	},
	Supplier: {
		title: __("Posted bills"),
		table: "opening_purchase_invoices",
		posted: "Created",
		undo: __("Undo this upload"),
	},
};

// What was posted, by where it came from: each source to view or undo, until going live
function show_posted(frm, kind, open_undone) {
	const info = POSTED_KINDS[kind];
	const is_balances = kind === "balances";
	const count_of = (count) => (is_balances ? count_balances(count) : count_invoices(count, kind));
	const live = frm.doc.status === "Live";
	const dialog = new frappe.ui.Dialog({
		title: info.title,
		size: "extra-large",
		fields: [
			{ fieldname: "body", fieldtype: "HTML" },
			!is_balances && {
				fieldname: "invoices",
				fieldtype: "Table",
				hidden: 1,
				cannot_add_rows: 1,
				cannot_delete_rows: 1,
				in_place_edit: 1,
				data: [],
				fields: [
					{
						fieldname: "party",
						fieldtype: "Data",
						label: kind === "Supplier" ? __("Supplier") : __("Customer"),
						in_list_view: 1,
						read_only: 1,
						columns: 3,
					},
					{
						fieldname: "account",
						fieldtype: "Link",
						options: "Account",
						label: __("Account"),
						in_list_view: 1,
						read_only: 1,
						columns: 2,
					},
					{
						fieldname: "invoice_number",
						fieldtype: "Dynamic Link",
						options: "invoice_type",
						label: kind === "Supplier" ? __("Bill No") : __("Invoice No"),
						in_list_view: 1,
						read_only: 1,
						columns: 2,
					},
					{
						fieldname: "invoice_date",
						fieldtype: "Date",
						label: __("Date"),
						in_list_view: 1,
						read_only: 1,
						columns: 1,
					},
					{
						fieldname: "outstanding_amount",
						fieldtype: "Currency",
						options: "currency",
						label: __("Amount"),
						in_list_view: 1,
						read_only: 1,
						columns: 2,
					},
					{ fieldname: "currency", fieldtype: "Link", options: "Currency", hidden: 1 },
					{ fieldname: "invoice_type", fieldtype: "Link", options: "DocType", hidden: 1 },
					{ fieldname: "invoice", fieldtype: "Data", hidden: 1 },
				],
			},
		].filter(Boolean),
	});
	const $body = dialog.get_field("body").$wrapper;
	const grid = dialog.fields_dict.invoices?.grid;
	if (grid) setup_dialog_grid(grid);
	// the grid takes a cell's click to edit the row, so a link in it would not open
	grid?.wrapper[0].addEventListener(
		"click",
		(e) => {
			const name = e.target.closest("a[href]") && e.target.closest(".grid-row")?.dataset.name;
			const row = name && grid.grid_rows_by_docname[name]?.doc;
			if (!row?.invoice) return;
			e.preventDefault();
			e.stopPropagation();
			dialog.hide();
			frappe.set_route("Form", row.invoice_type, row.invoice);
		},
		true
	);
	const show_grid = (rows, read_only = false) => {
		if (!grid) return;
		dialog.set_df_property("invoices", "hidden", rows ? 0 : 1);
		dialog.set_df_property("invoices", "read_only", read_only ? 1 : 0);
		grid.df.data = rows || [];
		grid.refresh();
	};
	let posted;

	const set_footer = (primary, on_primary, back) => {
		dialog.set_primary_action(primary, on_primary);
		dialog.get_primary_btn().removeAttr("data-theme");
		dialog.get_secondary_btn().toggleClass("hide", !back);
		if (back) {
			dialog.set_secondary_action_label(__("Back"));
			dialog.set_secondary_action(back);
		}
	};
	const count_undone = (group) =>
		is_balances
			? count_of(group.count)
			: count_entries(
					group.count,
					(frm.doc[UNDONE_TABLES[kind]] || []).filter(
						(row) => (row.source || "") === group.source && flt(row.outstanding_amount) < 0
					).length,
					kind
			  );
	const line_of = (group) =>
		[
			group.posted_on && __("Posted {0}", [format_time_posted(group.posted_on)]),
			is_balances
				? [
						count_of(group.count),
						plural(
							group.entries.length,
							__("1 journal entry"),
							__("{0} journal entries", [group.entries.length])
						),
				  ].join(" · ")
				: count_entries(group.count, group.advances || 0, kind),
			group.unposted && __("{0} not posted", [group.unposted]),
		]
			.filter(Boolean)
			.join(" · ");
	const show_list = () => {
		dialog.set_title(info.title);
		set_footer(__("Close"), () => dialog.hide());
		show_grid(null);
		const rows = posted.groups.map((group) => [
			make_title_cell(source_name(group.source), line_of(group)),
			!is_balances &&
				make_text(group.count ? money(frm, group.amount) : "—", "text-ink-gray-8 shrink-0"),
			make_view_button(() => show_group(group)),
		]);
		if (posted.outside.count) {
			rows.push([
				make_title_cell(
					__("Made outside Company Setup"),
					is_balances
						? __(
								"{0} opening journal entries from Data Import or by hand · counted in your totals",
								[posted.outside.count]
						  )
						: __("{0} opening entries from Data Import or by hand · counted in your totals", [
								posted.outside.count,
						  ]),
					"text-ink-gray-7"
				),
				!is_balances && make_text(money(frm, posted.outside.amount), "text-ink-gray-8 shrink-0"),
				make_view_button(() => show_outside()),
			]);
		}
		const undone_rows = (posted.undone || []).map((group) => [
			make_title_cell(
				source_name(group.source),
				[
					group.more_than_once
						? __("Last undone {0}", [format_time_posted(group.undone_on)])
						: __("Undone {0}", [format_time_posted(group.undone_on)]),
					count_undone(group),
					__("taken out of your books"),
				].join(" · ")
			),
			make_text(money(frm, group.amount), "text-ink-gray-8 shrink-0"),
			make_view_button(() => show_undone(group)),
		]);
		$body
			.empty()
			.append(
				$(`<div class="text-p-base text-ink-gray-7 mb-3"></div>`).text(
					live
						? __("One row for each sheet or typed-in batch.")
						: is_balances
						? __("You can undo these until you go live. The balances go back to drafts.")
						: kind === "Supplier"
						? __("You can undo these until you go live. Paid bills can't be undone.")
						: __("You can undo these until you go live. Paid invoices can't be undone.")
				),
				live ? [] : make_posting_reds(frm, frm.doc.__onload?.posting_reds?.undo?.[kind]),
				make_box(rows.map((cells) => cells.filter(Boolean))),
				undone_rows.length
					? [
							$(`<div class="text-base-medium text-ink-gray-8 mt-5 mb-2"></div>`).text(
								__("Undone")
							),
							make_box(undone_rows),
					  ]
					: []
			);
	};

	const header = (title, line, actions) =>
		make_back_header(title, line, () => show_list(), actions).addClass("mb-3");
	const link = (doctype, name) => $(frappe.utils.get_form_link(doctype, name, true));

	const show_group = (group) => {
		set_footer(__("Close"), () => dialog.hide());
		// a sheet's rows not posted yet show too: undoing the sheet takes them as well
		const rows = (frm.doc[info.table] || []).filter(
			(row) => (is_balances ? row.status === info.posted : true) && (row.source || "") === group.source
		);
		if (!is_balances) {
			const $selected = $(`<div></div>`);
			const $whole = live
				? $()
				: frappe.ui.button({
						label: info.undo,
						variant: "outline",
						size: "sm",
						onclick: () => show_confirm(group),
				  });
			const update_selected = () => {
				const selected = grid.get_selected_children().filter((row) => row.invoice);
				$whole.toggleClass("hide", !!selected.length);
				$selected.empty();
				if (!live && selected.length) {
					$selected.append(
						frappe.ui.button({
							label: __("Undo {0} selected · {1}", [
								selected.length,
								money(
									frm,
									selected.reduce((sum, row) => sum + flt(row.outstanding_amount), 0)
								),
							]),
							icon: "undo-2",
							theme: "red",
							size: "sm",
							onclick: () => show_confirm(group, selected),
						})
					);
				}
			};
			$body
				.empty()
				.append(
					header(
						source_name(group.source),
						line_of(group),
						$(`<div class="flex items-center gap-2"></div>`).append($selected, $whole)
					)
				);
			show_grid(
				rows.map((row) => ({
					party: row.party || row.party_name,
					account: row.account || posted.accounts[row.invoice] || "",
					// ERPNext's name, which can differ from the sheet's after a re-post; an advance's
					// entry keeps the sheet's number, like a credit memo's
					invoice_number:
						(row.status === info.posted &&
							!(flt(row.outstanding_amount) < 0 && row.invoice_number) &&
							row.invoice) ||
						row.invoice_number,
					invoice_date: row.invoice_date,
					outstanding_amount: row.outstanding_amount,
					currency: erpnext.get_currency(frm.doc.company),
					invoice_type:
						row.status !== info.posted
							? ""
							: flt(row.outstanding_amount) < 0
							? "Journal Entry"
							: PARTY[kind].doctype,
					invoice: row.status === info.posted ? row.invoice : "",
				}))
			);
			grid.wrapper.off("change.undo").on("change.undo", ".grid-row-check", update_selected);
			return;
		}
		const cancelled = new Set(posted.cancelled);
		$body.empty().append(
			header(
				source_name(group.source),
				line_of(group),
				!live &&
					frappe.ui.button({
						label: info.undo,
						variant: "outline",
						size: "sm",
						onclick: () => show_confirm(group),
					})
			),
			make_search_table(
				[
					[__("From sheet")],
					[__("Account")],
					[__("Debit"), true],
					[__("Credit"), true],
					[__("Entry"), true],
				],
				rows,
				(row) => [
					row.original_account || "",
					row.account || "",
					flt(row.debit) ? money(frm, row.debit) : "",
					flt(row.credit) ? money(frm, row.credit) : "",
					cancelled.has(row.journal_entry)
						? make_text(__("Cancelled in ERPNext"), "text-ink-red-5")
						: link("Journal Entry", row.journal_entry),
				]
			)
		);
	};

	const show_undone = (group) => {
		set_footer(__("Close"), () => dialog.hide());
		$body.empty().append(
			header(
				__("Undone from {0}", [source_name(group.source)]),
				__("{0} · taken out of your books. Fix them and upload again.", [count_undone(group)]),
				frappe.ui.button({
					label: __("Download"),
					icon: "download",
					variant: "outline",
					size: "sm",
					onclick: () =>
						download_sheet(frm.doc.company, "opening_invoices.download_opening_invoices", {
							party_type: kind,
							undone_from: group.source || "",
						}),
				})
			)
		);
		show_grid(
			(frm.doc[UNDONE_TABLES[kind]] || [])
				.filter((row) => (row.source || "") === group.source)
				.map((row) => ({
					party: row.party || row.party_name,
					account: row.account || "",
					invoice_number: row.invoice_number,
					invoice_date: row.invoice_date,
					outstanding_amount: row.outstanding_amount,
					currency: erpnext.get_currency(frm.doc.company),
					invoice_type: "",
					invoice: "",
				})),
			true
		);
	};

	const show_outside = () => {
		set_footer(__("Close"), () => dialog.hide());
		show_grid(null);
		const outside = posted.outside;
		const doctype = is_balances ? "Journal Entry" : PARTY[kind].doctype;
		$body.empty().append(
			header(
				__("Made outside Company Setup"),
				[
					outside.count > outside.rows.length
						? __("The first {0} of {1}", [outside.rows.length, outside.count])
						: count_of(outside.count),
					__("undo these where they were made"),
				].join(" · ")
			),
			make_search_table(
				[
					[__("Entry")],
					[is_balances ? __("Type") : kind === "Supplier" ? __("Supplier") : __("Customer")],
					[__("Date")],
					[__("Amount"), true],
				],
				outside.rows,
				(row) => [
					link(doctype, row.name),
					row.party || "",
					frappe.datetime.str_to_user(row.posting_date),
					money(frm, row.amount),
				]
			)
		);
	};

	const show_confirm = (group, selected) => {
		if (selected) return show_confirm_selected(group, selected);
		show_grid(null);
		dialog.set_title(is_balances ? __("Undo this posting?") : __("Undo this upload?"));
		const others = is_balances ? group.affected - group.count : 0;
		const $choice = make_delete_choice(
			frm,
			is_balances
				? {
						count: group.entries.length,
						kind: kind,
						source: group.source,
						one: __("journal entry"),
						what: plural(
							group.entries.length,
							__("1 journal entry"),
							__("{0} journal entries", [group.entries.length])
						),
				  }
				: {
						count: group.count,
						kind: kind,
						source: group.source,
						what: count_entries(group.count, group.advances || 0, kind),
						one: group.advances
							? kind === "Supplier"
								? __("debit or advance")
								: __("credit or advance")
							: invoice_word(kind),
						example: group.example,
				  }
		);
		$body
			.empty()
			.append(
				$(`<div class="flex flex-col gap-3"></div>`).append(
					$choice,
					$(`<div class="text-p-base text-ink-gray-7"></div>`).text(
						is_balances
							? __(
									"Their balances go back to drafts, so you can fix them or upload the right sheet, then post again."
							  )
							: __(
									"The rows move to Undone, so you can download them, fix them and upload again. Other uploads stay as they are, and so do the customers or suppliers made from it."
							  )
					),
					is_balances
						? make_box(
								group.entries.map((entry) => [
									link("Journal Entry", entry.name).addClass("shrink-0"),
									make_text(__(entry.voucher_type || ""), "text-ink-gray-6 grow"),
									make_text(money(frm, entry.total), "text-ink-gray-8 shrink-0"),
								])
						  )
						: make_box(
								[
									[
										make_text(source_name(group.source), "text-ink-gray-8 grow truncate"),
										make_text(
											count_entries(group.count, group.advances || 0, kind),
											"text-ink-gray-6 shrink-0"
										),
										make_text(money(frm, group.amount), "text-ink-gray-8 shrink-0"),
									],
									group.unposted && [
										make_text(
											plural(
												group.unposted,
												__("1 row not posted is removed too"),
												__("{0} rows not posted are removed too", [group.unposted])
											),
											"text-ink-gray-6 grow"
										),
									],
									group.paid && [
										make_text(
											__("{0} have a payment against them, so they stay posted", [
												group.paid,
											]),
											"text-ink-gray-6 grow"
										),
									],
								].filter(Boolean)
						  ),
					others > 0 &&
						grey_line(
							__("These entries hold {0} other balances too. They go back to drafts as well.", [
								others,
							])
						)
				)
			);
		set_footer(
			is_balances ? __("Undo posting") : __("Undo upload"),
			() =>
				call_method(frm, "undo_posting", {
					kind: kind,
					source: group.source,
					delete_entries: $choice.delete_entries(),
				}).then((r) => {
					dialog.hide();
					after_run(frm, r.message, UNDOING_TEXT(), (result) =>
						show_undo_result(frm, kind, result, group.source)
					);
				}),
			() => show_list()
		);
		dialog.get_primary_btn().attr("data-theme", "red");
	};

	const show_confirm_selected = (group, selected) => {
		show_grid(null);
		const title = count_entries(
			selected.length,
			selected.filter((row) => flt(row.outstanding_amount) < 0).length,
			kind
		);
		dialog.set_title(__("Undo {0}?", [title]));
		const example = selected.find((row) => flt(row.outstanding_amount) >= 0)?.invoice;
		const $choice = make_delete_choice(frm, {
			count: selected.length,
			kind: kind,
			source: group.source,
			names: selected.map((row) => row.invoice),
			what: title,
			one:
				flt(selected[0]?.outstanding_amount) >= 0
					? invoice_word(kind)
					: kind === "Supplier"
					? __("debit or advance")
					: __("credit or advance"),
			example: example && `${example}-1`,
		});
		$body.empty().append(
			$(`<div class="flex flex-col gap-3"></div>`).append(
				$choice,
				$(`<div class="text-p-base text-ink-gray-7"></div>`).text(
					plural(
						selected.length,
						__("You can download it, fix it and upload again."),
						__("You can download them, fix them and upload again.")
					)
				),
				make_box([
					...selected.map((row) => [
						make_text(row.invoice_number || row.invoice, "text-ink-gray-8 shrink-0"),
						make_text(row.party, "text-ink-gray-6 grow truncate"),
						make_text(money(frm, row.outstanding_amount), "text-ink-gray-8 shrink-0"),
					]),
					[
						make_text(__("Total"), "text-ink-gray-6 grow"),
						make_text(
							money(
								frm,
								selected.reduce((sum, row) => sum + flt(row.outstanding_amount), 0)
							),
							"text-base-medium text-ink-gray-8 shrink-0"
						),
					],
				])
			)
		);
		set_footer(
			__("Undo {0}", [title]),
			() =>
				call_method(frm, "undo_posting", {
					kind: kind,
					source: group.source,
					invoices: selected.map((row) => row.invoice),
					delete_entries: $choice.delete_entries(),
				}).then((r) => {
					dialog.hide();
					after_run(frm, r.message, UNDOING_TEXT(), (result) =>
						show_undo_result(frm, kind, result, group.source)
					);
				}),
			() => {
				dialog.set_title(info.title);
				show_group(group);
			}
		);
		dialog.get_primary_btn().attr("data-theme", "red");
	};

	get_data(frm, "get_posted_entries", { kind: kind }).then((r) => {
		posted = r.message;
		const undone = (open_undone && posted.undone) || [];
		undone.length === 1 ? show_undone(undone[0]) : show_list();
		dialog.show();
	});
}

const UNDONE_TABLES = { Customer: "undone_sales_invoices", Supplier: "undone_purchase_invoices" };

// What an undo did, and what it couldn't
// Entries an undo had to keep cancelled, and why: one line
function kept_cancelled_line(result) {
	const kept = result?.kept_cancelled || [];
	if (!kept.length) return "";
	return kept.length === 1 ? kept[0] : __("{0} entries are kept cancelled. {1}", [kept.length, kept[0]]);
}

function show_undo_result(frm, kind, result, source) {
	const kept = kept_cancelled_line(result);
	if (kept) {
		frappe.ui.toast({ type: "info", message: kept, duration: 15000 });
	}
	if (kind === "assets") {
		frappe.ui.toast({
			type: "success",
			message: __("{0} undone", [count_assets(result.removed)]),
			description: __(
				"They're taken out of your books. Download them from Posted assets to fix and upload again."
			),
			action: {
				label: __("Download"),
				onclick: () =>
					download_sheet(frm.doc.company, "fixed_assets.download_fixed_assets", {
						undone_from: source || "",
					}),
			},
			duration: 10000,
		});
		return;
	}
	if (kind === "stock") {
		frappe.ui.toast({
			type: "success",
			message: __("{0} undone", [count_rows(result.removed)]),
			description: __("They're taken out of your books and kept under Undone in Posted stock."),
			action: {
				label: __("Download"),
				onclick: () =>
					download_sheet(frm.doc.company, "opening_stock.download_opening_stock", {
						undone_from: source || "",
					}),
			},
			duration: 10000,
		});
		return;
	}
	if (kind === "cheques") {
		frappe.ui.toast({
			type: "success",
			message: __("{0} undone", [count_rows(result.removed)]),
			description: cheque_words(
				__("They're taken out of your books and kept under Undone in Posted cheques.")
			),
			action: {
				label: __("Download"),
				onclick: () =>
					download_sheet(frm.doc.company, "bank_accounts.download_uncleared_cheques", {
						undone_from: source || "",
					}),
			},
			duration: 10000,
		});
		return;
	}
	if (kind === "balances") {
		frappe.ui.toast({
			type: "success",
			message: [
				plural(result.entries, __("1 entry undone."), __("{0} entries undone.", [result.entries])),
				plural(
					result.drafts,
					__("1 balance is a draft again."),
					__("{0} balances are drafts again.", [result.drafts])
				),
			].join(" "),
		});
		return;
	}
	frappe.ui.toast({
		type: "success",
		message: __("{0} undone", [count_invoices(result.removed, kind)]),
		description: [
			result.removed &&
				(kind === "Supplier"
					? __("They're taken out of your books and kept under Undone in Posted bills.")
					: __("They're taken out of your books and kept under Undone in Posted invoices.")),
			result.unposted &&
				plural(
					result.unposted,
					__("1 row not posted removed."),
					__("{0} rows not posted removed.", [result.unposted])
				),
			result.kept && __("{0} with a payment stay posted.", [result.kept]),
		]
			.filter(Boolean)
			.join(" "),
		action: result.removed
			? {
					label: __("Download"),
					onclick: () =>
						download_sheet(frm.doc.company, "opening_invoices.download_opening_invoices", {
							party_type: kind,
							undone_from: source || "",
						}),
			  }
			: undefined,
		duration: result.removed ? 10000 : 5000,
	});
	if (result.failed?.length) {
		frappe.msgprint({
			title: __("Some were not cancelled"),
			message: result.failed.map((line) => frappe.utils.escape_html(line)).join("<br>"),
			indicator: "orange",
		});
	}
}

function show_opening_invoices(frm, party_type, result, replacing) {
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

function make_opening_invoice_stats(frm, party_type, state, currency) {
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

function upload_opening_invoices(frm, party_type, dialog) {
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
function post_opening_invoices(
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
function ledger_over_title(frm, over, currency) {
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
function get_ledger_overs(frm, party_type, ticked = [], disabled = new Set(), moved = {}) {
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
function make_hand_posted_test(names) {
	const key = (party, number, amount) => [party || "", number || "", flt(amount, 2)].join("\u0001");
	const keys = new Set(
		(names.hand_posted || []).map((row) => key(row.party, row.invoice_number, row.amount))
	);
	return (row) => keys.has(key(row.party, row.invoice_number, row.outstanding_amount));
}

function invoice_word(party_type) {
	return party_type === "Customer" ? __("invoice") : __("bill");
}

function count_invoices(count, party_type) {
	if (party_type === "Customer") {
		return plural(count, __("1 invoice"), __("{0} invoices", [count]));
	}
	return plural(count, __("1 bill"), __("{0} bills", [count]));
}

// Like "4 invoices stay as drafts (₹ 65,52,970): 2 in USD, 2 for a disabled customer"
function get_left_out_text(left_out, money) {
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
function count_entries(count, advances, party_type) {
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
function show_posting_result(frm, party_type, new_names = []) {
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

function import_buttons(frm, doctype, step) {
	if (!frappe.model.can_create("Data Import")) return [];
	return [
		{
			label: step?.is_complete
				? __("Import more")
				: doctype === "Customer"
				? __("Import customers")
				: __("Import suppliers"),
			disabled: !!import_blocked(frm, doctype),
			tooltip: import_blocked(frm, doctype),
			onclick: () => open_party_import(frm, doctype),
		},
	];
}

// why this user can't import the sheet, or nothing when they can
function import_blocked(frm, doctype) {
	const reason = frm.doc.__onload?.import_blocked?.[doctype];
	if (!reason || doctype !== "BOM") return reason;
	return `${reason} ${__("Make your BOMs in BOM Creator.")}`;
}

// items with batch or serial numbers import only once Stock Settings tracks them: asked before
// the first items import opens, while they are off
function ask_serial_and_batch(frm) {
	return new Promise((resolve) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Batch or serial numbers?"),
			fields: [{ fieldname: "body", fieldtype: "HTML" }],
		});
		const answer = (yes) => {
			dialog.hide();
			if (!yes) return resolve();
			call_method(frm, "turn_on_serial_and_batch")
				.then(() => reload_fresh(frm))
				.then(resolve);
		};
		dialog.get_field("body").$wrapper.append(
			$(`<div class="flex flex-col gap-3"></div>`).append(
				$(`<div class="text-p-base text-ink-gray-8"></div>`).text(
					__("Do any of your items use batch or serial numbers?")
				),
				grey_line(__("Pick No if you don't track batches. You can turn it on per item later.")),
				$(`<div class="flex justify-end gap-2"></div>`).append(
					// most companies don't track batches
					frappe.ui.button({ label: __("Yes"), onclick: () => answer(true) }),
					frappe.ui.button({ label: __("No"), variant: "solid", onclick: () => answer(false) })
				)
			)
		);
		dialog.show();
	});
}

// customers, suppliers and items come as other software exports them: the sheet is read here first,
// so the title rows above its headings are left out before Data Import takes the first row
const TITLED_IMPORTS = ["Customer", "Supplier", "Item"];

function open_party_import(frm, doctype, after = null, asked = false) {
	if (!frappe.model.can_create("Data Import")) return;
	if (import_blocked(frm, doctype)) {
		return frappe.msgprint({ title: __("Can't import"), message: import_blocked(frm, doctype) });
	}
	if (doctype === "Item" && !asked && frm.doc.__onload?.serial_batch_off && !frm.doc.__onload?.has_items) {
		return ask_serial_and_batch(frm).then(() => open_party_import(frm, doctype, after, true));
	}
	const open = (data_import) =>
		frappe.require("data_import_tools.bundle.js", () => {
			// a BOM is used only once submitted; a BOM's sub-assemblies need theirs first
			if (doctype === "BOM") frappe.route_options = { submit_after_import: 1 };
			frappe.data_import.open_data_import_dialog({
				data_import: data_import,
				reference_doctype: doctype,
				import_type: "Insert New Records",
				title: doctype === "BOM" ? __("Import BOMs: sub-assemblies first") : null,
				// the counts on the tasks change after an import
				on_close: () => reload_fresh(frm).then(() => after?.()),
			});
		});
	if (!TITLED_IMPORTS.includes(doctype)) return open(null);
	let picked = false;
	const uploader = pick_sheet((file) => {
		picked = true;
		call_method(frm, "prepare_import", { doctype: doctype, file_url: file.file_url }).then((r) =>
			open(r.message)
		);
	});
	// closed with no file: back to where the import was opened from
	uploader.dialog?.$wrapper.on("hidden.bs.modal", () => !picked && after?.());
}

// The chart to edit; until a chart is chosen, the one made during setup is confirmed from here
function show_chart(frm) {
	const company = frm.doc.company;
	const chosen = !!frm.doc.chart_of_accounts_source;
	const dialog = new frappe.ui.Dialog({
		title: __("Chart of accounts"),
		size: "large",
		fields: [{ fieldname: "chart", fieldtype: "HTML" }],
		primary_action_label: chosen ? __("Done") : __("Use this chart"),
		primary_action: () =>
			chosen
				? dialog.hide()
				: call_method(frm, "use_existing_chart")
						.then(() => reload_fresh(frm))
						.then(() => dialog.hide()),
		secondary_action_label: __("Open full page"),
		secondary_action: () => {
			frappe.route_options = { company: company };
			frappe.set_route("Tree", "Account");
		},
	});
	new CompanyChart(company, dialog.get_field("chart").$wrapper);
	dialog.show();
}

// The company's accounts with the same rows and actions as the Chart of Accounts page
class CompanyChart {
	constructor(company, $wrapper) {
		this.company = company;
		this.make_header($wrapper);
		this.$body = $(`<div></div>`).appendTo($wrapper);
		// loads the Account tree settings too, for the labels and the new account fields
		frappe.model.with_doctype("Account", () => this.make_tree());
	}

	make_header($wrapper) {
		const $header = $(`<div class="flex items-center gap-2 mb-3">
			<div class="text-p-sm text-ink-gray-6 grow min-w-0"></div>
		</div>`).appendTo($wrapper);
		$header.find(".text-p-sm").text(__("Add, rename or delete accounts. Changes are saved right away."));

		this.$toggle = $(
			frappe.ui.button({
				label: __("Collapse all"),
				variant: "ghost",
				onclick: () => this.toggle_all(),
			})
		).appendTo($header);
	}

	make_tree() {
		this.tree = new frappe.ui.Tree({
			parent: this.$body,
			label: this.company,
			root_value: this.company,
			expandable: true,
			use_row_actions: true,
			args: { doctype: "Account", company: this.company },
			method: "erpnext.accounts.utils.get_children",
			get_label: frappe.treeview_settings.Account.get_label,
			on_render: (node) => this.add_badges(node),
			on_node_render: open_all_once(),
			toolbar: this.get_actions(),
		});
	}

	add_badges(node) {
		if (node.is_root || !node.data) return;
		const flags = [];
		if (node.data.account_number) {
			flags.push(frappe.ui.badge({ label: node.data.account_number }));
		}
		const currency = node.data.account_currency;
		if (currency && currency !== erpnext.get_currency(this.company)) {
			flags.push(frappe.ui.badge({ label: currency, theme: "blue" }));
		}
		erpnext.utils.render_tree_node_flags(node, flags);
	}

	get_actions() {
		return [
			{
				label: __("Edit"),
				icon: "pencil",
				inline: true,
				condition: (node) => !node.is_root,
				click: (node) => frappe.set_route("Form", "Account", node.label),
			},
			{
				label: __("Add account"),
				icon: "plus",
				condition: (node) => node.expandable && frappe.model.can_create("Account"),
				click: (node) => this.add_account(node),
			},
			{
				label: __("Rename"),
				icon: "text-cursor-input",
				condition: (node) => !node.is_root && frappe.model.can_write("Account"),
				click: (node) => this.rename_account(node),
			},
			{
				label: __("Delete"),
				icon: "trash",
				danger: true,
				condition: (node) => !node.is_root && frappe.model.can_delete("Account"),
				click: (node) => frappe.model.delete_doc("Account", node.label, () => node.parent.remove()),
			},
		];
	}

	add_account(parent) {
		const dialog = new frappe.ui.Dialog({
			title: __("New account under {0}", [parent.data.account_name || parent.label]),
			fields: frappe.treeview_settings.Account.fields,
			primary_action_label: __("Add"),
			primary_action: (values) =>
				frappe
					.call({
						method: "erpnext.accounts.utils.add_ac",
						args: {
							...values,
							doctype: "Account",
							company: this.company,
							parent: parent.label,
							is_root: parent.is_root ? 1 : 0,
						},
						freeze: true,
					})
					.then(() => {
						dialog.hide();
						this.tree.load_children(parent);
					}),
		});
		dialog.show();
	}

	// ERPNext's own rename: it also rebuilds the "number - name - abbr" account name
	rename_account(node) {
		const dialog = new frappe.ui.Dialog({
			title: __("Rename account"),
			fields: [
				{
					fieldname: "account_name",
					fieldtype: "Data",
					label: __("Account Name"),
					reqd: 1,
					default: node.data.account_name,
				},
				{
					fieldname: "account_number",
					fieldtype: "Data",
					label: __("Account Number"),
					default: node.data.account_number,
				},
			],
			primary_action_label: __("Rename"),
			primary_action: (values) =>
				frappe
					.call({
						method: "erpnext.accounts.doctype.account.account.update_account_number",
						args: { name: node.label, ...values },
						freeze: true,
					})
					.then(() => {
						dialog.hide();
						this.tree.load_children(node.parent_node);
					}),
		});
		dialog.show();
	}

	toggle_all() {
		const expand = ["collapsed", "partial"].includes(this.tree.get_expansion_state());
		Promise.resolve(this.tree.load_children(this.tree.root_node, expand)).then(() =>
			this.$toggle.find(".es-button__label").text(expand ? __("Collapse all") : __("Expand all"))
		);
	}
}

// Opens every account of a tree once its first level is in
function open_all_once() {
	let opened = false;
	return function (node, deep) {
		if (node.is_root && !deep && !opened) {
			opened = true;
			this.load_children(node, true);
		}
	};
}

// Two steps, like the invoices: the sheet, then what it will do, before anything changes
function show_chart_import(frm) {
	const company = frm.doc.company;
	const dialog = new frappe.ui.Dialog({
		title: __("Import a chart of accounts"),
		size: "large",
		fields: [
			{ fieldname: "steps", fieldtype: "HTML" },
			{
				fieldname: "file",
				fieldtype: "Attach",
				label: __("Your chart as a sheet"),
				description: __(
					"Excel or CSV, one row per account. Fill the Opening Debit and Opening Credit columns to bring balances too."
				),
				options: { restrictions: { allowed_file_types: [".csv", ".xlsx", ".xls"] } },
				change: () => preview(),
			},
			{ fieldname: "downloads", fieldtype: "HTML" },
			{ fieldname: "review", fieldtype: "HTML", hidden: 1 },
			{
				fieldname: "new_chart_section",
				fieldtype: "Section Break",
				label: __("View the new chart"),
				collapsible: 1,
				hidden: 1,
			},
			{ fieldname: "new_chart", fieldtype: "HTML" },
			...where_to_find_fields(frm, "chart_of_accounts", {
				has_data: true,
				posted: frm.doc.chart_of_accounts_source === "Imported",
			}),
		],
		primary_action_label: __("Next"),
		primary_action: () => show_step(1),
	});
	fill_where(frm, dialog, "chart_of_accounts");

	let last_preview;
	// what each group of a Busy list that Busy doesn't ship holds, as the user says
	const group_kinds = {};
	const stepper = new frappe.ui.Stepper({
		steps: [{ label: __("Upload") }, { label: __("Review") }],
		is_locked: (index) => index === 1 && !last_preview,
		on_step_click: (index) => show_step(index),
	});
	dialog.get_field("steps").$wrapper.append(stepper.$el.addClass("company-setup-stepper mb-2"));

	const $downloads = $(`<div class="flex flex-wrap items-center gap-1"></div>`).appendTo(
		dialog.get_field("downloads").$wrapper
	);
	$downloads.append(
		grey_line(__("Start from")),
		frappe.ui.button({
			label: __("the sample sheet"),
			icon: "download",
			variant: "ghost",
			size: "sm",
			onclick: () =>
				download_sheet(company, "chart_sheet.download_chart_template", {
					template: "Sample Template",
				}),
		}),
		grey_line(__("or")),
		frappe.ui.button({
			label: __("your current chart"),
			icon: "download",
			variant: "ghost",
			size: "sm",
			onclick: () =>
				download_sheet(company, "chart_sheet.download_chart_template", { template: "Current Chart" }),
		})
	);

	const $review = dialog.get_field("review").$wrapper;
	const $new_chart = dialog.get_field("new_chart").$wrapper;
	const show_step = (index) => {
		const review = index === 1 && Boolean(last_preview);
		stepper.set_current(review ? 1 : 0);
		for (const fieldname of ["file", "downloads"]) dialog.get_field(fieldname).$wrapper.toggle(!review);
		dialog.$body
			.find(".form-section")
			.filter((i, section) => !section.contains($review[0]) && !section.contains($new_chart[0]))
			.toggle(!review);
		dialog.set_df_property("review", "hidden", !review);
		dialog.set_df_property("new_chart_section", "hidden", !review);
		if (review) {
			dialog.set_primary_action(__("Replace chart"), replace);
			dialog.set_secondary_action_label(__("Back"));
			dialog.set_secondary_action(() => show_step(0));
			dialog.get_secondary_btn().removeClass("hidden");
		} else {
			dialog.set_primary_action(__("Next"), () => show_step(1));
			dialog.get_secondary_btn().addClass("hidden");
		}
		dialog.get_primary_btn().prop("disabled", !last_preview);
	};
	const preview = () => {
		const file_url = dialog.get_value("file");
		last_preview = null;
		$review.empty();
		$new_chart.empty();
		show_step(0);
		if (!file_url) return;
		call_method(frm, "preview_chart", { file_url: file_url, group_kinds: group_kinds }).then((r) => {
			// another file was picked meanwhile
			if (dialog.get_value("file") !== file_url) return;
			last_preview = r.message;
			render_import_preview(frm, $review, $new_chart, r.message, company, file_url, () => {
				dialog.set_value("file", "");
			});
			$review.prepend(
				make_group_kinds(r.message.unknown_groups, group_kinds, (group, kind) => {
					if (!kind || kind === group_kinds[group]) return;
					group_kinds[group] = kind;
					preview();
				})
			);
			show_step(1);
		});
	};
	const replace = () =>
		frappe.warn(
			__("Replace the chart?"),
			__(
				"Delete the {0} accounts and tax templates of {1} and make the chart from your sheet? This can't be undone.",
				[last_preview?.current_accounts, frappe.utils.escape_html(company)]
			),
			() =>
				call_method(
					frm,
					"import_chart",
					{ file_url: dialog.get_value("file"), group_kinds: group_kinds },
					{ freeze: true, freeze_message: __("Importing the chart...") }
				).then((r) => {
					close_and_reload(frm, dialog);
					const added = r.message?.added_accounts || [];
					frappe.ui.toast({
						type: "success",
						message: added.length
							? __("Chart imported. Added accounts ERPNext needs: {0}", [added.join(", ")])
							: __("Chart imported"),
						duration: (added.length ? 10 : 5) * 1000,
					});
				}),
			__("Replace chart")
		);

	dialog.show();
	show_step(0);
}

// For each group of a Busy list that Busy doesn't ship: does it hold customers, suppliers or
// accounts of the company's own; customers and suppliers become one ledger of the group's name
function make_group_kinds(groups, kinds, on_change) {
	if (!groups?.length) return [];
	return $(`<div class="flex flex-col gap-2 mb-3"></div>`).append(
		frappe.ui.alert({
			theme: groups.every((group) => kinds[group]) ? "blue" : "yellow",
			title: __("Busy doesn't say what these groups hold"),
			description: __(
				"A group of customers, like insurers under Sundry Debtors, becomes one receivable account; their invoices come in under Selling."
			),
		}),
		groups.map((group) => {
			const [$select] = make_kind_select(kinds[group], (kind) => on_change(group, kind));
			return $(`<div class="flex items-center justify-between gap-3"></div>`).append(
				$(`<span class="text-base text-ink-gray-8"></span>`).text(group),
				$select
			);
		})
	);
}

function make_kind_select(value, onchange) {
	const $parent = $(`<div class="shrink-0"></div>`);
	const control = frappe.ui.form.make_control({
		df: {
			fieldtype: "Select",
			fieldname: "kind",
			options: [
				{ value: "", label: __("Pick one") },
				{ value: "Customers", label: __("Customers") },
				{ value: "Suppliers", label: __("Suppliers") },
				{ value: "Accounts", label: __("Our own accounts") },
			],
			change: () => onchange(control.get_value()),
		},
		parent: $parent,
		only_input: true,
		render_input: true,
	});
	control.set_value(value || "");
	$parent.find(".form-group").addClass("mb-0");
	return [$parent, control];
}

// What the sheet holds and what replacing the chart does; the new chart itself folds away
function render_import_preview(frm, $preview, $tree, data, company, file_url, replace_file) {
	const currency = erpnext.get_currency(company);
	const $body = $(`<div class="flex flex-col gap-3"></div>`).appendTo($preview);
	$(`<div class="flex items-center gap-2 text-p-sm text-ink-gray-6"></div>`)
		.append(
			$(`<span class="text-ink-gray-5"></span>`).html(frappe.utils.icon("file-spreadsheet", "sm")),
			$(`<span class="grow truncate"></span>`).text(file_url.split("/").pop()),
			frappe.ui.button({
				label: __("Replace file"),
				icon: "upload",
				size: "sm",
				onclick: replace_file,
			})
		)
		.appendTo($body);

	const left_out = data.left_out || {};
	const left_out_total =
		Math.abs(left_out.Receivable || 0) + Math.abs(left_out.Payable || 0) + Math.abs(left_out.Stock || 0);
	const cards = [
		{
			label: __("Accounts"),
			value: format_number(data.accounts.length, null, 0),
			caption: data.added_accounts?.length
				? __("Replaces your {0} · ERPNext adds {1}", [
						data.current_accounts,
						data.added_accounts.join(", "),
				  ])
				: __("Replaces your {0}", [data.current_accounts]),
		},
		{
			label: __("Opening balances"),
			value: format_number(data.balances || 0, null, 0),
			caption: data.balances
				? data.draft_balances
					? plural(
							data.draft_balances,
							__("Replace your 1 draft"),
							__("Replace your {0} drafts", [data.draft_balances])
					  )
					: __("Added as drafts, not posted")
				: data.draft_balances
				? plural(
						data.draft_balances,
						__("Your 1 draft stays"),
						__("Your {0} drafts stay", [data.draft_balances])
				  )
				: __("None on the sheet"),
		},
	];
	if (left_out_total) {
		cards.push({
			label: __("Left out"),
			value: format_currency(left_out_total, currency),
			caption: left_out.Stock
				? __("Customers, suppliers and stock come in their own way")
				: __("Customers and suppliers come in as invoices and bills"),
		});
	}
	frappe.ui.stat_cards({ items: cards }).appendTo($body);

	$body.append(
		frappe.ui.alert({
			theme: "yellow",
			title: __("Replaces your {0} accounts and tax templates", [data.current_accounts]),
			description: __(
				"Your sheet becomes the chart. The company's default accounts are set again. Add your tax templates in Taxes after the import."
			),
		})
	);
	if (data.renamed?.length) {
		grey_line(
			__("Names used under more than one parent get the parent's name: {0}", [data.renamed.join(", ")])
		).appendTo($body);
	}
	const left_out_note = left_out_text(frm, left_out);
	if (left_out_note) {
		grey_line(left_out_note).appendTo($body);
	}
	const difference = data.balances ? flt(data.debit - data.credit, 2) : 0;
	if (difference) {
		grey_line(
			__(
				"Debit and credit on the sheet differ by {0}. It shows in Opening balances until the invoices are in.",
				[format_currency(Math.abs(difference), currency)]
			)
		).appendTo($body);
	}
	if (!data.balances && data.draft_balances) {
		grey_line(
			__(
				"Your draft balances move to the new accounts with the same name. Check them after the import."
			)
		).appendTo($body);
	}

	const children_of = {};
	for (const account of data.accounts) {
		const parent = account.parent_account || "";
		children_of[parent] = children_of[parent] || [];
		children_of[parent].push(account);
	}
	new SheetTree({
		parent: $tree,
		label: __("New chart"),
		root_value: "",
		expandable: true,
		row_style: true,
		// no doctype: these accounts don't exist yet, so no link previews
		args: {},
		children_of: children_of,
		get_label: (node) => frappe.utils.escape_html(node.label),
		on_node_render: open_all_once(),
	});
}

// Frappe's tree over the accounts of an uploaded sheet, so it needs no server call per group
class SheetTree extends frappe.ui.Tree {
	get_nodes(value, is_root) {
		return Promise.resolve(this.children_of[is_root ? "" : value] || []);
	}

	get_all_nodes() {
		return Promise.resolve(
			Object.entries(this.children_of).map(([parent, data]) => ({
				parent: parent || this.label,
				data: data,
			}))
		);
	}
}

function make_company_card(frm) {
	const details = frm.doc.__onload?.company_details || {};
	// white bordered card, the same contact-box style as the contact cards on Customer
	const $card = $(`<div class="contact-box flex flex-col gap-3 mb-0">
		<div class="flex items-center gap-3">
			<span class="company-logo shrink-0"></span>
			<div class="flex flex-col gap-1 min-w-0 grow">
				<div class="company-name text-base-semibold text-ink-gray-8 truncate"></div>
				<div class="company-meta text-sm text-ink-gray-5"></div>
			</div>
			<span class="company-edit flex items-center gap-1 shrink-0"></span>
		</div>
		${frappe.ui.divider.html()}
		<div class="company-lines grid gap-3 company-setup-two-columns"></div>
	</div>`);

	$card.find(".company-logo").html(
		frappe.ui.avatar.html({
			image: details.company_logo,
			label: details.company_name,
			size: "xl",
			shape: "square",
		})
	);
	$card.find(".company-name").append(
		$(`<a class="text-ink-gray-8"></a>`)
			.attr("href", frappe.utils.get_form_link("Company", frm.doc.company))
			.text(details.company_name || frm.doc.company)
	);
	$card
		.find(".company-meta")
		.text([details.abbr, details.country, details.default_currency].filter(Boolean).join(" · "));
	$card.find(".company-edit").append(
		frappe.ui.button({
			label: __("Edit"),
			icon: "pencil",
			onclick: () =>
				frappe.model.with_doctype("Company").then(() => edit_company_details(frm, details)),
		})
	);

	const address = ["address_line1", "address_line2", "city", "state", "pincode", "country"]
		.map((field) => details[field])
		.filter(Boolean)
		.join(", ");
	const lines = [
		["receipt", __("Tax ID"), details.tax_id],
		["phone", __("Phone"), details.phone_no],
		["mail", __("Email"), details.email],
		["globe", __("Website"), details.website],
		["map-pin", __("Address"), details.address_line1 ? address : "", true],
	];
	const $lines = $card.find(".company-lines");
	for (const [icon, label, value, full_width] of lines) {
		const $line = $(`<div class="contact-line flex items-center gap-2 min-w-0"></div>`)
			.attr("title", label)
			.append(frappe.utils.icon(icon, "sm", "", "", "text-ink-gray-5 shrink-0", true))
			.appendTo($lines);
		// the address is the long one, so it takes the whole row
		if (full_width) $line.addClass("company-setup-full-row");
		$("<span class='truncate'></span>")
			.addClass(value ? "text-ink-gray-7" : "text-ink-gray-4")
			.text(value || __("{0} not set", [label]))
			.appendTo($line);
	}
	return $card;
}

function edit_company_details(frm, details) {
	const dialog = new frappe.ui.Dialog({
		title: __("Company details"),
		fields: [
			{ fieldname: "company_logo", fieldtype: "Attach Image", label: __("Logo") },
			{ fieldname: "tax_id", fieldtype: "Data", label: __("Tax ID") },
			{ fieldtype: "Column Break" },
			{ fieldname: "phone_no", fieldtype: "Data", options: "Phone", label: __("Phone") },
			{ fieldname: "email", fieldtype: "Data", options: "Email", label: __("Email") },
			{ fieldname: "website", fieldtype: "Data", options: "URL", label: __("Website") },
			{ fieldtype: "Section Break", label: __("Address") },
			{ fieldname: "address_line1", fieldtype: "Data", label: __("Address Line 1") },
			{ fieldname: "address_line2", fieldtype: "Data", label: __("Address Line 2") },
			{ fieldname: "city", fieldtype: "Data", label: __("City") },
			{ fieldtype: "Column Break" },
			{ fieldname: "state", fieldtype: "Data", label: __("State") },
			{ fieldname: "pincode", fieldtype: "Data", label: __("Postal Code") },
			{ fieldname: "country", fieldtype: "Link", options: "Country", label: __("Country") },
			...get_module_fields(),
			{ fieldtype: "Section Break", label: __("More"), collapsible: 1 },
			{ fieldname: "date_of_incorporation", fieldtype: "Date", label: __("Date of Incorporation") },
			{
				fieldname: "registration_details",
				fieldtype: "Small Text",
				label: __("Registration Details"),
				description: __("For example PAN, CIN or MSME number."),
			},
		],
		primary_action_label: __("Save"),
		primary_action: (values) =>
			call_method(frm, "update_company_details", { values: values }).then(() => {
				close_and_reload(frm, dialog);
			}),
	});
	dialog.set_values(details);
	dialog.show();
}

// The Company's Modules Used checks it has, as on its form: each child needs its parent ticked
function get_module_fields() {
	const parents = { manufacturing: "stock", subcontracting: "manufacturing" };
	const check = (fieldname) =>
		frappe.meta.has_field("Company", fieldname) && {
			fieldname: fieldname,
			fieldtype: "Check",
			label: __(frappe.meta.get_label("Company", fieldname)),
			depends_on: parents[fieldname] && `eval:doc.${parents[fieldname]}`,
			onchange: function () {
				const child = Object.keys(parents).find((name) => parents[name] === fieldname);
				if (child && !this.get_value() && this.layout.get_value(child)) {
					this.layout.set_value(child, 0);
				}
			},
		};
	const left = ["stock", "manufacturing", "subcontracting"].map(check).filter(Boolean);
	const right = ["assets", "projects", "pos"].map(check).filter(Boolean);
	if (!left.length && !right.length) return [];
	return [
		{ fieldtype: "Section Break", label: __("Modules Used") },
		...left,
		...(left.length && right.length ? [{ fieldtype: "Column Break" }] : []),
		...right,
	];
}

// the countries whose English says "check", as the server words it
const CHECK_COUNTRIES = ["United States", "Canada"];
let says_check = false;

function cheque_words(text) {
	return says_check ? text.replace(/cheque/g, "check").replace(/Cheque/g, "Check") : text;
}

function bank_menu(frm) {
	const bank = frm.doc.__onload?.bank || {};
	return [
		(bank.cheques || bank.charges || bank.deposits || (frm.doc.undone_cheques || []).length) && {
			label: cheque_words(__("Posted cheques")),
			icon: "list",
			onclick: () => show_posted_cheques(frm),
		},
		{
			label: __("Download sheet"),
			icon: "download",
			onclick: () => download_sheet(frm.doc.company, "bank_accounts.download_uncleared_cheques"),
		},
	].filter(Boolean);
}

// Like "11 cheques and 1 deposit", or "4 cheques, 1 card charge and 1 deposit"
function count_cheques(cheques, deposits, charges = 0) {
	const parts = [
		cheques && plural(cheques, cheque_words(__("1 cheque")), cheque_words(__("{0} cheques", [cheques]))),
		charges && plural(charges, __("1 card charge"), __("{0} card charges", [charges])),
		deposits && plural(deposits, __("1 deposit"), __("{0} deposits", [deposits])),
	].filter(Boolean);
	return frappe.utils.comma_and(parts);
}

// What uncleared rows count as: a credit card's issued rows are charges, not cheques
function count_cheque_rows(frm, rows) {
	const cards = new Set(frm.doc.__onload?.bank?.card_ledgers || []);
	const issued = rows.filter((row) => row.entry_type === "Issued");
	return count_cheques(
		issued.filter((row) => !cards.has(row.bank_account)).length,
		rows.filter((row) => row.entry_type === "Deposited").length,
		issued.filter((row) => cards.has(row.bank_account)).length
	);
}

function count_rows(count) {
	return plural(count, __("1 row"), __("{0} rows", [count]));
}

function count_assets(count) {
	return plural(count, __("1 asset"), __("{0} assets", [count]));
}

function count_warehouses(count) {
	return plural(count, __("1 warehouse"), __("{0} warehouses", [count]));
}

function count_balances(count) {
	return plural(count, __("1 balance"), __("{0} balances", [count]));
}

// a row below zero: a credit or advance the customer has, a debit or advance with a supplier
function count_advances(count, party_type) {
	return party_type === "Supplier"
		? plural(count, __("1 debit or advance"), __("{0} debits or advances", [count]))
		: plural(count, __("1 credit or advance"), __("{0} credits or advances", [count]));
}

function short_account(frm, account) {
	const abbr = frm.doc.__onload?.company_details?.abbr;
	return abbr && account ? account.replace(new RegExp(` - ${abbr}$`), "") : account || "";
}

function show_cheques_posted(frm) {
	const bank = frm.doc.__onload?.bank || {};
	frappe.ui.toast({
		type: "success",
		message: __("{0} posted on {1}.", [
			count_cheques(bank.cheques, bank.deposits, bank.charges),
			day_before_text(frm),
		]),
		duration: 10000,
	});
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

// Bank accounts: the bank details of each bank ledger, then the uncleared cheques and deposits
function show_bank_accounts(frm, replacing) {
	Promise.all([
		get_data(frm, "get_bank_accounts"),
		frappe.db.get_list("Bank", { fields: ["name"], limit: 0 }),
	]).then(([r, banks]) =>
		new BankAccountsWindow(
			frm,
			r.message,
			banks.map((bank) => bank.name)
		).show(replacing)
	);
}

class BankAccountsWindow {
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

// What was posted, by sheet: each to view, and to undo in part or whole until going live
function show_posted_cheques(frm) {
	const total_of = (rows) => rows.reduce((sum, row) => sum + flt(row.amount), 0);
	show_posted_window(frm, {
		title: cheque_words(__("Posted cheques")),
		load: () => get_data(frm, "get_posted_entries", { kind: "cheques" }).then((r) => r.message),
		posted_word: (when) => __("Posted {0}", [when]),
		group_line: (group) => count_rows(group.count),
		group_amount: (group) =>
			__("{0} issued · {1} deposited", [money(frm, group.issued), money(frm, group.deposited)]),
		undone: (posted) =>
			posted.undone.map((group) => ({
				source: group.source,
				line: __("{0} · taken out of your books", [count_rows(group.count)]),
				amount: money(frm, group.amount),
				download: () =>
					download_sheet(frm.doc.company, "bank_accounts.download_uncleared_cheques", {
						undone_from: group.source,
					}),
			})),
		fields: [
			["bank", "Data", __("Bank"), 2],
			["entry_type", "Data", __("Issued/Deposited"), 1],
			["reference_no", "Data", cheque_words(__("Cheque no")), 1],
			["reference_date", "Date", __("Date"), 1],
			["party_name", "Data", __("Party"), 2],
			["amount", "Currency", __("Amount"), 2],
			["clearance_date", "Date", __("Cleared"), 1],
		].map(([fieldname, fieldtype, label, columns]) => ({ fieldname, fieldtype, label, columns })),
		rows: (group) =>
			group.rows.map((row) => ({ ...row, bank: row.bank || short_account(frm, row.bank_account) })),
		blocked: (row) => row.clearance_date,
		blocked_alert: (rows) => ({
			title: plural(
				rows.length,
				cheque_words(__("1 cheque is already cleared.")),
				cheque_words(__("{0} cheques are already cleared.", [rows.length]))
			),
			description: __("Remove the clearance date first."),
		}),
		undo_label: (selected, group) =>
			selected.length
				? __("Undo {0} selected · {1}", [selected.length, money(frm, total_of(selected))])
				: __("Undo this upload"),
		confirm_title: (rows) => __("Undo {0}?", [count_cheque_rows(frm, rows)]),
		confirm_text: (rows) =>
			plural(
				rows.length,
				__("You can download it, fix it and upload again."),
				__("You can download them, fix them and upload again.")
			),
		confirm_what: (rows, selected) => ({
			count: rows.length,
			kind: "cheques",
			names: selected ? selected.map((row) => row.journal_entry) : null,
			what: count_cheque_rows(frm, rows),
			one: rows[0]?.entry_type === "Deposited" ? __("deposit") : cheque_words(__("cheque")),
		}),
		confirm_rows: (rows) =>
			rows.map((row) => [
				make_text(row.reference_no || "—", "text-ink-gray-8 shrink-0"),
				make_text(
					[row.party_name, short_account(frm, row.bank_account)].filter(Boolean).join(" · "),
					"text-ink-gray-5 grow truncate"
				),
				make_text(money(frm, row.amount), "text-ink-gray-8 shrink-0"),
			]),
		total: total_of,
		confirm_button: (rows) => __("Undo {0}", [count_cheque_rows(frm, rows)]),
		undo: (group, selected, delete_entries) =>
			call_method(frm, "undo_uncleared_cheques", {
				source: group.source,
				entries: selected ? selected.map((row) => row.journal_entry) : null,
				delete_entries: delete_entries,
			}),
		after_undo: (result, group) => show_undo_result(frm, "cheques", result, group.source),
	});
}

// a qty with decimals only when it has them
function format_qty(value) {
	return format_number(value, null, Number.isInteger(flt(value)) ? 0 : 3);
}

function plural(count, one, many) {
	return count === 1 ? one : many;
}

function day_before_text(frm) {
	return frm.doc.migration_date
		? frappe.datetime.str_to_user(frappe.datetime.add_days(frm.doc.migration_date, -1))
		: __("the day before migration");
}

// A read-only table of a few rows, like the stock lines against the trial balance
// on a phone a wide table scrolls inside its box, and an amount keeps its ₹ on its line
function make_read_table(columns, rows) {
	return $(`<div class="border rounded-lg overflow-x-auto"></div>`).append(
		$(`<table class="table mt-0 mb-0"></table>`).append(
			$(`<thead><tr class="bg-surface-gray-1"></tr></thead>`)
				.find("tr")
				.append(
					columns.map(([label, right]) =>
						$(`<th class="text-sm-medium text-ink-gray-6 border-0"></th>`)
							.toggleClass("text-right", !!right)
							.text(label)
					)
				)
				.end(),
			$(`<tbody></tbody>`).append(
				rows.map((cells) =>
					$(`<tr></tr>`).append(
						cells.map((cell, index) =>
							$(`<td class="text-base text-ink-gray-8"></td>`)
								.toggleClass("text-right whitespace-nowrap", !!columns[index][1])
								.append(cell)
						)
					)
				)
			)
		)
	);
}

// A read-only table narrowed by a search box, the first 100 rows shown
function make_search_table(columns, rows, to_cells) {
	const $table = $(`<div></div>`);
	const $count = grey_line("");
	const render = (query) => {
		const shown = rows.filter(
			(row) => !query || JSON.stringify(row).toLowerCase().includes(query.toLowerCase())
		);
		$table.empty().append(make_read_table(columns, shown.slice(0, 100).map(to_cells)));
		$count.text(
			shown.length > 100 ? __("Showing 100 of {0}. Search to find others.", [shown.length]) : ""
		);
	};
	const $search = $(`<div class="w-64"></div>`);
	const search = frappe.ui.form.make_control({
		df: {
			fieldtype: "Data",
			fieldname: "search",
			placeholder: __("Search"),
			change: () => render(search.get_value()),
		},
		parent: $search,
		only_input: true,
		render_input: true,
	});
	search.$input.on("input", () => render(search.get_value()));
	render("");
	return $(`<div class="flex flex-col gap-2"></div>`).append($search, $table, $count);
}

// A blank sheet of the fields Data Import needs, with the required child table fields too
function download_import_sample(doctype) {
	frappe.model.with_doctype(doctype, () => {
		const required = (dt) =>
			frappe
				.get_meta(dt)
				.fields.filter(
					(df) =>
						df.reqd &&
						!df.hidden &&
						!frappe.model.table_fields.includes(df.fieldtype) &&
						!frappe.model.no_value_type.includes(df.fieldtype)
				)
				.map((df) => df.fieldname);
		const fields = { [doctype]: required(doctype) };
		for (const df of frappe.get_meta(doctype).fields) {
			if (df.reqd && frappe.model.table_fields.includes(df.fieldtype)) {
				fields[df.fieldname] = required(df.options);
			}
		}
		open_url_post("/api/method/frappe.core.doctype.data_import.data_import.download_template", {
			doctype: doctype,
			export_fields: JSON.stringify(fields),
			export_records: "blank_template",
			file_type: "Excel",
		});
	});
}

function import_option(frm, doctype, label) {
	return {
		label,
		disabled: !!import_blocked(frm, doctype),
		description: import_blocked(frm, doctype),
		onclick: () => open_party_import(frm, doctype),
	};
}

function items_import_menu(frm) {
	if (!frappe.model.can_create("Data Import")) return [];
	return [
		frappe.ui.dropdown({
			button: { label: __("Import"), icon_right: "chevron-down" },
			align: "end",
			options: [
				{
					group: "",
					options: [
						import_option(frm, "Item Group", __("Item groups")),
						import_option(frm, "UOM", __("Units")),
						import_option(frm, "Item", __("Items")),
					],
				},
				{
					group: "",
					options: [
						{ label: __("Download sample"), onclick: () => download_import_sample("Item") },
					],
				},
			],
		}),
	];
}

const ORDER_TYPES = {
	open_sales_orders: "Sales Order",
	open_purchase_orders: "Purchase Order",
	open_work_orders: "Work Order",
	open_subcontracting_orders: "Subcontracting Order",
};

function order_rows(frm, order_type, made) {
	return (frm.doc.open_orders || []).filter(
		(row) => row.order_type === order_type && (row.status === "Made") === made
	);
}

function order_buttons(frm, step) {
	const order_type = ORDER_TYPES[step.name];
	const drafts = order_rows(frm, order_type, false).length;
	return [
		{
			label: drafts
				? __("Edit orders")
				: order_type === "Work Order"
				? __("Add work orders")
				: __("Add orders"),
			onclick: () => show_open_orders(frm, order_type),
		},
	];
}

function order_menu(frm, step) {
	const order_type = ORDER_TYPES[step.name];
	return [
		(order_rows(frm, order_type, true).length || has_undone_orders(frm, order_type)) && {
			label: made_orders_title(order_type),
			icon: "list",
			onclick: () => show_made_orders(frm, order_type),
		},
		{
			label: __("Download sheet"),
			icon: "download",
			onclick: () =>
				download_sheet(frm.doc.company, "open_orders.download_open_orders", {
					order_type: order_type,
				}),
		},
	].filter(Boolean);
}

function has_undone_orders(frm, order_type) {
	return (frm.doc.undone_orders || []).some((row) => row.order_type === order_type);
}

function made_orders_title(order_type) {
	return {
		"Sales Order": __("Made sales orders"),
		"Purchase Order": __("Made purchase orders"),
		"Work Order": __("Made work orders"),
		"Subcontracting Order": __("Made subcontracting orders"),
	}[order_type];
}

function has_rows(rows, posted) {
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
	boms: (frm) =>
		frappe.model.can_create("Data Import") && !import_blocked(frm, "BOM")
			? [{ label: __("Import BOMs"), onclick: () => open_party_import(frm, "BOM") }]
			: [bom_creator_button()],
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
	boms: (frm) =>
		[
			!import_blocked(frm, "BOM") && { ...bom_creator_button(), icon: "arrow-up-right" },
			{ label: __("Download sample"), icon: "download", onclick: () => download_import_sample("BOM") },
		].filter(Boolean),
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

// ---------- Taxes ----------

// ERPNext's own tax templates, one row per tax line; no rows means no tax is charged
function show_taxes(frm) {
	get_data(frm, "get_taxes").then((r) => {
		const taxes = r.message;
		const templates = {};
		for (const row of taxes.rows) {
			const key = `${row.tax_type}\n${row.title}`;
			templates[key] = templates[key] || { ...row, lines: [] };
			templates[key].lines.push(row);
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Taxes"),
			size: "extra-large",
			fields: [
				{ fieldname: "intro", fieldtype: "HTML" },
				{
					fieldname: "rows",
					fieldtype: "Table",
					in_place_edit: 1,
					data: taxes.rows.map((row) => ({ ...row })),
					fields: [
						{
							fieldname: "tax_type",
							fieldtype: "Select",
							options: ["Sales", "Purchase"],
							label: __("Type"),
							in_list_view: 1,
							columns: 2,
							default: "Sales",
							onchange: () => refresh_defaults(),
						},
						{
							fieldname: "title",
							fieldtype: "Data",
							label: __("Template"),
							in_list_view: 1,
							columns: 4,
							onchange: () => refresh_defaults(),
						},
						{
							fieldname: "account",
							fieldtype: "Link",
							options: "Account",
							label: __("Tax account"),
							in_list_view: 1,
							columns: 3,
							// a new tax account is added with Add tax account, not the full form
							only_select: 1,
							filter_description: __("Only accounts of type Tax are listed."),
							get_query: () => ({
								filters: {
									company: frm.doc.company,
									account_type: "Tax",
									is_group: 0,
									disabled: 0,
								},
							}),
						},
						{
							fieldname: "rate",
							fieldtype: "Float",
							label: __("Rate %"),
							in_list_view: 1,
							columns: 1,
							reqd: 1,
						},
					],
				},
				{ fieldname: "notes", fieldtype: "HTML" },
				// the template new invoices and bills start with, only when the user picks one: a company
				// may sell exempt services too
				{ fieldtype: "Section Break" },
				...["Sales", "Purchase"].flatMap((tax_type) => [
					...(tax_type === "Purchase" ? [{ fieldtype: "Column Break" }] : []),
					{
						fieldname: `default_${tax_type.toLowerCase()}`,
						fieldtype: "Select",
						label:
							tax_type === "Sales"
								? __("Default sales template")
								: __("Default purchase template"),
						description:
							tax_type === "Sales"
								? __("New invoices start with it. Leave empty for none.")
								: __("New bills start with it. Leave empty for none."),
						default: taxes.defaults?.[tax_type] || "",
						// a section built with only hidden fields stays hidden
						hidden: taxes.rows.some((row) => row.tax_type === tax_type) ? 0 : 1,
					},
				]),
			],
			primary_action_label: __("Save taxes"),
			// a cell still being typed in is taken first: a typed account is checked on the server
			primary_action: () =>
				new Promise((resolve) => {
					document.activeElement?.blur();
					frappe.after_ajax(resolve);
				})
					.then(() =>
						call_method(
							frm,
							"save_taxes",
							{
								rows: dialog.get_value("rows") || [],
								defaults: {
									Sales: dialog.get_value("default_sales"),
									Purchase: dialog.get_value("default_purchase"),
								},
							},
							{ freeze: true }
						)
					)
					.then(() => {
						close_and_reload(frm, dialog);
						frappe.ui.toast({ type: "success", message: __("Taxes saved.") });
					}),
			secondary_action_label: __("Close"),
			secondary_action: () => dialog.hide(),
		});
		// the default pickers offer the templates in the rows as they are typed
		const refresh_defaults = () => {
			for (const tax_type of ["Sales", "Purchase"]) {
				const fieldname = `default_${tax_type.toLowerCase()}`;
				const titles = [
					...new Set(
						(dialog.fields_dict.rows.df.data || [])
							.filter((row) => row.tax_type === tax_type && cstr(row.title).trim())
							.map((row) => cstr(row.title).trim())
					),
				];
				const value = defaults_shown ? dialog.get_value(fieldname) : taxes.defaults?.[tax_type];
				dialog.set_df_property(fieldname, "options", ["", ...titles]);
				dialog.set_df_property(fieldname, "hidden", titles.length ? 0 : 1);
				dialog.set_value(fieldname, titles.includes(value) ? value : "");
			}
			defaults_shown = true;
		};
		let defaults_shown = false;
		const example = {
			India: __(
				"One row per tax line. Rows with the same template name make one template, like CGST 9% and SGST 9% in GST 18%."
			),
			"United States": __(
				"One row per tax line. Rows with the same template name make one template, like State 6.25% and County 2% in Sales Tax 8.25%."
			),
		};
		const add_tax_account = () => {
			const prompt = frappe.prompt(
				[
					{ fieldname: "account_name", fieldtype: "Data", label: __("Account Name"), reqd: 1 },
					{
						fieldname: "parent",
						fieldtype: "Link",
						options: "Account",
						label: __("Under"),
						reqd: 1,
						only_select: 1,
						default: taxes.tax_group,
						get_query: () => ({ filters: { company: frm.doc.company, is_group: 1 } }),
					},
				],
				(values) =>
					frappe
						.call({
							method: "erpnext.accounts.utils.add_ac",
							args: {
								doctype: "Account",
								company: frm.doc.company,
								account_name: values.account_name,
								account_type: "Tax",
								parent: values.parent,
								is_group: 0,
								is_root: 0,
							},
							freeze: true,
						})
						.then((r) => {
							taxes.accounts.push(r.message);
							dialog.$wrapper.find("[data-no-tax-account]").remove();
							frappe.ui.toast({
								type: "success",
								message: __("{0} added. Pick it in a row.", [values.account_name]),
								duration: 8000,
							});
						}),
				__("Add tax account"),
				__("Add")
			);
			// typing replaces the suggested group instead of adding to it
			const $parent = prompt.get_field("parent").$input;
			$parent.on("focus", () => setTimeout(() => $parent.select()));
		};
		dialog.get_field("intro").$wrapper.append(
			$(`<div class="flex flex-wrap items-start justify-between gap-2 mb-3"></div>`).append(
				$(`<div class="text-p-base text-ink-gray-7"></div>`).text(
					example[taxes.country] ||
						__(
							"One row per tax line, like VAT 20%. Rows with the same template name make one template with all their lines."
						)
				),
				frappe.ui.button({ label: __("Add tax account"), icon: "plus", onclick: add_tax_account })
			),
			taxes.chart_replaced &&
				taxes.country === "India" &&
				!taxes.rows.length &&
				!taxes.other_templates?.length
				? $(`<div class="mb-3"></div>`).append(
						frappe.ui.alert({
							theme: "blue",
							title: __("The chart import removed the old tax templates"),
							description: __("Add your GST templates here, like GST 18%."),
						})
				  )
				: [],
			taxes.from_existing
				? $(`<div></div>`).append(
						frappe.ui.alert({
							theme: "blue",
							title: __("These templates are already in ERPNext"),
							description: __(
								"Check them, change or add what is missing, then save. Nothing is done until you save."
							),
						})
				  )
				: [],
			taxes.other_templates?.length
				? $(`<div class="mb-3"></div>`).append(
						frappe.ui.alert({
							theme: "blue",
							title: __("Templates already in ERPNext: {0}", [
								taxes.other_templates
									.map((template) =>
										template.is_default
											? __("{0} ({1}, default)", [
													template.title,
													__(template.tax_type),
											  ])
											: __("{0} ({1})", [template.title, __(template.tax_type)])
									)
									.join(", "),
							]),
							description: taxes.other_templates.some((template) => template.tax_category)
								? __(
										"Some have a tax category, like {0}: an invoice whose customer or address has that category takes that template, not the default here. Add only what they lack here, and disable the ones you don't use from their list.",
										[
											taxes.other_templates.find((template) => template.tax_category)
												.tax_category,
										]
								  )
								: __(
										"Invoices can pick these too, by tax category or as the default. Add only what they lack here; your default template of each type is set below. Disable the ones you don't use from their list."
								  ),
						})
				  )
				: []
		);
		dialog.get_field("notes").$wrapper.append(
			Object.keys(templates).length
				? make_kv_lines(
						Object.values(templates).map((template) => [
							__("{0} · {1}", [template.title, __(template.tax_type)]),
							template.lines.length > 1
								? __("{0} = {1}%", [
										template.lines
											.map((line) =>
												__("{0} {1}%", [short_account(frm, line.account), line.rate])
											)
											.join(" + "),
										template.lines.reduce((sum, line) => sum + flt(line.rate), 0),
								  ])
								: __("{0}%", [template.lines[0].rate]),
						])
				  ).addClass("mb-3")
				: [],
			grey_line(
				__(
					"Leave it empty if you charge no tax: saving with no rows marks this done. Tax accounts list only accounts of type Tax."
				)
			),
			taxes.accounts.length
				? []
				: make_alerts([
						{
							theme: "yellow",
							title: __("This company has no account of type Tax."),
							description: __(
								"Add one with Add tax account, or save with no rows if you charge no tax."
							),
							buttons: [{ label: __("Add tax account"), onclick: add_tax_account }],
						},
				  ])
						.addClass("mt-3")
						.attr("data-no-tax-account", 1)
		);
		dialog.show();
		refresh_defaults();
	});
}

// ---------- Warehouses ----------

const WAREHOUSE_KINDS = ["Our warehouse", "Job worker", "Work in progress"];

// Every warehouse with its kind, job worker and stock line; the ones only on a stock sheet are new
// after: what to open again once the window closes, saved or not
function show_warehouses(frm, { job_workers = false, after = null } = {}) {
	Promise.all([
		get_data(frm, "get_warehouses"),
		frappe.db.get_list("Supplier", { fields: ["name"], limit: 0 }),
	]).then(([r, suppliers]) => {
		const details = r.message;
		const known = new Set(suppliers.map((supplier) => supplier.name.trim().toLowerCase()));
		const is_new_supplier = (name) => Boolean(name) && !known.has(name.trim().toLowerCase());
		const all = details.rows.map((row) => ({ ...row }));
		const shown = job_workers ? all.filter((row) => row.kind === "Job worker") : all;
		const badge = (text, is_new) =>
			text ? name_with_badge(frappe.utils.escape_html(text), is_new && new_badge()) : "";
		const refresh = () => refresh_state();
		const dialog = new frappe.ui.Dialog({
			title: __("Warehouses"),
			size: "extra-large",
			fields: [
				{ fieldname: "intro", fieldtype: "HTML" },
				{
					fieldname: "rows",
					fieldtype: "Table",
					in_place_edit: 1,
					data: shown,
					fields: [
						{
							fieldname: "warehouse_name",
							fieldtype: "Data",
							label: __("Warehouse"),
							in_list_view: 1,
							columns: 3,
							formatter: (value, df, options, doc) => badge(value, doc && !doc.warehouse),
							onchange: refresh,
						},
						{
							fieldname: "kind",
							fieldtype: "Select",
							options: WAREHOUSE_KINDS,
							label: __("Kind"),
							in_list_view: 1,
							columns: 2,
							default: job_workers ? "Job worker" : "Our warehouse",
							onchange: refresh,
						},
						{
							fieldname: "job_worker",
							fieldtype: "Link",
							options: "Supplier",
							label: __("Job worker"),
							in_list_view: 1,
							columns: 2,
							ignore_link_validation: 1,
							formatter: (value) => badge(value, is_new_supplier(value)),
							onchange: refresh,
						},
						{
							fieldname: "account",
							fieldtype: "Link",
							options: "Account",
							label: __("Stock account in your books"),
							in_list_view: 1,
							columns: 2,
							get_query: () => ({
								filters: { company: frm.doc.company, account_type: "Stock", is_group: 0 },
							}),
						},
						{
							fieldname: "rows_on_sheet",
							fieldtype: "Int",
							label: __("Rows on sheet"),
							in_list_view: 1,
							read_only: 1,
							columns: 1,
						},
						{ fieldname: "warehouse", fieldtype: "Data", hidden: 1 },
					],
				},
				{ fieldname: "notes", fieldtype: "HTML" },
			],
			primary_action_label: __("Save warehouses"),
			primary_action: () => save(),
			secondary_action_label: __("Close"),
			secondary_action: () => dialog.hide(),
		});
		const grid = dialog.fields_dict.rows.grid;
		setup_dialog_grid(grid, refresh);
		const rows = () => grid.df.data || [];
		let reloaded = null;
		if (after) dialog.onhide = () => (reloaded || Promise.resolve()).then(after);
		const save = () => {
			const visible = rows();
			const kept = all.filter((row) => !shown.includes(row));
			return call_method(
				frm,
				"save_warehouses",
				{ rows: [...kept, ...visible].filter((row) => row.warehouse_name) },
				{ freeze: true }
			).then(() => {
				reloaded = reload_fresh(frm);
				dialog.hide();
				frappe.ui.toast({ type: "success", message: __("Warehouses saved.") });
			});
		};
		const refresh_state = () => {
			const $intro = dialog.get_field("intro").$wrapper.empty();
			$intro.append(
				$(`<div class="text-p-base text-ink-gray-7 mb-3"></div>`).text(
					job_workers
						? __("Your job workers, each with a warehouse for your stock with them.")
						: __("One row per warehouse. New warehouses from your stock sheet are listed too.")
				)
			);
			const new_workers = [
				...new Set(
					rows()
						.filter((row) => row.kind === "Job worker" && is_new_supplier(row.job_worker))
						.map((row) => row.job_worker)
				),
			];
			const alerts = [];
			const no_worker = rows().filter((row) => row.kind === "Job worker" && !row.job_worker);
			if (no_worker.length) {
				alerts.push({
					theme: "red",
					title: plural(
						no_worker.length,
						__("Name the job worker of 1 warehouse."),
						__("Name the job worker of {0} warehouses.", [no_worker.length])
					),
				});
			}
			if (new_workers.length && !details.can_create_supplier) {
				alerts.push({
					theme: "red",
					title: __("You can't add a Supplier, so {0} can't be made. Ask someone who can.", [
						new_workers.join(", "),
					]),
				});
			}
			$intro.append(make_alerts(alerts).addClass("mb-3"));
			if (new_workers.length && details.can_create_supplier) {
				$intro.append(
					grey_line(
						new_workers.length === 1
							? __("1 job worker is new: {0}. It is made as a supplier when you save.", [
									new_workers[0],
							  ])
							: __("{0} job workers are new: {1}. They are made as suppliers when you save.", [
									new_workers.length,
									new_workers.join(", "),
							  ])
					).addClass("mb-3")
				);
			}
			const locked = rows().filter((row) => row.locked && row.account);
			dialog
				.get_field("notes")
				.$wrapper.empty()
				.append(
					locked.map((row) =>
						grey_line(
							__("{0} has stock entries, so its stock account stays {1}.", [
								row.warehouse_name,
								short_account(frm, row.account),
							])
						).addClass("mt-2")
					)
				);
			const added = rows().filter((row) => !row.warehouse && row.warehouse_name).length;
			dialog
				.get_primary_btn()
				.text(
					added
						? plural(added, __("Add 1 warehouse"), __("Add {0} warehouses", [added]))
						: __("Save warehouses")
				);
			set_primary_disabled(dialog, alerts.length > 0);
		};
		dialog.show();
		refresh_state();
	});
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

function setup_dialog_grid(grid, on_delete) {
	grid.delete_all_rows = () =>
		frappe.confirm(__("Are you sure you want to delete all {0} rows?", [grid.data.length]), () => {
			grid.df.data = [];
			grid.refresh();
		});
	if (!on_delete) return;
	const refresh = grid.refresh.bind(grid);
	let count = (grid.df.data || []).length;
	grid.refresh = (...args) => {
		const result = refresh(...args);
		const before = count;
		count = (grid.df.data || []).length;
		if (count < before) on_delete();
		return result;
	};
}

// a qty or rate typed keeps the row's rate: the value follows
function set_rate_on_amount() {
	if (!flt(this.doc.qty)) return;
	this.doc.rate = flt(this.doc.amount) / flt(this.doc.qty);
	this.grid_row?.refresh_field("rate");
}

function keep_rate_on_qty() {
	const row = this.doc;
	const rate = flt(row.rate) || (flt(row.old_qty) ? flt(row.old_amount) / flt(row.old_qty) : 0);
	if (!rate) return;
	row.rate = rate;
	row.amount = flt(flt(row.qty) * rate, 2);
	this.grid_row?.refresh_field("amount");
}

function list_column(fieldname, fieldtype, label, columns, extra = {}) {
	return { fieldname, fieldtype, label, columns, in_list_view: 1, ...extra };
}

// Show the fields of one view of a window, as {view: [fieldnames]}, and hide the others
function show_view_fields(dialog, views, view) {
	for (const [name, fields] of Object.entries(views)) {
		for (const field of fields) {
			if (dialog.fields_dict[field]) dialog.set_df_property(field, "hidden", name === view ? 0 : 1);
		}
	}
}

// A window's confirm in its "confirm" field: back says what going back does, then the body
function fill_confirm(dialog, { title, back, on_back, body, primary, on_primary, theme }) {
	dialog.set_title(title);
	dialog
		.get_field("confirm")
		.$wrapper.empty()
		.append($(`<div class="flex flex-col gap-3"></div>`).append(make_back_line(back, on_back), body));
	set_primary_disabled(dialog, false);
	dialog.set_primary_action(primary, on_primary);
	if (theme) dialog.get_primary_btn().attr("data-theme", theme);
	dialog.set_secondary_action_label(__("Back"));
	dialog.set_secondary_action(on_back);
}

// The sheet, its cards and messages, and the confirm that replaces it. Each step's window fills it.
class SheetWindow {
	// row_form: each row opens a form with the fields not in the list. open: opens this window again.
	// save_method and get_method keep the rows and read the checks, with args.
	constructor(
		frm,
		{
			title,
			where,
			fields,
			extra_fields = [],
			has_data,
			row_form = false,
			open,
			save_method,
			get_method,
			args = {},
		}
	) {
		const win = this;
		this.frm = frm;
		this.open = open;
		this.save_method = save_method;
		this.get_method = get_method;
		this.args = args;
		this.day = day_before_text(frm);
		this.title = title;
		this.dirty = false;
		this.alerts = [];
		this.lines = [];
		// the key of the alert whose rows alone show
		this.only = null;
		this.dialog = new frappe.ui.Dialog({
			title: title,
			size: "extra-large",
			fields: [
				{ fieldname: "head", fieldtype: "HTML" },
				{ fieldname: "stats", fieldtype: "HTML" },
				{ fieldname: "alerts", fieldtype: "HTML" },
				{
					fieldname: "rows",
					fieldtype: "Table",
					in_place_edit: row_form ? 0 : 1,
					data: [],
					fields: fields.map((df) =>
						df.hidden
							? df
							: {
									...df,
									formatter: (value, column, options, doc) =>
										win.format_cell(df, value, column, options, doc),
									...(!df.read_only && {
										onchange: function (e) {
											df.onchange?.apply(this, [e]);
											win.changed();
										},
									}),
							  }
					),
				},
				{ fieldname: "notes", fieldtype: "HTML" },
				...extra_fields,
				{ fieldname: "confirm", fieldtype: "HTML", hidden: 1 },
				...(where ? where_to_find_fields(frm, where, { has_data: has_data, posted: false }) : []),
			],
			primary_action_label: __("Post to books"),
			primary_action: () => this.post(),
			secondary_action_label: __("Save draft"),
			secondary_action: () => this.save_draft(),
		});
		if (where) fill_where(frm, this.dialog, where);
		this.grid = this.dialog.fields_dict.rows.grid;
		setup_dialog_grid(this.grid, () => this.changed());
		ask_before_closing(
			this.dialog,
			() => this.dirty,
			() => this.save().then(() => (this.dirty = false))
		);
		this.$back = $(`<div class="hide"></div>`).appendTo(this.dialog.custom_actions);
		this.views = {
			sheet: ["head", "stats", "alerts", "rows", "notes", "where_section"],
			confirm: ["confirm"],
		};
	}

	render() {}

	refresh_footer() {}

	set_view(view) {
		show_view_fields(this.dialog, this.views, view);
		this.dialog.get_primary_btn().removeAttr("data-theme").removeClass("hide").prop("disabled", false);
		this.$back.empty().addClass("hide");
		this.view = view;
	}

	set_back(onclick) {
		this.$back
			.empty()
			.removeClass("hide")
			.append(frappe.ui.button({ label: __("Back"), onclick: onclick }));
	}

	set_rows(rows) {
		this.grid.df.data = rows;
		this.grid.refresh();
	}

	get_rows() {
		return this.grid.df.data || [];
	}

	// only the rows an alert is about, first so they are on the table's first page; or all again
	show_rows(alert) {
		this.only = this.only === alert.key ? null : alert.key;
		if (this.only) this.move_first(alert);
		this.draw_alerts();
	}

	move_first(alert) {
		const open = new Set(this.open_rows(alert));
		const rows = this.get_rows();
		this.set_rows([...rows.filter((row) => open.has(row)), ...rows.filter((row) => !open.has(row))]);
	}

	// a cell an alert marked, not changed since
	is_open(alert, row, field) {
		const seen = alert.seen.get(row);
		return Boolean(seen) && field in seen && cstr(row[field]) === cstr(seen[field]);
	}

	// an alert's rows still in the table; with marked cells, those with one left
	open_rows(alert) {
		return this.get_rows().filter((row) => {
			const seen = alert.seen.get(row);
			if (!seen) return false;
			const fields = Object.keys(seen);
			return !fields.length || fields.some((field) => this.is_open(alert, row, field));
		});
	}

	format_cell(df, value, column, options, doc) {
		const formatted = (df.formatter || frappe.form.get_formatter(df.fieldtype))(
			value,
			column,
			options,
			doc
		);
		const alert =
			doc && this.alerts.find((alert) => alert.seen && this.is_open(alert, doc, df.fieldname));
		if (!alert) return formatted;
		if (value == null || value === "") {
			return frappe.ui.badge.html({ label: __("Needed"), theme: "red" });
		}
		const badge = frappe.ui.badge.html({ label: alert.badge || __("Check"), theme: "red" });
		return `<div class="flex items-center gap-1"><div class="truncate min-w-0">${formatted}</div><div class="shrink-0">${badge}</div></div>`;
	}

	changed() {
		this.dirty = true;
		this.draw_alerts();
		this.refresh_footer();
	}

	make_head(intro, { upload, menu = [] } = {}) {
		this.dialog.get_field("head").$wrapper.empty().append(make_sheet_head(intro, upload, menu));
	}

	set_stats(cards) {
		this.dialog
			.get_field("stats")
			.$wrapper.empty()
			.append(
				cards.length
					? $(`<div class="es-stat-card-grid company-setup-stats mb-3"></div>`).append(cards)
					: []
			);
	}

	// rows (doc_idx or a test): the rows an alert is about, for Show rows. fields: the cells in them
	// that show a badge until changed, the empty ones, else all
	set_alerts(alerts, lines = []) {
		const rows = this.get_rows();
		this.alerts = alerts.map((alert) => {
			if (!alert.rows) return alert;
			const idxs = new Set(typeof alert.rows === "function" ? [] : alert.rows);
			const test = typeof alert.rows === "function" ? alert.rows : (row) => idxs.has(row.doc_idx);
			const fields = alert.fields || [];
			const seen = rows.filter(test).map((row) => {
				const empty = fields.filter((field) => !row[field]);
				return [row, Object.fromEntries((empty.length ? empty : fields).map((f) => [f, row[f]]))];
			});
			return seen.length ? { ...alert, key: alert.key || alert.title, seen: new Map(seen) } : alert;
		});
		this.lines = lines;
		const only = this.alerts.find((alert) => alert.seen && alert.key === this.only);
		if (only) this.move_first(only);
		this.draw_alerts();
	}

	draw_alerts() {
		const all = this.get_rows().length;
		const alerts = this.alerts.filter((alert) => !alert.seen || this.open_rows(alert).length);
		const only = alerts.find((alert) => alert.seen && alert.key === this.only);
		if (!only) this.only = null;
		const showing = (count) =>
			count === 1
				? __("Showing only this one of {0}.", [all])
				: __("Showing only these {0} of {1}.", [count, all]);
		this.dialog
			.get_field("alerts")
			.$wrapper.empty()
			.append(
				make_alerts(
					alerts.map((alert) =>
						alert.seen
							? {
									...alert,
									description:
										alert === only
											? [showing(this.open_rows(alert).length), alert.description]
													.filter(Boolean)
													.join(" ")
											: alert.description,
									buttons: [
										...(alert.buttons || []),
										{
											label: alert === only ? __("Show all") : __("Show rows"),
											onclick: () => this.show_rows(alert),
										},
									],
							  }
							: alert
					)
				).addClass("mb-3"),
				this.lines
					.filter(Boolean)
					.map((line) => (line.jquery ? line : grey_line(line)).addClass("mb-3"))
			);
		const open = only && new Set(this.open_rows(only));
		// a grid refreshes a changed cell only in its first row, so the badges are drawn here
		const fields = new Set(this.alerts.flatMap((alert) => alert.fields || []));
		for (const grid_row of this.grid.grid_rows || []) {
			grid_row.wrapper.toggle(!open || open.has(grid_row.doc));
			for (const field of fields) grid_row.refresh_field(field);
		}
	}

	upload(method, replaces, after = (result) => this.refresh(result)) {
		upload_sheet(this.frm, method, this.args, { replaces, after });
	}

	show(replacing) {
		this.set_rows(this.drafts);
		this.show_sheet();
		show_in_place_of(this.dialog, replacing);
	}

	save(rows = this.get_rows(), args = {}) {
		return call_method(this.frm, this.save_method, {
			...this.args,
			...args,
			rows: rows.map((row) => ({ ...row, doc_idx: undefined, idx: undefined })),
		});
	}

	save_draft() {
		return this.save()
			.then(() => this.refresh())
			.then(() => frappe.ui.toast({ type: "success", message: __("Saved as a draft.") }));
	}

	// the saved rows and their checks, drawn again in this window; new columns need a new one
	refresh(result = null) {
		const scroll = this.dialog.$wrapper.scrollTop();
		return reload_fresh(this.frm)
			.then(() => get_data(this.frm, this.get_method, this.args))
			.then((r) => {
				if (this.layout_changed(r.message)) return this.open(result, this.dialog);
				this.load(r.message);
				this.result = result;
				this.dirty = false;
				this.set_rows(this.drafts);
				this.show_sheet();
				this.dialog.$wrapper.scrollTop(scroll);
			});
	}

	// the checks again, on the rows saved: a red shows on the sheet, else the confirm
	post() {
		const go = () =>
			get_data(this.frm, this.get_method, this.args).then((r) => {
				this.checks = r.message;
				if (this.checks.blocker) return this.render();
				this.confirm_post();
			});
		if (!this.dirty) return go();
		return this.save().then(() => {
			this.dirty = false;
			return go();
		});
	}

	show_confirm(confirm) {
		this.set_view("confirm");
		fill_confirm(this.dialog, { ...confirm, on_back: () => this.show_sheet() });
	}

	show_sheet() {
		this.set_view("sheet");
		this.dialog.set_title(this.title);
		this.render();
	}

	layout_changed() {
		return false;
	}
}

// ---------- Opening stock ----------

const STOCK_FIELDS = [
	"item_name",
	"warehouse_name",
	"qty",
	"uom",
	"rate",
	"amount",
	"stock_group",
	"batch_no",
	"expiry_date",
	"serial_no",
	"challan_no",
	"challan_date",
	"return_due",
	"source",
	"idx",
];

function show_opening_stock(frm, result, replacing) {
	get_data(frm, "get_opening_stock").then((r) =>
		new OpeningStockWindow(frm, r.message, result).show(replacing)
	);
}

// The columns opening stock shows: the challan ones for job work, else the batch ones
function get_stock_layout(frm, checks) {
	const drafts = (frm.doc.opening_stock || []).filter((row) => row.status !== "Posted");
	const job_work =
		checks.reds.some((red) => red.key === "challan") ||
		drafts.some((row) => row.challan_no || row.challan_date);
	const batched = !job_work && drafts.some((row) => row.batch_no || row.expiry_date);
	return { drafts, job_work, batched };
}

class OpeningStockWindow extends SheetWindow {
	constructor(frm, checks, result) {
		const { drafts, job_work, batched } = get_stock_layout(frm, checks);
		super(frm, {
			title: __("Opening stock"),
			open: (result, replacing) => show_opening_stock(frm, result, replacing),
			save_method: "save_opening_stock",
			get_method: "get_opening_stock",
			where: "opening_stock",
			has_data: drafts.length,
			fields: [
				list_column("item_name", "Data", __("Item"), job_work || batched ? 2 : 3),
				list_column("warehouse_name", "Data", __("Warehouse"), job_work || batched ? 1 : 2),
				list_column("qty", "Float", __("Qty"), 1, { onchange: keep_rate_on_qty }),
				list_column("uom", "Data", __("Unit"), 1),
				// the rate shows a typo in qty or value at a glance
				list_column("rate", "Currency", __("Rate"), 1, { onchange: keep_rate_on_qty }),
				list_column("amount", "Currency", __("Value"), job_work ? 1 : 2, {
					onchange: set_rate_on_amount,
				}),
				job_work
					? list_column("challan_no", "Data", __("Challan no"), 1)
					: { fieldname: "challan_no", fieldtype: "Data", label: __("Challan no") },
				job_work
					? list_column("challan_date", "Date", __("Challan date"), 1)
					: { fieldname: "challan_date", fieldtype: "Date", label: __("Challan date") },
				job_work
					? list_column("return_due", "Date", __("Return due"), 1)
					: { fieldname: "return_due", fieldtype: "Date", label: __("Return due") },
				batched
					? list_column("batch_no", "Data", __("Batch"), 1)
					: { fieldname: "batch_no", fieldtype: "Data", label: __("Batch") },
				batched
					? list_column("expiry_date", "Date", __("Expiry date"), 1)
					: { fieldname: "expiry_date", fieldtype: "Date", label: __("Expiry date") },
				{ fieldname: "serial_no", fieldtype: "Small Text", label: __("Serial No") },
				{ fieldname: "stock_group", fieldtype: "Data", label: __("Stock group") },
				{ fieldname: "source", fieldtype: "Data", hidden: 1 },
				{ fieldname: "doc_idx", fieldtype: "Int", hidden: 1 },
			],
			extra_fields: [{ fieldname: "lines", fieldtype: "HTML" }],
		});
		this.views.sheet.push("lines");
		this.result = result;
		this.layout = [job_work, batched].join();
		this.load(checks);
	}

	// back in front after warehouses were added in another browser tab: the checks again, unless
	// rows are being edited
	show(replacing) {
		super.show(replacing);
		const refresh = () => this.dialog.display && !this.dirty && this.refresh();
		$(window).on("focus", refresh);
		this.dialog.onhide = () => $(window).off("focus", refresh);
	}

	layout_changed(checks) {
		const { job_work, batched } = get_stock_layout(this.frm, checks);
		return [job_work, batched].join() !== this.layout;
	}

	load(checks) {
		this.checks = checks;
		this.drafts = (this.frm.doc.opening_stock || [])
			.filter((row) => row.status !== "Posted")
			.map((row) => ({
				...Object.fromEntries(STOCK_FIELDS.map((field) => [field, row[field]])),
				old_qty: row.qty,
				old_amount: row.amount,
				doc_idx: row.idx,
			}));
	}

	render() {
		const { checks, frm } = this;
		const drafts = checks.drafts;
		const posted = checks.posted;
		const tb = checks.trial_balance;
		const lines = Object.keys(tb.lines).length;
		this.make_head(
			drafts.rows || !posted.rows
				? __("One row per item per warehouse, as on {0}. Upload one sheet or several.", [this.day])
				: __("One row per item per warehouse, as on {0}.", [this.day]),
			{
				upload: () =>
					this.upload(
						"import_opening_stock",
						this.get_rows().length &&
							__(
								"The sheet replaces draft rows of the same item and warehouse, and its own drafts if it was uploaded before. Other drafts and posted rows stay as they are. Continue?"
							)
					),
				menu: [
					posted.rows && {
						label: __("Posted stock"),
						icon: "list",
						onclick: () => {
							this.dialog.hide();
							show_posted_stock(frm);
						},
					},
					{
						label: __("Download sheet"),
						icon: "download",
						onclick: () =>
							download_sheet(frm.doc.company, "opening_stock.download_opening_stock"),
					},
					...[...new Set((frm.doc.undone_stock || []).map((row) => row.source || ""))].map(
						(source) => ({
							label: __("Undone rows back to drafts: {0}", [source_name(source)]),
							icon: "undo-2",
							onclick: () =>
								call_method(frm, "restore_undone_stock", { source }).then(() =>
									this.refresh()
								),
						})
					),
					...(tb.movable || []).map((account) => ({
						label: __("Keep {0} as a balance", [short_account(frm, account)]),
						icon: "undo-2",
						onclick: () =>
							frappe.confirm(
								__(
									"{0} goes back to Opening balances as a plain balance, and no stock rows post to it. Continue?",
									[short_account(frm, account)]
								),
								() =>
									call_method(frm, "move_to_opening_balances", { account }).then(() =>
										this.refresh()
									)
							),
					})),
				].filter(Boolean),
			}
		);
		const sources = (list) => list.map(source_name).join(", ");
		const value = (rows, amount) => __("{0} · {1}", [count_rows(rows), money(this.frm, amount)]);
		this.set_stats(
			[
				drafts.rows &&
					frappe.ui.stat_card({
						label: drafts.sources.length > 1 ? __("In draft") : __("On this sheet"),
						value: value(drafts.rows, drafts.value),
						caption: [
							this.result?.read &&
								plural(
									this.result.read,
									__("1 row read"),
									__("{0} rows read", [this.result.read])
								),
							sources(drafts.sources),
							count_warehouses(drafts.warehouses),
						]
							.filter(Boolean)
							.join(" · "),
					}),
				frappe.ui.stat_card({
					label: __("Posted"),
					value: posted.rows ? value(posted.rows, posted.value) : __("Nothing yet"),
					caption: posted.rows
						? drafts.rows
							? sources(posted.sources)
							: [
									plural(
										posted.sources.length,
										__("1 upload"),
										__("{0} uploads", [posted.sources.length])
									),
									count_warehouses(posted.warehouses),
							  ].join(" · ")
						: null,
				}),
				frappe.ui.stat_card({
					label: __("Your trial balance"),
					value: money(this.frm, tb.total),
					caption: lines
						? plural(
								lines,
								__("1 stock account"),
								__("{0} stock accounts, each checked on its own", [lines])
						  )
						: __("No stock in your trial balance"),
				}),
			].filter(Boolean)
		);
		const kept = Object.entries(tb.kept || {}).map(([account, amount]) =>
			__("{0} {1} is kept in your opening balances, so it is not counted here.", [
				short_account(frm, account),
				money(this.frm, amount, 0),
			])
		);
		const upload_lines = [];
		if (this.result?.replaced) {
			upload_lines.push(
				plural(
					this.result.replaced,
					__("1 draft row was replaced by the new sheet."),
					__("{0} draft rows were replaced by the new sheet.", [this.result.replaced])
				)
			);
		}
		if (this.result?.skipped) {
			upload_lines.push(
				plural(
					this.result.skipped,
					__("1 row is already posted and is left out."),
					__("{0} rows are already posted and are left out.", [this.result.skipped])
				)
			);
		}
		this.set_alerts(this.get_alerts(), [...upload_lines, ...kept]);
		this.dialog.set_df_property("rows", "hidden", drafts.rows || !posted.rows ? 0 : 1);
		this.dialog.set_df_property("where_section", "hidden", drafts.rows || !posted.rows ? 0 : 1);
		this.render_lines();
		this.refresh_footer();
	}

	get_alerts() {
		const { checks, frm } = this;
		const go = (label, open) => ({
			label: label,
			onclick: () => {
				this.dialog.hide();
				open();
			},
		});
		const reopen = () => show_opening_stock(frm);
		// the server's reds, in order; a few get a button to the step that fixes them
		const actions = {
			posting_blocker: checks.posting_fix
				? [fix_button(checks.posting_fix, frm, () => this.refresh())]
				: [],
			perpetual_inventory: [
				{
					label: __("Turn it on"),
					onclick: () => call_method(frm, "turn_on_perpetual_inventory").then(() => this.refresh()),
				},
			],
			missing_warehouses: [go(__("Go to Warehouses"), () => show_warehouses(frm, { after: reopen }))],
			wrong_lines: [go(__("Go to Warehouses"), () => show_warehouses(frm, { after: reopen }))],
			no_serials: [
				{
					label: __("Make serial numbers"),
					onclick: () =>
						(this.dirty ? this.save() : Promise.resolve())
							.then(() => call_method(frm, "make_serial_numbers"))
							.then(() => this.refresh()),
				},
			],
			untracked: frm.doc.__onload?.serial_batch_off
				? [
						{
							label: __("Turn on batch and serial numbers"),
							onclick: () =>
								call_method(frm, "turn_on_serial_and_batch").then(() => this.refresh()),
						},
				  ]
				: [],
			missing_items: [go(__("Go to Items"), () => open_party_import(frm, "Item"))],
			in_stock: [go(__("Posted stock"), () => show_posted_stock(frm))],
			kept: checks.reds.find((red) => red.key === "kept")?.account
				? [
						{
							label: __("Move it to stock"),
							onclick: () =>
								call_method(frm, "move_to_opening_stock", {
									account: checks.reds.find((red) => red.key === "kept").account,
								}).then(() => this.refresh()),
						},
				  ]
				: [],
		};
		const names_of = (idxs) => {
			const set = new Set(idxs);
			const names = [
				...new Set(
					this.get_rows()
						.filter((row) => set.has(row.doc_idx))
						.map((row) => row.item_name)
				),
			];
			return names.slice(0, 5).join(", ") + (names.length > 5 ? ", …" : "");
		};
		// the cells a red is about, marked in its rows
		const fields = {
			missing_warehouses: ["warehouse_name"],
			missing_items: ["item_name"],
			negative: ["qty"],
			no_qty: ["qty"],
			unknown_units: ["uom"],
			part_qty: ["qty"],
			challan: ["challan_no", "challan_date", "return_due"],
			no_batch: ["batch_no"],
		};
		const alerts = (checks.reds || [])
			.filter((item) => item.key !== "no_drafts")
			.map((item) => ({
				theme: "red",
				key: item.key,
				title: item.text,
				description: item.key === "missing_items" ? names_of(item.rows) : undefined,
				buttons: actions[item.key] || [],
				rows: item.rows,
				fields: fields[item.key],
				badge: item.key.startsWith("missing_") ? __("Not in ERPNext") : undefined,
			}));
		let red_count = alerts.length;
		const close = Object.entries(checks.close_items || {});
		if (close.length) {
			alerts.push({
				theme: "yellow",
				title: plural(
					close.length,
					__("1 item has a close match: {0}.", [
						close.map(([sheet, item]) => `${sheet} → ${item}`)[0],
					]),
					__("{0} items have close matches.", [close.length])
				),
				description: __("Check them before you use them."),
				buttons: [{ label: __("Use them"), onclick: () => this.use_close_items(close) }],
				rows: (row) => close.some(([sheet]) => sheet === row.item_name),
				fields: ["item_name"],
			});
		}
		if (checks.expired?.length) {
			alerts.push({
				theme: "yellow",
				title: plural(
					checks.expired.length,
					__("1 batch expired by {0}.", [this.day]),
					__("{0} batches expired by {1}.", [checks.expired.length, this.day])
				),
				description: __("They post as stock. Leave them out if you will write them off."),
				rows: checks.expired,
			});
		}
		if (this.result?.differ) {
			alerts.push({
				theme: "yellow",
				title: plural(
					this.result.differ,
					__("1 row left out differs from what was posted."),
					__("{0} rows left out differ from what was posted.", [this.result.differ])
				),
				description: __("Correct them in Posted stock."),
				buttons: [go(__("Posted stock"), () => show_posted_stock(frm))],
			});
		}
		red_count = alerts.filter((alert) => alert.theme === "red").length;
		const off = checks.drafts.rows ? checks.off : checks.match?.off ?? checks.off;
		if (
			!red_count &&
			off !== null &&
			Math.abs(off) >= 1 &&
			Object.keys(checks.trial_balance.lines).length
		) {
			alerts.push({
				theme: "yellow",
				title:
					off > 0
						? __("{0} less than your trial balance.", [money(this.frm, off)])
						: __("{0} more than your trial balance.", [money(this.frm, -off)]),
				description: this.get_biggest_rows(),
				buttons:
					checks.drafts.rows || !checks.posted.rows
						? []
						: [
								{ label: __("Post the difference"), onclick: () => this.show_difference() },
								{
									label: __("Show rows"),
									variant: "outline",
									onclick: () =>
										this.dialog
											.get_field("lines")
											.$wrapper[0].scrollIntoView({ block: "center" }),
								},
						  ],
			});
		} else if (!red_count && !checks.drafts.rows && checks.match?.lines_off?.length) {
			alerts.push({
				theme: "red",
				title: __("The total matches, but stock is on the wrong accounts."),
				description: __(
					"Undo the warehouses on the wrong account, set their stock account in Warehouses, and post them again."
				),
				buttons: [
					go(__("Posted stock"), () => show_posted_stock(frm)),
					go(__("Go to Warehouses"), () => show_warehouses(frm, { after: reopen })),
				],
			});
		}
		return alerts;
	}

	// the rows of most value: a typo in qty or value is usually one of them
	get_biggest_rows() {
		const rows = this.checks.drafts.rows
			? this.get_rows()
			: (this.frm.doc.opening_stock || []).filter((row) => row.status === "Posted");
		const biggest = [...rows]
			.sort((a, b) => Math.abs(flt(b.amount)) - Math.abs(flt(a.amount)))
			.slice(0, 3)
			.map((row) => `${row.item_name} ${money(this.frm, row.amount)}`);
		return biggest.length ? __("Biggest rows: {0}", [biggest.join(", ")]) : undefined;
	}

	// every warehouse posted with its stock line, then each line against the trial balance
	render_lines() {
		const $lines = this.dialog.get_field("lines").$wrapper.empty();
		const match = this.checks.match;
		if (this.checks.drafts.rows || !match?.warehouses?.length) return;
		$lines.append(
			make_read_table(
				[
					[__("Warehouse")],
					[__("Rows"), true],
					[__("Posted"), true],
					[__("Stock account in your books")],
				],
				match.warehouses.map((warehouse) => [
					warehouse.warehouse,
					format_number(warehouse.rows, null, 0),
					money(this.frm, warehouse.posted),
					short_account(this.frm, warehouse.account),
				])
			).addClass("mb-3"),
			make_read_table(
				[
					[__("Stock account in your books")],
					[__("Posted"), true],
					...(match.difference_posted ? [[__("Difference posted"), true]] : []),
					[__("Trial balance"), true],
					[__("Still off"), true],
				],
				match.lines.map((line) => [
					short_account(this.frm, line.account) || __("Stock"),
					money(this.frm, line.books),
					...(match.difference_posted ? [money(this.frm, line.difference_posted)] : []),
					money(this.frm, line.line),
					money(this.frm, line.difference),
				])
			)
		);
	}

	// the checks were for the saved rows: keep only the alerts about rows, which follow the edits
	changed() {
		if (!this.dirty) {
			this.alerts = this.alerts.filter((alert) => alert.seen);
			this.lines = [__("Rows changed. Save draft to check them again.")];
			this.dialog.get_field("lines").$wrapper.empty();
		}
		super.changed();
	}

	refresh_footer() {
		if (this.view !== "sheet") return;
		const has_drafts = this.get_rows().length;
		this.dialog.set_primary_action(__("Post to books"), () => this.post());
		this.dialog.get_primary_btn().toggleClass("hide", !has_drafts);
		this.dialog.set_secondary_action_label(has_drafts || this.dirty ? __("Save draft") : __("Close"));
		this.dialog.set_secondary_action(() =>
			has_drafts || this.dirty ? this.save_draft() : this.dialog.hide()
		);
		const blocker = this.checks.blocker;
		set_primary_disabled(
			this.dialog,
			!this.dirty && has_drafts > 0 && (!this.checks.perpetual_inventory || Boolean(blocker))
		);
	}

	use_close_items(close) {
		const map = Object.fromEntries(close);
		const rows = this.get_rows().map((row) =>
			map[row.item_name] ? { ...row, item_name: map[row.item_name] } : row
		);
		this.save(rows).then(() => this.refresh());
	}

	confirm_post() {
		const { checks } = this;
		const warehouses = [...new Set(this.get_rows().map((row) => row.warehouse_name))];
		const off = checks.off;
		const migration = frappe.datetime.str_to_user(this.frm.doc.migration_date);
		this.show_confirm({
			title: __("Post opening stock?"),
			back: __("Nothing is posted until you confirm."),
			body: [
				make_kv_lines(
					[
						[
							__("To post"),
							[
								count_rows(checks.drafts.rows),
								warehouses.length <= 2
									? warehouses.join(", ")
									: __("{0} warehouses", [warehouses.length]),
								money(this.frm, checks.drafts.value),
							].join(" · "),
						],
						checks.posted.rows && [
							__("Posted before"),
							[
								count_rows(checks.posted.rows),
								money(this.frm, checks.posted.value),
								checks.posted.sources.map(source_name).join(", "),
							].join(" · "),
						],
					].filter(Boolean)
				),
				off !== null &&
					Math.abs(off) >= 1 &&
					grey_line(
						off > 0
							? __(
									"{0} less than your trial balance. Fix rows or post the difference after this.",
									[money(this.frm, off)]
							  )
							: __(
									"{0} more than your trial balance. Fix rows or post the difference after this.",
									[money(this.frm, -off)]
							  )
					),
				checks.later_entries &&
					grey_line(
						__(
							"Cost of goods sold and profit since {0} change. ERPNext works out the new costs in the background, usually within the hour.",
							[migration]
						)
					),
				checks.reposting_timeslot &&
					grey_line(
						__("Later entries are valued again between {0} and {1}.", [
							checks.reposting_timeslot.start_time,
							checks.reposting_timeslot.end_time,
						])
					),
			].filter(Boolean),
			primary: __("Post {0}", [count_rows(checks.drafts.rows)]),
			on_primary: () =>
				call_method(this.frm, "post_opening_stock", {}, { freeze: true }).then((r) => {
					this.dialog.hide();
					after_run(
						this.frm,
						r.message,
						__("Posting the stock in the background. This page updates when it's done."),
						() => show_stock_posted(this.frm, checks.drafts.rows)
					);
				}),
		});
	}

	show_difference() {
		get_data(this.frm, "get_stock_difference").then((r) => {
			const difference = r.message;
			if (difference.blocker) {
				frappe.ui.toast({ type: "error", message: difference.blocker });
				return;
			}
			const total = difference.lines.reduce((sum, line) => sum + flt(line.difference), 0);
			const big = difference.lines.find((line) => line.big);
			let $big = null;
			let checked = null;
			if (big) {
				$big = make_alerts([
					{
						theme: "yellow",
						title: [
							big.difference > 0
								? __("{0} less than your trial balance", [money(this.frm, big.difference)])
								: __("{0} more than your trial balance", [money(this.frm, -big.difference)]),
							this.get_biggest_rows(),
						]
							.filter(Boolean)
							.join(" · "),
						description: __(
							"A gap this big is often a missing warehouse or a typo. Upload the missing warehouse or fix the row, or tick below to post the difference."
						),
					},
				]);
				checked = frappe.ui.form.make_control({
					df: {
						fieldtype: "Check",
						fieldname: "checked",
						label: __("I checked: no warehouse is missing"),
					},
					parent: $big,
					render_input: true,
				});
				checked.$wrapper.addClass("mb-0 mt-2");
				checked.$input.on("change", () =>
					this.dialog.get_primary_btn().prop("disabled", !checked.get_value())
				);
			}
			this.show_confirm({
				title: __("Post {0} to {1}?", [
					money(this.frm, Math.abs(total)),
					short_account(this.frm, difference.account),
				]),
				back: __("Nothing is posted until you confirm."),
				body: [
					make_read_table(
						[
							[__("Line")],
							[__("Posted"), true],
							[__("Trial balance"), true],
							[__("Difference"), true],
						],
						difference.lines.map((line) => [
							short_account(this.frm, line.account) || __("Stock"),
							money(this.frm, line.books),
							money(this.frm, line.line),
							money(this.frm, line.difference),
						])
					),
					$big,
					difference.empty_warehouses?.length &&
						make_alerts([
							{
								theme: "red",
								title: __("No stock posted in {0}.", [
									difference.empty_warehouses.join(", "),
								]),
								description: __(
									"Upload their rows first. Post the difference only if they are empty."
								),
							},
						]),
					grey_line(__("Temporary Opening goes down by {0}.", [money(this.frm, Math.abs(total))])),
					grey_line(
						total > 0
							? __(
									"Your trial balance has {0} more stock than the rows posted. Your stock stays as posted, and the {0} goes to {1}. Show this to your CA.",
									[money(this.frm, total, 0), short_account(this.frm, difference.account)]
							  )
							: __(
									"Your trial balance has {0} less stock than the rows posted. Your stock stays as posted, and the {0} goes to {1}. Show this to your CA.",
									[money(this.frm, -total, 0), short_account(this.frm, difference.account)]
							  )
					),
				].filter(Boolean),
				primary: __("Post the difference"),
				on_primary: () =>
					call_method(
						this.frm,
						"post_stock_difference",
						{ checked: checked?.get_value() ? 1 : 0 },
						{ freeze: true }
					).then(() => {
						close_and_reload(this.frm, this.dialog);
						frappe.ui.toast({
							type: "success",
							message: __("{0} posted to {1}.", [
								money(this.frm, Math.abs(total)),
								short_account(this.frm, difference.account),
							]),
						});
					}),
			});
			if (big) this.dialog.get_primary_btn().prop("disabled", true);
		});
	}
}

// a correction as it changes the stock value, like "Lowers stock by ₹23,31,000"
function stock_change_text(frm, amount) {
	if (Math.abs(flt(amount)) < 0.01) return __("Stock value unchanged");
	return amount > 0
		? __("Raises stock by {0}", [money(frm, amount)])
		: __("Lowers stock by {0}", [money(frm, -amount)]);
}

function show_stock_corrected(frm, result) {
	frappe.ui.toast({
		type: "success",
		message: [__("{0} corrected", [count_rows(result.rows)]), stock_change_text(frm, result.amount)].join(
			" · "
		),
	});
}

function show_stock_posted(frm, rows) {
	frappe.ui.toast({
		type: "success",
		message: __("{0} posted on {1}.", [count_rows(rows), day_before_text(frm)]),
	});
}

// ---------- Posted stock, assets and made orders: by upload, then View, undo ----------

// What was posted from each upload, to view and undo in part or whole. opts says what one row is.
function show_posted_window(frm, opts) {
	const live = frm.doc.status === "Live";
	const can_undo = !live || opts.undo_after_live;
	const dialog = new frappe.ui.Dialog({
		title: opts.title,
		size: "extra-large",
		fields: [
			{ fieldname: "body", fieldtype: "HTML" },
			{
				fieldname: "rows",
				fieldtype: "Table",
				hidden: 1,
				cannot_add_rows: 1,
				cannot_delete_rows: 1,
				in_place_edit: 1,
				data: [],
				fields: opts.fields.map((df) => ({ in_list_view: 1, read_only: 1, ...df })),
			},
			{ fieldname: "extra", fieldtype: "HTML" },
		],
		secondary_action_label: __("Close"),
		secondary_action: () => dialog.hide(),
	});
	const $body = dialog.get_field("body").$wrapper;
	const grid = dialog.fields_dict.rows.grid;
	setup_dialog_grid(grid);
	const line_of = (group) =>
		[group.posted_on && opts.posted_word(format_time_posted(group.posted_on)), opts.group_line(group)]
			.filter(Boolean)
			.join(" · ");
	const show_grid = (rows) => {
		dialog.set_df_property("rows", "hidden", rows ? 0 : 1);
		grid.df.data = rows || [];
		grid.refresh();
	};
	const set_primary = (label, action) => {
		const $primary = dialog.get_primary_btn();
		if (!label) return $primary.addClass("hide");
		dialog.set_primary_action(label, action);
		$primary.attr("data-theme", "red");
	};
	let posted;

	const show_list = () => {
		dialog.set_title(opts.title);
		set_primary(null);
		dialog.set_secondary_action_label(__("Close"));
		dialog.set_secondary_action(() => dialog.hide());
		show_grid(null);
		dialog.get_field("extra").$wrapper.empty();
		const extra_rows = opts.extra_rows?.(posted) || [];
		const undone = opts.undone?.(posted) || [];
		$body.empty().append(
			$(`<div class="flex flex-col gap-3"></div>`).append(
				grey_line(
					can_undo
						? __("You can undo these until you go live.")
						: __("One row for each sheet or typed-in batch.")
				),
				posted.groups.length || extra_rows.length
					? make_box([
							...posted.groups.map((group) => [
								make_title_cell(source_name(group.source), line_of(group)),
								opts.group_amount
									? make_text(opts.group_amount(group), "text-ink-gray-8 shrink-0")
									: [],
								make_view_button(() => show_group(group)),
							]),
							...extra_rows,
					  ])
					: [],
				undone.length
					? [
							$(`<div class="text-base-medium text-ink-gray-8 mt-2"></div>`).text(__("Undone")),
							make_box(
								undone.map((group) => [
									make_title_cell(source_name(group.source), group.line),
									group.amount ? make_text(group.amount, "text-ink-gray-8 shrink-0") : [],
									group.download
										? frappe.ui.button({
												label: __("Download"),
												icon: "download",
												variant: "ghost",
												size: "sm",
												onclick: () => group.download(),
										  })
										: [],
								])
							),
					  ]
					: []
			)
		);
	};

	const show_group = (group, first) => {
		dialog.set_title(opts.title);
		set_primary(null);
		dialog.set_secondary_action_label(__("Close"));
		dialog.set_secondary_action(() => dialog.hide());
		const rows = opts.rows(group);
		show_grid(first ? [...rows.filter(first), ...rows.filter((row) => !first(row))] : rows);
		const $head = $(`<div class="flex flex-col gap-3 mb-3"></div>`);
		$body.empty().append($head);
		const refresh = () => {
			const selected = grid.get_selected_children();
			const blocked = (selected.length ? selected : grid.df.data).filter(opts.blocked);
			const alert = blocked.length && can_undo && opts.blocked_alert(blocked, group, dialog);
			$head.empty().append(
				make_back_header(source_name(group.source), line_of(group), () => show_list(), [
					...(opts.group_actions?.(group, dialog) || []),
					...(can_undo
						? [
								frappe.ui.button({
									label: opts.undo_label(selected, group),
									icon: "undo-2",
									theme: "red",
									variant: selected.length ? "subtle" : "outline",
									disabled: Boolean(blocked.length),
									onclick: () => show_confirm(group, selected.length ? selected : null),
								}),
						  ]
						: []),
				]),
				alert
					? make_alerts([
							{
								theme: "red",
								...alert,
								buttons: [
									...(alert.buttons || []),
									{
										label: __("Show rows"),
										variant: "outline",
										onclick: () => show_group(group, opts.blocked),
									},
								],
							},
					  ])
					: [],
				opts.group_note ? opts.group_note(group) : []
			);
		};
		grid.wrapper.off("change.undo").on("change.undo", ".grid-row-check", () => refresh());
		refresh();
	};

	const show_confirm = (group, selected) => {
		const rows = selected || opts.rows(group);
		show_grid(null);
		dialog.set_title(opts.confirm_title(rows, selected));
		const $choice = make_delete_choice(frm, {
			...opts.confirm_what(rows, selected),
			source: group.source,
		});
		$body.empty().append(
			$(`<div class="flex flex-col gap-3"></div>`).append(
				$choice,
				make_back_line(opts.confirm_text(rows, selected), () => show_group(group)),
				make_box([
					...opts.confirm_rows(rows),
					...(opts.total
						? [
								[
									make_text(__("Total"), "text-ink-gray-6 grow"),
									make_text(
										money(frm, opts.total(rows)),
										"text-base-medium text-ink-gray-8 shrink-0"
									),
								],
						  ]
						: []),
				]),
				opts.confirm_note?.(rows, selected) || []
			)
		);
		dialog.set_secondary_action_label(__("Back"));
		dialog.set_secondary_action(() => show_group(group));
		set_primary(opts.confirm_button(rows, selected), () =>
			opts.undo(group, selected, $choice.delete_entries()).then((r) => {
				dialog.hide();
				after_run(frm, r.message, UNDOING_TEXT(), (result) => opts.after_undo(result, group));
			})
		);
	};

	opts.load().then((data) => {
		posted = data;
		show_list();
		dialog.show();
	});
}

function show_posted_stock(frm) {
	const match = frm.doc.__onload?.stock_match;
	const line_of = Object.fromEntries(
		(match?.warehouses || []).map((row) => [row.warehouse, row.line ?? row.account])
	);
	let difference;
	show_posted_window(frm, {
		title: __("Posted stock"),
		load: () =>
			get_data(frm, "get_posted_entries", { kind: "stock" }).then((r) => {
				difference = r.message.difference;
				return r.message;
			}),
		posted_word: (when) => __("Posted {0}", [when]),
		group_line: (group) =>
			[count_rows(group.rows), count_warehouses(group.warehouses.length)].join(" · "),
		group_amount: (group) => money(frm, group.value),
		extra_rows: (posted) =>
			posted.difference
				? [
						[
							make_title_cell(
								__("Stock difference"),
								[
									posted.difference.posted_on &&
										__("Posted {0}", [
											frappe.datetime
												.str_to_user(posted.difference.posted_on)
												.split(" ")[0],
										]),
									[
										...new Set(
											Object.keys(posted.difference.lines).map(
												(account) => short_account(frm, account) || __("Stock")
											)
										),
									].join(", "),
								]
									.filter(Boolean)
									.join(" · "),
								"text-ink-gray-8"
							),
							make_text(money(frm, posted.difference.amount), "text-ink-gray-8 shrink-0"),
						],
				  ]
				: [],
		undone: (posted) =>
			posted.undone.map((group) => ({
				source: group.source,
				line: __("{0} · taken out of your books", [count_rows(group.rows)]),
				download: () =>
					download_sheet(frm.doc.company, "opening_stock.download_opening_stock", {
						undone_from: group.source,
					}),
			})),
		fields: [
			{ fieldname: "warehouse", fieldtype: "Data", label: __("Warehouse"), columns: 4 },
			{ fieldname: "rows", fieldtype: "Int", label: __("Rows"), columns: 2 },
			{ fieldname: "value", fieldtype: "Currency", label: __("Value"), columns: 2 },
			{ fieldname: "later_entries", fieldtype: "Int", label: __("Later stock entries"), columns: 2 },
		],
		rows: (group) => group.warehouses.map((row) => ({ ...row })),
		blocked: (row) => row.later_entries > 0,
		blocked_alert: (rows, group, dialog) => ({
			title:
				rows.length === 1
					? __("{0} can not be undone.", [rows[0].warehouse])
					: __("{0} warehouses can not be undone.", [rows.length]),
			description: __("{0} stock entries came after {1}. Correct the wrong rows instead.", [
				rows.reduce((sum, row) => sum + row.later_entries, 0),
				day_before_text(frm),
			]),
			buttons: [{ label: __("Correct rows"), onclick: () => show_correct_rows(frm, group, dialog) }],
		}),
		group_actions: (group, dialog) =>
			frm.doc.status === "Live"
				? []
				: [
						frappe.ui.button({
							label: __("Correct rows"),
							variant: "ghost",
							onclick: () => show_correct_rows(frm, group, dialog),
						}),
				  ],
		undo_label: (selected, group) =>
			selected.length
				? __("Undo {0} · {1}", [
						count_warehouses(selected.length),
						money(
							frm,
							selected.reduce((sum, row) => sum + flt(row.value), 0)
						),
				  ])
				: __("Undo this upload · {0}", [money(frm, group.value)]),
		confirm_title: (rows, selected) =>
			selected ? __("Undo {0}?", [count_warehouses(rows.length)]) : __("Undo this upload?"),
		confirm_text: () => __("You can download the rows, fix them and upload again."),
		// "these": a warehouse's stock entries are many
		confirm_what: (rows, selected) => ({
			count: 2,
			kind: "stock",
			names: selected ? selected.map((row) => row.warehouse) : null,
			what: __("stock entries of {0}", [count_warehouses(rows.length)]),
		}),
		confirm_rows: (rows) =>
			rows.map((row) => [
				$(`<span class="text-base text-ink-gray-8 grow truncate"></span>`).text(row.warehouse),
				$(`<span class="text-base text-ink-gray-5 shrink-0"></span>`).text(count_rows(row.rows)),
				$(`<span class="text-base text-ink-gray-8 shrink-0"></span>`).text(money(frm, row.value)),
			]),
		total: (rows) => rows.reduce((sum, row) => sum + flt(row.value), 0),
		confirm_note: (rows) => {
			const lines = new Set(Object.keys(difference?.lines || {}));
			return rows.some((row) => lines.has(line_of[row.warehouse]))
				? grey_line(
						__("The posted stock difference of {0} is undone too.", [
							money(frm, difference.amount),
						])
				  )
				: [];
		},
		confirm_button: (rows, selected) =>
			selected ? __("Undo {0}", [count_warehouses(rows.length)]) : __("Undo this upload"),
		undo: (group, selected, delete_entries) =>
			call_method(frm, "undo_opening_stock", {
				source: group.source,
				warehouses: selected ? selected.map((row) => row.warehouse) : null,
				delete_entries: delete_entries,
			}),
		after_undo: (result, group) => show_undo_result(frm, "stock", result, group.source),
	});
}

// Change the qty or value of posted rows: one correcting entry per warehouse, after the opening
function show_correct_rows(frm, group, posted_dialog) {
	posted_dialog.hide();
	const posted = (frm.doc.opening_stock || []).filter(
		(row) => row.status === "Posted" && (row.source || "") === group.source
	);
	const originals = Object.fromEntries(
		posted.map((row) => [row.name, { qty: row.qty, amount: row.amount }])
	);
	// a batch or serial column tells rows of one item apart
	const tracked = posted.some((row) => row.batch_no)
		? "batch_no"
		: posted.some((row) => row.serial_no)
		? "serial_no"
		: null;
	const win = new SheetWindow(frm, {
		title: __("Posted stock"),
		fields: [
			{
				fieldname: "item_name",
				fieldtype: "Data",
				label: __("Item"),
				in_list_view: 1,
				read_only: 1,
				columns: tracked ? 2 : 3,
			},
			{
				fieldname: "warehouse",
				fieldtype: "Data",
				label: __("Warehouse"),
				in_list_view: 1,
				read_only: 1,
				columns: 2,
			},
			{
				fieldname: "qty",
				fieldtype: "Float",
				label: __("Qty"),
				in_list_view: 1,
				columns: 1,
				onchange: keep_rate_on_qty,
			},
			{
				fieldname: "uom",
				fieldtype: "Data",
				label: __("Unit"),
				in_list_view: 1,
				read_only: 1,
				columns: 1,
			},
			{
				fieldname: "amount",
				fieldtype: "Currency",
				label: __("Value"),
				in_list_view: 1,
				columns: 2,
				onchange: set_rate_on_amount,
			},
			{
				fieldname: "batch_no",
				fieldtype: "Data",
				label: __("Batch"),
				read_only: 1,
				in_list_view: tracked === "batch_no" ? 1 : 0,
				columns: 1,
			},
			{
				fieldname: "serial_no",
				fieldtype: "Small Text",
				label: __("Serial No"),
				read_only: 1,
				in_list_view: tracked === "serial_no" ? 1 : 0,
				columns: 1,
			},
			{ fieldname: "name", fieldtype: "Data", hidden: 1 },
			{ fieldname: "item_code", fieldtype: "Data", hidden: 1 },
		],
	});
	win.grid.df.cannot_add_rows = 1;
	win.grid.df.cannot_delete_rows = 1;
	const changed = () =>
		win.get_rows().filter((row) => {
			const old = originals[row.name];
			return (
				Math.abs(flt(row.qty) - flt(old.qty)) > 0.0001 ||
				Math.abs(flt(row.amount) - flt(old.amount)) >= 0.01
			);
		});
	win.render = () => {
		win.dialog
			.get_field("head")
			.$wrapper.empty()
			.append(
				make_back_header(
					__("Correct rows"),
					__("{0} · Dated {1}, against Temporary Opening.", [
						source_name(group.source),
						day_before_text(frm),
					]),
					() => {
						win.dialog.hide();
						show_posted_stock(frm);
					}
				).addClass("mb-3")
			);
		win.refresh_footer();
	};
	win.refresh_footer = () => {
		if (win.view !== "sheet") return;
		const count = changed().length;
		win.dialog
			.get_field("notes")
			.$wrapper.empty()
			.append(
				grey_line(
					plural(
						win.get_rows().length,
						__("{0} changed of 1 row", [count]),
						__("{0} changed of {1} rows", [count, win.get_rows().length])
					)
				).addClass("mt-2 text-right")
			);
		win.dialog.set_secondary_action_label(__("Close"));
		win.dialog.set_secondary_action(() => win.dialog.hide());
		win.dialog.set_primary_action(
			count ? plural(count, __("Correct 1 row"), __("Correct {0} rows", [count])) : __("Correct rows"),
			() => check()
		);
		set_primary_disabled(win.dialog, !count || Boolean(win.refused));
	};
	win.changed = () => {
		win.refused = null;
		win.set_alerts([]);
		win.refresh_footer();
	};
	// what the server would refuse, said here before the confirm
	const check = () => {
		const rows = changed();
		return get_data(frm, "check_stock_correction", {
			rows: rows.map((row) => ({ name: row.name, qty: row.qty, amount: row.amount })),
		}).then((r) => {
			const result = r.message;
			if (!result.blocker) return confirm(result.later_entries);
			win.refused = result;
			const below = new Set(
				(result.below_zero || []).map((item) => `${item.item_code}::${item.warehouse}`)
			);
			const is_below = (row) => below.has(`${row.item_code}::${row.warehouse}`);
			win.set_alerts([
				{
					theme: "red",
					title: result.blocker,
					description: (result.below_zero || []).length
						? __("Put their quantities back, or ask your CA.")
						: undefined,
					rows: is_below,
				},
			]);
			win.refresh_footer();
		});
	};
	const confirm = (later_entries) => {
		const rows = changed();
		const net = rows.reduce((sum, row) => sum + flt(row.amount) - flt(originals[row.name].amount), 0);
		win.show_confirm({
			title: plural(rows.length, __("Correct 1 row?"), __("Correct {0} rows?", [rows.length])),
			back: __("Nothing changes until you confirm."),
			body: [
				make_kv_lines([
					[__("To correct"), [count_rows(rows.length), stock_change_text(frm, net)].join(" · ")],
					[__("Dated"), day_before_text(frm)],
				]),
				later_entries &&
					grey_line(
						__(
							"Cost of goods sold and profit since {0} change. ERPNext works out the new costs in the background, usually within the hour.",
							[frappe.datetime.str_to_user(frm.doc.migration_date)]
						)
					),
			].filter(Boolean),
			primary: plural(rows.length, __("Correct 1 row"), __("Correct {0} rows", [rows.length])),
			on_primary: () =>
				call_method(
					frm,
					"correct_opening_stock",
					{ rows: rows.map((row) => ({ name: row.name, qty: row.qty, amount: row.amount })) },
					{ freeze: true }
				).then((r) => {
					win.dialog.hide();
					after_run(
						frm,
						r.message,
						__("Correcting the rows in the background. This page updates when it's done."),
						(result) => show_stock_corrected(frm, result)
					);
				}),
		});
	};
	win.set_rows(
		posted.map((row) => ({
			name: row.name,
			item_name: row.item_name,
			item_code: row.item_code,
			warehouse: row.warehouse,
			qty: row.qty,
			uom: row.uom,
			amount: row.amount,
			old_qty: row.qty,
			old_amount: row.amount,
			batch_no: row.batch_no,
			serial_no: row.serial_no,
		}))
	);
	win.show_sheet();
	win.dialog.show();
}

// Stock at job workers: the job worker rows of Opening stock, read only
function show_job_worker_stock(frm) {
	const job_workers = new Set(
		(frm.doc.warehouses || [])
			.filter((row) => row.kind === "Job worker" && row.warehouse)
			.map((row) => row.warehouse)
	);
	const rows = (frm.doc.opening_stock || []).filter((row) => job_workers.has(row.warehouse));
	const soon = frappe.datetime.add_days(frappe.datetime.get_today(), 30);
	const due = rows.filter((row) => row.return_due && row.return_due <= soon);
	const currency = erpnext.get_currency(frm.doc.company);
	const dialog = new frappe.ui.Dialog({
		title: __("Stock at job workers"),
		size: "extra-large",
		fields: [{ fieldname: "body", fieldtype: "HTML" }],
		secondary_action_label: __("Close"),
		secondary_action: () => dialog.hide(),
	});
	const draw = (first) => {
		const sorted = first ? [...rows.filter(first), ...rows.filter((row) => !first(row))] : rows;
		dialog
			.get_field("body")
			.$wrapper.empty()
			.append(
				$(`<div class="flex flex-col gap-3"></div>`).append(
					$(`<div class="flex items-center justify-between gap-3"></div>`).append(
						$(`<div class="text-p-base text-ink-gray-7"></div>`).text(
							__("Stock at your job workers on {0}, from Opening stock.", [
								day_before_text(frm),
							])
						),
						frappe.ui.button({
							label: __("Open in Opening stock"),
							onclick: () => {
								dialog.hide();
								show_tab(frm, "stock_tab");
								show_opening_stock(frm);
							},
						})
					),
					due.length
						? make_alerts([
								{
									theme: "yellow",
									title: plural(
										due.length,
										__("1 row is due back within 30 days."),
										__("{0} rows are due back within 30 days.", [due.length])
									),
									buttons: [
										{
											label: __("Show rows"),
											onclick: () => draw((row) => due.includes(row)),
										},
									],
								},
						  ])
						: [],
					rows.length
						? make_read_table(
								[
									[__("Item")],
									[__("Job worker")],
									[__("Qty"), true],
									[__("Value"), true],
									[__("Challan no")],
									[__("Challan date")],
									[__("Return due")],
								],
								sorted.map((row) => [
									row.item_name,
									row.warehouse_name || row.warehouse,
									format_qty(row.qty),
									format_currency(row.amount, currency),
									row.challan_no || "",
									row.challan_date ? frappe.datetime.str_to_user(row.challan_date) : "",
									row.return_due ? frappe.datetime.str_to_user(row.return_due) : "",
								])
						  )
						: grey_line(__("No stock at job workers in Opening stock yet."))
				)
			);
	};
	draw();
	dialog.show();
}

// ---------- BOMs ----------

// where BOMs are made when they can't be imported
function bom_creator_button() {
	return { label: __("Open BOM Creator"), onclick: () => frappe.new_doc("BOM Creator") };
}

// The items made here that have no BOM; BOMs come in through Data Import
function show_boms(frm) {
	get_data(frm, "get_boms").then((r) => {
		const status = r.message;
		const blocked = import_blocked(frm, "BOM");
		if (!status.without_bom.length) {
			return blocked ? frappe.new_doc("BOM Creator") : open_party_import(frm, "BOM");
		}
		const dialog = new frappe.ui.Dialog({
			title: __("BOMs"),
			size: "large",
			fields: [{ fieldname: "body", fieldtype: "HTML" }],
			primary_action_label: blocked ? __("Open BOM Creator") : __("Import BOMs"),
			primary_action: () => {
				dialog.hide();
				blocked ? frappe.new_doc("BOM Creator") : open_party_import(frm, "BOM");
			},
			secondary_action_label: __("Close"),
			secondary_action: () => dialog.hide(),
		});
		dialog.get_field("body").$wrapper.append(
			$(`<div class="flex flex-col gap-3"></div>`).append(
				grey_line(
					plural(
						status.without_bom.length,
						__("1 item you make has no BOM. Add it before its first work order."),
						__("{0} items you make have no BOM. Add them before their first work order.", [
							status.without_bom.length,
						])
					)
				),
				make_read_table(
					[[__("Item")], [__("Item group")], [__("Unit")]],
					status.without_bom.map((item) => [
						item.item_name || item.name,
						item.item_group,
						item.stock_uom,
					])
				)
			)
		);
		dialog.show();
	});
}

// ---------- Open orders ----------

const ORDER_TEXT = {
	"Sales Order": {
		title: __("Open sales orders"),
		intro: __(
			"Sales orders open on {0}. Enter only the qty not yet delivered; do not add advances again."
		),
		party: __("Customer"),
		note: __("Advances are not added again."),
	},
	"Purchase Order": {
		title: __("Open purchase orders"),
		intro: __(
			"Purchase orders open on {0}. Enter only the qty not yet received; do not add advances again."
		),
		party: __("Supplier"),
		note: __("Advances are not added again."),
	},
	"Work Order": {
		title: __("Open work orders"),
		intro: __("Work orders running on {0}. Enter the qty still to make."),
		note: __(
			"A job with its material in the work in progress warehouse makes from there. The others transfer material first, as usual."
		),
	},
	"Subcontracting Order": {
		title: __("Open subcontracting orders"),
		intro: __("Orders with goods still at the job worker on {0}."),
		party: __("Job worker"),
		note: __("The material is already at the job worker from Opening stock, so nothing is sent again."),
	},
};

function format_quantities(quantities) {
	return quantities.map(([qty, unit]) => [format_qty(qty), unit].filter(Boolean).join(" ")).join(", ");
}

function count_orders(order_type, count) {
	return order_type === "Work Order"
		? plural(count, __("1 work order"), __("{0} work orders", [count]))
		: plural(count, __("1 order"), __("{0} orders", [count]));
}

function show_open_orders(frm, order_type, result, replacing) {
	get_data(frm, "get_open_orders", { order_type: order_type }).then((r) =>
		new OpenOrdersWindow(frm, order_type, r.message, result).show(replacing)
	);
}

// Per item: its service item, and a copy of its BOM that leaves out what is added in-house
function show_job_work_setup(frm, items, after) {
	get_data(frm, "get_job_work_setups", { items: items }, { freeze: true }).then((r) => {
		const setups = r.message;
		const ready = setups.filter((setup) => setup.bom);
		const fields = setups.flatMap((setup, i) => [
			{
				fieldtype: "Section Break",
				label: frappe.utils.escape_html(
					[
						setup.item_name && setup.item_name !== setup.item_code
							? `${setup.item_code}: ${setup.item_name}`
							: setup.item_code,
						...setup.job_workers,
					].join(" · ")
				),
			},
			...(setup.bom ? job_work_fields(setup, i) : [{ fieldname: `no_bom_${i}`, fieldtype: "HTML" }]),
		]);
		const dialog = new frappe.ui.Dialog({
			title: __("Set up job work"),
			size: "large",
			fields: fields,
			primary_action_label: plural(
				ready.length,
				__("Save 1 job-work setup"),
				__("Save {0} job-work setups", [ready.length])
			),
			primary_action: (values) =>
				call_method(
					frm,
					"set_job_work_setups",
					{
						setups: setups
							.map((setup, i) => ({ setup, i }))
							.filter(({ setup }) => setup.bom)
							.map(({ setup, i }) => ({
								item_code: setup.item_code,
								materials: dialog.fields_dict[`materials_${i}`].grid
									.get_selected_children()
									.map((row) => row.item_code),
								service_item: values[`service_item_${i}`],
								finished_good_qty: values[`finished_good_qty_${i}`],
							})),
					},
					{ freeze: true }
				).then(() => {
					dialog.hide();
					after();
				}),
		});
		setups.forEach((setup, i) => {
			if (!setup.bom) {
				dialog
					.get_field(`no_bom_${i}`)
					.$wrapper.append(grey_line(__("This item has no default BOM. Add its BOM first.")));
			}
		});
		set_primary_disabled(dialog, !ready.length);
		dialog.show();
	});
}

function job_work_fields(setup, i) {
	return [
		{
			fieldname: `service_item_${i}`,
			fieldtype: "Link",
			options: "Item",
			label: __("Service item"),
			reqd: 1,
			default: setup.service_item,
			get_query: () => ({ filters: { is_stock_item: 0, is_fixed_asset: 0, disabled: 0 } }),
			filter_description: __("Only services are listed: no stock or asset items."),
		},
		{ fieldtype: "Column Break" },
		{
			fieldname: `finished_good_qty_${i}`,
			fieldtype: "Float",
			label: __("Finished goods per service unit"),
			reqd: 1,
			non_negative: 1,
			default: setup.finished_good_qty,
		},
		{ fieldtype: "Section Break" },
		{
			fieldname: `materials_${i}`,
			fieldtype: "Table",
			label: __("Materials sent to the job worker"),
			cannot_add_rows: 1,
			cannot_delete_rows: 1,
			data: setup.materials.map((row) => ({
				...row,
				__checked: row.held ? 1 : 0,
				material:
					row.item_name && row.item_name !== row.item_code
						? `${row.item_code}: ${row.item_name}`
						: row.item_code,
				note: row.held ? "" : __("added by you"),
			})),
			fields: [
				{
					fieldname: "material",
					fieldtype: "Data",
					label: __("Material"),
					in_list_view: 1,
					read_only: 1,
					columns: 5,
				},
				{
					fieldname: "qty",
					fieldtype: "Float",
					label: __("Qty"),
					in_list_view: 1,
					read_only: 1,
					columns: 2,
				},
				{
					fieldname: "uom",
					fieldtype: "Data",
					label: __("Unit"),
					in_list_view: 1,
					read_only: 1,
					columns: 1,
				},
				{
					fieldname: "note",
					fieldtype: "Data",
					label: __("Note"),
					in_list_view: 1,
					read_only: 1,
					columns: 2,
				},
				{ fieldname: "item_code", fieldtype: "Data", hidden: 1 },
			],
		},
	];
}

class OpenOrdersWindow extends SheetWindow {
	constructor(frm, order_type, checks, result) {
		const text = ORDER_TEXT[order_type];
		const warehouse = (fieldname, label, columns) =>
			list_column(fieldname, "Link", label, columns, {
				options: "Warehouse",
				get_query: () => ({ filters: { company: frm.doc.company, is_group: 0 } }),
			});
		const fields = {
			"Work Order": [
				list_column("order_no", "Data", __("Order no"), 1),
				list_column("item_name", "Data", __("Item to make"), 2),
				list_column("bom", "Link", __("BOM"), 2, { options: "BOM" }),
				list_column("qty", "Float", __("Qty left"), 1),
				list_column("uom", "Data", __("Unit"), 1),
				warehouse("wip_warehouse", __("WIP warehouse"), 1),
				warehouse("warehouse", __("Finished goods to"), 2),
			],
			"Subcontracting Order": [
				list_column("order_no", "Data", __("Purchase order"), 1),
				list_column("party_name", "Data", __("Job worker"), 2),
				list_column("item_name", "Data", __("Item to receive"), 2),
				list_column("qty", "Float", __("Qty left"), 1),
				list_column("uom", "Data", __("Unit"), 1),
				list_column("rate", "Currency", __("Job work rate"), 1),
				warehouse("warehouse", __("Receive into"), 2),
				{ fieldname: "order_date", fieldtype: "Date", label: __("Order date") },
				{ fieldname: "delivery_date", fieldtype: "Date", label: __("Required by") },
				{ fieldname: "service_item", fieldtype: "Link", options: "Item", label: __("Service item") },
				{ fieldname: "bom", fieldtype: "Link", options: "BOM", label: __("BOM") },
				{ fieldname: "material", fieldtype: "Data", label: __("Material at the job worker") },
			],
		}[order_type] || [
			list_column("order_no", "Data", __("Order no"), 1),
			list_column("party_name", "Data", text.party, 2),
			list_column("item_name", "Data", __("Item"), 2),
			list_column("qty", "Float", __("Qty left"), 1),
			list_column("uom", "Data", __("Unit"), 1),
			list_column("rate", "Currency", __("Rate"), 1),
			list_column(
				"delivery_date",
				"Date",
				order_type === "Sales Order" ? __("Delivery date") : __("Required by"),
				1
			),
			warehouse("warehouse", order_type === "Sales Order" ? __("Deliver from") : __("Receive into"), 1),
			{ fieldname: "order_date", fieldtype: "Date", label: __("Order date") },
		];
		const where = {
			"Sales Order": "open_orders",
			"Purchase Order": "open_orders",
			"Subcontracting Order": "job_work_orders",
		}[order_type];
		super(frm, {
			title: text.title,
			where: where,
			has_data: order_rows(frm, order_type, false).length,
			open: (result, replacing) => show_open_orders(frm, order_type, result, replacing),
			save_method: "save_open_orders",
			get_method: "get_open_orders",
			args: { order_type: order_type },
			fields: [
				...fields,
				{ fieldname: "source", fieldtype: "Data", hidden: 1 },
				{ fieldname: "doc_idx", fieldtype: "Int", hidden: 1 },
			],
		});
		this.order_type = order_type;
		this.text = text;
		this.result = result;
		this.fieldnames = [...fields.map((df) => df.fieldname), "source"];
		this.load(checks);
	}

	load(checks) {
		this.checks = checks;
		this.drafts = order_rows(this.frm, this.order_type, false).map((row) => ({
			...Object.fromEntries(this.fieldnames.map((field) => [field, row[field]])),
			doc_idx: row.idx,
		}));
	}

	render() {
		const { checks, frm, order_type } = this;
		this.make_head(this.text.intro.replace("{0}", this.day), {
			upload: () => this.upload("import_open_orders", null),
			menu: [
				checks.made && {
					label: made_orders_title(order_type),
					icon: "list",
					onclick: () => {
						this.dialog.hide();
						show_made_orders(frm, order_type);
					},
				},
				{
					label: __("Download sheet"),
					icon: "download",
					onclick: () =>
						download_sheet(frm.doc.company, "open_orders.download_open_orders", {
							order_type: order_type,
						}),
				},
			].filter(Boolean),
		});
		this.set_stats(
			checks.drafts
				? [
						frappe.ui.stat_card({
							label: __("On your sheet"),
							value: count_orders(order_type, checks.orders),
							caption: __("{0} left", [format_quantities(checks.quantities)]),
						}),
						frappe.ui.stat_card({
							label: __("Made"),
							value: checks.made ? count_orders(order_type, checks.made) : __("Nothing yet"),
						}),
				  ]
				: []
		);
		const go = (label, open) => ({
			label,
			onclick: () => {
				this.dialog.hide();
				open();
			},
		});
		const customer = order_type === "Sales Order";
		const actions = {
			missing_parties: [
				go(customer ? __("Go to Customers") : __("Go to Suppliers"), () =>
					open_party_import(frm, customer ? "Customer" : "Supplier")
				),
			],
			missing_items: [go(__("Go to Items"), () => open_party_import(frm, "Item"))],
			no_bom: [
				import_blocked(frm, "BOM")
					? go(__("Open BOM Creator"), () => frappe.new_doc("BOM Creator"))
					: go(__("Import BOMs"), () => open_party_import(frm, "BOM")),
			],
			no_job_worker: [
				go(__("Go to Warehouses"), () =>
					show_warehouses(frm, {
						job_workers: true,
						after: () => show_open_orders(frm, order_type),
					})
				),
			],
			material_not_posted: [
				go(__("Go to Opening stock"), () => {
					show_tab(frm, "stock_tab");
					show_opening_stock(frm);
				}),
			],
			not_subcontracted: [
				{
					label: __("Mark them subcontracted"),
					onclick: () =>
						(this.dirty ? this.save() : Promise.resolve())
							.then(() => call_method(frm, "mark_items_subcontracted", {}, { freeze: true }))
							.then(() => this.refresh()),
				},
			],
		};
		// the names the rows carry, for reds about parties or items
		const field = { missing_parties: "party_name", missing_items: "item_name", no_bom: "item_name" };
		const names_of = (key, idxs) => {
			if (!field[key]) return undefined;
			const set = new Set(idxs);
			const names = [
				...new Set(
					this.get_rows()
						.filter((row) => set.has(row.doc_idx))
						.map((row) => row[field[key]])
						.filter(Boolean)
				),
			];
			return names.slice(0, 5).join(", ") + (names.length > 5 ? ", …" : "");
		};
		// the cells a red is about, marked in its rows
		const fields = {
			no_party_name: ["party_name"],
			missing_parties: ["party_name"],
			missing_items: ["item_name"],
			no_qty: ["qty"],
			unknown_units: ["uom"],
			no_bom: ["bom"],
			no_wip: ["wip_warehouse"],
			no_warehouse: ["warehouse"],
		};
		const alerts = (checks.reds || []).map((item) => ({
			theme: "red",
			key: item.key,
			title: item.text,
			description: names_of(item.key, item.rows),
			rows: item.rows,
			fields: fields[item.key],
			badge: ["missing_parties", "missing_items"].includes(item.key) ? __("Not in ERPNext") : undefined,
			buttons: [
				...(actions[item.key] || []),
				...(item.fix ? [fix_button(item.fix, frm, () => this.refresh())] : []),
				...(["job_work_setup", "material_not_at_job_worker"].includes(item.key)
					? [
							{
								label: __("Set up job work"),
								onclick: () =>
									(this.dirty ? this.save() : Promise.resolve()).then(() =>
										show_job_work_setup(frm, item.items, () => this.refresh())
									),
							},
					  ]
					: []),
			],
		}));
		this.set_alerts(alerts, [
			this.result &&
				plural(
					this.result.added,
					__("1 row read from the sheet."),
					__("{0} rows read from the sheet.", [this.result.added])
				),
			this.result?.skipped &&
				plural(
					this.result.skipped,
					__("1 row is already made and is left out."),
					__("{0} rows are already made and are left out.", [this.result.skipped])
				),
			this.result?.replaced &&
				plural(
					this.result.replaced,
					__("1 draft row with the same order number was replaced."),
					__("{0} draft rows with the same order number were replaced.", [this.result.replaced])
				),
			order_type === "Subcontracting Order" && __("Receive goods back with a Subcontracting Receipt."),
		]);
		this.refresh_footer();
	}

	refresh_footer() {
		if (this.view !== "sheet") return;
		const { checks } = this;
		this.dialog.set_primary_action(order_type_make_label(this.order_type, checks.orders), () =>
			this.post()
		);
		this.dialog.set_secondary_action_label(__("Save draft"));
		this.dialog.set_secondary_action(() => this.save_draft());
		const empty = !this.get_rows().length;
		set_primary_disabled(this.dialog, empty || (!this.dirty && Boolean(checks.blocker)));
	}

	confirm_post() {
		const { frm, order_type } = this;
		this.show_confirm({
			title: __("{0}?", [order_type_make_label(order_type, this.checks.orders)]),
			back: __("Nothing is made until you confirm."),
			body: [
				make_kv_lines([
					[
						__("To make"),
						__("{0} · {1} left", [
							count_orders(order_type, this.checks.orders),
							format_quantities(this.checks.quantities),
						]),
					],
				]),
				grey_line(__("No money moves: orders post nothing to your books.")),
				grey_line(this.text.note),
				this.checks.unnumbered &&
					grey_line(
						plural(
							this.checks.unnumbered,
							order_type === "Sales Order"
								? __("1 row has no order number: it becomes an order of its own customer.")
								: __("1 row has no order number: it becomes an order of its own supplier."),
							order_type === "Sales Order"
								? __("{0} rows have no order number: they become one order per customer.", [
										this.checks.unnumbered,
								  ])
								: __("{0} rows have no order number: they become one order per supplier.", [
										this.checks.unnumbered,
								  ])
						)
					),
			].filter(Boolean),
			primary: order_type_make_label(order_type, this.checks.orders),
			on_primary: () =>
				call_method(frm, "make_open_orders", { order_type: order_type }, { freeze: true }).then(
					(r) => {
						this.dialog.hide();
						after_run(
							frm,
							r.message,
							__("Making the orders in the background. This page updates when it's done."),
							(result) =>
								frappe.ui.toast({
									type: "success",
									message: __("{0} made.", [count_orders(order_type, result.length)]),
									action: {
										label: __("View"),
										onclick: () => show_made_orders(frm, order_type),
									},
								})
						);
					}
				),
		});
	}
}

function order_type_make_label(order_type, count) {
	if (!count) {
		return order_type === "Work Order"
			? __("Make work orders")
			: order_type === "Subcontracting Order"
			? __("Make subcontracting orders")
			: __("Make orders");
	}
	return order_type === "Work Order"
		? plural(count, __("Make 1 work order"), __("Make {0} work orders", [count]))
		: order_type === "Subcontracting Order"
		? plural(count, __("Make 1 subcontracting order"), __("Make {0} subcontracting orders", [count]))
		: plural(count, __("Make 1 order"), __("Make {0} orders", [count]));
}

function show_made_orders(frm, order_type) {
	const party = ORDER_TEXT[order_type].party;
	const unique = (rows) => [...new Set(rows.map((row) => row.document))];
	show_posted_window(frm, {
		title: made_orders_title(order_type),
		load: () => get_data(frm, "get_made_orders", { order_type: order_type }).then((r) => r.message),
		posted_word: (when) => __("Made {0}", [when]),
		group_line: (group) => count_orders(order_type, group.orders),
		undone: (made) =>
			(made.undone || []).map((group) => ({
				source: group.source,
				line: __("{0} · removed from ERPNext", [count_rows(group.count)]),
				download: () =>
					download_sheet(frm.doc.company, "open_orders.download_open_orders", {
						order_type: order_type,
						undone_from: group.source,
					}),
			})),
		fields: [
			{
				fieldname: "document",
				fieldtype: "Link",
				options: order_type,
				label: __(order_type),
				columns: 2,
			},
			{
				fieldname: "order_no",
				fieldtype: "Data",
				label: __("Order no"),
				columns: party ? 1 : 2,
			},
			...(party ? [{ fieldname: "party_name", fieldtype: "Data", label: party, columns: 2 }] : []),
			{
				fieldname: "item_code",
				fieldtype: "Data",
				label: order_type === "Work Order" ? __("Item to make") : __("Item"),
				columns: party ? 2 : 3,
			},
			{ fieldname: "qty", fieldtype: "Float", label: __("Qty left"), columns: 1 },
			{ fieldname: "uom", fieldtype: "Data", label: __("Unit"), columns: 1 },
			{
				fieldname: "done_since",
				fieldtype: "Float",
				label: order_type === "Work Order" ? __("Produced since") : __("Done since"),
				columns: 1,
			},
		],
		rows: (group) => group.rows.map((row) => ({ ...row })),
		blocked: (row) => flt(row.done_since) > 0,
		blocked_alert: (rows) => ({
			title: __("{0} already have deliveries, receipts or production.", [
				count_orders(order_type, unique(rows).length),
			]),
			description: __("Cancel those first."),
		}),
		undo_label: (selected) =>
			selected.length ? __("Undo {0} selected", [unique(selected).length]) : __("Undo this upload"),
		confirm_title: (rows) => __("Undo {0}?", [count_orders(order_type, unique(rows).length)]),
		confirm_text: (rows) =>
			plural(
				unique(rows).length,
				__("Its rows are kept, so you can fix it and make it again."),
				__("Their rows are kept, so you can fix them and make them again.")
			),
		confirm_what: (rows) => ({
			count: unique(rows).length,
			what: count_orders(order_type, unique(rows).length),
			one: __(order_type),
			has_ledger: false,
		}),
		confirm_rows: (rows) =>
			rows.map((row) => [
				$(`<span class="text-base text-ink-gray-8 shrink-0"></span>`).text(row.document),
				$(`<span class="text-base text-ink-gray-5 grow truncate"></span>`).text(
					[row.party_name, row.item_code].filter(Boolean).join(" · ")
				),
				$(`<span class="text-base text-ink-gray-8 shrink-0"></span>`).text(
					[format_qty(row.qty), row.uom].filter(Boolean).join(" ")
				),
			]),
		confirm_button: (rows) => __("Undo {0}", [count_orders(order_type, unique(rows).length)]),
		undo: (group, selected) =>
			call_method(frm, "undo_open_orders", {
				order_type: order_type,
				source: group.source,
				documents: selected ? unique(selected) : null,
			}),
		after_undo: (result) =>
			frappe.ui.toast({
				type: "success",
				message: __("{0} undone", [count_orders(order_type, result.removed)]),
				description: [__("They're cancelled and deleted in ERPNext."), kept_cancelled_line(result)]
					.filter(Boolean)
					.join(" "),
			}),
	});
}

// ---------- Fixed assets ----------

const ASSET_FIELDS = [
	"asset_name",
	"asset_account_name",
	"asset_account",
	"purchase_date",
	"gross",
	"opening_depreciation",
	"hand_booked",
	"qty",
	"salvage",
	"method",
	"life",
	"rate",
	"source",
];

// result: what an upload just did, said at the top of the window
function show_fixed_assets(frm, result, replacing) {
	get_data(frm, "get_fixed_assets").then((r) =>
		new FixedAssetsWindow(frm, r.message, result).show(replacing)
	);
}

class FixedAssetsWindow extends SheetWindow {
	constructor(frm, checks, result) {
		const drafts = (frm.doc.fixed_assets || []).filter((row) => row.status !== "Posted");
		super(frm, {
			title: __("Fixed assets"),
			open: (result, replacing) => show_fixed_assets(frm, result, replacing),
			save_method: "save_fixed_assets",
			get_method: "get_fixed_assets",
			where: "fixed_assets",
			has_data: drafts.length,
			row_form: true,
			fields: [
				list_column("asset_name", "Data", __("Asset name"), 2),
				list_column("asset_account", "Link", __("Asset account"), 2, {
					options: "Account",
					get_query: () => ({
						filters: { company: frm.doc.company, root_type: "Asset", is_group: 0 },
					}),
				}),
				list_column("purchase_date", "Date", __("Purchase date"), 1),
				list_column("gross", "Currency", __("Cost"), 2),
				list_column("opening_depreciation", "Currency", __("Depreciation till date"), 2),
				{ fieldname: "asset_account_name", fieldtype: "Data", label: __("Account on sheet") },
				{ fieldname: "qty", fieldtype: "Int", label: __("Qty") },
				{ fieldname: "salvage", fieldtype: "Currency", label: __("Salvage value") },
				{
					fieldname: "method",
					fieldtype: "Select",
					options: ["", "Straight Line", "Written Down Value"],
					label: __("Method"),
				},
				list_column("life", "Int", __("Life (years)"), 1),
				{ fieldname: "rate", fieldtype: "Float", label: __("Rate %") },
				{ fieldname: "hand_booked", fieldtype: "Currency", label: __("Booked by hand since") },
				{ fieldname: "source", fieldtype: "Data", hidden: 1 },
				{ fieldname: "doc_idx", fieldtype: "Int", hidden: 1 },
			],
			extra_fields: [
				{
					fieldname: "depreciation_booked",
					fieldtype: "Select",
					options: [
						{ value: "", label: "" },
						{ value: "Every month", label: __("Every month") },
						{ value: "Only at year end", label: __("Only at year end") },
					],
					hidden: 1,
				},
				{ fieldname: "categories_head", fieldtype: "HTML", hidden: 1 },
				{
					fieldname: "categories",
					fieldtype: "Table",
					hidden: 1,
					cannot_add_rows: 1,
					cannot_delete_rows: 1,
					in_place_edit: 1,
					data: [],
					fields: [
						list_column("asset_category", "Data", __("Category"), 2),
						list_column("method", "Select", __("Method"), 2, {
							options: ["", "Straight Line", "Written Down Value"],
							reqd: 1,
						}),
						list_column("life", "Int", __("Life (years)"), 1),
						list_column("rate", "Float", __("Rate %"), 1),
						list_column("accumulated_account", "Link", __("Accumulated depreciation"), 2, {
							options: "Account",
							read_only_depends_on: "eval:doc.net_value",
							get_query: () => ({
								filters: {
									company: frm.doc.company,
									root_type: "Asset",
									account_type: ["in", ["Accumulated Depreciation", ""]],
									is_group: 0,
								},
							}),
						}),
						list_column("depreciation_account", "Link", __("Depreciation expense"), 2, {
							options: "Account",
							get_query: () => ({
								filters: {
									company: frm.doc.company,
									root_type: "Expense",
									account_type: ["in", ["Depreciation", ""]],
									is_group: 0,
								},
							}),
						}),
						checks.hsn_label && list_column("hsn_code", "Data", checks.hsn_label, 1),
						{ fieldname: "asset_account", fieldtype: "Data", hidden: 1 },
						{ fieldname: "net_value", fieldtype: "Check", hidden: 1 },
					].filter(Boolean),
				},
			],
		});
		this.views.sheet.push("depreciation_booked");
		this.views.categories = ["categories_head", "categories"];
		this.result = result;
		this.load(checks);
	}

	load(checks) {
		this.checks = checks;
		this.drafts = (this.frm.doc.fixed_assets || [])
			.filter((row) => row.status !== "Posted")
			.map((row) => ({
				...Object.fromEntries(ASSET_FIELDS.map((field) => [field, row[field]])),
				doc_idx: row.idx,
			}));
	}

	render() {
		const { checks, frm } = this;
		const sheet = checks.sheet;
		const books = checks.books;
		this.make_head(__("One row per asset, as on {0}, with depreciation till that day.", [this.day]), {
			upload: () =>
				this.upload(
					"import_fixed_assets",
					this.get_rows().length &&
						__(
							"The sheet replaces draft rows of the same asset name, and its own if it was uploaded before. Posted assets stay. Continue?"
						)
				),
			menu: [
				(has_rows(frm.doc.fixed_assets, true) || (frm.doc.undone_assets || []).length) && {
					label: __("Posted assets"),
					icon: "list",
					onclick: () => {
						this.dialog.hide();
						show_posted_assets(frm);
					},
				},
				{
					label: __("Download sheet"),
					icon: "download",
					onclick: () => download_sheet(frm.doc.company, "fixed_assets.download_fixed_assets"),
				},
				...[...new Set((frm.doc.undone_assets || []).map((row) => row.source || ""))].map(
					(source) => ({
						label: __("Undone rows back to drafts: {0}", [source_name(source)]),
						icon: "undo-2",
						onclick: () =>
							call_method(frm, "restore_undone_assets", { source }).then((r) =>
								this.refresh(r.message)
							),
					})
				),
				sheet.categories && {
					label: __("Depreciation by category"),
					icon: "settings",
					onclick: () => this.show_categories(),
				},
			].filter(Boolean),
		});
		const net = books.fixed_assets - books.depreciation;
		const hand = checks.hand;
		this.set_stats(
			sheet.assets
				? [
						make_lines_card(
							__("On your sheet"),
							[
								[__("{0} · cost", [count_assets(sheet.assets)]), money(this.frm, sheet.cost)],
								[__("Depreciation till date"), money(this.frm, sheet.depreciation)],
							],
							[
								plural(
									sheet.categories,
									__("1 category"),
									__("{0} categories", [sheet.categories])
								),
								__("on {0}", [this.day]),
							].join(" · ")
						),
						make_lines_card(
							__("Your books today"),
							[
								[__("Fixed assets"), money(this.frm, books.fixed_assets)],
								[__("Depreciation"), money(this.frm, books.depreciation)],
							],
							[
								__("Net {0}", [money(this.frm, net, 0)]),
								hand.amount &&
									(hand.amount - hand.counted >= 1
										? __("{0} booked by hand since {1} still to count", [
												money(this.frm, hand.amount - hand.counted, 0),
												frappe.datetime.str_to_user(frm.doc.migration_date),
										  ])
										: __("{0} booked by hand since {1}, counted on the rows", [
												money(this.frm, hand.amount, 0),
												frappe.datetime.str_to_user(frm.doc.migration_date),
										  ])),
							]
								.filter(Boolean)
								.join(" · ")
						),
				  ]
				: []
		);
		this.render_question();
		this.set_alerts(this.get_alerts(), this.get_greys());
		this.refresh_footer();
	}

	// a migration date in the middle of the year: how the old books booked this year's depreciation
	render_question() {
		const { checks, frm } = this;
		const ask = checks.mid_year && this.view === "sheet";
		const field = this.dialog.get_field("depreciation_booked");
		this.dialog.set_df_property("depreciation_booked", "hidden", ask ? 0 : 1);
		if (!ask) return;
		const months = `${moment(checks.year_start).format("MMM")}–${moment(checks.day_before).format(
			"MMM YYYY"
		)}`;
		field.df.label = __("Did your old books book depreciation for {0}?", [months]);
		field.refresh();
		field.set_input(frm.doc.depreciation_booked || "");
		field.df.change = () => {
			const value = field.get_value();
			if ((value || "") === (frm.doc.depreciation_booked || "")) return;
			this.save(undefined, { depreciation_booked: value }).then(() => this.refresh());
		};
	}

	get_alerts() {
		const { checks, frm } = this;
		const reload = () => this.refresh();
		const set_up = [{ label: __("Set up"), onclick: () => this.show_categories() }];
		// reds about accounts or categories name them; the others are about rows
		const actions = {
			posting: checks.posting_fix ? [fix_button(checks.posting_fix, frm, () => this.refresh())] : [],
			untyped: [
				{
					label: get_mark_label(Object.values(checks.untyped_types || {})),
					onclick: () => call_method(frm, "mark_asset_accounts").then(reload),
				},
			],
			categories: set_up,
			hsn_code: set_up,
			bad_hsn: set_up,
			wrong_type: set_up,
			accounts: set_up,
			hand: [
				{
					label: __("Spread by schedule"),
					onclick: () => call_method(frm, "spread_hand_booked", {}, { freeze: true }).then(reload),
				},
				{
					label: __("Download sheet"),
					variant: "outline",
					onclick: () => download_sheet(frm.doc.company, "fixed_assets.download_fixed_assets"),
				},
			],
		};
		const named = { untyped: true, wrong_type: true, hsn_code: true, accounts: true };
		// one red at a time, in the order to fix them
		const reds = (checks.reds || []).filter((item) => !["empty", "answer"].includes(item.key));
		const more =
			reds.length > 1 &&
			plural(
				reds.length - 1,
				__("1 more to fix after this."),
				__("{0} more to fix after this.", [reds.length - 1])
			);
		// the cells a red is about, marked in its rows
		const fields = {
			no_account: ["asset_account"],
			no_date: ["purchase_date"],
			after_migration: ["purchase_date"],
			over_cost: ["opening_depreciation"],
			life_over: ["life"],
		};
		const alerts = reds.slice(0, 1).map((item) => ({
			theme: "red",
			key: item.key,
			title: item.text,
			description:
				[named[item.key] && item.rows.map((name) => short_account(frm, name)).join(", "), more]
					.filter(Boolean)
					.join(" · ") || undefined,
			buttons: actions[item.key] || [],
			rows: !named[item.key] && item.rows,
			fields: fields[item.key],
		}));
		alerts.unshift(...get_hand_after_alerts(frm), ...get_upload_alerts(this.result));
		const yellows = checks.yellows || {};
		const yellow = (title, description, extra = {}) =>
			alerts.push({ theme: "yellow", title, description, ...extra });
		for (const [account, amount] of Object.entries(checks.off || {})) {
			yellow(
				__("{0} is off by {1} from its line.", [
					short_account(frm, account),
					money(this.frm, Math.abs(amount), 0),
				]),
				__("Check the cost on the sheet, or the line in your trial balance.")
			);
		}
		for (const [account, amount] of Object.entries(checks.depreciation_off || {})) {
			yellow(
				__("{0} is off by {1} from the depreciation on the sheet.", [
					short_account(frm, account),
					money(this.frm, Math.abs(amount), 0),
				]),
				__("Check the depreciation till date on the sheet, or the line in your trial balance.")
			);
		}
		if (yellows.no_depreciation?.length) {
			yellow(
				plural(
					yellows.no_depreciation.length,
					__("1 asset bought before this year has no depreciation till date."),
					__("{0} assets bought before this year have no depreciation till date.", [
						yellows.no_depreciation.length,
					])
				),
				__("Check the sheet: ERPNext books their depreciation from the start."),
				{ rows: yellows.no_depreciation, fields: ["opening_depreciation"] }
			);
		}
		if (yellows.ending?.length) {
			yellow(
				plural(
					yellows.ending.length,
					__("1 asset reaches the end of its life with the first depreciation: {0}.", [
						money(this.frm, yellows.ending_amount),
					]),
					__("{0} assets reach the end of their life with the first depreciation: {1}.", [
						yellows.ending.length,
						money(this.frm, yellows.ending_amount),
					])
				),
				undefined,
				{ rows: yellows.ending }
			);
		}
		if (yellows.year_end_with_hand) {
			yellow(
				__(
					"Your old books book depreciation only at year end, but some was booked by hand this year."
				),
				__("Check the answer to the question above.")
			);
		}
		if (yellows.defaults?.length) {
			yellow(
				__("The company has no default {0} account.", [yellows.defaults.join(", ")]),
				__("Set it in the Company before you buy or scrap an asset.")
			);
		}
		return alerts;
	}

	// what the grey lines under the messages say
	get_greys() {
		const greys = this.checks.greys || {};
		return [
			greys.life_from_rate &&
				plural(
					greys.life_from_rate,
					__("1 Written Down Value asset has no life: it runs until its value is small."),
					__("{0} Written Down Value assets have no life: they run until their value is small.", [
						greys.life_from_rate,
					])
				),
			greys.existing_categories?.length &&
				__("Posting sets the accounts of {0}, made before, to the ones here.", [
					greys.existing_categories.join(", "),
				]),
		];
	}

	refresh_footer() {
		if (this.view !== "sheet") return;
		const { checks } = this;
		this.dialog.set_primary_action(__("Post to books"), () => this.post());
		this.dialog.set_secondary_action_label(__("Save draft"));
		this.dialog.set_secondary_action(() => this.save_draft());
		const first = (checks.reds || [])[0]?.key;
		set_primary_disabled(this.dialog, !this.get_rows().length || (!this.dirty && Boolean(first)));
	}

	// Depreciation by category: one row each, applied to all its assets
	show_categories() {
		const accounts = new Set(this.get_rows().map((row) => row.asset_account));
		const net_value = this.checks.net_value_accounts || {};
		const categories = (this.frm.doc.asset_categories || [])
			.filter((row) => accounts.has(row.asset_account))
			.map((row) => ({
				asset_account: row.asset_account,
				asset_category: row.asset_category,
				method: row.method,
				life: row.life,
				rate: row.rate,
				accumulated_account: net_value[row.asset_account] || row.accumulated_account,
				depreciation_account: row.depreciation_account,
				hsn_code: row.hsn_code,
				net_value: net_value[row.asset_account] ? 1 : 0,
			}));
		this.set_view("categories");
		const grid = this.dialog.fields_dict.categories.grid;
		setup_dialog_grid(grid);
		grid.df.data = categories;
		grid.refresh();
		this.dialog
			.get_field("categories_head")
			.$wrapper.empty()
			.append(
				make_back_header(
					__("Depreciation by category"),
					__("Rows with a life or rate of their own keep it."),
					() => this.show_sheet()
				).addClass("mb-3"),
				grey_line(__("Written Down Value uses the rate; Straight Line uses the life.")).addClass(
					"mb-3"
				)
			);
		set_primary_disabled(this.dialog, false);
		this.dialog.set_secondary_action_label(__("Back"));
		this.dialog.set_secondary_action(() => this.show_sheet());
		const count = this.get_rows().length;
		this.dialog.set_primary_action(
			count ? plural(count, __("Apply to 1 row"), __("Apply to {0} rows", [count])) : __("Apply"),
			() => {
				const no_method = (grid.df.data || []).filter((row) => !row.method);
				if (no_method.length) {
					return frappe.ui.toast({
						type: "error",
						message: __("Pick a Method for {0}.", [
							no_method.map((row) => row.asset_category || row.asset_account).join(", "),
						]),
					});
				}
				return this.save()
					.then(() =>
						call_method(this.frm, "save_asset_categories", {
							rows: (grid.df.data || []).map(({ net_value, ...row }) =>
								net_value ? { ...row, accumulated_account: undefined } : row
							),
						})
					)
					.then(() => this.refresh());
			}
		);
	}

	confirm_post() {
		const { checks, frm } = this;
		const count = checks.sheet.assets;
		const categories = Object.fromEntries(
			(frm.doc.asset_categories || []).map((row) => [row.asset_account, row])
		);
		const net_value = Object.entries(checks.net_value || {});
		const first = checks.first_depreciation;
		this.show_confirm({
			title: plural(count, __("Post 1 asset?"), __("Post {0} assets?", [count])),
			back: __("Nothing is posted until you confirm."),
			body: [
				make_kv_lines([
					[
						__("To post"),
						[
							count_assets(count),
							__("cost {0}", [money(this.frm, checks.sheet.cost)]),
							__("depreciation {0}", [money(this.frm, checks.sheet.depreciation)]),
						].join(" · "),
					],
				]),
				checks.hand.counted &&
					grey_line(
						__("{0} booked by hand since {1} is counted. Monthly from {2}.", [
							money(this.frm, checks.hand.counted),
							frappe.datetime.str_to_user(frm.doc.migration_date),
							frappe.datetime.str_to_user(first.date),
						])
					),
				...net_value.map(([account, amount]) =>
					grey_line(
						__(
							"Your balances do not change. Only the depreciation of {0} moves to its own line: {1} to {2}, dated {3}.",
							[
								categories[account]?.asset_category || short_account(frm, account),
								money(this.frm, amount),
								short_account(frm, checks.net_value_accounts?.[account]) ||
									__("its accumulated depreciation line"),
								frappe.datetime.str_to_user(checks.net_value_date),
							]
						)
					)
				),
				checks.wdv_gap &&
					grey_line(
						__("{0} less depreciation this year; ERPNext catches it up over the asset's life.", [
							money(this.frm, checks.wdv_gap),
						])
					),
				checks.fully_depreciated &&
					grey_line(
						plural(
							checks.fully_depreciated,
							__("1 asset is fully depreciated: it is posted with no schedule."),
							__("{0} assets are fully depreciated: they are posted with no schedule.", [
								checks.fully_depreciated,
							])
						)
					),
				make_kv_lines([
					[
						__("First depreciation"),
						[frappe.datetime.str_to_user(first.date), money(this.frm, first.amount)].join(" · "),
					],
				]),
				checks.year_missing &&
					frappe.model.can_create("Fiscal Year") &&
					grey_line(
						__("The financial year starting {0} is added when you post.", [
							frappe.datetime.str_to_user(checks.year_start),
						])
					),
				!checks.auto_depreciation &&
					grey_line(
						__("Depreciation is not booked by itself on this site. Book it from each asset.")
					),
			].filter(Boolean),
			primary: plural(count, __("Post 1 asset"), __("Post {0} assets", [count])),
			on_primary: () =>
				call_method(frm, "post_fixed_assets", {}, { freeze: true }).then(
					(r) => {
						this.dialog.hide();
						after_run(
							frm,
							r.message,
							__("Posting the assets in the background. This page updates when it's done."),
							() =>
								frappe.ui.toast({
									type: "success",
									message: __("{0} posted.", [count_assets(count)]),
								})
						);
					},
					// the confirm is stale once posting turns it down: back to the sheet and its reds
					() => this.refresh()
				),
		});
	}
}

// depreciation booked by hand after the assets were posted: ERPNext's schedule books it too
const HAND_AFTER_TEXT = () =>
	__(
		"This depreciation was booked by hand after the assets were added. ERPNext's schedule books it too, so it would count twice. Cancel the entry."
	);

function get_hand_after_alerts(frm) {
	const currency = erpnext.get_currency(frm.doc.company);
	return (frm.doc.__onload?.asset_match?.hand_after_entries || []).map((entry) => ({
		theme: "red",
		title: HAND_AFTER_TEXT(),
		description: [
			entry.name,
			frappe.datetime.str_to_user(entry.posting_date),
			format_currency(entry.amount, currency),
			entry.accounts.map((account) => short_account(frm, account)).join(", "),
		].join(" · "),
		buttons: [
			{
				label: __("Open entry"),
				onclick: () => frappe.set_route("Form", "Journal Entry", entry.name),
			},
		],
	}));
}

// What an upload of the asset sheet left out and replaced
// "Mark them", or what the one account becomes
function get_mark_label(types) {
	if (types.length !== 1) return __("Mark them");
	return {
		"Fixed Asset": __("Mark as fixed asset"),
		Depreciation: __("Mark as depreciation"),
		"Accumulated Depreciation": __("Mark as accumulated depreciation"),
	}[types[0]];
}

function get_upload_alerts(result) {
	if (!result?.posted && !result?.replaced) return [];
	const total = (result.posted || 0) + (result.added || 0);
	return [
		{
			theme: "yellow",
			title: result.posted
				? result.added
					? __("{0} of {1} assets on this sheet are posted already", [result.posted, total])
					: plural(
							result.posted,
							__("The asset on this sheet is posted already"),
							__("All {0} assets on this sheet are posted already", [result.posted])
					  )
				: plural(
						result.replaced,
						__("1 draft row was replaced by the new sheet."),
						__("{0} draft rows were replaced by the new sheet.", [result.replaced])
				  ),
			description: [
				result.posted &&
					__(
						"They were left out, so nothing counts twice. To change a posted asset, undo it first."
					),
				result.posted &&
					result.replaced &&
					plural(
						result.replaced,
						__("1 draft row was replaced by the new sheet."),
						__("{0} draft rows were replaced by the new sheet.", [result.replaced])
					),
			]
				.filter(Boolean)
				.join(" "),
		},
	];
}

function show_posted_assets(frm) {
	show_posted_window(frm, {
		title: __("Posted assets"),
		undo_after_live: true,
		load: () => get_data(frm, "get_posted_entries", { kind: "assets" }).then((r) => r.message),
		posted_word: (when) => __("Posted {0}", [when]),
		group_line: (group) => count_assets(group.assets),
		group_amount: (group) => money(frm, group.cost),
		undone: (posted) =>
			(posted.undone || []).map((group) => ({
				source: group.source,
				line: __("{0} · {1} · taken out of your books", [
					count_assets(group.count),
					money(frm, group.cost),
				]),
				download: () =>
					download_sheet(frm.doc.company, "fixed_assets.download_fixed_assets", {
						undone_from: group.source,
					}),
			})),
		fields: [
			{ fieldname: "asset", fieldtype: "Link", options: "Asset", label: __("Asset"), columns: 2 },
			{ fieldname: "asset_name", fieldtype: "Data", label: __("Asset name"), columns: 2 },
			{ fieldname: "asset_account", fieldtype: "Data", label: __("Asset account"), columns: 2 },
			{ fieldname: "purchase_date", fieldtype: "Date", label: __("Purchase date"), columns: 1 },
			{ fieldname: "gross", fieldtype: "Currency", label: __("Cost"), columns: 2 },
			{
				fieldname: "depreciated_text",
				fieldtype: "Data",
				label: __("Depreciation posted"),
				columns: 1,
			},
		],
		rows: (group) =>
			group.rows.map((row) => ({
				...row,
				depreciated_text: row.depreciated ? __("Yes") : "",
			})),
		blocked: (row) => row.depreciated,
		blocked_alert: (rows) => ({
			title: plural(
				rows.length,
				__("1 asset already has depreciation posted."),
				__("{0} assets already have depreciation posted.", [rows.length])
			),
			description: __("Cancel those depreciation entries first. Ask your CA."),
		}),
		undo_label: (selected, group) =>
			selected.length
				? __("Undo {0} selected · {1}", [
						selected.length,
						money(
							frm,
							selected.reduce((sum, row) => sum + flt(row.gross), 0)
						),
				  ])
				: __("Undo this upload · {0}", [money(frm, group.cost)]),
		confirm_title: (rows) => __("Undo {0}?", [count_assets(rows.length)]),
		confirm_text: (rows) =>
			plural(
				rows.length,
				__("You can download it, fix it and upload again."),
				__("You can download them, fix them and upload again.")
			),
		confirm_what: (rows, selected) => ({
			count: rows.length,
			kind: "assets",
			names: selected ? selected.map((row) => row.asset) : null,
			what: count_assets(rows.length),
			one: __("asset"),
		}),
		confirm_rows: (rows) =>
			rows.map((row) => [
				$(`<span class="text-base text-ink-gray-8 grow truncate"></span>`).text(row.asset_name),
				$(`<span class="text-base text-ink-gray-5 shrink-0"></span>`).text(
					short_account(frm, row.asset_account)
				),
				$(`<span class="text-base text-ink-gray-8 shrink-0"></span>`).text(money(frm, row.gross)),
			]),
		total: (rows) => rows.reduce((sum, row) => sum + flt(row.gross), 0),
		confirm_button: (rows) => __("Undo {0}", [count_assets(rows.length)]),
		undo: (group, selected, delete_entries) =>
			call_method(frm, "undo_fixed_assets", {
				source: group.source,
				asset_names: selected ? selected.map((row) => row.asset) : null,
				delete_entries: delete_entries,
			}),
		after_undo: (result, group) => show_undo_result(frm, "assets", result, group.source),
	});
}

// ---------- Opening balances: stock in the trial balance ----------

// Work in progress or job worker lines kept as balances: move them to Opening stock, or keep them
function make_balance_yellows(frm, dialog) {
	const currency = erpnext.get_currency(frm.doc.company);
	const reopen = () => {
		reload_fresh(frm).then(() => show_opening_balances(frm, null, dialog));
	};
	const alerts = [];
	for (const line of frm.doc.__onload?.stock_like_lines || []) {
		alerts.push({
			theme: "yellow",
			title: __("Move {0} of {1} into Opening stock?", [
				format_currency(line.amount, currency, 0),
				short_account(frm, line.account),
			]),
			description: __(
				"Move it if you count this as stock, by item, in a warehouse: it posts with Opening stock. Keep it if it is only a balance. You can change this later in Opening stock."
			),
			buttons: [
				{
					label: __("Move it"),
					onclick: () =>
						call_method(frm, "move_to_opening_stock", { account: line.account }).then(reopen),
				},
				{
					label: __("Keep as a balance"),
					variant: "outline",
					onclick: () =>
						call_method(frm, "keep_as_balance", { account: line.account }).then(reopen),
				},
			],
		});
	}
	return make_alerts(alerts).addClass("mb-3");
}

// ---------- realtime ----------

// tabs that run past a phone's width fade at the end, so the rest are seen to be there
function show_tabs_cue(frm) {
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
function watch_stock_posting(frm) {
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
function listen_for_postings(frm) {
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
