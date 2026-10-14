frappe.ui.form.on("Company Setup", {
	setup(frm) {
		// invoices made in the background
		frappe.realtime.off("company_setup_invoices");
		frappe.realtime.on("company_setup_invoices", (data) => {
			if (data.company !== frm.doc.company) return;
			if (data.error) {
				frappe.msgprint({ title: __("Invoices not posted"), message: data.error, indicator: "red" });
			}
			frm.reload_doc();
		});
	},

	refresh(frm) {
		if (frm.is_new()) return;

		frm.events.render(frm);
		// steps are checked against the company's data each time the form opens
		frm.call("check_steps").then((r) => r.message && frm.reload_doc());
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
		doctype: "Sales Invoice",
		total_label: __("Receivable"),
		report: __("receivables"),
	},
	Supplier: {
		table: "opening_purchase_invoices",
		doctype: "Purchase Invoice",
		total_label: __("Payable"),
		report: __("payables"),
	},
};

function make_invoice_stats(frm, party_type) {
	const stats = frm.doc.__onload?.stats || {};
	const currency = erpnext.get_currency(frm.doc.company);
	const rows = frm.doc[PARTY[party_type].table] || [];
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

	const $task = $(`<div class="flex items-center gap-3"></div>`);
	$(`<span class="shrink-0 ${color}"></span>`).html(frappe.utils.icon(icon, "md")).appendTo($task);
	const $text = $(`<div class="flex flex-col gap-0.5 grow min-w-0"></div>`).appendTo($task);
	$(`<div class="text-base-medium text-ink-gray-8"></div>`).text(step.title).appendTo($text);
	if (note) $(`<div class="text-p-sm text-ink-gray-5"></div>`).text(note).appendTo($text);

	if (buttons.length) {
		const $actions = $(`<div class="flex flex-wrap justify-end items-center gap-2 shrink-0"></div>`);
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
	const reports = [
		[__("Unpaid customer invoices"), __("The receivables report. It goes in Selling.")],
		[__("Unpaid supplier bills"), __("The payables report. It goes in Buying.")],
		[__("Trial balance"), __("It goes in Accounts, as opening balances.")],
	];
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
		],
		primary_action_label: __("Save"),
		primary_action: (values) => save_migration_date(frm, dialog, values.migration_date, 0),
		secondary_action_label: __("This is a new business, nothing to bring"),
		secondary_action: () => {
			const migration_date = dialog.get_value("migration_date");
			if (!migration_date) return frappe.msgprint(__("Pick the migration date first."));
			save_migration_date(frm, dialog, migration_date, 1);
		},
	});

	const $reports = $(`<div class="flex flex-col gap-2"></div>`);
	$(`<div class="text-base-medium text-ink-gray-8"></div>`)
		.text(__("Keep these ready from your old software"))
		.appendTo($reports);
	for (const [title, description] of reports) {
		$(`<div class="border-t pt-2"></div>`)
			.append($(`<div class="text-base text-ink-gray-8"></div>`).text(title))
			.append($(`<div class="text-p-sm text-ink-gray-5"></div>`).text(description))
			.appendTo($reports);
	}
	dialog.get_field("reports").$wrapper.append($reports);
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

// What is done and what is left; nothing here stops going live
function show_go_live(frm) {
	const steps = get_steps(frm).filter((step) => step.name !== "go_live");
	const stats = frm.doc.__onload?.stats || {};
	const currency = erpnext.get_currency(frm.doc.company);
	const dialog = new frappe.ui.Dialog({
		title: __("Go live"),
		fields: [{ fieldname: "summary", fieldtype: "HTML" }],
		primary_action_label: __("Go live"),
		primary_action: () =>
			frm.call("go_live").then(() => {
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
			frm.doc.migration_date
				? __("{0} moves to ERPNext on {1}. Nothing here stops you.", [
						frm.doc.company,
						frappe.datetime.str_to_user(frm.doc.migration_date),
				  ])
				: __("Nothing here stops you.")
		)
		.appendTo($body);
	const $list = $(`<div class="flex flex-col"></div>`).appendTo($body);
	for (const step of steps) {
		$list.append($(`<div class="border-t py-2"></div>`).append(make_task(frm, step, [])));
	}
	if (stats.temporary_opening) {
		$body.append(
			frappe.ui.alert({
				theme: "yellow",
				title: __("{0} is left in Temporary Opening", [
					format_currency(Math.abs(stats.temporary_opening), currency),
				]),
				description: __("Fix it any time with a journal entry, or by adding what is missing."),
				footer: () =>
					$(`<div class="flex gap-2"></div>`).append(frappe.ui.button(see_books_button(frm))),
			})
		);
	}
	dialog.get_field("summary").$wrapper.append($body);
	dialog.show();
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
									dialog.fields_dict.balances.grid.refresh();
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
						grid.df.data.push({ account: r.message, ...info.message });
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
					? frm.call("post_opening_balances").then(() => {
							dialog.hide();
							frm.reload_doc();
					  })
					: dialog.hide(),
			secondary_action_label: can_post ? __("Back") : undefined,
			secondary_action: can_post ? () => dialog.hide() : undefined,
		});
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
						of("Asset"),
						1,
						money,
						temporary?.balance > 0 ? [[__("Temporary Opening"), temporary.balance, true]] : []
					),
					make_statement_side(
						__("Liabilities and Equity"),
						of("Liability", "Equity"),
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
			onclick: () => create_opening_invoices(frm, party_type, drafts),
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
	const party = PARTY[party_type];
	const rows = frm.doc[party.table] || [];
	const created = rows.filter((row) => row.status === "Created").length;
	const failed = rows.filter((row) => row.status === "Failed");
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
				data: rows
					.filter((row) => row.status !== "Created")
					.map((row) => ({ ...row, name: undefined })),
				fields: [
					{
						fieldname: "party_name",
						fieldtype: "Data",
						label: __(party_type),
						in_list_view: 1,
						columns: 3,
						reqd: 1,
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
						columns: 2,
					},
					{
						fieldname: "due_date",
						fieldtype: "Date",
						label: __("Due Date"),
						in_list_view: 1,
						columns: 1,
					},
					{
						fieldname: "outstanding_amount",
						fieldtype: "Currency",
						label: __("Outstanding"),
						in_list_view: 1,
						columns: 2,
						reqd: 1,
						onchange: () => update_totals(),
					},
				],
			},
			{ fieldname: "totals", fieldtype: "HTML" },
		],
		primary_action_label: __("Save"),
		primary_action: () => {
			const values = dialog.get_values();
			if (!values) return;
			frm.call("save_opening_invoices", { party_type: party_type, rows: values.invoices || [] }).then(
				() => {
					dialog.hide();
					frm.reload_doc();
				}
			);
		},
	});

	const $intro = dialog.get_field("intro").$wrapper;
	$(`<div class="flex flex-wrap items-center justify-between gap-2 mb-3"></div>`)
		.append(
			$(`<div class="text-p-sm text-ink-gray-6"></div>`).text(
				__(
					"Every invoice still unpaid on the day before migration, from your {0} report. Each one becomes an opening invoice, so payments can be matched to it later.",
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
	const $notes = $(`<div class="flex flex-col gap-2 mb-3"></div>`).appendTo($intro);
	if (result?.advances) {
		$notes.append(
			frappe.ui.alert({
				theme: "yellow",
				title: __("{0} rows with a negative amount were left out", [result.advances]),
				description: __("Money paid in advance is not an invoice. Record it as an advance payment."),
			})
		);
	}
	if (failed.length) {
		$notes.append(
			frappe.ui.alert({
				theme: "red",
				title: __("{0} invoices could not be posted", [failed.length]),
				description: failed.map((row) => `${row.party_name}: ${row.error}`).join("; "),
			})
		);
	}
	if (created) {
		$(`<div class="text-p-sm text-ink-gray-5"></div>`)
			.text(__("{0} invoices are posted already and stay as they are.", [created]))
			.appendTo($notes);
	}
	frm.call("get_invoice_names", { party_type: party_type }).then((r) =>
		$notes.prepend(make_name_notes(dialog, party_type, r.message))
	);

	const currency = erpnext.get_currency(frm.doc.company);
	const update_totals = () => {
		const data = dialog.fields_dict.invoices.df.data || [];
		const total = data.reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
		dialog
			.get_field("totals")
			.$wrapper.empty()
			.append(
				$(`<div class="flex justify-end text-sm-medium text-ink-gray-8"></div>`).text(
					__("{0} invoices · {1}", [data.length, format_currency(total, currency)])
				)
			);
	};
	dialog.show();
	update_totals();
}

// New names, and names that look like an existing customer or supplier
function make_name_notes(dialog, party_type, names) {
	const $notes = $(`<div class="flex flex-col gap-2"></div>`);
	for (const [name, existing] of Object.entries(names.suggestions || {})) {
		const use_existing = () => {
			for (const row of dialog.fields_dict.invoices.df.data || []) {
				if (row.party_name === name) row.party_name = existing;
			}
			dialog.fields_dict.invoices.grid.refresh();
			$alert.remove();
		};
		const $alert = frappe.ui.alert({
			theme: "yellow",
			title: __("{0} looks like {1}", [name, existing]),
			footer: () =>
				$(`<div class="flex gap-2"></div>`).append(
					frappe.ui.button({ label: __("Use {0}", [existing]), onclick: use_existing }),
					frappe.ui.button({
						label: __("Keep as new"),
						variant: "ghost",
						onclick: () => $alert.remove(),
					})
				),
		});
		$notes.append($alert);
	}
	const fresh = (names.new || []).filter((name) => !names.suggestions?.[name]);
	if (fresh.length) {
		$(`<div class="text-p-sm text-ink-gray-5"></div>`)
			.text(
				party_type === "Customer"
					? __("New customers, added when you post: {0}", [fresh.join(", ")])
					: __("New suppliers, added when you post: {0}", [fresh.join(", ")])
			)
			.appendTo($notes);
	}
	return $notes;
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

function create_opening_invoices(frm, party_type, drafts) {
	const currency = erpnext.get_currency(frm.doc.company);
	const total = drafts.reduce((sum, row) => sum + flt(row.outstanding_amount), 0);
	const args = [
		drafts.length,
		format_currency(total, currency),
		frappe.datetime.str_to_user(frappe.datetime.add_days(frm.doc.migration_date, -1)),
	];
	frappe.confirm(
		party_type === "Customer"
			? __(
					"Post {0} unpaid invoices ({1}) to the books as opening invoices? They record what customers owed on {2}. Nothing is sent to customers. New names become customers.",
					args
			  )
			: __(
					"Post {0} unpaid bills ({1}) to the books as opening invoices? They record what you owed suppliers on {2}. Nothing is sent to suppliers. New names become suppliers.",
					args
			  ),
		() =>
			frm.call("create_opening_invoices", { party_type: party_type }).then((r) => {
				if (r.message === "queued") {
					frappe.show_alert({
						message: __(
							"Posting the invoices in the background. This page updates when they are done."
						),
						indicator: "blue",
					});
				} else {
					frm.reload_doc();
				}
			})
	);
}

function import_buttons(frm, doctype) {
	return [
		{
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
	];
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
						.then(() => {
							dialog.hide();
							frm.reload_doc();
							frappe.show_alert({ message: __("Chart imported"), indicator: "green" });
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
