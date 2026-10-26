import { call_method, grey_line, pick_sheet, reload_fresh } from "./common";

export function import_buttons(frm, doctype, step) {
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
export function import_blocked(frm, doctype) {
	return frm.doc.__onload?.import_blocked?.[doctype];
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

export function open_party_import(frm, doctype, after = null, asked = false) {
	if (!frappe.model.can_create("Data Import")) return;
	if (import_blocked(frm, doctype)) {
		return frappe.msgprint({ title: __("Can't import"), message: import_blocked(frm, doctype) });
	}
	if (doctype === "Item" && !asked && frm.doc.__onload?.serial_batch_off && !frm.doc.__onload?.has_items) {
		return ask_serial_and_batch(frm).then(() => open_party_import(frm, doctype, after, true));
	}
	const open = (data_import) =>
		frappe.require("data_import_tools.bundle.js", () => {
			frappe.data_import.open_data_import_dialog({
				data_import: data_import,
				reference_doctype: doctype,
				import_type: "Insert New Records",
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

// A blank sheet of the fields Data Import needs, with the required child table fields too
export function download_import_sample(doctype) {
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

export function items_import_menu(frm) {
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
