import {
	call_method,
	cheque_words,
	count_rows,
	day_before_text,
	download_sheet,
	get_data,
	make_text,
	money,
	plural,
	short_account,
} from "./common";
import { show_undo_result } from "./invoices_posted";
import { BankAccountsWindow } from "./bank_window";
import { get_bank_details } from "./bank_details";
import { show_posted_window } from "./posted";

export function bank_menu(frm) {
	const bank = frm.doc.__onload?.bank || {};
	return [
		(bank.cheques || bank.charges || bank.deposits || (frm.doc.undone_cheques || []).length) && {
			label: cheque_words(__("Posted cheques")),
			icon: "list",
			onclick: () => show_posted_cheques(frm),
		},
		{
			label: __("Download sheet"),
			icon: "download",
			onclick: () => download_sheet(frm.doc.company, "bank_accounts.download_uncleared_cheques"),
		},
	].filter(Boolean);
}

// Like "11 cheques and 1 deposit", or "4 cheques, 1 card charge and 1 deposit"
function count_cheques(cheques, deposits, charges = 0) {
	const parts = [
		cheques && plural(cheques, cheque_words(__("1 cheque")), cheque_words(__("{0} cheques", [cheques]))),
		charges && plural(charges, __("1 card charge"), __("{0} card charges", [charges])),
		deposits && plural(deposits, __("1 deposit"), __("{0} deposits", [deposits])),
	].filter(Boolean);
	return frappe.utils.comma_and(parts);
}

// What uncleared rows count as: a credit card's issued rows are charges, not cheques
export function count_cheque_rows(frm, rows) {
	const cards = new Set(frm.doc.__onload?.bank?.card_ledgers || []);
	const issued = rows.filter((row) => row.entry_type === "Issued");
	return count_cheques(
		issued.filter((row) => !cards.has(row.bank_account)).length,
		rows.filter((row) => row.entry_type === "Deposited").length,
		issued.filter((row) => cards.has(row.bank_account)).length
	);
}

export function show_cheques_posted(frm) {
	const bank = frm.doc.__onload?.bank || {};
	frappe.ui.toast({
		type: "success",
		message: __("{0} posted on {1}.", [
			count_cheques(bank.cheques, bank.deposits, bank.charges),
			day_before_text(frm),
		]),
		duration: 10000,
	});
}

// Bank accounts: the bank details of each bank ledger, then the uncleared cheques and deposits
export function show_bank_accounts(frm) {
	get_bank_details(frm).then(([details, bank_names]) =>
		new BankAccountsWindow(frm, details, bank_names).show()
	);
}

// What was posted, by sheet: each to view, and to undo in part or whole until going live
function show_posted_cheques(frm) {
	const total_of = (rows) => rows.reduce((sum, row) => sum + flt(row.amount), 0);
	show_posted_window(frm, {
		title: cheque_words(__("Posted cheques")),
		load: () => get_data(frm, "get_posted_entries", { kind: "cheques" }).then((r) => r.message),
		posted_word: (when) => __("Posted {0}", [when]),
		group_line: (group) => count_rows(group.count),
		group_amount: (group) =>
			__("{0} issued · {1} deposited", [money(frm, group.issued), money(frm, group.deposited)]),
		undone: (posted) =>
			posted.undone.map((group) => ({
				source: group.source,
				line: __("{0} · taken out of your books", [count_rows(group.count)]),
				amount: money(frm, group.amount),
				download: () =>
					download_sheet(frm.doc.company, "bank_accounts.download_uncleared_cheques", {
						undone_from: group.source,
					}),
			})),
		fields: [
			["bank", "Data", __("Bank"), 2],
			["entry_type", "Data", __("Issued/Deposited"), 1],
			["reference_no", "Data", cheque_words(__("Cheque no")), 1],
			["reference_date", "Date", __("Date"), 1],
			["party_name", "Data", __("Party"), 2],
			["amount", "Currency", __("Amount"), 2],
			["clearance_date", "Date", __("Cleared"), 1],
		].map(([fieldname, fieldtype, label, columns]) => ({ fieldname, fieldtype, label, columns })),
		rows: (group) =>
			group.rows.map((row) => ({ ...row, bank: row.bank || short_account(frm, row.bank_account) })),
		blocked: (row) => row.clearance_date,
		blocked_alert: (rows) => ({
			title: plural(
				rows.length,
				cheque_words(__("1 cheque is already cleared.")),
				cheque_words(__("{0} cheques are already cleared.", [rows.length]))
			),
			description: __("Remove the clearance date first."),
		}),
		undo_label: (selected, group) =>
			selected.length
				? __("Undo {0} selected · {1}", [selected.length, money(frm, total_of(selected))])
				: __("Undo this upload"),
		confirm_title: (rows) => __("Undo {0}?", [count_cheque_rows(frm, rows)]),
		confirm_text: (rows) =>
			plural(
				rows.length,
				__("You can download it, fix it and upload again."),
				__("You can download them, fix them and upload again.")
			),
		confirm_what: (rows, selected) => ({
			count: rows.length,
			kind: "cheques",
			names: selected ? selected.map((row) => row.journal_entry) : null,
			what: count_cheque_rows(frm, rows),
			one: rows[0]?.entry_type === "Deposited" ? __("deposit") : cheque_words(__("cheque")),
		}),
		confirm_rows: (rows) =>
			rows.map((row) => [
				make_text(row.reference_no || "—", "text-ink-gray-8 shrink-0"),
				make_text(
					[row.party_name, short_account(frm, row.bank_account)].filter(Boolean).join(" · "),
					"text-ink-gray-5 grow truncate"
				),
				make_text(money(frm, row.amount), "text-ink-gray-8 shrink-0"),
			]),
		total: total_of,
		confirm_button: (rows) => __("Undo {0}", [count_cheque_rows(frm, rows)]),
		undo: (group, selected, delete_entries) =>
			call_method(frm, "undo_uncleared_cheques", {
				source: group.source,
				entries: selected ? selected.map((row) => row.journal_entry) : null,
				delete_entries: delete_entries,
			}),
		after_undo: (result, group) => show_undo_result(frm, "cheques", result, group.source),
	});
}
