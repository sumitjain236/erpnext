frappe.ui.form.on("Company Setup", {
	setup(frm) {
		// invoices made in the background: how far they are, then done
		frappe.realtime.off("company_setup_invoices_progress");
		frappe.realtime.on("company_setup_invoices_progress", (data) => {
			if (data.company !== frm.doc.company) return;
			frm.posting = { ...frm.posting, [data.party_type]: { done: data.done, total: data.total } };
			update_posting(frm, data.party_type);
		});
		frappe.realtime.off("company_setup_invoices");
		frappe.realtime.on("company_setup_invoices", (data) => {
			if (data.company !== frm.doc.company) return;
			if (frm.posting) delete frm.posting[data.party_type];
			if (frm.posting_dialog?.party_type === data.party_type) frm.posting_dialog.hide();
			if (data.error) {
				frappe.msgprint({
					title:
						data.party_type === "Supplier" ? __("Bills not posted") : __("Invoices not posted"),
					message: data.error,
					indicator: "red",
				});
			}
			frm.reload_doc().then(() => !data.error && show_posting_result(frm, data.party_type));
		});
	},

	refresh(frm) {
		if (frm.is_new()) return;

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
		frm.events.render(frm);
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
			make_overview_stats(frm),
			make_checklist(frm, steps)
		);
		for (const tab of ["accounts_tab", "selling_tab", "buying_tab"]) {
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
	return (frm.doc.steps || []).map((row) => ({
		key: row.step_key,
		name: row.step_key.split(".").pop(),
		title: details[row.step_key]?.title || row.step,
		description: details[row.step_key]?.description,
		summary: details[row.step_key]?.summary,
		tab: details[row.step_key]?.tab,
		is_started: details[row.step_key]?.started,
		is_complete: row.status === "Done",
		is_skipped: row.status === "Skipped",
	}));
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
	// advances are negative rows: they lower the total but aren't invoices
	const owed = (rows, label, count_label) => {
		const total = rows.reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
		const invoices = rows.filter((row) => flt(row.outstanding_amount) > 0).length;
		return frappe.ui.stat_card({
			label: label,
			value: rows.length ? format_currency(total, currency) : null,
			caption: rows.length && count_label(format_number(invoices, null, 0)),
		});
	};
	return $(`<div class="es-stat-card-grid"></div>`).append(
		frappe.ui.stat_card({
			label: __("Temporary Opening"),
			value: posted ? format_currency(Math.abs(temporary), currency) : __("Not posted yet"),
			caption: !posted
				? __("Shows what doesn't balance once you post.")
				: temporary
				? __("Not explained yet. Goes to zero once everything is in.")
				: __("Your books add up."),
			onclick: frm.doc.migration_date ? () => show_posting_preview(frm, false) : undefined,
		}),
		owed(sales, __("Customers owe you"), (count) => __("{0} invoices", [count])),
		owed(purchases, __("You owe suppliers"), (count) => __("{0} bills", [count]))
	);
}

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
	const done = steps.filter((step) => step.is_complete).length;
	const skipped = steps.filter((step) => step.is_skipped).length;
	const $card = $(`<div class="contact-box flex flex-col gap-1 mb-0"></div>`);
	$card.append(
		frappe.ui.progress({
			label: __("Set up {0}", [frm.doc.company]),
			hint: () =>
				skipped
					? __("{0} of {1} done, {2} skipped", [done, steps.length, skipped])
					: __("{0} of {1} done", [done, steps.length]),
			intervals: true,
			interval_count: steps.length,
			size: "md",
			value: steps.length ? (done / steps.length) * 100 : 0,
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
		$(
			`<div class="company-setup-step flex items-center rounded-md mx-n2 p-2 gap-3" role="button" tabindex="0"></div>`
		)
			.append(make_task(frm, step, []).addClass("grow min-w-0"))
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
	chart_of_accounts: (frm, step) => step.is_complete && show_chart(frm.doc.company),
	opening_balances: (frm) => show_opening_balances(frm),
	opening_sales_invoices: (frm) => show_opening_invoices(frm, "Customer"),
	opening_purchase_invoices: (frm) => show_opening_invoices(frm, "Supplier"),
	customers: (frm, step) => !step.is_complete && open_party_import(frm, "Customer"),
	suppliers: (frm, step) => !step.is_complete && open_party_import(frm, "Supplier"),
	check_books: (frm) =>
		!frm.doc.migration_date
			? show_migration_date(frm)
			: has_drafts(frm)
			? show_posting_preview(frm, false)
			: frappe.set_route("query-report", "Balance Sheet", { company: frm.doc.company }),
	go_live: (frm, step) => !step.is_complete && show_go_live(frm),
};

// A task on its tab: the same buttons whatever its state, the rest in a menu
function make_task_card(frm, step) {
	const buttons = (STEP_ACTIONS[step.name]?.(frm, step) || []).filter(Boolean);
	const more = [...(STEP_MORE[step.name]?.(frm, step) || []), ...skip_items(frm, step)];
	if (more.length) {
		buttons.push(
			frappe.ui.dropdown({
				button: { icon: "ellipsis", variant: "ghost", attrs: { "aria-label": __("More") } },
				options: more,
				align: "end",
			})
		);
	}
	if (step.name === "chart_of_accounts" && !step.is_complete && frm.doc.__onload?.chart_import_blocked) {
		step = { ...step, description: frm.doc.__onload.chart_import_blocked };
	}
	const posting_party = { opening_sales_invoices: "Customer", opening_purchase_invoices: "Supplier" }[
		step.name
	];
	if (frm.posting?.[posting_party]) {
		step = { ...step, is_posting: true, summary: get_posting_text(frm, posting_party) };
	}
	const $card = $(`<div class="contact-box mb-0"></div>`).append(make_task(frm, step, buttons));
	if (step.is_posting) {
		$card.find(".text-p-sm").first().attr("data-posting-note", posting_party);
		$card.append($(`<div class="ps-8 pt-2"></div>`).append(make_posting_bar(frm, posting_party)));
	}
	return $card;
}

// While invoices or bills post in the background: what the card and window say, and the bar
function get_posting_text(frm, party_type) {
	const { done, total } = frm.posting?.[party_type] || {};
	if (!total) {
		return party_type === "Customer"
			? __("Posting invoices in the background")
			: __("Posting bills in the background");
	}
	return party_type === "Customer"
		? __("Posting {0} invoices in the background · {1} done", [total, done])
		: __("Posting {0} bills in the background · {1} done", [total, done]);
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
	if (step.is_complete || step.is_started) return [];
	return [{ label: __("Skip this step"), icon: "circle-minus", onclick: () => skip_step(frm, step, 1) }];
}

// Where each report or list is in the software people move from
const WHERE_TO_FIND = {
	chart_of_accounts: [
		["Tally", __("Display More Reports › List of Accounts, then Alt+E to export")],
		["Zoho Books", __("Accountant › Chart of Accounts › Export")],
		["QuickBooks", __("Settings › Chart of Accounts › Run Report › Export")],
		["Excel", __("Download the sample chart and fill it in.")],
	],
	opening_balances: [
		["Tally", __("Display More Reports › Trial Balance, Alt+F5 for every ledger, then Alt+E")],
		["Zoho Books", __("Reports › Trial Balance")],
		["QuickBooks", __("Reports › Trial Balance")],
		["Excel", __("One row per account with its debit or credit.")],
	],
	opening_sales_invoices: [
		["Tally", __("Display More Reports › Statements of Accounts › Outstandings › Receivables")],
		["Zoho Books", __("Reports › AR Aging Details")],
		["QuickBooks", __("Reports › Open Invoices")],
		["Excel", __("One row per unpaid invoice: customer, invoice number, date, amount.")],
	],
	opening_purchase_invoices: [
		["Tally", __("Display More Reports › Statements of Accounts › Outstandings › Payables")],
		["Zoho Books", __("Reports › AP Aging Details")],
		["QuickBooks", __("Reports › Unpaid Bills")],
		["Excel", __("One row per unpaid bill: supplier, bill number, date, amount.")],
	],
};

// The "Where do I find this?" section at the end of a dialog: open while nothing is entered yet,
// folded once something is, and left out once anything is posted
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
		{
			fieldname: "where_to_find",
			fieldtype: "HTML",
			options: make_where_table(frm, WHERE_TO_FIND[task_name]).outerHTML,
		},
	];
}

function make_where_table(frm, places) {
	const $table = $(`<div class="flex flex-col gap-2"></div>`).append(make_where_box(places));
	if (frm.doc.migration_date) {
		$(`<div class="text-p-sm text-ink-gray-5"></div>`)
			.text(
				__("Take it as on {0}, the day before you start.", [
					frappe.datetime.str_to_user(frappe.datetime.add_days(frm.doc.migration_date, -1)),
				])
			)
			.appendTo($table);
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
				$(`<div class="text-p-sm text-ink-gray-5"></div>`).text(software),
				$(`<div class="text-p-sm text-ink-gray-8"></div>`).text(where)
			)
		)
	);
}

// Status icon, title, one line on what's done or left, and the buttons
function make_task(frm, step, buttons) {
	const [icon, color] = step.is_posting
		? ["loader-circle", "text-ink-gray-8"]
		: step.is_complete
		? ["circle-check", "text-ink-gray-8"]
		: step.is_skipped
		? ["circle-minus", "text-ink-gray-5"]
		: step.is_started
		? ["circle-dot", "text-ink-gray-8"]
		: ["circle-dashed", "text-ink-gray-5"];
	const note = step.is_skipped ? __("Skipped for now.") : step.summary || step.description;

	// more than two buttons go on a row of their own, so the text keeps its width; a menu doesn't count
	const stacked = buttons.filter((opts) => !(opts.jquery || opts instanceof Element)).length > 2;
	const $task = $(`<div class="flex items-center gap-3 ${stacked ? "flex-wrap" : ""}"></div>`);
	$(`<span class="shrink-0 ${color}"></span>`).html(frappe.utils.icon(icon, "md")).appendTo($task);
	const $text = $(`<div class="flex flex-col gap-0.5 grow min-w-0"></div>`).appendTo($task);
	$(`<div class="text-base-medium text-ink-gray-8"></div>`).text(step.title).appendTo($text);
	if (note) $(`<div class="text-p-sm text-ink-gray-5"></div>`).text(note).appendTo($text);

	if (buttons.length) {
		const $actions = $(
			`<div class="flex flex-wrap items-center gap-2 ${
				stacked ? "w-full ps-8" : "justify-end shrink-0"
			}"></div>`
		);
		buttons.forEach((opts) =>
			$actions.append(opts.jquery || opts instanceof Element ? opts : frappe.ui.button(opts))
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
	return call_method(frm, "skip_step", { step_key: step.key, skip: skip }).then(() => frm.reload_doc());
}

function show_migration_date(frm) {
	const bring = "eval:!doc.no_opening_data";
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
			{
				fieldname: "no_opening_data",
				fieldtype: "Check",
				label: __("This is a new business. There is nothing to bring over."),
				default: frm.doc.no_opening_data,
			},
			{ fieldname: "reports", fieldtype: "HTML", depends_on: bring },
			{
				fieldtype: "Section Break",
				label: __("Where is each report?"),
				collapsible: 1,
				depends_on: bring,
			},
			{ fieldname: "where", fieldtype: "HTML" },
		],
		primary_action_label: __("Save"),
		primary_action: (values) =>
			save_migration_date(frm, dialog, values.migration_date, values.no_opening_data ? 1 : 0),
	});

	const $reports = $(`<div class="flex flex-col gap-2 pt-2"></div>`);
	$(`<div class="text-sm-medium text-ink-gray-7"></div>`)
		.text(__("Keep these ready, as on the day before you start"))
		.appendTo($reports);
	const $list = $(`<div class="flex flex-col border rounded-lg"></div>`).appendTo($reports);
	[
		[__("Trial balance"), __("Every account with its balance")],
		[__("Unpaid customer invoices"), __("Who owes you, invoice by invoice")],
		[__("Unpaid supplier bills"), __("Whom you owe, bill by bill")],
	].forEach(([title, note], index) => {
		$(`<div class="flex items-center gap-3 px-3 py-2.5 ${index ? "border-t" : ""}"></div>`)
			.append(
				$(`<span class="flex shrink-0 text-ink-gray-5"></span>`).html(
					frappe.utils.icon("file-spreadsheet", "sm")
				),
				$(`<div class="flex flex-col gap-0.5 min-w-0"></div>`).append(
					$(`<div class="text-base text-ink-gray-8"></div>`).text(title),
					$(`<div class="text-p-sm text-ink-gray-5"></div>`).text(note)
				)
			)
			.appendTo($list);
	});
	$(`<div class="text-p-sm text-ink-gray-5"></div>`)
		.text(
			__("Customer and supplier lists help too. Without them, names are made from the unpaid invoices.")
		)
		.appendTo($reports);
	dialog.get_field("reports").$wrapper.append($reports);

	const $where = $(`<div class="flex flex-col gap-3"></div>`);
	for (const [title, task_name] of [
		[__("Trial balance"), "opening_balances"],
		[__("Unpaid customer invoices"), "opening_sales_invoices"],
		[__("Unpaid supplier bills"), "opening_purchase_invoices"],
	]) {
		$where.append(make_where_box(WHERE_TO_FIND[task_name], title));
	}
	dialog.get_field("where").$wrapper.append($where);
	dialog.show();
}

function save_migration_date(frm, dialog, migration_date, no_opening_data) {
	// set_value applies the values asynchronously, so check for changes once it is done
	return frm
		.set_value({ migration_date: migration_date, no_opening_data: no_opening_data })
		.then(() => (frm.is_dirty() ? frm.save() : null))
		.then(() => dialog.hide());
}

const REPORTS = [
	["Balance Sheet", __("Balance Sheet"), "scale"],
	["Profit and Loss Statement", __("Profit and Loss Statement"), "trending-up"],
	["Trial Balance", __("Trial Balance"), "list"],
	["General Ledger", __("General Ledger"), "book-open"],
];

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

// What is done and what is left; money left in Temporary Opening needs a tick, nothing else stops going live
function show_go_live(frm) {
	// only what is left is listed: a done step has nothing to say here
	const stats = frm.doc.__onload?.stats || {};
	// the box below says what Check your books would: the amount left in Temporary Opening
	const steps = get_steps(frm).filter(
		(step) =>
			step.name !== "go_live" &&
			!step.is_complete &&
			!step.is_skipped &&
			!(step.name === "check_books" && stats.temporary_opening)
	);
	const currency = erpnext.get_currency(frm.doc.company);
	const temporary = format_currency(Math.abs(stats.temporary_opening || 0), currency);
	const dialog = new frappe.ui.Dialog({
		title: __("Go live"),
		fields: [
			{ fieldname: "summary", fieldtype: "HTML" },
			{
				fieldname: "accept_temporary_opening",
				fieldtype: "Check",
				label: __("I understand {0} in Temporary Opening isn't explained yet", [temporary]),
				hidden: !stats.temporary_opening,
				onchange: () => set_primary_enabled(),
			},
		],
		primary_action_label: __("Go live"),
		primary_action: (values) =>
			call_method(frm, "go_live", { accept_temporary_opening: values.accept_temporary_opening }).then(
				() => {
					dialog.hide();
					frappe.show_alert({ message: __("{0} is live", [frm.doc.company]), indicator: "green" });
					frm.reload_doc();
				}
			),
		secondary_action_label: __("Not yet"),
		secondary_action: () => dialog.hide(),
	});

	const $body = $(`<div class="flex flex-col gap-3"></div>`);
	$(`<div class="text-p-sm text-ink-gray-6"></div>`)
		.text(
			[
				frm.doc.migration_date &&
					__("{0} moves to ERPNext on {1}.", [
						frm.doc.company,
						frappe.datetime.str_to_user(frm.doc.migration_date),
					]),
				stats.temporary_opening
					? null
					: steps.length
					? __("These steps are open. Nothing here stops you.")
					: __("Everything is in and your books add up."),
			]
				.filter(Boolean)
				.join(" ")
		)
		.appendTo($body);
	const set_primary_enabled = () =>
		dialog
			.get_primary_btn()
			.prop(
				"disabled",
				Boolean(stats.temporary_opening) && !dialog.get_value("accept_temporary_opening")
			);
	if (steps.length) {
		const $list = $(`<div class="flex flex-col"></div>`).appendTo($body);
		for (const step of steps) {
			$list.append($(`<div class="border-t py-2"></div>`).append(make_task(frm, step, [])));
		}
	}
	if (stats.temporary_opening) {
		$body.append(
			frappe.ui.alert({
				theme: "yellow",
				title: __("{0} is left in Temporary Opening", [temporary]),
				description: __(
					"Something from your old books isn't in yet, such as stock, an advance or a balance left out of your sheet. You can go live and add it later."
				),
				footer: () =>
					$(`<div class="flex gap-2"></div>`).append(frappe.ui.button(see_books_button(frm))),
			})
		);
	}
	dialog.get_field("summary").$wrapper.append($body);
	dialog.show();
	set_primary_enabled();
}

// Buttons of each task on its own tab, by the last part of its hook path
const STEP_ACTIONS = {
	chart_of_accounts: (frm, step) => {
		if (step.is_complete) {
			return [{ label: __("Edit chart"), icon: "pencil", onclick: () => show_chart(frm.doc.company) }];
		}
		return [
			{
				label: __("Keep the standard chart"),
				onclick: () => call_method(frm, "use_existing_chart").then(() => frm.reload_doc()),
			},
			!frm.doc.__onload?.chart_import_blocked && {
				label: __("Import my chart"),
				icon: "import",
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
				icon: "pencil",
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
	customers: (frm, step) => import_buttons(frm, "Customer", step),
	suppliers: (frm, step) => import_buttons(frm, "Supplier", step),
	check_books: (frm) => [
		has_drafts(frm) && see_books_button(frm),
		// the reports show only what is posted
		has_posted(frm) &&
			frappe.ui.dropdown({
				button: { label: __("Reports"), icon_right: "chevron-down" },
				options: REPORTS.map(([report, label, icon]) => ({
					label: label,
					icon: icon,
					onclick: () => frappe.set_route("query-report", report, { company: frm.doc.company }),
				})),
				align: "end",
			}),
	],
};

// What else a task can do, in its ⋯ menu
const STEP_MORE = {
	chart_of_accounts: (frm, step) =>
		step.is_complete
			? [
					{
						label: __("Change the chart"),
						icon: "replace",
						onclick: () => call_method(frm, "change_chart_choice").then(() => frm.reload_doc()),
					},
					{
						label: __("Download current chart"),
						icon: "download",
						onclick: () => download_chart_sheet(frm.doc.company, "Current Chart"),
					},
					{
						label: __("Open Chart of Accounts page"),
						icon: "arrow-up-right",
						onclick: () => {
							frappe.route_options = { company: frm.doc.company };
							frappe.set_route("Tree", "Account");
						},
					},
			  ]
			: [],
	opening_sales_invoices: (frm) => invoice_menu(frm, "Customer"),
	opening_purchase_invoices: (frm) => invoice_menu(frm, "Supplier"),
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

function show_opening_balances(frm, result) {
	const company = frm.doc.company;
	const posted = (frm.doc.opening_balances || []).filter((row) => row.status === "Posted");
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
				on_delete_rows: () => update_totals(),
				data: (frm.doc.opening_balances || [])
					.filter((row) => row.status !== "Posted")
					.map((row) => ({ ...row, name: undefined })),
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
								});
						},
					},
					{
						fieldname: "account_type",
						fieldtype: "Data",
						label: __("Account Type"),
						read_only: 1,
						in_list_view: 1,
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
						onchange: () => update_totals(),
					},
					{
						fieldname: "credit",
						fieldtype: "Currency",
						label: __("Credit"),
						in_list_view: 1,
						columns: 2,
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
		secondary_action: () => save(),
	});
	const save = () =>
		call_method(frm, "save_opening_balances", { rows: dialog.get_values().balances || [] }).then(() => {
			dialog.hide();
			return frm.reload_doc();
		});

	const $intro = dialog.get_field("intro").$wrapper;
	$(`<div class="flex flex-wrap items-center justify-between gap-2 mb-3"></div>`)
		.append(
			$(`<div class="text-p-sm text-ink-gray-6"></div>`).text(
				__(
					"Bank, cash, loans, capital, tax and the rest, as on {0}. Customers, suppliers and stock come in their own way.",
					[
						frm.doc.migration_date
							? frappe.datetime.str_to_user(
									frappe.datetime.add_days(frm.doc.migration_date, -1)
							  )
							: __("the day before migration"),
					]
				)
			),
			$(`<div class="flex gap-1 shrink-0"></div>`).append(
				frappe.ui.button({
					label: __("Upload sheet"),
					icon: "upload",
					onclick: () => upload_opening_balances(frm, dialog),
				}),
				frappe.ui.dropdown({
					button: { icon: "ellipsis", variant: "ghost", attrs: { "aria-label": __("More") } },
					align: "end",
					options: [
						{
							label: __("Download sheet"),
							icon: "download",
							onclick: () =>
								open_url_post(
									"/api/method/erpnext.setup.doctype.company_setup.chart_sheet.download_opening_balances",
									{ company: company }
								),
						},
						{
							label: __("Add account"),
							icon: "plus",
							onclick: () =>
								frappe.model.with_doctype("Account", () => add_balance_account(frm, dialog)),
						},
					],
				})
			)
		)
		.appendTo($intro);
	const $cards = $(`<div class="es-stat-card-grid mb-3"></div>`).appendTo($intro);
	if (result) $intro.append(make_upload_result(frm, result));
	if (posted.length) {
		$(`<div class="text-p-sm text-ink-gray-5 mb-3"></div>`)
			.text(__("{0} balances are posted already and stay as they are.", [posted.length]))
			.appendTo($intro);
	}

	const currency = erpnext.get_currency(company);
	// the difference first: it is what doesn't add up yet
	const update_totals = () => {
		const rows = dialog.fields_dict.balances.df.data || [];
		const sum = (field) => rows.reduce((total, row) => total + flt(row[field]), 0);
		const count = (field) => rows.filter((row) => flt(row[field])).length;
		const debit = sum("debit");
		const credit = sum("credit");
		$cards.empty().append(
			frappe.ui.stat_card({
				label: __("Debit"),
				value: format_currency(debit, currency),
				caption: __("{0} accounts", [count("debit")]),
			}),
			frappe.ui.stat_card({
				label: __("Credit"),
				value: format_currency(credit, currency),
				caption: __("{0} accounts", [count("credit")]),
			}),
			frappe.ui.stat_card({
				label: __("Difference"),
				value: format_currency(Math.abs(debit - credit), currency),
				caption: get_difference_caption(frm, debit - credit),
			})
		);
	};
	dialog.show();
	update_totals();
}

const OPENING_EXCLUDED_TYPES = ["Receivable", "Payable", "Stock", "Temporary"];

// A new account of this company, added as a row of the balances table
function add_balance_account(frm, balances_dialog) {
	const company = frm.doc.company;
	const types = frappe.meta
		.get_docfield("Account", "account_type")
		.options.split("\n")
		.filter((type) => !OPENING_EXCLUDED_TYPES.includes(type));
	const dialog = new frappe.ui.Dialog({
		title: __("Add account"),
		fields: [
			{ fieldname: "group_note", fieldtype: "HTML" },
			{ fieldname: "account_name", fieldtype: "Data", label: __("Account Name"), reqd: 1 },
			{
				fieldname: "account_type",
				fieldtype: "Select",
				label: __("Account Type"),
				options: types,
				description: __("Bank or Cash for money accounts; leave empty for most others."),
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
		primary_action_label: __("Add"),
		primary_action: (values) =>
			frappe
				.call({
					method: "erpnext.accounts.utils.add_ac",
					args: {
						doctype: "Account",
						company: company,
						account_name: values.account_name,
						account_type: values.account_type,
						parent: values.parent_account,
						is_group: 0,
						is_root: 0,
					},
					freeze: true,
				})
				.then((r) =>
					frappe.db.get_value("Account", r.message, ["account_type", "root_type"]).then((info) => {
						const grid = balances_dialog.fields_dict.balances.grid;
						const name = values.account_name.trim().toLowerCase();
						// the sheet's label may carry the account number: "1001 - Rent Deposit"
						const waiting = grid.df.data.find(
							(row) =>
								!row.account &&
								[row.original_account, cstr(row.original_account).split(" - ").pop()].some(
									(label) => cstr(label).trim().toLowerCase() === name
								)
						);
						if (waiting) Object.assign(waiting, { account: r.message, ...info.message });
						else grid.df.data.push({ account: r.message, ...info.message });
						grid.refresh();
						dialog.hide();
					})
				),
	});
	// money accounts go under the group of their kind, like Bank Accounts
	const suggest_parent = () => {
		const account_type = dialog.get_value("account_type");
		if (!account_type) return;
		frappe.db
			.get_value("Account", { company: company, is_group: 1, account_type: account_type }, "name")
			.then((r) => r.message?.name && dialog.set_value("parent_account", r.message.name));
	};
	dialog.show();
	show_group_chart_note(company, dialog.get_field("group_note").$wrapper);
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
	if (!drafts) return choose_balance_sheet(frm, dialog);
	frappe.confirm(
		__(
			"The sheet replaces the {0} balances in this table, unsaved changes included. Posted balances stay as they are. Continue?",
			[drafts]
		),
		() => choose_balance_sheet(frm, dialog)
	);
}

function choose_balance_sheet(frm, dialog) {
	new frappe.ui.FileUploader({
		restrictions: { allowed_file_types: [".xlsx", ".csv"] },
		on_success: (file) =>
			call_method(frm, "import_opening_balances", { file_url: file.file_url }).then((r) => {
				dialog.hide();
				frm.reload_doc().then(() => show_opening_balances(frm, r.message));
			}),
	});
}

// What the upload did, and where what it left out goes
function make_upload_result(frm, result) {
	const $result = $(`<div class="flex flex-col gap-1 mb-3"></div>`);
	if (result.unmatched?.length) {
		$result.append(
			$(`<div class="mb-2"></div>`).append(
				frappe.ui.alert({
					theme: "yellow",
					title: __("Pick an account for {0} rows", [result.unmatched.length]),
					description: __("These names are not in your chart: {0}", [result.unmatched.join(", ")]),
				})
			)
		);
	}
	// customers and suppliers are told by the Difference card; only stock needs a line of its own
	const left_out = left_out_text(frm, { Stock: result.left_out?.Stock });
	if (left_out) $(`<div class="text-p-sm text-ink-gray-5"></div>`).text(left_out).appendTo($result);
	if (result.groups) {
		$(`<div class="text-p-sm text-ink-gray-5"></div>`)
			.text(__("{0} group lines were skipped: they only add up their sub-accounts.", [result.groups]))
			.appendTo($result);
	}
	return $result;
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
	if (!invoices) return __("Waiting for your customer invoices and supplier bills to be posted.");
	const left = flt(difference + invoices, 2);
	if (Math.abs(left) < 1) return __("Covered by the customer invoices and supplier bills posted.");
	return __("{0} not covered yet by the invoices and bills posted.", [
		format_currency(Math.abs(left), erpnext.get_currency(frm.doc.company)),
	]);
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
function make_fiscal_year_note(frm) {
	const missing = frm.doc.__onload?.missing_fiscal_year;
	if (!missing) return null;
	return frappe.ui.alert({
		theme: "blue",
		title: __("The financial year {0} is added when you post", [missing.year]),
		description: __(
			"Opening entries are dated {0}, the day before you start, and ERPNext doesn't have that year yet.",
			[frappe.datetime.str_to_user(missing.date)]
		),
	});
}

// by name: the form reloads itself after a save, and a call sent mid-reload has no doc to send
function call_method(frm, method, args, opts = {}) {
	return frappe.call({
		method: "run_doc_method",
		args: { dt: frm.doctype, dn: frm.docname, method: method, args: args },
		...opts,
	});
}

// The books on the day before the migration date; with can_post, as they will be after posting
function show_posting_preview(frm, can_post = true) {
	call_method(frm, "preview_opening_balances").then((r) => {
		const preview = r.message;
		const currency = erpnext.get_currency(frm.doc.company);
		const dialog = new frappe.ui.Dialog({
			title: __("Your books on {0}", [frappe.datetime.str_to_user(preview.posting_date)]),
			size: "extra-large",
			fields: [{ fieldname: "preview", fieldtype: "HTML" }],
			primary_action_label: can_post ? __("Post") : __("Done"),
			primary_action: () =>
				can_post
					? call_method(frm, "post_opening_balances", null, { freeze: true }).then(() => {
							dialog.hide();
							frm.reload_doc();
					  })
					: dialog.hide(),
			secondary_action_label: can_post ? __("Back") : undefined,
			secondary_action: can_post ? () => dialog.hide() : undefined,
		});
		const year_note = can_post && make_fiscal_year_note(frm);
		if (year_note)
			dialog.get_field("preview").$wrapper.append($(`<div class="mb-3"></div>`).append(year_note));
		dialog.get_field("preview").$wrapper.append(make_posting_preview(preview, currency, frm.doc.company));
		dialog.show();
	});
}

function make_posting_preview(preview, currency, company) {
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
				? __(
						"{0} balances and {1} invoices and bills aren't posted yet. This is how your books will look once they are.",
						[preview.drafts, preview.draft_invoices]
				  )
				: preview.drafts
				? __("{0} balances aren't posted yet. This is how your books will look once they are.", [
						preview.drafts,
				  ])
				: preview.draft_invoices
				? __(
						"{0} invoices and bills aren't posted yet. This is how your books will look once they are.",
						[preview.draft_invoices]
				  )
				: __("Your books as they are, with everything posted so far.")
		)
		.appendTo($body);
	if (preview.left_out_invoices) {
		$(`<div class="text-p-sm text-ink-gray-6"></div>`)
			.text(
				preview.left_out_invoices === 1
					? __(
							"1 invoice or bill is left out: its customer or supplier isn't confirmed yet, is disabled or is in another currency."
					  )
					: __(
							"{0} invoices and bills are left out: their customer or supplier isn't confirmed yet, is disabled or is in another currency.",
							[preview.left_out_invoices]
					  )
			)
			.appendTo($body);
	}
	if (temporary) {
		$body.append(
			frappe.ui.alert({
				theme: "yellow",
				title: __("{0} doesn't balance yet", [money(Math.abs(temporary.balance))]),
				description: __(
					"It waits in Temporary Opening until your customer invoices and supplier bills, or a balance missing from your sheet, are in. Then it goes to zero. Nothing stops you meanwhile."
				),
			})
		);
	}

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
	// each statement links to the full report it stands for
	for (const tab of tabs) {
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
							frappe.set_route("query-report", tab.report, { company: company });
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
	const $side = $(`<div class="flex flex-col min-w-0" style="flex: 1 1 0"></div>`);
	$(`<div class="text-sm-medium text-ink-gray-8 pb-2 border-b"></div>`).text(title).appendTo($side);
	let parent;
	let total = 0;
	for (const row of rows) {
		if (row.parent !== parent) {
			parent = row.parent;
			$(`<div class="text-p-sm text-ink-gray-5 pt-2"></div>`)
				.text(parent || "")
				.appendTo($side);
		}
		total += sign * row.balance;
		$side.append(make_statement_line(row.account_name, money(sign * row.balance)));
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
		$(`<span class="shrink-0 text-right" style="width: 140px"></span>`).text(debit),
		$(`<span class="shrink-0 text-right" style="width: 140px"></span>`).text(credit)
	);
}

function invoice_buttons(frm, party_type) {
	const rows = frm.doc[PARTY[party_type].table] || [];
	const drafts = rows.filter((row) => row.status !== "Created");
	const blocker = !frm.doc.migration_date && __("Set the migration date first.");
	return [
		{
			label:
				party_type === "Customer"
					? rows.length
						? __("Edit invoices")
						: __("Enter invoices")
					: rows.length
					? __("Edit bills")
					: __("Enter bills"),
			icon: "pencil",
			onclick: () => show_opening_invoices(frm, party_type),
		},
		// only a draft has something to post, once no posting is running
		drafts.length &&
			!frm.posting?.[party_type] && {
				label: __("Post to books"),
				disabled: !!blocker,
				tooltip: blocker || undefined,
				onclick: () => post_from_card(frm, party_type),
			},
	];
}

function invoice_menu(frm, party_type) {
	const rows = frm.doc[PARTY[party_type].table] || [];
	return [
		rows.some((row) => row.status === "Created") && {
			label: __("Open posted invoices"),
			icon: "arrow-up-right",
			onclick: () =>
				frappe.set_route("List", PARTY[party_type].doctype, {
					company: frm.doc.company,
					is_opening: "Yes",
				}),
		},
		rows.length && {
			label: __("Download sheet"),
			icon: "download",
			onclick: () =>
				open_url_post(
					"/api/method/erpnext.setup.doctype.company_setup.opening_invoices.download_opening_invoices",
					{ company: frm.doc.company, party_type: party_type }
				),
		},
	].filter(Boolean);
}

function show_opening_invoices(frm, party_type, result, opts = {}) {
	call_method(frm, "get_invoice_names", { party_type: party_type }).then((r) =>
		make_opening_invoices_dialog(frm, party_type, r.message, result, opts)
	);
}

// Post from the tab: straight to the confirm, unless names still need an answer
function post_from_card(frm, party_type) {
	call_method(frm, "get_invoice_names", { party_type: party_type }).then((r) => {
		const names = r.message;
		const rows = get_draft_rows(frm, party_type, names);
		const kind_of = (row) => get_name_kind(row, names, new Set(names.disabled || []));
		if (rows.some((row) => OPEN_KINDS.includes(kind_of(row)))) {
			make_opening_invoices_dialog(frm, party_type, names, null, { post: true });
		} else {
			post_opening_invoices(frm, party_type, { rows: rows, names: names });
		}
	});
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
			return badge(__("New"), "orange");
		case "create":
			return badge(party_type === "Customer" ? __("New customer") : __("New supplier"), "gray");
		case "confirmed":
			return badge(__("Confirmed"), "green");
		case "disabled":
			return badge(__("Disabled"), "red");
		case "other_currency":
			return badge(currency || __("Other currency"), "red");
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
	};
}

function make_opening_invoices_dialog(frm, party_type, names, result, opts = {}) {
	const party = PARTY[party_type];
	const is_customer = party_type === "Customer";
	const rows = frm.doc[party.table] || [];
	const created = rows.filter((row) => row.status === "Created").length;
	const disabled = new Set(names.disabled || []);
	const other_currency = names.other_currency || {};
	const kind_of = (row) => get_name_kind(row, names, disabled);
	// several receivable or payable accounts: each invoice shows the one it goes to, and can change
	const accounts = names.accounts || [];
	const party_accounts = { ...names.party_accounts };
	const data = get_draft_rows(frm, party_type, names).map((row) => ({
		...row,
		name: undefined,
		account: accounts.length ? row.account || party_accounts[row.party] : undefined,
	}));
	// names confirmed in this window, shown as Confirmed in the names list
	const confirmed = new Set();
	const day_before = frm.doc.migration_date
		? frappe.datetime.str_to_user(frappe.datetime.add_days(frm.doc.migration_date, -1))
		: __("the day before migration");
	const dialog = new frappe.ui.Dialog({
		title: is_customer ? __("Opening sales invoices") : __("Opening purchase invoices"),
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
				on_delete_rows: () => refresh_state(),
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
							return `<span class="flex items-center gap-2 min-w-0">
								<span class="truncate">${frappe.form.formatters.Data(value, df)}</span>
								${badge ? `<span class="shrink-0">${badge}</span>` : ""}
							</span>`;
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
								row.account = null;
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
						onchange: function () {
							const row = this.doc;
							if (row?.party && row.account) set_party_account(row);
							refresh_state();
						},
					},
					{
						fieldname: "invoice_number",
						fieldtype: "Data",
						label: is_customer ? __("Invoice No") : __("Bill No"),
						in_list_view: 1,
						columns: 2,
					},
					{
						fieldname: "invoice_date",
						fieldtype: "Date",
						label: is_customer ? __("Invoice Date") : __("Bill Date"),
						in_list_view: 1,
						columns: 1,
					},
					{ fieldname: "due_date", fieldtype: "Date", label: __("Due Date") },
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
	const save = () =>
		call_method(frm, "save_opening_invoices", {
			party_type: party_type,
			rows: dialog.get_value("invoices") || [],
		}).then(() => frm.reload_doc());
	const grid = dialog.fields_dict.invoices.grid;
	dialog.fields_dict.invoices.$wrapper.addClass("company-setup-invoices");
	const currency = erpnext.get_currency(frm.doc.company);

	// one line and the sheet's buttons; the cards under them
	const $intro = dialog.get_field("intro").$wrapper;
	const menu = [
		...invoice_menu(frm, party_type).filter((item) => item.icon === "download"),
		names.new?.length &&
			names.can_create && {
				label: is_customer ? __("Download new customers") : __("Download new suppliers"),
				icon: "download",
				onclick: () => download_new_names(),
			},
	].filter(Boolean);
	const $sheet_buttons = $(`<div class="flex gap-1 shrink-0"></div>`).append(
		frappe.ui.button({
			label: __("Upload sheet"),
			icon: "upload",
			onclick: () => upload_opening_invoices(frm, party_type, dialog),
		}),
		menu.length
			? frappe.ui.dropdown({
					button: { icon: "ellipsis", variant: "ghost", attrs: { "aria-label": __("More") } },
					options: menu,
					align: "end",
			  })
			: []
	);
	$(`<div class="flex items-center justify-between gap-3 mb-3"></div>`)
		.append(
			$(`<div class="text-p-sm text-ink-gray-6"></div>`).text(
				is_customer
					? __(
							"Invoices still unpaid on {0}, one row each. Enter an advance as a negative amount.",
							[day_before]
					  )
					: __("Bills still unpaid on {0}, one row each. Enter an advance as a negative amount.", [
							day_before,
					  ])
			),
			$sheet_buttons
		)
		.appendTo($intro);
	const $stats = $(`<div class="es-stat-card-grid mb-3"></div>`).appendTo($intro);

	// what was read and left out, under the table
	const $notes = $(`<div class="flex flex-col gap-1 mt-2"></div>`).appendTo(
		dialog.get_field("notes").$wrapper
	);
	const grey = (text) => $(`<div class="text-p-sm text-ink-gray-5"></div>`).text(text).appendTo($notes);
	if (result?.advances) {
		grey(
			is_customer
				? result.advances === 1
					? __("1 negative row is an advance: money a customer paid before any invoice.")
					: __("{0} negative rows are advances: money customers paid before any invoice.", [
							result.advances,
					  ])
				: result.advances === 1
				? __("1 negative row is an advance: money you paid a supplier before any bill.")
				: __("{0} negative rows are advances: money you paid suppliers before any bill.", [
						result.advances,
				  ])
		);
	}
	if (result?.unused_columns?.length) {
		grey(
			is_customer
				? __("Not read: {0}. Only the name, invoice number, dates and amount are used.", [
						result.unused_columns.join(", "),
				  ])
				: __("Not read: {0}. Only the name, bill number, dates and amount are used.", [
						result.unused_columns.join(", "),
				  ])
		);
	}
	if (created && !result?.skipped) {
		grey(
			is_customer
				? __("{0} invoices are posted already and stay as they are.", [created])
				: __("{0} bills are posted already and stay as they are.", [created])
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
						? __("All {0} invoices on it are posted already, so none were added.", [skipped])
						: __("All {0} bills on it are posted already, so none were added.", [skipped])
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
		$review.append(
			$(`<div class="border rounded-lg py-10"></div>`).append(
				frappe.ui.empty_state({
					icon: "file-spreadsheet",
					title: is_customer
						? __("Upload your receivables report")
						: __("Upload your payables report"),
					description: is_customer
						? __(
								"Excel or CSV from Tally, Busy, Zoho or QuickBooks. Each unpaid invoice is read with its customer, number, date and amount."
						  )
						: __(
								"Excel or CSV from Tally, Busy, Zoho or QuickBooks. Each unpaid bill is read with its supplier, number, date and amount."
						  ),
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
								open_url_post(
									"/api/method/erpnext.setup.doctype.company_setup.opening_invoices.download_opening_invoices",
									{ company: frm.doc.company, party_type: party_type }
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
				? n === 1
					? __("1 customer")
					: __("{0} customers", [n])
				: n === 1
				? __("1 supplier")
				: __("{0} suppliers", [n]);
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
							? __("{0} customer names need a look before posting", [state.review.length])
							: __("{0} supplier names need a look before posting", [state.review.length])
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
					)
				),
				frappe.ui.button({
					label: __("Review names"),
					icon: "user-check",
					onclick: () => show_names(),
				})
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
		grid.wrapper.closest(".frappe-control").toggle(!empty);
		dialog.get_primary_btn().prop("disabled", empty);
		dialog.get_secondary_btn().toggle(!empty);
		$stats
			.empty()
			.toggle(!!state.names.length)
			.append(state.names.length ? make_opening_invoice_stats(frm, party_type, state, currency) : []);
		$review.append(make_skipped_box());
		if (empty) return show_empty();
		if (posting) return show_posting();
		// rows posting turned down for a reason of their own, not their name
		const failed = all.filter((row) => row.status === "Failed" && kind_of(row) === "found");
		if (failed.length) {
			$review.append(
				frappe.ui
					.alert({
						theme: "red",
						title: is_customer
							? __("{0} invoices could not be posted", [failed.length])
							: __("{0} bills could not be posted", [failed.length]),
						description: __(
							"Each shows Failed in the table; the reason is in the row. Fix them and post again."
						),
					})
					.addClass("mb-3")
			);
		}
		if (state.review.length) make_review_box(state);
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
	// an account belongs to the customer or supplier: one row's change is all its rows', with Undo
	const set_party_account = (row) => {
		const all = (dialog.get_value("invoices") || []).filter((other) => other.party === row.party);
		const previous = party_accounts[row.party];
		if (all.every((other) => other.account === row.account)) return;
		all.forEach((other) => (other.account = row.account));
		party_accounts[row.party] = row.account;
		grid.refresh();
		frappe.show_alert(
			{
				message:
					frappe.utils.escape_html(
						is_customer
							? __("All {0} invoices of {1} now use {2}.", [all.length, row.party, row.account])
							: __("All {0} bills of {1} now use {2}.", [all.length, row.party, row.account])
					) + ` <a class="underline" data-action="undo">${__("Undo")}</a>`,
				indicator: "green",
			},
			10,
			{
				undo: () => {
					all.forEach((other) => (other.account = previous));
					party_accounts[row.party] = previous;
					grid.refresh();
				},
			}
		);
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
				$(`<div class="text-p-sm text-ink-gray-5"></div>`).text(
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
								return `<span class="flex items-center gap-2 min-w-0">
									<span class="truncate">${frappe.form.formatters.Data(value, df)}</span>
									${badge ? `<span class="shrink-0">${badge}</span>` : ""}
								</span>`;
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
			dialog.set_primary_action(
				!actions.length
					? __("Confirm")
					: creates
					? is_customer
						? __("Confirm {0} · create {1} new customer", [actions.length, creates])
						: __("Confirm {0} · create {1} new supplier", [actions.length, creates])
					: __("Confirm {0}", [actions.length]),
				() => confirm_names()
			);
			dialog.get_primary_btn().prop("disabled", !actions.length);
		};
		const confirm_names = () => {
			const before = (dialog.get_value("invoices") || []).map((row) => ({
				row: row,
				party: row.party,
				create_party: row.create_party,
				account: row.account,
			}));
			let done = 0;
			for (const name of names_grid.get_selected_children()) {
				const action = get_action(name);
				if (!action) continue;
				set_rows_party(
					name.name_on_sheet,
					action === "link"
						? { party: name.party, create_party: 0, account: null }
						: { party: null, create_party: 1, account: null }
				);
				confirmed.add(name.name_on_sheet);
				done += 1;
			}
			if (!done) return;
			fill_accounts();
			const open = (dialog.get_value("invoices") || []).some((row) =>
				OPEN_KINDS.includes(kind_of(row))
			);
			frappe.show_alert(
				{
					message:
						frappe.utils.escape_html(
							done === 1 ? __("1 name confirmed.") : __("{0} names confirmed.", [done])
						) + ` <a class="underline" data-action="undo">${__("Undo")}</a>`,
					indicator: "green",
				},
				10,
				{
					undo: () => {
						before.forEach((saved) =>
							Object.assign(saved.row, {
								party: saved.party,
								create_party: saved.create_party,
								account: saved.account,
							})
						);
						confirmed.clear();
						grid.refresh();
						refresh_state();
					},
				}
			);
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
			open_url_post(
				"/api/method/erpnext.setup.doctype.company_setup.opening_invoices.download_new_parties",
				{ company: frm.doc.company, party_type: party_type }
			)
		);
	const post = () => {
		const all = dialog.get_value("invoices") || [];
		const open = new Set(
			all.filter((row) => OPEN_KINDS.includes(kind_of(row))).map((row) => row.party_name)
		);
		if (open.size) {
			post_after = true;
			show_names(
				open.size === 1
					? __("Confirm this name first, then post.")
					: __("Confirm these {0} names first, then post.", [open.size])
			);
			return;
		}
		const values = dialog.get_values();
		if (!values) return;
		save().then(() =>
			post_opening_invoices(frm, party_type, {
				rows: values.invoices || [],
				names: names,
				before_post: () => dialog.hide(),
			})
		);
	};
	const save_draft = () => {
		if (!dialog.get_values()) return;
		save().then(() => dialog.hide());
	};

	if (posting) {
		frm.posting_dialog = Object.assign(dialog, { party_type: party_type });
		dialog.set_df_property("invoices", "read_only", 1);
	}
	dialog.show();
	refresh_state();
	grid.refresh();
	if (opts.post) post();
}

function make_opening_invoice_stats(frm, party_type, state, currency) {
	const is_customer = party_type === "Customer";
	const total = state.invoices.reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
	const day_before =
		frm.doc.migration_date &&
		frappe.datetime.str_to_user(frappe.datetime.add_days(frm.doc.migration_date, -1));
	const count = state.names.length;
	return [
		frappe.ui.stat_card({
			label: __("On your sheet"),
			value: format_currency(total, currency),
			caption: state.advance
				? is_customer
					? __("Less {0} customers paid in advance", [format_currency(state.advance, currency)])
					: __("Less {0} paid to suppliers in advance", [format_currency(state.advance, currency)])
				: day_before && __("On {0}", [day_before]),
		}),
		frappe.ui.stat_card({
			label: is_customer ? __("Invoices") : __("Bills"),
			value: format_number(state.invoices.length, null, 0),
			caption:
				count === 1
					? __("From {0}", [state.names[0].name_on_sheet])
					: is_customer
					? __("From {0} customers", [format_number(count, null, 0)])
					: __("From {0} suppliers", [format_number(count, null, 0)]),
		}),
		frappe.ui.stat_card({
			label: is_customer ? __("Customers") : __("Suppliers"),
			value: state.review.length
				? __("{0} of {1} ready", [state.ready.length, count])
				: __("All {0} ready", [count]),
			caption: state.review.length ? __("{0} to review", [state.review.length]) : __("Ready to post"),
		}),
	];
}

function upload_opening_invoices(frm, party_type, dialog) {
	const drafts = (dialog.fields_dict.invoices.df.data || []).filter(
		(row) => row.party_name || flt(row.outstanding_amount)
	).length;
	const choose = () =>
		new frappe.ui.FileUploader({
			restrictions: { allowed_file_types: [".xlsx", ".csv"] },
			on_success: (file) =>
				call_method(frm, "import_opening_invoices", {
					party_type: party_type,
					file_url: file.file_url,
				}).then((r) => {
					dialog.hide();
					frm.reload_doc().then(() => show_opening_invoices(frm, party_type, r.message));
				}),
		});
	if (!drafts) return choose();
	frappe.confirm(
		__(
			"The sheet replaces the {0} invoices in this table, unsaved changes included. Posted invoices stay as they are. Continue?",
			[drafts]
		),
		choose
	);
}

// Ask before posting, saying what is posted and what stays a draft, then post and say how it went
function post_opening_invoices(frm, party_type, { rows, names = {}, before_post }) {
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
	const why_waiting = (row) =>
		!row.party && !row.create_party
			? "unconfirmed"
			: disabled.has(row.party)
			? "disabled"
			: other_currency[row.party]
			? "other_currency"
			: null;
	const posting = named.filter((row) => !why_waiting(row));
	const invoices = posting.filter((row) => flt(row.outstanding_amount) > 0);
	const waiting = named.filter(why_waiting);
	const unique = (list) => [...new Set(list)];
	const new_names = unique(
		named.filter((row) => !row.party && row.create_party).map((row) => row.party_name)
	);
	if (!posting.length) {
		frappe.msgprint(
			is_customer
				? __("None of these invoices can be posted yet: their customers need a look first.")
				: __("None of these bills can be posted yet: their suppliers need a look first.")
		);
		return;
	}
	const advances = posting.filter((row) => flt(row.outstanding_amount) < 0);
	const day_before = frappe.datetime.str_to_user(frappe.datetime.add_days(frm.doc.migration_date, -1));
	const list = (items) =>
		items.length > 3
			? __("{0} and {1} more", [items.slice(0, 3).join(", "), items.length - 3])
			: items.join(", ");
	const reasons = [
		["disabled", (parties) => __("{0} disabled", [list(parties)])],
		["other_currency", (parties) => __("{0} in another currency", [list(parties)])],
		[
			"unconfirmed",
			(parties) =>
				parties.length === 1
					? __("1 name not confirmed")
					: __("{0} names not confirmed", [parties.length]),
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
		invoices.length && [
			__("Posted now"),
			is_customer
				? __("{0} invoices · {1}", [invoices.length, money(invoices)])
				: __("{0} bills · {1}", [invoices.length, money(invoices)]),
		],
		advances.length && [
			__("Advances"),
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
	const note = [
		__("Dated {0}.", [day_before]),
		missing_year && __("The financial year {0} is added when you post.", [missing_year.year]),
		is_customer ? __("Nothing is sent to customers.") : __("Nothing is sent to suppliers."),
	]
		.filter(Boolean)
		.join(" ");
	const confirm = new frappe.ui.Dialog({
		title: is_customer ? __("Post opening invoices?") : __("Post opening bills?"),
		fields: [{ fieldname: "summary", fieldtype: "HTML" }],
		primary_action_label: __("Post to books"),
		primary_action: () => {
			confirm.hide();
			before_post?.();
			call_method(frm, "create_opening_invoices", { party_type: party_type }, { freeze: true }).then(
				(r) => {
					if (r.message === "queued") {
						frm.posting = { ...frm.posting, [party_type]: { done: 0, total: posting.length } };
						// the reply carries the document without what the page is drawn from
						frm.reload_doc();
						frappe.show_alert({
							message: is_customer
								? __(
										"Posting the invoices in the background. This page updates when they are done."
								  )
								: __(
										"Posting the bills in the background. This page updates when they are done."
								  ),
							indicator: "blue",
						});
					} else {
						frm.reload_doc().then(() => show_posting_result(frm, party_type, new_names));
					}
				}
			);
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
	$(`<div class="text-p-sm text-ink-gray-5 mt-3"></div>`).text(note).appendTo($summary);
	confirm.get_field("summary").$wrapper.append($summary);
	confirm.show();
}

// Say which new names were added and why any row stays a draft, not only in the table
function show_posting_result(frm, party_type, new_names = []) {
	const rows = frm.doc[PARTY[party_type].table] || [];
	const failed = rows.filter((row) => row.status === "Failed");
	const added = new_names.filter((name) => rows.some((row) => row.party_name === name && row.party));
	if (!failed.length) {
		frappe.show_alert({
			message: added.length
				? __("Posted. Added {0}.", [added.join(", ")])
				: party_type === "Customer"
				? __("Invoices posted.")
				: __("Bills posted."),
			indicator: "green",
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
				? failed.length === 1
					? __("1 invoice could not be posted")
					: __("{0} invoices could not be posted", [failed.length])
				: failed.length === 1
				? __("1 bill could not be posted")
				: __("{0} bills could not be posted", [failed.length]),
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
			icon: "import",
			onclick: () => open_party_import(frm, doctype),
		},
	];
}

function open_party_import(frm, doctype) {
	if (!frappe.model.can_create("Data Import")) return;
	frappe.require("data_import_tools.bundle.js", () =>
		frappe.data_import.open_data_import_dialog({
			reference_doctype: doctype,
			import_type: "Insert New Records",
			// the counts on the tasks change after an import
			on_close: () => frm.reload_doc(),
		})
	);
}

function show_chart(company) {
	const dialog = new frappe.ui.Dialog({
		title: __("Chart of Accounts"),
		size: "large",
		fields: [{ fieldname: "chart", fieldtype: "HTML" }],
		primary_action_label: __("Done"),
		primary_action: () => dialog.hide(),
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

function download_chart_sheet(company, template) {
	open_url_post("/api/method/erpnext.setup.doctype.company_setup.chart_sheet.download_chart_template", {
		company: company,
		template: template,
	});
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

	let last_preview;
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
		$(`<span class="text-p-sm text-ink-gray-5"></span>`).text(__("Start from")),
		frappe.ui.button({
			label: __("the sample sheet"),
			icon: "download",
			variant: "ghost",
			size: "sm",
			onclick: () => download_chart_sheet(company, "Sample Template"),
		}),
		$(`<span class="text-p-sm text-ink-gray-5"></span>`).text(__("or")),
		frappe.ui.button({
			label: __("your current chart"),
			icon: "download",
			variant: "ghost",
			size: "sm",
			onclick: () => download_chart_sheet(company, "Current Chart"),
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
		call_method(frm, "preview_chart", { file_url: file_url }).then((r) => {
			// another file was picked meanwhile
			if (dialog.get_value("file") !== file_url) return;
			last_preview = r.message;
			render_import_preview(frm, $review, $new_chart, r.message, company, file_url, () => {
				dialog.set_value("file", "");
			});
			show_step(1);
		});
	};
	const replace = () =>
		frappe.confirm(
			__(
				"Delete the {0} accounts and tax templates of {1} and make the chart from your sheet? This can't be undone.",
				[last_preview?.current_accounts, company]
			),
			() =>
				call_method(
					frm,
					"import_chart",
					{ file_url: dialog.get_value("file") },
					{ freeze: true, freeze_message: __("Importing the chart...") }
				).then((r) => {
					dialog.hide();
					frm.reload_doc();
					const added = r.message?.added_accounts || [];
					frappe.show_alert(
						{
							message: added.length
								? __("Chart imported. Added accounts ERPNext needs: {0}", [added.join(", ")])
								: __("Chart imported"),
							indicator: "green",
						},
						added.length ? 10 : 5
					);
				})
		);

	dialog.show();
	show_step(0);
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
		frappe.ui.stat_card({
			label: __("Accounts"),
			value: format_number(data.accounts.length, null, 0),
			caption: data.added_accounts?.length
				? __("Replaces your {0} · ERPNext adds {1}", [
						data.current_accounts,
						data.added_accounts.join(", "),
				  ])
				: __("Replaces your {0}", [data.current_accounts]),
		}),
		frappe.ui.stat_card({
			label: __("Opening balances"),
			value: format_number(data.balances || 0, null, 0),
			caption: data.balances
				? data.draft_balances
					? __("Replace your {0} drafts", [data.draft_balances])
					: __("Added as drafts, not posted")
				: data.draft_balances
				? __("Your {0} drafts stay", [data.draft_balances])
				: __("None on the sheet"),
		}),
	];
	if (left_out_total) {
		cards.push(
			frappe.ui.stat_card({
				label: __("Left out"),
				value: format_currency(left_out_total, currency),
				caption: left_out.Stock
					? __("Customers, suppliers and stock come in their own way")
					: __("Customers and suppliers come in as invoices and bills"),
			})
		);
	}
	$(`<div class="es-stat-card-grid"></div>`).append(cards).appendTo($body);

	$body.append(
		frappe.ui.alert({
			theme: "yellow",
			title: __("Replaces your {0} accounts and tax templates", [data.current_accounts]),
			description: __("Your sheet becomes the chart. The company's default accounts are set again."),
		})
	);
	const left_out_note = left_out_text(frm, left_out);
	if (left_out_note) {
		$(`<div class="text-p-sm text-ink-gray-5"></div>`).text(left_out_note).appendTo($body);
	}
	const difference = data.balances ? flt(data.debit - data.credit, 2) : 0;
	if (difference) {
		$(`<div class="text-p-sm text-ink-gray-5"></div>`)
			.text(
				__(
					"Debit and credit on the sheet differ by {0}. It shows in Opening balances until the invoices are in.",
					[format_currency(Math.abs(difference), currency)]
				)
			)
			.appendTo($body);
	}
	if (!data.balances && data.draft_balances) {
		$(`<div class="text-p-sm text-ink-gray-5"></div>`)
			.text(
				__(
					"Your draft balances move to the new accounts with the same name. Check them after the import."
				)
			)
			.appendTo($body);
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
		<div class="company-lines grid gap-3" style="grid-template-columns: repeat(2, minmax(0, 1fr))"></div>
	</div>`);

	$card.find(".company-logo").html(
		frappe.ui.avatar.html({
			image: details.company_logo,
			label: details.company_name,
			size: "xl",
			shape: "square",
		})
	);
	$card.find(".company-name").text(details.company_name || frm.doc.company);
	$card
		.find(".company-meta")
		.text([details.abbr, details.country, details.default_currency].filter(Boolean).join(" · "));
	$card.find(".company-edit").append(
		// date format, time zone and the rest are set for the whole site
		frappe.model.can_read("System Settings")
			? frappe.ui.button({
					icon: "settings",
					variant: "ghost",
					tooltip: __("System Settings"),
					onclick: () => {
						frappe.set_route("Form", "System Settings");
					},
			  })
			: [],
		frappe.ui.button({
			label: __("Edit"),
			icon: "pencil",
			onclick: () => edit_company_details(frm, details),
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
		if (full_width) $line.css("grid-column", "1 / -1");
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
				dialog.hide();
				frm.reload_doc();
			}),
	});
	dialog.set_values(details);
	dialog.show();
}
