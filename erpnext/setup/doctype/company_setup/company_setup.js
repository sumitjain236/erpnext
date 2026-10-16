frappe.ui.form.on("Company Setup", {
	setup(frm) {
		// invoices made in the background
		frappe.realtime.off("company_setup_invoices");
		frappe.realtime.on("company_setup_invoices", (data) => {
			if (data.company !== frm.doc.company) return;
			if (data.error) {
				frappe.msgprint({ title: __("Invoices not posted"), message: data.error, indicator: "red" });
			}
			frm.reload_doc().then(() => !data.error && show_posting_result(frm, data.party_type));
		});
	},

	refresh(frm) {
		if (frm.is_new()) return;

		frm.events.render(frm);
		if (frm.reopen_opening_invoices) {
			show_opening_invoices(frm, frm.reopen_opening_invoices);
			delete frm.reopen_opening_invoices;
		}
		// a step done or undone since the last save, from the company's data, is saved now
		if (frm.doc.__onload?.steps_changed) {
			frm.call("check_steps").then((r) => r.message && frm.reload_doc());
		}
	},

	async render(frm) {
		await frappe.require(["desk_charts.bundle.js", "desk_charts.bundle.css"]);
		const steps = get_steps(frm);
		make_page(frm, "onboarding_html").append(
			make_company_card(frm),
			make_live_note(frm),
			make_overview_stats(frm),
			make_checklist(frm, steps)
		);
		for (const tab of ["accounts_tab", "selling_tab", "buying_tab"]) {
			const field = tab.replace("_tab", "_html");
			make_page(frm, field).append(
				TAB_STATS[tab]?.(frm) || [],
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

function make_overview_stats(frm) {
	const stats = frm.doc.__onload?.stats || {};
	const currency = erpnext.get_currency(frm.doc.company);
	const migration_date = frm.doc.migration_date;
	return $(`<div class="es-stat-card-grid"></div>`).append(
		frappe.ui.stat_card({
			label: __("Migration date"),
			value: migration_date ? frappe.datetime.str_to_user(migration_date) : null,
			caption:
				migration_date &&
				__("Balances as on {0}", [
					frappe.datetime.str_to_user(frappe.datetime.add_days(migration_date, -1)),
				]),
		}),
		frappe.ui.stat_card({
			label: __("Customers · Suppliers"),
			value: `${format_number(stats.customers, null, 0)} · ${format_number(stats.suppliers, null, 0)}`,
		}),
		frappe.ui.stat_card({
			label: __("Left in Temporary Opening"),
			value: format_currency(Math.abs(stats.temporary_opening || 0), currency),
			caption: __("Should be zero when you finish"),
		})
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

// more new names than this are better added from a sheet
const ADD_ONE_BY_ONE_LIMIT = 10;

function make_invoice_stats(frm, party_type) {
	const stats = frm.doc.__onload?.stats || {};
	const currency = erpnext.get_currency(frm.doc.company);
	const all_rows = frm.doc[PARTY[party_type].table] || [];
	// advances are negative rows; they aren't invoices and are shown apart
	const rows = all_rows.filter((row) => flt(row.outstanding_amount) > 0);
	const advance = -all_rows
		.filter((row) => flt(row.outstanding_amount) < 0)
		.reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
	const created = rows.filter((row) => row.status === "Created");
	const total = rows.reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
	const parties = new Set(rows.map((row) => row.party_name));
	return $(`<div class="es-stat-card-grid"></div>`).append(
		frappe.ui.stat_card({
			label: party_type === "Customer" ? __("Customers") : __("Suppliers"),
			value: format_number(party_type === "Customer" ? stats.customers : stats.suppliers, null, 0),
		}),
		frappe.ui.stat_card({
			label: __("Unpaid invoices"),
			value: rows.length ? format_number(rows.length, null, 0) : null,
			caption:
				rows.length &&
				(created.length === rows.length
					? __("From {0} names · posted", [parties.size])
					: __("From {0} names · {1} not posted yet", [
							parties.size,
							rows.length - created.length,
					  ])),
		}),
		frappe.ui.stat_card({
			label: PARTY[party_type].total_label,
			value: format_currency(total, currency),
			caption:
				advance &&
				(party_type === "Customer"
					? __("Less {0} customers paid in advance", [format_currency(advance, currency)])
					: __("Less {0} paid to suppliers in advance", [format_currency(advance, currency)])),
		})
	);
}

const TAB_STATS = {
	selling_tab: (frm) => make_invoice_stats(frm, "Customer"),
	buying_tab: (frm) => make_invoice_stats(frm, "Supplier"),
	accounts_tab: (frm) => {
		const stats = frm.doc.__onload?.stats || {};
		const currency = erpnext.get_currency(frm.doc.company);
		const rows = frm.doc.opening_balances || [];
		const posted = rows.filter((row) => row.status === "Posted").length;
		const total = (field) => rows.reduce((sum, row) => sum + flt(row[field]), 0);
		return $(`<div class="es-stat-card-grid"></div>`).append(
			frappe.ui.stat_card({
				label: __("Opening balances"),
				value: rows.length ? __("{0} accounts", [rows.length]) : null,
				caption: rows.length && (posted === rows.length ? __("Posted") : __("Not posted yet")),
			}),
			frappe.ui.stat_card({
				label: __("Debit"),
				value: format_currency(total("debit"), currency),
				caption: __("Credit {0}", [format_currency(total("credit"), currency)]),
			}),
			frappe.ui.stat_card({
				label: __("Left in Temporary Opening"),
				value: format_currency(Math.abs(stats.temporary_opening || 0), currency),
				caption: __("Should be zero when you finish"),
			})
		);
	},
};

// Every task with its status, in any order; each one is done on its own tab
function make_checklist(frm, steps) {
	const done = steps.filter((step) => step.is_complete).length;
	const skipped = steps.filter((step) => step.is_skipped).length;
	const $card = $(`<div class="contact-box flex flex-col gap-2 mb-0"></div>`);
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
	for (const step of steps) {
		const $row = $(`<div class="border-t py-3"></div>`).appendTo($card);
		$row.append(make_task(frm, step, get_overview_buttons(frm, step)));
	}
	return $card;
}

function make_task_card(frm, step) {
	const make_actions = STEP_ACTIONS[step.name];
	return $(`<div class="contact-box mb-0"></div>`).append(
		make_task(frm, step, make_actions ? make_actions(frm, step) : [])
	);
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
	const $table = $(`<div class="flex flex-col gap-2"></div>`);
	for (const [software, where] of places) {
		$(`<div class="flex flex-col gap-0.5"></div>`)
			.append($(`<div class="text-p-sm text-ink-gray-5"></div>`).text(software))
			.append($(`<div class="text-p-sm text-ink-gray-8"></div>`).text(where))
			.appendTo($table);
	}
	if (frm.doc.migration_date) {
		$(`<div class="text-p-sm text-ink-gray-5 border-t pt-2"></div>`)
			.text(
				__("Take it as on {0}, the day before you start.", [
					frappe.datetime.str_to_user(frappe.datetime.add_days(frm.doc.migration_date, -1)),
				])
			)
			.appendTo($table);
	}
	return $table[0];
}

// Status icon, title, one line on what's done or left, and the buttons
function make_task(frm, step, buttons) {
	const [icon, color] = step.is_complete
		? ["circle-check", "text-ink-gray-8"]
		: step.is_skipped
		? ["circle-minus", "text-ink-gray-5"]
		: step.is_started
		? ["circle-dot", "text-ink-gray-8"]
		: ["circle-dashed", "text-ink-gray-5"];
	const note = step.is_skipped ? __("Skipped for now.") : step.summary || step.description;

	// more than two buttons go on a row of their own, so the text keeps its width
	const stacked = buttons.length > 2;
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
		buttons.forEach((opts) => $actions.append(frappe.ui.button(opts)));
		$task.append($actions);
	}
	return $task;
}

function get_overview_buttons(frm, step) {
	if (OVERVIEW_ACTIONS[step.name]) return OVERVIEW_ACTIONS[step.name](frm, step);
	if (!step.tab) return [];

	const buttons = [
		{
			label: __("Go to {0}", [__(frappe.meta.get_label(frm.doctype, step.tab))]),
			icon_right: "arrow-right",
			onclick: () => show_tab(frm, step.tab),
		},
	];
	if (step.is_skipped) {
		buttons.unshift({ label: __("Undo skip"), variant: "ghost", onclick: () => skip_step(frm, step, 0) });
	} else if (!step.is_complete && !step.is_started) {
		buttons.unshift({ label: __("Skip"), variant: "ghost", onclick: () => skip_step(frm, step, 1) });
	}
	return buttons;
}

function show_tab(frm, fieldname) {
	frm.layout.tabs.find((tab) => tab.df.fieldname === fieldname)?.set_active();
	frappe.utils.scroll_to(0);
}

function skip_step(frm, step, skip) {
	return frm.call("skip_step", { step_key: step.key, skip: skip }).then(() => frm.reload_doc());
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

	const $reports = $(`<div class="flex flex-col gap-1"></div>`);
	$(`<div class="text-base text-ink-gray-8"></div>`)
		.text(__("Keep these three reports ready, all as on the day before you start:"))
		.appendTo($reports);
	const $list = $(`<ol class="text-base text-ink-gray-8 ps-5 mb-0"></ol>`).appendTo($reports);
	for (const report of [
		__("Trial balance"),
		__("Unpaid customer invoices (who owes you, invoice by invoice)"),
		__("Unpaid supplier bills (whom you owe, bill by bill)"),
	]) {
		$("<li></li>").text(report).appendTo($list);
	}
	$(`<div class="text-p-sm text-ink-gray-5 pt-1"></div>`)
		.text(
			__(
				"You can also bring your customer and supplier lists. If you don't, names are created from the unpaid invoices."
			)
		)
		.appendTo($reports);
	dialog.get_field("reports").$wrapper.append($reports);

	const $where = $(`<div class="flex flex-col gap-3"></div>`);
	for (const [title, task_name] of [
		[__("Trial balance"), "opening_balances"],
		[__("Unpaid customer invoices"), "opening_sales_invoices"],
		[__("Unpaid supplier bills"), "opening_purchase_invoices"],
	]) {
		$(`<div class="flex flex-col gap-1"></div>`)
			.append($(`<div class="text-sm-medium text-ink-gray-8"></div>`).text(title))
			.append(
				WHERE_TO_FIND[task_name].map(([software, where]) =>
					$(`<div class="text-p-sm text-ink-gray-8"></div>`).append(
						$(`<span class="text-ink-gray-5"></span>`).text(`${software}: `),
						document.createTextNode(where)
					)
				)
			)
			.appendTo($where);
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
	["Balance Sheet", __("Balance Sheet")],
	["Profit and Loss Statement", __("Profit and Loss Statement")],
	["Trial Balance", __("Trial Balance")],
];

function report_buttons(frm, reports) {
	return reports.map(([report, label]) => ({
		label: label,
		icon_right: "arrow-up-right",
		onclick: () => frappe.set_route("query-report", report, { company: frm.doc.company }),
	}));
}

// Buttons on the Overview checklist, where a task is not just a link to its tab
const OVERVIEW_ACTIONS = {
	migration_date: (frm, step) => [
		{
			label: step.is_complete ? __("Change") : __("Set migration date"),
			onclick: () => show_migration_date(frm),
		},
	],
	check_books: (frm) => [see_books_button(frm), ...report_buttons(frm, REPORTS.slice(0, 2))],
	go_live: (frm, step) =>
		step.is_complete ? [] : [{ label: __("Go live"), onclick: () => show_go_live(frm) }],
};

function see_books_button(frm) {
	return {
		label: __("See your books"),
		disabled: !frm.doc.migration_date,
		tooltip: frm.doc.migration_date ? undefined : __("Set the migration date first."),
		onclick: () => show_posting_preview(frm, false),
	};
}

// Once live, money still in Temporary Opening stays pointed out until it is explained
function make_live_note(frm) {
	const temporary = frm.doc.__onload?.stats?.temporary_opening;
	if (frm.doc.status !== "Live" || !temporary) return [];
	return frappe.ui.alert({
		theme: "yellow",
		title: __("Live · {0} still in Temporary Opening", [
			format_currency(Math.abs(temporary), erpnext.get_currency(frm.doc.company)),
		]),
		description: __(
			"Something from your old books is still missing, such as stock, an advance or a balance left out of your sheet. Add it and this goes away."
		),
		footer: () => $(`<div class="flex gap-2"></div>`).append(frappe.ui.button(see_books_button(frm))),
	});
}

// What is done and what is left; money left in Temporary Opening needs a tick, nothing else stops going live
function show_go_live(frm) {
	const steps = get_steps(frm).filter((step) => step.name !== "go_live");
	const stats = frm.doc.__onload?.stats || {};
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
			frm.call("go_live", { accept_temporary_opening: values.accept_temporary_opening }).then(() => {
				dialog.hide();
				frappe.show_alert({ message: __("{0} is live", [frm.doc.company]), indicator: "green" });
				frm.reload_doc();
			}),
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
					? __("Only money left in Temporary Opening needs a tick.")
					: __("Nothing here stops you."),
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
	const $list = $(`<div class="flex flex-col"></div>`).appendTo($body);
	for (const step of steps) {
		$list.append($(`<div class="border-t py-2"></div>`).append(make_task(frm, step, [])));
	}
	if (stats.temporary_opening) {
		$body.append(
			frappe.ui.alert({
				theme: "red",
				title: __("{0} is left in Temporary Opening", [temporary]),
				description: __(
					"Your opening books don't add up yet: something from your old books is missing, such as stock, an advance or a balance left out of your sheet. You can still go live and add it later."
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
			return [
				{
					label: __("Edit chart"),
					icon: "pencil",
					onclick: () => show_chart(frm.doc.company),
				},
				{
					label: __("Change"),
					variant: "ghost",
					onclick: () => frm.call("change_chart_choice").then(() => frm.reload_doc()),
				},
			];
		}
		return [
			{
				label: __("Use the existing chart"),
				icon: "list-tree",
				onclick: () => frm.call("use_existing_chart").then(() => frm.reload_doc()),
			},
			{
				label: __("Import a sheet"),
				icon: "import",
				disabled: !!frm.doc.__onload?.chart_import_blocked,
				tooltip: frm.doc.__onload?.chart_import_blocked,
				onclick: () => show_chart_import(frm),
			},
			{ label: __("From Tally"), disabled: true, tooltip: __("Coming soon") },
		];
	},
	opening_balances: (frm) => {
		const drafts = (frm.doc.opening_balances || []).filter((row) => row.status !== "Posted");
		const blocker = !drafts.length
			? __("Enter some balances first.")
			: !frm.doc.migration_date && __("Set the migration date first.");
		return [
			{
				label: drafts.length ? __("Edit balances") : __("Enter balances"),
				icon: "pencil",
				onclick: () => show_opening_balances(frm),
			},
			{
				label: __("Preview and post"),
				disabled: !!blocker,
				tooltip: blocker || undefined,
				onclick: () => show_posting_preview(frm),
			},
		];
	},
	opening_sales_invoices: (frm) => invoice_buttons(frm, "Customer"),
	opening_purchase_invoices: (frm) => invoice_buttons(frm, "Supplier"),
	customers: (frm) => import_buttons(frm, "Customer"),
	suppliers: (frm) => import_buttons(frm, "Supplier"),
	check_books: (frm) => [see_books_button(frm), ...report_buttons(frm, REPORTS)],
};

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
			{ fieldname: "totals", fieldtype: "HTML" },
			...where_to_find_fields(frm, "opening_balances", {
				has_data: (frm.doc.opening_balances || []).length > posted.length,
				posted: posted.length,
			}),
		],
		primary_action_label: __("Save"),
		primary_action: () => {
			const rows = dialog.get_values().balances || [];
			frm.call("save_opening_balances", { rows: rows }).then(() => {
				dialog.hide();
				frm.reload_doc();
			});
		},
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
			$(`<div class="flex gap-2 shrink-0"></div>`).append(
				frappe.ui.button({
					label: __("Add account"),
					icon: "plus",
					onclick: () =>
						frappe.model.with_doctype("Account", () => add_balance_account(frm, dialog)),
				}),
				frappe.ui.button({
					label: __("Upload sheet"),
					icon: "upload",
					onclick: () => upload_opening_balances(frm, dialog),
				}),
				frappe.ui.button({
					label: __("Download sheet"),
					icon: "download",
					variant: "ghost",
					onclick: () =>
						open_url_post(
							"/api/method/erpnext.setup.doctype.company_setup.chart_sheet.download_opening_balances",
							{ company: company }
						),
				})
			)
		)
		.appendTo($intro);
	if (result) $intro.append(make_upload_result(frm, dialog, result));
	const year_note = make_fiscal_year_note(frm);
	if (year_note) $(`<div class="mb-3"></div>`).append(year_note).appendTo($intro);
	if (posted.length) {
		$(`<div class="text-p-sm text-ink-gray-5 mb-3"></div>`)
			.text(__("{0} balances are posted already and stay as they are.", [posted.length]))
			.appendTo($intro);
	}

	const currency = erpnext.get_currency(company);
	const update_totals = () => {
		const rows = dialog.fields_dict.balances.df.data || [];
		const debit = rows.reduce((sum, row) => sum + flt(row.debit), 0);
		const credit = rows.reduce((sum, row) => sum + flt(row.credit), 0);
		const $totals = dialog.get_field("totals").$wrapper.empty();
		$(`<div class="flex flex-wrap justify-between gap-2 text-p-sm"></div>`)
			.append(
				$(`<span class="text-ink-gray-5"></span>`).text(
					__(
						"Debit and credit don't have to match: the difference waits in Temporary Opening until invoices are in."
					)
				),
				$(`<span class="text-sm-medium text-ink-gray-8"></span>`).text(
					__("Debit {0} · Credit {1}", [
						format_currency(debit, currency),
						format_currency(credit, currency),
					])
				)
			)
			.appendTo($totals);
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
			frm.call("import_opening_balances", { file_url: file.file_url }).then((r) => {
				dialog.hide();
				frm.reload_doc().then(() => show_opening_balances(frm, r.message));
			}),
	});
}

// What the upload did, and where what it left out goes
// Amounts a sheet held for customers, suppliers or stock, which come in their own way
function make_left_out_note(frm, left_out = {}, go) {
	const currency = erpnext.get_currency(frm.doc.company);
	const money = (amount) => format_currency(Math.abs(amount), currency);
	const parts = [
		left_out.Receivable && __("customers {0}", [money(left_out.Receivable)]),
		left_out.Payable && __("suppliers {0}", [money(left_out.Payable)]),
		left_out.Stock && __("stock {0}", [money(left_out.Stock)]),
	].filter(Boolean);
	if (!parts.length) return [];

	const description = [];
	if (left_out.Receivable || left_out.Payable) {
		description.push(
			__(
				"Customers and suppliers come in as opening invoices in Selling and Buying, so each bill is kept."
			)
		);
	}
	if (left_out.Stock) {
		description.push(
			__("Stock needs an opening stock entry; until then its amount stays in Temporary Opening.")
		);
	}
	return frappe.ui.alert({
		theme: "yellow",
		title: __("Left out of your sheet: {0}", [parts.join(", ")]),
		description: description.join(" "),
		footer:
			go &&
			(() =>
				$(`<div class="flex gap-2"></div>`).append(
					left_out.Receivable
						? frappe.ui.button({ label: __("Go to Selling"), onclick: go("selling_tab") })
						: [],
					left_out.Payable
						? frappe.ui.button({ label: __("Go to Buying"), onclick: go("buying_tab") })
						: []
				)),
	});
}

function make_upload_result(frm, dialog, result) {
	const $result = $(`<div class="flex flex-col gap-2 mb-3"></div>`);
	const go = (tab) => () => {
		dialog.hide();
		show_tab(frm, tab);
	};
	$result.append(make_left_out_note(frm, result.left_out, go));
	if (result.unmatched?.length) {
		$result.append(
			frappe.ui.alert({
				theme: "yellow",
				title: __("Pick an account for {0} rows", [result.unmatched.length]),
				description: __("These names are not in your chart: {0}", [result.unmatched.join(", ")]),
			})
		);
	}
	if (result.groups) {
		$(`<div class="text-p-sm text-ink-gray-5"></div>`)
			.text(__("{0} group lines were skipped: they only add up their sub-accounts.", [result.groups]))
			.appendTo($result);
	}
	return $result;
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

// The books on the day before the migration date; with can_post, as they will be after posting
function show_posting_preview(frm, can_post = true) {
	frm.call("preview_opening_balances").then((r) => {
		const preview = r.message;
		const currency = erpnext.get_currency(frm.doc.company);
		const dialog = new frappe.ui.Dialog({
			title: __("Your books on {0}", [frappe.datetime.str_to_user(preview.posting_date)]),
			size: "large",
			fields: [{ fieldname: "preview", fieldtype: "HTML" }],
			primary_action_label: can_post ? __("Post") : __("Done"),
			primary_action: () =>
				can_post
					? frm.call({ doc: frm.doc, method: "post_opening_balances", freeze: true }).then(() => {
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
		dialog.get_field("preview").$wrapper.append(make_posting_preview(preview, currency));
		dialog.show();
	});
}

function make_posting_preview(preview, currency) {
	const temporary = preview.rows.find((row) => row.account === preview.temporary_account);
	const rows = preview.rows.filter((row) => row !== temporary);
	const of = (...root_types) => rows.filter((row) => root_types.includes(row.root_type));
	const sum = (list, sign = 1) => list.reduce((total, row) => total + sign * row.balance, 0);
	const income = sum(of("Income"), -1);
	const expenses = sum(of("Expense"));
	const profit = income - expenses;
	const money = (value) => format_currency(value, currency);
	// each account sits on the side its balance falls on, so Input GST (a debit under taxes) is an asset
	const balance_sheet = of("Asset", "Liability", "Equity");
	const debits = balance_sheet.filter((row) => row.balance > 0);
	const credits = balance_sheet.filter((row) => row.balance < 0);

	const $body = $(`<div class="flex flex-col gap-3"></div>`);
	$(`<div class="text-p-sm text-ink-gray-6"></div>`)
		.text(
			preview.drafts || preview.draft_invoices
				? __(
						"Includes {0} balances and {1} invoices not posted yet. This is what the books will show once everything is posted.",
						[preview.drafts, preview.draft_invoices]
				  )
				: __("What the books show on the day before migration, with everything posted so far.")
		)
		.appendTo($body);
	if (preview.left_out_invoices) {
		$(`<div class="text-p-sm text-ink-gray-6"></div>`)
			.text(
				preview.left_out_invoices === 1
					? __(
							"1 invoice is left out: its customer or supplier isn't in ERPNext yet, or is disabled."
					  )
					: __(
							"{0} invoices are left out: their customer or supplier isn't in ERPNext yet, or is disabled.",
							[preview.left_out_invoices]
					  )
			)
			.appendTo($body);
	}
	if (temporary) {
		$body.append(
			frappe.ui.alert({
				theme: "yellow",
				title: __("{0} is left in Temporary Opening", [money(Math.abs(temporary.balance))]),
				description: __(
					"It becomes zero once everything is in: usually the customer and supplier invoices, or a balance missing from your sheet. Nothing stops you meanwhile."
				),
			})
		);
	}

	const tabs = [
		{
			label: __("Balance Sheet"),
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
			content: () => make_trial_balance(preview.rows, money),
		},
	];
	if (of("Income", "Expense").length) {
		tabs.splice(1, 0, {
			label: __("Profit and Loss"),
			content: () =>
				$(`<div class="flex gap-6 pt-3"></div>`).append(
					make_statement_side(__("Income"), of("Income"), -1, money),
					make_statement_side(__("Expenses"), of("Expense"), 1, money, [
						[profit >= 0 ? __("Profit") : __("Loss"), Math.abs(profit)],
					])
				),
		});
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
	const blocker = !drafts.length
		? __("Enter some invoices first.")
		: !frm.doc.migration_date && __("Set the migration date first.");
	const buttons = [
		{
			label: drafts.length ? __("Edit invoices") : __("Enter invoices"),
			icon: "pencil",
			onclick: () => show_opening_invoices(frm, party_type),
		},
		{
			label: __("Post to books"),
			disabled: !!blocker,
			tooltip: blocker || undefined,
			onclick: () => post_opening_invoices(frm, party_type, { rows: drafts }),
		},
	];
	if (rows.some((row) => row.status === "Created")) {
		buttons.push({
			label: __("Open invoices"),
			variant: "ghost",
			icon_right: "arrow-up-right",
			onclick: () =>
				frappe.set_route("List", PARTY[party_type].doctype, {
					company: frm.doc.company,
					is_opening: "Yes",
				}),
		});
	}
	return buttons;
}

function show_opening_invoices(frm, party_type, result) {
	frm.call("get_invoice_names", { party_type: party_type }).then((r) =>
		make_opening_invoices_dialog(frm, party_type, r.message, result)
	);
}

function make_opening_invoices_dialog(frm, party_type, names, result) {
	const party = PARTY[party_type];
	const rows = frm.doc[party.table] || [];
	const created = rows.filter((row) => row.status === "Created").length;
	const disabled = new Set(names.disabled || []);
	const data = rows
		.filter((row) => row.status !== "Created")
		.map((row) => ({ ...row, name: undefined, party: row.party || names.found?.[row.party_name] }));
	const dialog = new frappe.ui.Dialog({
		title: party_type === "Customer" ? __("Opening sales invoices") : __("Opening purchase invoices"),
		size: "extra-large",
		fields: [
			{ fieldname: "intro", fieldtype: "HTML" },
			{
				fieldname: "invoices",
				fieldtype: "Table",
				label: __("Unpaid invoices"),
				in_place_edit: true,
				on_delete_rows: () => refresh_state(),
				data: data,
				fields: [
					{
						fieldname: "party_name",
						fieldtype: "Data",
						label: __("From sheet"),
						in_list_view: 1,
						columns: 3,
						formatter: (value, df, options, doc) => {
							const badge = value && get_party_badge(doc, names, disabled);
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
							this.grid_row?.refresh_field("party_name");
							refresh_state();
						},
					},
					{
						fieldname: "invoice_number",
						fieldtype: "Data",
						label: party_type === "Customer" ? __("Invoice No") : __("Bill No"),
						in_list_view: 1,
						columns: 2,
					},
					{
						fieldname: "invoice_date",
						fieldtype: "Date",
						label: __("Invoice Date"),
						in_list_view: 1,
						columns: 1,
					},
					{ fieldname: "due_date", fieldtype: "Date", label: __("Due Date") },
					{
						fieldname: "outstanding_amount",
						fieldtype: "Currency",
						label: __("Outstanding"),
						in_list_view: 1,
						columns: 2,
						reqd: 1,
						onchange: () => refresh_state(),
					},
				],
			},
			...where_to_find_fields(frm, party.table, { has_data: data.length, posted: created }),
			{ fieldname: "add_missing_section", fieldtype: "Section Break" },
			{
				fieldname: "add_missing",
				fieldtype: "Check",
				description:
					party_type === "Customer"
						? __(
								"Each is saved like any new customer. If one can't be saved, its invoices stay as drafts and you see why."
						  )
						: __(
								"Each is saved like any new supplier. If one can't be saved, its bills stay as drafts and you see why."
						  ),
			},
		],
		primary_action_label: __("Post to books"),
		primary_action: () => {
			const values = dialog.get_values();
			if (!values) return;
			save().then(() =>
				post_opening_invoices(frm, party_type, {
					rows: values.invoices || [],
					// neither a disabled party's invoices nor those in another currency are posted
					disabled: new Set([...disabled, ...Object.keys(names.other_currency || {})]),
					add_missing: !!values.add_missing,
					before_post: () => dialog.hide(),
				})
			);
		},
		secondary_action_label: __("Save draft"),
		secondary_action: () => {
			if (!dialog.get_values()) return;
			save().then(() => dialog.hide());
		},
	});
	const save = () =>
		frm
			.call("save_opening_invoices", {
				party_type: party_type,
				rows: dialog.get_value("invoices") || [],
			})
			.then(() => frm.reload_doc());

	const $intro = dialog.get_field("intro").$wrapper;
	$(`<div class="flex flex-wrap items-center justify-between gap-2 mb-3"></div>`)
		.append(
			$(`<div class="text-p-sm text-ink-gray-6"></div>`).text(
				__(
					"Every invoice still unpaid on the day before migration, from your {0} report. Each one becomes an opening invoice, so payments can be matched to it later. Enter an advance as a negative amount.",
					[party.report]
				)
			),
			$(`<div class="flex gap-2 shrink-0"></div>`).append(
				frappe.ui.button({
					label: __("Upload sheet"),
					icon: "upload",
					onclick: () => upload_opening_invoices(frm, party_type, dialog),
				}),
				frappe.ui.button({
					label: __("Download sheet"),
					icon: "download",
					variant: "ghost",
					onclick: () =>
						open_url_post(
							"/api/method/erpnext.setup.doctype.company_setup.opening_invoices.download_opening_invoices",
							{ company: frm.doc.company, party_type: party_type }
						),
				})
			)
		)
		.appendTo($intro);
	const $stats = $(`<div class="es-stat-card-grid mb-3"></div>`).appendTo($intro);
	const $names = $(`<div class="flex flex-col gap-2 mb-3"></div>`).appendTo($intro);
	const $notes = $(`<div class="flex flex-col gap-2 mb-3"></div>`).appendTo($intro);
	const year_note = make_fiscal_year_note(frm);
	if (year_note) $notes.append(year_note);
	if (result?.advances) {
		$notes.append(
			frappe.ui.alert({
				theme: "blue",
				title:
					result.advances === 1
						? __("1 row with a negative amount is an advance")
						: __("{0} rows with a negative amount are advances", [result.advances]),
				description:
					party_type === "Customer"
						? __(
								"Money customers paid before any bill. Each one is posted as an advance, to be set against their next invoice."
						  )
						: __(
								"Money you paid suppliers before any bill. Each one is posted as an advance, to be set against their next bill."
						  ),
			})
		);
	}
	// missing and disabled names have their own banners
	const failed = data.filter((row) => row.status === "Failed" && row.party && !disabled.has(row.party));
	if (failed.length) {
		$notes.append(
			frappe.ui.alert({
				theme: "red",
				title:
					failed.length === 1
						? __("1 invoice could not be posted")
						: __("{0} invoices could not be posted", [failed.length]),
				description: failed.map((row) => `${row.party_name}: ${row.error}`).join("; "),
			})
		);
	}
	if (result?.unused_columns?.length) {
		$notes.append(
			frappe.ui.alert({
				theme: "yellow",
				title: __("Not read from your sheet: {0}", [result.unused_columns.join(", ")]),
				description: __(
					"Only the name, invoice number, invoice date, due date and outstanding amount are read. Rename a column to one of these if it holds them, then upload again."
				),
			})
		);
	}
	if (created || result?.skipped) {
		$(`<div class="text-p-sm text-ink-gray-5"></div>`)
			.text(
				result?.skipped
					? result.skipped === 1
						? __("1 invoice on the sheet is posted already, so it was left out.")
						: __("{0} invoices on the sheet are posted already, so they were left out.", [
								result.skipped,
						  ])
					: created === 1
					? __("1 invoice is posted already and stays as it is.")
					: __("{0} invoices are posted already and stay as they are.", [created])
			)
			.appendTo($notes);
	}

	const currency = erpnext.get_currency(frm.doc.company);
	const refresh_state = () => {
		const state = get_party_state(dialog.get_value("invoices") || [], disabled);
		$stats
			.empty()
			.toggle(!!state.names.length)
			.append(state.names.length ? make_opening_invoice_stats(frm, party_type, state, currency) : []);
		$names.empty().append(make_party_banners(party_type, state, names, () => make_add_party_menu()));
		dialog.set_df_property("add_missing", "hidden", !state.new_names.length);
		dialog.set_df_property(
			"add_missing",
			"label",
			party_type === "Customer"
				? state.new_names.length === 1
					? __("Create {0} when posting", [frappe.utils.escape_html(state.new_names[0])])
					: __("Create the {0} new customers when posting", [state.new_names.length])
				: state.new_names.length === 1
				? __("Create {0} when posting", [frappe.utils.escape_html(state.new_names[0])])
				: __("Create the {0} new suppliers when posting", [state.new_names.length])
		);
		if (!state.new_names.length) dialog.set_value("add_missing", 0);
	};
	const link_rows = (name, party_name) => {
		const rows = (dialog.get_value("invoices") || []).filter(
			(row) => !row.party && row.party_name === name
		);
		rows.forEach((row) => (row.party = party_name));
		dialog.fields_dict.invoices.grid.refresh();
		refresh_state();
		return rows.length;
	};
	const add_party = (name) =>
		frappe.model.with_doctype(party_type, () => {
			const doc = frappe.model.get_new_doc(party_type);
			doc[party.name_field] = name;
			frappe.ui.form.make_quick_entry(
				party_type,
				(saved) => {
					// a full form passes itself; the quick entry passes the saved doc
					const linked = link_rows(name, (saved.doc || saved).name);
					frappe.show_alert({
						message:
							linked === 1
								? __("{0} added. 1 invoice linked.", [name])
								: __("{0} added. {1} invoices linked.", [name, linked]),
						indicator: "green",
					});
				},
				null,
				doc
			);
		});
	const make_add_party_menu = () => {
		const state = get_party_state(dialog.get_value("invoices") || [], disabled);
		const shown = state.new_names.slice(0, ADD_ONE_BY_ONE_LIMIT);
		const rows = shown.map((name) => {
			const invoices = state.invoices.filter((row) => !row.party && row.party_name === name);
			const total = format_currency(
				invoices.reduce((sum, row) => sum + flt(row.outstanding_amount), 0),
				currency
			);
			const existing = names.suggestions?.[name];
			if (!existing) {
				return {
					label: name,
					icon: "user",
					icon_right: "plus",
					description:
						party_type === "Customer"
							? invoices.length === 1
								? __("1 invoice · {0} · Create", [total])
								: __("{0} invoices · {1} · Create", [invoices.length, total])
							: invoices.length === 1
							? __("1 bill · {0} · Create", [total])
							: __("{0} bills · {1} · Create", [invoices.length, total]),
					onclick: () => add_party(name),
				};
			}
			return {
				label: name,
				icon: "user",
				description: __("Did you mean {0}?", [existing]),
				submenu: [
					{
						label: __("Use {0}", [existing]),
						icon: "link",
						description:
							party_type === "Customer"
								? __("An existing customer. Links its invoices.")
								: __("An existing supplier. Links its bills."),
						onclick: () => {
							const linked = link_rows(name, existing);
							frappe.show_alert({
								message:
									linked === 1
										? __("1 invoice linked to {0}.", [existing])
										: __("{0} invoices linked to {1}.", [linked, existing]),
								indicator: "green",
							});
						},
					},
					{
						label: __("Create {0}", [name]),
						icon: "plus",
						description:
							party_type === "Customer"
								? __("A new customer. Opens its form.")
								: __("A new supplier. Opens its form."),
						onclick: () => add_party(name),
					},
				],
			};
		});
		if (state.new_names.length > shown.length) {
			rows.push({
				label: __("And {0} more", [state.new_names.length - shown.length]),
				icon: "ellipsis",
				description: __("Too many to add one by one. Use a sheet for these."),
				disabled: true,
			});
		}
		const create_group = {
			group: __("Create now"),
			options: [
				{
					label: __("Fill one by one"),
					icon: "user-plus",
					description: __("Pick a name to open its form."),
					submenu: rows,
				},
			],
		};
		const sheet_group = {
			group: __("Use a sheet"),
			options: [
				{
					label: __("Download as sheet"),
					icon: "download",
					description: __("These names in the import sheet."),
					// the sheet lists the names still new once saved, without those linked here
					onclick: () =>
						save().then(() =>
							open_url_post(
								"/api/method/erpnext.setup.doctype.company_setup.opening_invoices.download_new_parties",
								{ company: frm.doc.company, party_type: party_type }
							)
						),
				},
				{
					label: __("Import filled sheet"),
					icon: "upload",
					description: __("Back through the standard import."),
					// the standard import is open to System Managers only
					condition: () => frappe.model.can_create("Data Import"),
					onclick: import_parties,
				},
			],
		};
		return frappe.ui.dropdown({
			button: {
				label:
					state.new_names.length === 1
						? __("Add {0}", [state.new_names[0]])
						: party_type === "Customer"
						? __("Add {0} customers", [state.new_names.length])
						: __("Add {0} suppliers", [state.new_names.length]),
				icon_right: "chevron-down",
			},
			// with many new names a sheet is the quicker way
			options:
				state.new_names.length > ADD_ONE_BY_ONE_LIMIT
					? [sheet_group, create_group]
					: [create_group, sheet_group],
		});
	};
	// the import runs in its own dialog; this one comes back after it, with the names matched again
	const import_parties = () =>
		save().then(() => {
			dialog.hide();
			frappe.require("data_import_tools.bundle.js", () =>
				frappe.data_import.open_data_import_dialog({
					reference_doctype: party_type,
					import_type: "Insert New Records",
					on_close: () => show_opening_invoices(frm, party_type),
				})
			);
		});

	// a quick entry that opens the full form takes the page: keep the rows and come back to them
	const route = frappe.get_route_str();
	dialog.onhide = () => {
		if (frappe.get_route_str() === route) return;
		frm.reopen_opening_invoices = party_type;
		frm.call("save_opening_invoices", {
			party_type: party_type,
			rows: dialog.get_value("invoices") || [],
		});
	};
	dialog.show();
	refresh_state();
}

// What the rows say about their customers or suppliers: new names, found ones and disabled ones
function get_party_state(rows, disabled) {
	const unique = (list) => [...new Set(list)];
	const named = rows.filter((row) => row.party_name || row.party);
	const invoices = named.filter((row) => flt(row.outstanding_amount) > 0);
	return {
		invoices: invoices,
		advance: -named
			.filter((row) => flt(row.outstanding_amount) < 0)
			.reduce((sum, row) => sum + flt(row.outstanding_amount), 0),
		names: unique(named.map((row) => row.party || row.party_name)),
		new_names: unique(named.filter((row) => !row.party).map((row) => row.party_name)),
		found: unique(named.filter((row) => row.party && !disabled.has(row.party)).map((row) => row.party)),
		disabled: unique(named.filter((row) => disabled.has(row.party)).map((row) => row.party)),
	};
}

function get_party_badge(row, names, disabled) {
	if (!row?.party) return frappe.ui.badge.html({ label: __("New"), theme: "orange" });
	if (disabled.has(row.party)) return frappe.ui.badge.html({ label: __("Disabled"), theme: "red" });
	const other_currency = names.other_currency?.[row.party];
	if (other_currency) return frappe.ui.badge.html({ label: other_currency, theme: "red" });
	if (names.similar?.[row.party_name] === row.party) {
		return frappe.ui.badge.html({ label: __("Similar name"), theme: "blue" });
	}
	return "";
}

function make_opening_invoice_stats(frm, party_type, state, currency) {
	const total = state.invoices.reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
	const day_before =
		frm.doc.migration_date &&
		frappe.datetime.str_to_user(frappe.datetime.add_days(frm.doc.migration_date, -1));
	const found_caption = [
		state.found.length && __("{0} found", [state.found.length]),
		state.disabled.length && __("{0} disabled", [state.disabled.length]),
	]
		.filter(Boolean)
		.join(" · ");
	return [
		frappe.ui.stat_card({
			label: __("On your sheet"),
			value: format_currency(total, currency),
			caption: state.advance
				? party_type === "Customer"
					? __("Less {0} customers paid in advance", [format_currency(state.advance, currency)])
					: __("Less {0} paid to suppliers in advance", [format_currency(state.advance, currency)])
				: day_before && __("On {0}", [day_before]),
		}),
		frappe.ui.stat_card({
			label: party_type === "Customer" ? __("Invoices") : __("Bills"),
			value: format_number(state.invoices.length, null, 0),
			caption:
				state.names.length === 1
					? __("From {0}", [state.names[0]])
					: party_type === "Customer"
					? __("From {0} customers", [state.names.length])
					: __("From {0} suppliers", [state.names.length]),
		}),
		frappe.ui.stat_card({
			label: party_type === "Customer" ? __("Customers") : __("Suppliers"),
			value: state.new_names.length
				? __("{0} new", [state.new_names.length])
				: __("All {0} found", [state.names.length]),
			caption: state.new_names.length
				? found_caption
				: state.disabled.length
				? __("{0} disabled", [state.disabled.length])
				: __("Ready to post"),
		}),
	];
}

// Names that may be a typo: of a party ERPNext has, or of another new name on the sheet
function get_look_alike_notes(party_type, state, names) {
	const is_new = (name) => state.new_names.includes(name);
	return [
		...Object.entries(names.suggestions || {})
			.filter(([name]) => is_new(name))
			.map(([name, existing]) =>
				party_type === "Customer"
					? __("{0} looks like {1}, a customer you have.", [name, existing])
					: __("{0} looks like {1}, a supplier you have.", [name, existing])
			),
		...(names.look_alikes || [])
			.filter((pair) => pair.every(is_new))
			.map(([name, other]) => __("{0} and {1} look like the same name spelt two ways.", [name, other])),
	].slice(0, 3);
}

function make_party_banners(party_type, state, names, make_add_menu) {
	const banners = [];
	const list = (names) =>
		names.length > 3
			? __("{0} and {1} more", [names.slice(0, 3).join(", "), names.length - 3])
			: names.join(", ");
	if (state.new_names.length) {
		banners.push(
			frappe.ui.alert({
				theme: "yellow",
				title:
					party_type === "Customer"
						? state.new_names.length === 1
							? __("{0} isn't a customer in ERPNext yet", [state.new_names[0]])
							: __("{0} customers aren't in ERPNext yet", [state.new_names.length])
						: state.new_names.length === 1
						? __("{0} isn't a supplier in ERPNext yet", [state.new_names[0]])
						: __("{0} suppliers aren't in ERPNext yet", [state.new_names.length]),
				description: [
					state.new_names.length === 1
						? __("Add it here, or tick the box at the end to create it when posting.")
						: list(state.new_names) +
						  ". " +
						  __("Add them here, or tick the box at the end to create them when posting."),
					...get_look_alike_notes(party_type, state, names),
				].join(" "),
				footer: make_add_menu,
			})
		);
	}
	const other_currency = state.found.filter((party) => names.other_currency?.[party]);
	if (other_currency.length) {
		banners.push(
			frappe.ui.alert({
				theme: "red",
				title:
					other_currency.length === 1
						? __("{0} is billed in {1}", [
								other_currency[0],
								names.other_currency[other_currency[0]],
						  ])
						: __("{0} are billed in another currency", [list(other_currency)]),
				description: __(
					"Opening invoices in another currency can't be posted here yet. They stay as drafts."
				),
			})
		);
	}
	if (state.disabled.length) {
		banners.push(
			frappe.ui.alert({
				theme: "red",
				title:
					state.disabled.length === 1
						? __("{0} is disabled", [state.disabled[0]])
						: __("{0} are disabled", [list(state.disabled)]),
				description:
					state.disabled.length === 1
						? __("Its invoices won't be posted. Enable it first.")
						: __("Their invoices won't be posted. Enable them first."),
			})
		);
	}
	return banners;
}

function upload_opening_invoices(frm, party_type, dialog) {
	const drafts = (dialog.fields_dict.invoices.df.data || []).filter(
		(row) => row.party_name || flt(row.outstanding_amount)
	).length;
	const choose = () =>
		new frappe.ui.FileUploader({
			restrictions: { allowed_file_types: [".xlsx", ".csv"] },
			on_success: (file) =>
				frm
					.call("import_opening_invoices", { party_type: party_type, file_url: file.file_url })
					.then((r) => {
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
function post_opening_invoices(frm, party_type, { rows, disabled = new Set(), add_missing, before_post }) {
	const currency = erpnext.get_currency(frm.doc.company);
	const money = (list) =>
		format_currency(
			list.reduce((sum, row) => sum + flt(row.outstanding_amount), 0),
			currency
		);
	const named = rows.filter((row) => row.status !== "Created" && (row.party_name || row.party));
	const can_post = (row) => (row.party || add_missing) && !disabled.has(row.party);
	const posting = named.filter(can_post);
	const invoices = posting.filter((row) => flt(row.outstanding_amount) > 0);
	const waiting = named.filter((row) => !can_post(row));
	const new_names = add_missing
		? [...new Set(named.filter((row) => !row.party).map((row) => row.party_name))]
		: [];
	if (!posting.length) {
		frappe.msgprint(
			party_type === "Customer"
				? __("None of these invoices can be posted yet: add or enable their customers first.")
				: __("None of these bills can be posted yet: add or enable their suppliers first.")
		);
		return;
	}
	const day_before = frappe.datetime.str_to_user(frappe.datetime.add_days(frm.doc.migration_date, -1));
	const lines = [
		party_type === "Customer"
			? invoices.length === 1
				? __(
						"Post 1 unpaid invoice ({0}) as an opening invoice, recording what the customer owed on {1}? Nothing is sent to the customer.",
						[money(invoices), day_before]
				  )
				: __(
						"Post {0} unpaid invoices ({1}) as opening invoices, recording what customers owed on {2}? Nothing is sent to customers.",
						[invoices.length, money(invoices), day_before]
				  )
			: invoices.length === 1
			? __(
					"Post 1 unpaid bill ({0}) as an opening invoice, recording what you owed the supplier on {1}? Nothing is sent to the supplier.",
					[money(invoices), day_before]
			  )
			: __(
					"Post {0} unpaid bills ({1}) as opening invoices, recording what you owed suppliers on {2}? Nothing is sent to suppliers.",
					[invoices.length, money(invoices), day_before]
			  ),
	];
	if (!invoices.length) lines.shift();
	if (new_names.length) {
		lines.push(
			party_type === "Customer"
				? __("New customers are added first: {0}.", [
						new_names.map(frappe.utils.escape_html).join(", "),
				  ])
				: __("New suppliers are added first: {0}.", [
						new_names.map(frappe.utils.escape_html).join(", "),
				  ])
		);
	}
	if (waiting.length) {
		lines.push(
			party_type === "Customer"
				? waiting.length === 1
					? __("1 invoice ({0}) stays as a draft until its customer is added or enabled.", [
							money(waiting),
					  ])
					: __("{0} invoices ({1}) stay as drafts until their customer is added or enabled.", [
							waiting.length,
							money(waiting),
					  ])
				: waiting.length === 1
				? __("1 bill ({0}) stays as a draft until its supplier is added or enabled.", [
						money(waiting),
				  ])
				: __("{0} bills ({1}) stay as drafts until their supplier is added or enabled.", [
						waiting.length,
						money(waiting),
				  ])
		);
	}
	const missing_year = frm.doc.__onload?.missing_fiscal_year;
	if (missing_year) lines.push(__("The financial year {0} is added first.", [missing_year.year]));
	const advances = posting.filter((row) => flt(row.outstanding_amount) < 0).length;
	if (advances) {
		lines.push(
			advances === 1
				? __("1 negative row is posted as an advance.")
				: __("{0} negative rows are posted as advances.", [advances])
		);
	}
	frappe.confirm(lines.join(" "), () => {
		before_post?.();
		frm.call({
			doc: frm.doc,
			method: "create_opening_invoices",
			args: { party_type: party_type, add_missing: add_missing ? 1 : 0 },
			freeze: true,
		}).then((r) => {
			if (r.message === "queued") {
				frappe.show_alert({
					message: __(
						"Posting the invoices in the background. This page updates when they are done."
					),
					indicator: "blue",
				});
			} else {
				frm.reload_doc().then(() => show_posting_result(frm, party_type, new_names));
			}
		});
	});
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

function import_buttons(frm, doctype) {
	return [
		frappe.model.can_create("Data Import") && {
			label: __("Import {0}", [__(doctype)]),
			icon: "upload",
			onclick: () =>
				frappe.require("data_import_tools.bundle.js", () =>
					frappe.data_import.open_data_import_dialog({
						reference_doctype: doctype,
						import_type: "Insert New Records",
						// the counts on the tasks change after an import
						on_close: () => frm.reload_doc(),
					})
				),
		},
		{
			label: __("{0} List", [__(doctype)]),
			variant: "ghost",
			icon_right: "arrow-up-right",
			onclick: () => frappe.set_route("List", doctype),
		},
	].filter(Boolean);
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

function show_chart_import(frm) {
	const company = frm.doc.company;
	const dialog = new frappe.ui.Dialog({
		title: __("Import a chart of accounts"),
		size: "large",
		fields: [
			{ fieldname: "template", fieldtype: "HTML" },
			{
				fieldname: "file",
				fieldtype: "Attach",
				label: __("Filled sheet"),
				options: { restrictions: { allowed_file_types: [".csv", ".xlsx", ".xls"] } },
				change: () => preview(),
			},
			{ fieldname: "preview", fieldtype: "HTML" },
			...where_to_find_fields(frm, "chart_of_accounts", {
				has_data: false,
				posted: frm.doc.chart_of_accounts_source === "Imported",
			}),
		],
		primary_action_label: __("Replace chart"),
		primary_action: ({ file }) =>
			frappe.confirm(
				__(
					"Delete the {0} accounts and tax templates of {1} and make the chart from your sheet? This can't be undone.",
					[last_preview?.current_accounts, company]
				),
				() =>
					frm
						.call({
							doc: frm.doc,
							method: "import_chart",
							args: { file_url: file },
							freeze: true,
							freeze_message: __("Importing the chart..."),
						})
						.then((r) => {
							dialog.hide();
							frm.reload_doc();
							const added = r.message?.added_accounts || [];
							frappe.show_alert(
								{
									message: added.length
										? __("Chart imported. Added accounts ERPNext needs: {0}", [
												added.join(", "),
										  ])
										: __("Chart imported"),
									indicator: "green",
								},
								added.length ? 10 : 5
							);
						})
			),
	});

	const $template = dialog.get_field("template").$wrapper;
	$(`<div class="text-p-sm text-ink-gray-6 mb-2"></div>`)
		.text(
			__(
				"Download a sheet, fill one row per account, then upload it. To bring opening balances too, fill the Opening Debit and Opening Credit columns."
			)
		)
		.appendTo($template);
	const $downloads = $(`<div class="flex flex-wrap gap-2 mb-4"></div>`).appendTo($template);
	for (const [label, template] of [
		[__("Current chart"), "Current Chart"],
		[__("Sample chart"), "Sample Template"],
	]) {
		$downloads.append(
			frappe.ui.button({
				label: label,
				icon: "download",
				onclick: () =>
					open_url_post(
						"/api/method/erpnext.setup.doctype.company_setup.chart_sheet.download_chart_template",
						{ company: company, template: template }
					),
			})
		);
	}

	const $preview = dialog.get_field("preview").$wrapper;
	const set_ready = (ready) => dialog.get_primary_btn().prop("disabled", !ready);
	let last_preview;
	const preview = () => {
		const file_url = dialog.get_value("file");
		$preview.empty();
		set_ready(false);
		if (!file_url) return;
		frm.call("preview_chart", { file_url: file_url }).then((r) => {
			// another file was picked meanwhile
			if (dialog.get_value("file") !== file_url) return;
			last_preview = r.message;
			render_import_preview(frm, $preview, r.message, company);
			set_ready(true);
		});
	};

	set_ready(false);
	dialog.show();
}

function render_import_preview(frm, $preview, data, company) {
	const $notes = $(`<div class="flex flex-col gap-2 mb-3"></div>`).appendTo($preview);
	$notes.append(
		frappe.ui.alert({
			theme: "yellow",
			title: __("This replaces the whole chart"),
			description: __(
				"The {0} accounts in the chart now and the tax templates are deleted. {1} accounts are made from the sheet, and the company's default accounts are set again.",
				[data.current_accounts, data.accounts.length]
			),
		})
	);

	$notes.append(make_left_out_note(frm, data.left_out));
	if (data.balances) {
		const notes = [__("They are added to Opening Balances as drafts. Nothing is posted.")];
		if (data.draft_balances) {
			notes.push(__("They replace the {0} draft balances there now.", [data.draft_balances]));
		}
		const difference = flt(data.debit - data.credit, 2);
		if (difference) {
			notes.push(
				__("Debit and credit differ by {0}.", [
					format_currency(Math.abs(difference), erpnext.get_currency(company)),
				])
			);
		}
		$notes.append(
			frappe.ui.alert({
				theme: "blue",
				title: __("{0} opening balances found", [data.balances]),
				description: notes.join(" "),
			})
		);
	} else if (data.draft_balances) {
		$notes.append(
			frappe.ui.alert({
				theme: "blue",
				title: __("Your {0} draft opening balances stay", [data.draft_balances]),
				description: __(
					"They move to the new accounts with the same name. Check them after the import."
				),
			})
		);
	}

	const children_of = {};
	for (const account of data.accounts) {
		const parent = account.parent_account || "";
		children_of[parent] = children_of[parent] || [];
		children_of[parent].push(account);
	}
	new SheetTree({
		parent: $(`<div></div>`).appendTo($preview),
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
			<span class="company-edit shrink-0"></span>
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
			frm.call("update_company_details", { values: values }).then(() => {
				dialog.hide();
				frm.reload_doc();
			}),
	});
	dialog.set_values(details);
	dialog.show();
}
