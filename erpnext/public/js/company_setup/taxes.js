import {
	call_method,
	close_and_reload,
	get_data,
	grey_line,
	make_alerts,
	make_kv_lines,
	short_account,
} from "./common";

// ---------- Taxes ----------

// ERPNext's own tax templates, one row per tax line; no rows means no tax is charged
export function show_taxes(frm) {
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
			dialog.refresh_sections();
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
