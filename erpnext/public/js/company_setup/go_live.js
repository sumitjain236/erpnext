import { call_method, fix_button, get_data, grey_line, make_alerts, plural, reload_fresh } from "./common";
import { PARTY, get_steps, open_step, show_tab } from "./overview";
import { get_books_reminders, show_posting_preview } from "./books";
import { show_bank_accounts } from "./bank";

// What is left, as reminders only: nothing here stops going live
export function show_go_live(frm) {
	const reminders = frm.doc.__onload?.go_live_reminders || {};
	const currency = erpnext.get_currency(frm.doc.company);
	const temporary = flt(reminders.temporary_opening);
	const steps = get_steps(frm);
	const by_key = (key) => steps.find((step) => step.key === key);
	// money left in Temporary Opening already says Check your books is not done
	const open_steps = (reminders.steps || [])
		.map(by_key)
		.filter((step) => step && !(step.name === "check_books" && temporary));
	// a step left while the trial balance has money for it, like fixed assets, is a gap; other
	// skipped steps only get a grey line
	const gaps = (reminders.gaps || []).map(by_key).filter((step) => step && !open_steps.includes(step));
	const skipped = (reminders.skipped || []).map(by_key).filter((step) => step && !gaps.includes(step));
	const open = (step) => () => {
		dialog.hide();
		open_step(frm, step);
	};
	const alerts = [
		reminders.closed_drafts?.length && {
			theme: "red",
			title: __("These opening drafts can no longer be posted"),
			description: __(
				"{0}. They are dated on or before {1}, and that year is closed. Remove them, or enter them as normal entries after the migration date.",
				[reminders.closed_drafts.join(" · "), frappe.datetime.str_to_user(reminders.closed_to)]
			),
			buttons: [
				{
					label: __("Remove drafts"),
					onclick: () =>
						frappe.confirm(
							__("Remove these opening drafts: {0}?", [
								frappe.utils.escape_html(reminders.closed_drafts.join(" · ")),
							]),
							() =>
								call_method(frm, "remove_closed_drafts", {}, { freeze: true }).then(() => {
									dialog.hide();
									reload_fresh(frm).then(() => show_go_live(frm));
								})
						),
				},
			],
		},
		temporary && {
			theme: "red",
			title: __("Temporary Opening is {0}, so it is not zero.", [
				format_currency(Math.abs(temporary), currency),
			]),
			buttons: [
				{
					label: __("See your books"),
					onclick: () => {
						dialog.hide();
						show_posting_preview(frm, false, () => show_go_live(frm));
					},
				},
			],
		},
		...open_steps.map((step) => {
			const party_type = {
				opening_sales_invoices: "Customer",
				opening_purchase_invoices: "Supplier",
			}[step.name];
			const table = party_type && PARTY[party_type].table;
			const drafts =
				party_type &&
				(frm.doc[table] || []).filter((row) => row.status !== "Created").length -
					flt(reminders.closed_draft_counts?.[table]);
			if (party_type && !drafts && reminders.closed_draft_counts?.[table]) return null;
			if (!drafts) {
				return {
					title: __("{0} is not done.", [step.title]),
					description: step.summary || undefined,
					buttons: [{ label: __("Open"), onclick: open(step) }],
				};
			}
			return party_type === "Customer"
				? {
						title: plural(
							drafts,
							__("1 opening invoice is still a draft."),
							__("{0} opening invoices are still drafts.", [drafts])
						),
						buttons: [{ label: __("Open invoices"), onclick: open(step) }],
				  }
				: {
						title: plural(
							drafts,
							__("1 opening bill is still a draft."),
							__("{0} opening bills are still drafts.", [drafts])
						),
						buttons: [{ label: __("Open bills"), onclick: open(step) }],
				  };
		}),
		...gaps.map((step) => ({
			title: step.is_skipped
				? __("{0} is skipped, but your trial balance has lines for it.", [step.title])
				: __("{0} is not done, and your trial balance has lines for it.", [step.title]),
			description: step.summary || undefined,
			buttons: [{ label: __("Open"), onclick: open(step) }],
		})),
		...get_books_reminders(frm),
		reminders.stock_reposts?.running && {
			title: plural(
				reminders.stock_reposts.running,
				__("1 stock cost update is still running."),
				__("{0} stock cost updates are still running.", [reminders.stock_reposts.running])
			),
			description: __(
				"They usually finish within the hour. Stock values and closing the year wait for them."
			),
		},
		reminders.stock_reposts?.failed && {
			title: plural(
				reminders.stock_reposts.failed,
				__("1 stock cost update failed."),
				__("{0} stock cost updates failed.", [reminders.stock_reposts.failed])
			),
			description: __("Stock values after the opening may be off until it runs again."),
			buttons: [
				{
					label: __("Open"),
					onclick: () => frappe.set_route("List", "Repost Item Valuation", { status: "Failed" }),
				},
			],
		},
		reminders.bank_accounts_without_number && {
			title:
				reminders.bank_accounts_without_number === 1
					? __("1 bank account has no account number.")
					: __("{0} bank accounts have no account number.", [
							reminders.bank_accounts_without_number,
					  ]),
			description: __("Add it before your first bank payment."),
			buttons: [
				{
					label: __("Add bank details"),
					onclick: () => {
						dialog.hide();
						show_tab(frm, "accounts_tab");
						show_bank_accounts(frm);
					},
				},
			],
		},
	]
		.filter(Boolean)
		.map((alert) => ({ theme: "yellow", ...alert }));
	const left = Boolean(temporary || open_steps.length || gaps.length);
	const notes = [
		skipped.length &&
			grey_line(
				__("Skipped: {0}. Your trial balance has nothing waiting for them.", [
					skipped.map((step) => step.title).join(", "),
				])
			),
		reminders.missing_years?.length &&
			(frappe.model.can_create("Fiscal Year")
				? frappe.ui.alert({
						theme: "blue",
						title: __("Going live adds the financial year {0}", [
							reminders.missing_years.join(", "),
						]),
						description: __("Your entries from the migration date on go in it."),
				  })
				: make_alerts([
						{
							theme: "yellow",
							title: __("Ask a System Manager to add the financial year {0}", [
								reminders.missing_years.join(", "),
							]),
							description: __("Your entries from the migration date on go in it."),
							buttons: [
								fix_button(
									{ label: __("New Fiscal Year"), doctype: "Fiscal Year", name: "new" },
									frm
								),
							],
						},
				  ])),
	].filter(Boolean);

	const dialog = new frappe.ui.Dialog({
		title: __("Go live"),
		fields: [{ fieldname: "body", fieldtype: "HTML" }],
		primary_action_label: __("Go live"),
		primary_action: () => show_confirm(),
		secondary_action_label: __("Close"),
		secondary_action: () => dialog.hide(),
	});
	const $body = dialog.get_field("body").$wrapper;

	const show_checks = () => {
		dialog.set_title(__("Go live"));
		$body
			.empty()
			.append(
				$(`<div class="flex flex-col gap-3"></div>`).append(
					grey_line(__("Start using ERPNext. We point out anything left, but nothing stops you.")),
					make_alerts(alerts),
					notes
				)
			);
		dialog.set_primary_action(__("Go live"), () => show_confirm());
		dialog.set_secondary_action_label(__("Close"));
		dialog.set_secondary_action(() => dialog.hide());
	};
	const show_confirm = () => {
		dialog.set_title(__("Go live?"));
		$body
			.empty()
			.append(
				$(`<div class="flex flex-col gap-3"></div>`).append(
					$(`<div class="text-p-base text-ink-gray-8"></div>`).text(
						__("After you go live you can't undo opening entries here.")
					),
					alerts.length
						? [
								$(`<div class="text-sm-medium text-ink-gray-5"></div>`).text(
									__("Still open")
								),
								$(`<div class="flex flex-col gap-2"></div>`).append(
									alerts.map((alert) =>
										$(`<div class="flex items-start gap-2"></div>`).append(
											frappe.ui.badge(
												alert.theme === "red"
													? { label: __("Not done"), theme: "red" }
													: { label: __("Check"), theme: "amber" }
											),
											$(`<div class="text-p-sm text-ink-gray-7 min-w-0"></div>`).text(
												alert.title
											)
										)
									)
								),
						  ]
						: grey_line(__("Your books match your trial balance.")),
					grey_line(__("Nothing changes until you confirm."))
				)
			);
		dialog.set_primary_action(left ? __("Go live anyway") : __("Go live"), () =>
			call_method(frm, "go_live", { accept_temporary_opening: temporary ? 1 : 0 }).then(() =>
				reload_fresh(frm).then(() => show_live())
			)
		);
		dialog.set_secondary_action_label(__("Back to checks"));
		dialog.set_secondary_action(() => show_checks());
	};
	const show_live = () => {
		dialog.set_title(__("Go live"));
		$body.empty().append(make_live_note(frm, () => dialog.hide()));
		dialog.set_primary_action(__("Close"), () => dialog.hide());
		dialog.get_secondary_btn().addClass("hide");
	};
	show_checks();
	dialog.show();
}

// After going live: the company is live, and the first things to do in ERPNext
export function make_live_note(frm, before_open) {
	const go = (open) => () => {
		before_open?.();
		open();
	};
	const has_bank = Boolean(frm.doc.__onload?.bank?.accounts);
	const from = frappe.datetime.str_to_user(frm.doc.migration_date);
	return $(`<div class="flex flex-col items-center text-center gap-2 py-3"></div>`).append(
		$(`<div class="text-lg-medium text-ink-gray-8"></div>`).text(__("{0} is live", [frm.doc.company])),
		grey_line(
			has_bank
				? __("Next: make your first invoice · reconcile banks from {0}", [from])
				: __("Next: make your first invoice")
		),
		$(`<div class="flex flex-wrap justify-center gap-2 pt-1"></div>`).append(
			frappe.ui.button({
				label: __("New Sales Invoice"),
				size: "sm",
				onclick: go(() => frappe.new_doc("Sales Invoice", { company: frm.doc.company })),
			}),
			has_bank &&
				frappe.ui.button({
					label: __("Bank Reconciliation"),
					size: "sm",
					onclick: go(() => frappe.set_route("Form", "Bank Reconciliation Tool")),
				})
		)
	);
}

// In an undo confirm: what Accounts Settings does with a deleted document's ledger entries, and a
// choice for this undo, starting from that setting, shown only when a document has ledger entries.
// what: like "5 invoices"; one: like "invoice"; example: a re-post's name; kind, source and names:
// the undo, for the server to say which of its documents have ledger entries. delete_entries()
// gives the choice, null until it shows.
export function make_delete_choice(
	frm,
	{ count, what, one = what, example, has_ledger = true, kind, source, names }
) {
	const no_ledger = () =>
		plural(
			count,
			__("This {0} has no ledger entries, so it is deleted.", [one]),
			__("These {0} have no ledger entries, so they are deleted.", [what])
		);
	const $choice = $(`<div class="flex flex-col gap-2"></div>`);
	$choice.delete_entries = () => null;
	if (!has_ledger) return $choice.append(grey_line(no_ledger()));
	const $line = grey_line(__("Reading Accounts Settings…")).appendTo($choice);
	const $options = $(`<div class="flex flex-col gap-2"></div>`).appendTo($choice);
	const $result = $(`<div class="text-p-sm text-ink-gray-7"></div>`).appendTo($choice);
	const group = frappe.utils.get_random(8);
	$choice.delete_entries = () => {
		const picked = $options.find("input:checked").val();
		return picked === undefined ? null : picked === "1";
	};
	const new_numbers = () =>
		example
			? plural(
					count,
					__("Posting again gives it a new number, like {0}.", [example]),
					__("Posting again gives them new numbers, like {0}.", [example])
			  )
			: plural(
					count,
					__("Posting again gives it a new number."),
					__("Posting again gives them new numbers.")
			  );
	// unticked, the documents with no ledger entries are still deleted
	const kept_text = (split) => {
		const kept = count_documents(split.with_ledger);
		const deleted = count_documents(split.without_ledger);
		if (!kept) return no_ledger();
		if (deleted) {
			return (
				__(
					"{0} will be deleted (no ledger entries). {1} will stay cancelled with their ledger entries.",
					[deleted, kept]
				) +
				" " +
				__("Posting again gives them new numbers.")
			);
		}
		return (
			plural(
				count,
				__("This {0} will stay as a cancelled document with its ledger entries.", [one]),
				__("These {0} will stay as cancelled documents with their ledger entries.", [what])
			) +
			" " +
			new_numbers()
		);
	};
	// ticked, every document goes; named by kind when there is more than one kind
	const deleted_text = (split) => {
		const all = { ...split.without_ledger };
		for (const [doctype, n] of Object.entries(split.with_ledger)) all[doctype] = (all[doctype] || 0) + n;
		if (Object.keys(all).length < 2) return null;
		return __("{0} will be deleted, with their ledger entries. Posting again keeps their numbers.", [
			count_documents(all),
		]);
	};
	const say = (deleting, split) =>
		$result.text(
			deleting
				? deleted_text(split) ||
						plural(
							count,
							__(
								"This {0} and its ledger entries will be deleted. Posting again keeps its number.",
								[one]
							),
							__(
								"These {0} and their ledger entries will be deleted. Posting again keeps their numbers.",
								[what]
							)
						)
				: kept_text(split)
		);
	Promise.all([
		get_data(frm, "get_delete_setting"),
		get_data(frm, "get_undo_split", { kind: kind, source: source || "", names: names || null }),
	]).then(([{ message: setting }, { message: split }]) => {
		if (!Object.keys(split.with_ledger).length) {
			$line.text(no_ledger());
			return;
		}
		$line.text(
			setting.delete
				? __("Your Accounts Settings delete ledger entries with the document.")
				: __("Your Accounts Settings keep cancelled copies of deleted entries.")
		);
		const option = (value, title, line) =>
			$(
				`<label class="flex items-start gap-2 border rounded-lg px-3 py-2 mb-0 cursor-pointer"></label>`
			)
				.append(
					$(`<input type="radio" class="mt-1">`)
						.attr({ name: group, value: value })
						.prop("checked", Boolean(value) === Boolean(setting.delete)),
					$(`<span class="flex flex-col"></span>`).append(
						$(`<span class="text-base-medium text-ink-gray-8"></span>`)
							.text(title)
							.append(
								Boolean(value) === Boolean(setting.delete)
									? $(`<span class="text-ink-gray-5"></span>`).text(
											" " + __("(your setting)")
									  )
									: []
							),
						grey_line(line)
					)
				)
				.appendTo($options);
		option(1, __("Remove completely"), __("The numbers can be used again"));
		option(
			0,
			__("Keep a cancelled copy"),
			example ? __("New numbers like {0}", [example]) : __("New numbers when posted again")
		);
		$options.on("change", "input", () => say($choice.delete_entries(), split));
		say(Boolean(setting.delete), split);
	});
	return $choice;
}

// Like "2 assets and 1 journal entry", from counts by doctype
function count_documents(counts) {
	const words = {
		"Sales Invoice": [__("invoice"), __("invoices")],
		"Purchase Invoice": [__("bill"), __("bills")],
		"Journal Entry": [__("journal entry"), __("journal entries")],
		"Stock Reconciliation": [__("stock entry"), __("stock entries")],
		Asset: [__("asset"), __("assets")],
	};
	return frappe.utils.comma_and(
		Object.entries(counts || {}).map(([doctype, n]) => {
			const [single, many] = words[doctype] || [__(doctype), __(doctype)];
			return __("{0} {1}", [n, n === 1 ? single : many]);
		})
	);
}
