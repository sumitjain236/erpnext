import {
	UNDOING_TEXT,
	after_run,
	call_method,
	cheque_words,
	count_assets,
	count_balances,
	count_rows,
	download_sheet,
	format_time_posted,
	get_data,
	grey_line,
	make_back_header,
	make_box,
	make_posting_reds,
	make_search_table,
	make_text,
	make_title_cell,
	make_view_button,
	money,
	plural,
	source_name,
} from "./common";
import { setup_dialog_grid } from "./sheet_window";
import { PARTY } from "./overview";
import { make_delete_choice } from "./go_live";
import { show_opening_invoices } from "./invoices";
import { count_entries, count_invoices, invoice_word } from "./invoice_posting";

export function invoice_buttons(frm, party_type) {
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

export function invoice_menu(frm, party_type) {
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
export function show_posted(frm, kind, open_undone) {
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

export const UNDONE_TABLES = { Customer: "undone_sales_invoices", Supplier: "undone_purchase_invoices" };

// What an undo did, and what it couldn't
// Entries an undo had to keep cancelled, and why: one line
export function kept_cancelled_line(result) {
	const kept = result?.kept_cancelled || [];
	if (!kept.length) return "";
	return kept.length === 1 ? kept[0] : __("{0} entries are kept cancelled. {1}", [kept.length, kept[0]]);
}

export function show_undo_result(frm, kind, result, source) {
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
