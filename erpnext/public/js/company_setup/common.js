// Status icon, title, one line on what's done or left, and the buttons
// a disabled button takes no pointer, so the reason it is disabled shows from a wrapper
export function make_button(opts) {
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
export function ask_before_closing(dialog, is_dirty, save) {
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
export function get_rows_key(grid, rows = grid.df.data || []) {
	const fields = grid.docfields.map((df) => df.fieldname);
	return JSON.stringify(rows.map((row) => JSON.stringify(fields.map((f) => row[f] ?? null))).sort());
}

// A grey line of text, as under a window's cards or in a confirm
export function grey_line(text) {
	return $(`<div class="text-p-sm text-ink-gray-5"></div>`).text(text);
}

export function make_text(value, classes = "text-ink-gray-8") {
	return $(`<span class="text-base"></span>`).addClass(classes).text(value);
}

// Rows of cells in one bordered box, a line between rows
export function make_box(rows) {
	return $(`<div class="flex flex-col border rounded-lg"></div>`).append(
		rows.map((cells, index) =>
			$(`<div class="flex items-center gap-4 px-3 py-2.5"></div>`)
				.toggleClass("border-t", index > 0)
				.append(cells)
		)
	);
}

// A box row's left side: the title, and a grey line under it
export function make_title_cell(title, line, title_classes = "text-ink-gray-8 truncate") {
	return $(`<div class="flex flex-col gap-0.5 grow min-w-0"></div>`).append(
		make_text(title, title_classes),
		grey_line(line)
	);
}

export function make_view_button(onclick) {
	return frappe.ui.button({ label: __("View"), variant: "ghost", size: "sm", onclick });
}

// The ⋯ menu of a card or window, when it has options
export function make_more_menu(options) {
	return options.length
		? frappe.ui.dropdown({
				button: { icon: "ellipsis", variant: "ghost", attrs: { "aria-label": __("More") } },
				options: options,
				align: "end",
		  })
		: [];
}

export function make_sheet_buttons(upload, menu = []) {
	return $(`<div class="flex gap-1 shrink-0"></div>`).append(
		upload ? frappe.ui.button({ label: __("Upload sheet"), icon: "upload", onclick: upload }) : [],
		make_more_menu(menu)
	);
}

// A window's head line: what goes in it, with Upload sheet and the ⋯ menu
export function make_sheet_head(intro, upload, menu) {
	return $(`<div class="flex items-center justify-between gap-3 mb-3"></div>`).append(
		$(`<div class="text-p-base text-ink-gray-7"></div>`).text(intro),
		make_sheet_buttons(upload, menu)
	);
}

// An amount in the company's currency
export function money(frm, value, decimals) {
	return format_currency(value, erpnext.get_currency(frm.doc.company), decimals);
}

// A sheet of the company's rows from one of our modules; undone_from: the rows undone from that source
export function download_sheet(company, method, args = {}) {
	open_url_post(`/api/method/erpnext.setup.doctype.company_setup.${method}`, {
		company: company,
		...Object.fromEntries(Object.entries(args).filter(([, value]) => value !== undefined)),
	});
}

// A grid cell: the name, cut short when long, with its badge
export function name_with_badge(name_html, badge_html) {
	return `<span class="flex items-center gap-2 min-w-0">
		<span class="truncate">${name_html}</span>
		${badge_html ? `<span class="shrink-0">${badge_html}</span>` : ""}
	</span>`;
}

export function new_badge() {
	return frappe.ui.badge.html({ label: __("New"), theme: "blue" });
}

// where rows came from: the sheet's name, or typed in
export function source_name(source) {
	return source || __("Typed in");
}

// The first line of a confirm that replaced a window's body: a back arrow and what confirming does
export function make_back_line(text, onclick) {
	return $(`<div class="flex items-center gap-2"></div>`).append(
		frappe.ui.button({ icon: "arrow-left", variant: "ghost", size: "sm", tooltip: __("Back"), onclick }),
		$(`<div class="text-p-base text-ink-gray-7"></div>`).text(text)
	);
}

// A sub-view's head: back arrow, title and line, and its buttons on the right
export function make_back_header(title, line, onclick, actions) {
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
export function make_alerts(alerts) {
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
export function set_primary_disabled(dialog, disabled) {
	disabled ? dialog.disable_primary_action() : dialog.enable_primary_action();
}

// Label and value lines, as in a confirm
export function make_kv_lines(lines) {
	return $(`<div class="flex flex-col"></div>`).append(
		lines.map(([label, value]) =>
			$(`<div class="flex justify-between gap-4 py-2 border-b text-base"></div>`).append(
				$(`<span class="text-ink-gray-6"></span>`).text(label),
				$(`<span class="text-base-medium text-ink-gray-8 text-right"></span>`).text(value)
			)
		)
	);
}

// A stat card with a few amounts that belong together, the one to read first in bold
export function make_lines_card(label, lines, caption, info) {
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

// Pick a sheet and read it with method, then draw the window again on what it read. replaces: the
// confirm to ask first, as the sheet replaces drafts.
// The uploader for a sheet: a .xlsx or .csv from this device only
export function pick_sheet(on_success) {
	return new frappe.ui.FileUploader({
		restrictions: { allowed_file_types: [".xlsx", ".csv"] },
		disable_file_browser: true,
		allow_web_link: false,
		allow_take_photo: false,
		allow_google_drive: false,
		on_success: on_success,
	});
}

export function upload_sheet(frm, method, args, { replaces, after }) {
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

// A button that opens where a red is fixed: a closing entry, the company, a dimension. after:
// draws the window again once a dimension's default is set; without it the window closes.
export function fix_button(fix, frm, after) {
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
export function make_posting_reds(frm, reds, after) {
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
export function close_and_reload(frm, dialog) {
	dialog?.hide();
	return reload_fresh(frm);
}

// A window drawn again shows where the old one was, at its scroll, with no close and open between
export function show_in_place_of(dialog, old) {
	if (!old?.display) return dialog.show();
	const scroll = old.$wrapper.scrollTop();
	dialog.animate = false;
	old.$wrapper.removeClass("fade");
	old.hide();
	dialog.show();
	dialog.$wrapper.scrollTop(scroll);
}

export function reload_fresh(frm) {
	frm.posting_step = null;
	frappe.dom.freeze(__("Updating…"));
	return frm.reload_doc().then(
		() => frappe.dom.unfreeze(),
		() => frappe.dom.unfreeze()
	);
}

export const UNDOING_TEXT = () => __("Undoing in the background. This page updates when it's done.");

// After a post or undo: a job queued says so, and the page updates again when it ends
export function after_run(frm, result, queued_text, done) {
	if (result === "queued") {
		frappe.ui.toast({ type: "info", message: queued_text });
		return reload_fresh(frm);
	}
	return reload_fresh(frm).then(() => done?.(result));
}

// Read-only data for a window: unlike call_method, the document does not come back with it
export function get_data(frm, method, args = {}, opts = {}) {
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
export function call_method(frm, method, args, opts = {}) {
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
export function format_time_posted(datetime) {
	if (!datetime) return "";
	return frappe.datetime.str_to_user(datetime).replace(/(\d{1,2}:\d{2}):\d{2}.*$/, "$1");
}

// the countries whose English says "check", as the server words it
export const CHECK_COUNTRIES = ["United States", "Canada"];

export let says_check = false;

export function set_says_check(value) {
	says_check = value;
}

export function cheque_words(text) {
	return says_check ? text.replace(/cheque/g, "check").replace(/Cheque/g, "Check") : text;
}

export function count_rows(count) {
	return plural(count, __("1 row"), __("{0} rows", [count]));
}

export function count_assets(count) {
	return plural(count, __("1 asset"), __("{0} assets", [count]));
}

export function count_warehouses(count) {
	return plural(count, __("1 warehouse"), __("{0} warehouses", [count]));
}

export function count_balances(count) {
	return plural(count, __("1 balance"), __("{0} balances", [count]));
}

// a row below zero: a credit or advance the customer has, a debit or advance with a supplier
export function count_advances(count, party_type) {
	return party_type === "Supplier"
		? plural(count, __("1 debit or advance"), __("{0} debits or advances", [count]))
		: plural(count, __("1 credit or advance"), __("{0} credits or advances", [count]));
}

export function short_account(frm, account) {
	const abbr = frm.doc.__onload?.company_details?.abbr;
	return abbr && account ? account.replace(new RegExp(` - ${abbr}$`), "") : account || "";
}

// a qty with decimals only when it has them
export function format_qty(value) {
	return format_number(value, null, Number.isInteger(flt(value)) ? 0 : 3);
}

export function plural(count, one, many) {
	return count === 1 ? one : many;
}

export function day_before_text(frm) {
	return frm.doc.migration_date
		? frappe.datetime.str_to_user(frappe.datetime.add_days(frm.doc.migration_date, -1))
		: __("the day before migration");
}

// A read-only table of a few rows, like the stock lines against the trial balance
// on a phone a wide table scrolls inside its box, and an amount keeps its ₹ on its line
export function make_read_table(columns, rows) {
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
export function make_search_table(columns, rows, to_cells) {
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

export const ORDER_TYPES = {
	open_sales_orders: "Sales Order",
	open_purchase_orders: "Purchase Order",
	open_work_orders: "Work Order",
	open_subcontracting_orders: "Subcontracting Order",
};
