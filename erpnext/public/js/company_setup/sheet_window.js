import {
	ask_before_closing,
	call_method,
	day_before_text,
	get_data,
	grey_line,
	make_alerts,
	make_back_line,
	make_sheet_head,
	reload_fresh,
	set_primary_disabled,
	upload_sheet,
} from "./common";
import { fill_where, where_to_find_fields } from "./where_to_find";

export function setup_dialog_grid(grid, on_delete) {
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
export function set_rate_on_amount() {
	if (!flt(this.doc.qty)) return;
	this.doc.rate = flt(this.doc.amount) / flt(this.doc.qty);
	this.grid_row?.refresh_field("rate");
}

export function keep_rate_on_qty() {
	const row = this.doc;
	const rate = flt(row.rate) || (flt(row.old_qty) ? flt(row.old_amount) / flt(row.old_qty) : 0);
	if (!rate) return;
	row.rate = rate;
	row.amount = flt(flt(row.qty) * rate, 2);
	this.grid_row?.refresh_field("amount");
}

export function list_column(fieldname, fieldtype, label, columns, extra = {}) {
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
export class SheetWindow {
	// row_form: each row opens a form with the fields not in the list. save_method and get_method
	// keep the rows and read the checks, with args. Without fields or extra_fields, get_fields(checks)
	// and get_extra_fields(checks) give them.
	// table: more of the rows table, like its label. posted: no "Where do I find this?".
	constructor(
		frm,
		{
			title,
			where,
			fields,
			extra_fields,
			has_data,
			posted = false,
			row_form = false,
			table = {},
			primary_label = __("Post to books"),
			save_method,
			get_method,
			args = {},
			checks,
		}
	) {
		this.frm = frm;
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
		// get_layout, get_fields and get_extra_fields run before a subclass constructor: use only frm and args
		this.layout = this.get_layout(checks);
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
					...table,
					fields: this.wrap_fields(fields || this.get_fields(checks)),
				},
				{ fieldname: "notes", fieldtype: "HTML" },
				...(extra_fields || this.get_extra_fields(checks)),
				{ fieldname: "confirm", fieldtype: "HTML", hidden: 1 },
				...(where ? where_to_find_fields(frm, where, { has_data: has_data, posted: posted }) : []),
			],
			primary_action_label: primary_label,
			primary_action: () => this.post(),
			secondary_action_label: __("Save draft"),
			secondary_action: () => this.save_draft(),
		});
		if (where) fill_where(frm, this.dialog, where);
		this.grid = this.dialog.fields_dict.rows.grid;
		setup_dialog_grid(this.grid, () => this.changed());
		ask_before_closing(
			this.dialog,
			() => this.is_dirty(),
			() => this.save_before_close()
		);
		this.$back = $(`<div class="hide"></div>`).appendTo(this.dialog.custom_actions);
		this.views = {
			sheet: ["head", "stats", "alerts", "rows", "notes", "where_section"],
			confirm: ["confirm"],
		};
	}

	// each cell shows the alerts' badges, and an edit marks the window changed
	wrap_fields(fields) {
		const win = this;
		return fields.map((df) =>
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
		);
	}

	// what the columns depend on: when it changes on a refresh, the columns are made again
	get_layout() {
		return "";
	}

	get_fields() {
		return [];
	}

	// the fields of the window's other views
	get_extra_fields() {
		return [];
	}

	set_fields(fields) {
		this.grid.df.fields = this.wrap_fields(fields);
		this.grid.reset_grid();
	}

	is_dirty() {
		return this.dirty;
	}

	// Save draft in the unsaved-changes prompt; the window closes after it
	save_before_close() {
		return this.save().then(() => (this.dirty = false));
	}

	// a redraw that keeps the window where it was scrolled to
	keep_scroll(draw) {
		const scroll = this.dialog.$wrapper.scrollTop();
		draw();
		this.dialog.$wrapper.scrollTop(scroll);
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
		const badge = frappe.ui.badge.html({
			label: alert.badge || __("Check"),
			theme: alert.badge_theme || "red",
		});
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

	show() {
		this.set_rows(this.drafts);
		this.show_sheet();
		this.dialog.show();
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

	// the saved rows and their checks, drawn again in this window
	refresh(result = null) {
		return reload_fresh(this.frm)
			.then(() => this.get_method && get_data(this.frm, this.get_method, this.args))
			.then((r) =>
				this.keep_scroll(() => {
					const checks = r?.message;
					const layout = this.get_layout(checks);
					if (layout !== this.layout) {
						this.layout = layout;
						this.set_fields(this.get_fields(checks));
					}
					this.result = result;
					this.load(checks);
					this.dirty = false;
					this.set_rows(this.drafts);
					this.show_sheet();
				})
			);
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
}
