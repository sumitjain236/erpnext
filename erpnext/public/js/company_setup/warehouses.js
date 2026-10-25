import {
	call_method,
	get_data,
	grey_line,
	make_alerts,
	name_with_badge,
	new_badge,
	plural,
	reload_fresh,
	set_primary_disabled,
	short_account,
} from "./common";
import { setup_dialog_grid } from "./sheet_window";

// ---------- Warehouses ----------

const WAREHOUSE_KINDS = ["Our warehouse", "Job worker", "Work in progress"];

// Every warehouse with its kind, job worker and stock line; the ones only on a stock sheet are new
// after: what to open again once the window closes, saved or not
export function show_warehouses(frm, { job_workers = false, after = null } = {}) {
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
