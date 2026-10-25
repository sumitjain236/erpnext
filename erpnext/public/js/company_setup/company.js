import { call_method, close_and_reload, grey_line } from "./common";
import {
	FOLLOWS_TRIAL_BALANCE,
	SOURCES,
	follow_trial_balance,
	get_places,
	get_source,
	make_source_select,
	make_where_box,
} from "./where_to_find";

export function show_migration_date(frm) {
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

export function make_company_card(frm) {
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
