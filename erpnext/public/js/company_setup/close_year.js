import { call_method, close_and_reload, get_data, grey_line, make_alerts, reload_fresh } from "./common";
import { get_steps, make_checklist, make_task_card } from "./overview";
import { profit_words } from "./books";

// What closing the last of years takes, in order, as the server's get_year_close_steps gives it:
// each year's stock to its end when it isn't closed yet, then the year
export function get_year_close_steps(years) {
	return years.flatMap((year) => [
		...(year.stock?.needed && !year.stock.done ? [{ stock: year.year_end_date }] : []),
		{ fiscal_year: year.fiscal_year },
	]);
}

// Close a year from Check your books: one click closes the stock and the years before it too, in
// order, in the background
export function close_year(frm, year, steps) {
	const year_end = frappe.datetime.str_to_user(year.year_end_date);
	const label = year.profit > 0 ? __("Profit goes to") : __("Loss goes to");
	const dialog = new frappe.ui.Dialog({
		title: __("Close {0}", [year.fiscal_year]),
		fields: [
			{ fieldname: "note", fieldtype: "HTML" },
			{
				fieldname: "closing_account",
				fieldtype: "Link",
				options: "Account",
				label: label,
				reqd: 1,
				only_select: 1,
				description: __("An equity account, usually Reserves & Surplus."),
				get_query: () => ({
					filters: {
						company: frm.doc.company,
						is_group: 0,
						disabled: 0,
						root_type: ["in", ["Equity", "Liability"]],
					},
				}),
			},
		],
		primary_action_label: __("Close {0}", [year.fiscal_year]),
		primary_action: (values) =>
			call_method(
				frm,
				"close_year",
				{ fiscal_year: year.fiscal_year, closing_account: values.closing_account },
				{ freeze: true }
			).then(() => close_and_reload(frm, dialog)),
	});
	const years = Object.fromEntries(
		(frm.doc.__onload?.books_reminders?.unclosed_years || []).map((row) => [row.fiscal_year, row])
	);
	const note = (step) =>
		step.stock
			? __("ERPNext needs this before a year with stock is closed.")
			: step.fiscal_year === year.fiscal_year
			? year.profit > 0
				? __("The profit goes to the account below.")
				: __("The loss goes to the account below.")
			: flt(years[step.fiscal_year]?.profit, 2)
			? __("It has {0}; years close in order.", [profit_words(frm, years[step.fiscal_year].profit)])
			: __("It has entries, like old invoices; years close in order.");
	dialog
		.get_field("note")
		.$wrapper.append(
			$(`<div class="text-base text-ink-gray-8 mb-2"></div>`).text(
				__(
					"{0} has {1}. Closing moves it out of income and expenses, dated {2}. One click does all of this, in order, in the background:",
					[year.fiscal_year, profit_words(frm, year.profit), year_end]
				)
			),
			$(`<div class="flex flex-col gap-2 border rounded-md p-3 mb-2"></div>`).append(
				steps.map((step, index) =>
					make_year_close_step(
						`${index + 1}. ${year_close_step_label(step, "waiting")}`,
						"waiting",
						note(step)
					)
				)
			),
			grey_line(
				__("After this, opening entries on or before {0} can't be posted or undone.", [year_end])
			).addClass("mb-3")
		);
	if (year.drafts?.length) {
		dialog.get_field("note").$wrapper.prepend(
			make_alerts([
				{
					theme: "red",
					title: __("Post or remove the opening drafts first"),
					description: __("{0}. After the close, entries on or before {1} can't be posted.", [
						year.drafts.join(" · "),
						year_end,
					]),
				},
			]).addClass("mb-3")
		);
		dialog.get_primary_btn().prop("disabled", true);
	}
	dialog.show();
	dialog.set_value("closing_account", year.closing_account || "");
	// typing replaces the suggested account instead of adding to it
	const $closing = dialog.get_field("closing_account").$input;
	$closing?.on("focus", () => setTimeout(() => $closing.select()));
}

// A step of a year close as it reads done, running or waiting
function year_close_step_label(step, status) {
	const date = step.stock && frappe.datetime.str_to_user(step.stock);
	const labels = step.stock
		? {
				done: __("Stock closed to {0}", [date]),
				running: __("Closing stock to {0}", [date]),
				waiting: __("Close stock to {0}", [date]),
		  }
		: {
				done: __("{0} closed", [step.fiscal_year]),
				running: __("Closing {0}", [step.fiscal_year]),
				waiting: __("Close {0}", [step.fiscal_year]),
		  };
	return labels[status];
}

function make_year_close_step(label, status, note) {
	const [icon, color] = {
		done: ["circle-check", "text-ink-green-6"],
		running: ["loader-circle", "text-ink-blue-7"],
		waiting: ["circle-dashed", "text-ink-gray-5"],
	}[status];
	return $(`<div class="flex items-start gap-2"></div>`).append(
		$(`<span class="shrink-0 ${color}"></span>`).html(frappe.utils.icon(icon, "sm")),
		$(`<div class="flex flex-col min-w-0"></div>`).append(
			$(`<span class="text-base text-ink-gray-8"></span>`).text(label),
			note ? grey_line(note) : []
		)
	);
}

// A year closing in the background, on Check your books: each step ticks as it is done; one that
// failed says why and tries again from there
export function make_year_close_alert(frm) {
	const state = frm.year_close;
	if (!state) return [];
	const year = (frm.doc.__onload?.books_reminders?.unclosed_years || []).find(
		(row) => row.fiscal_year === state.fiscal_year
	);
	const steps = state.steps.map((step, index) => {
		const status = index < state.done ? "done" : index === state.done ? "running" : "waiting";
		const failed = state.error && index === state.done;
		const label = year_close_step_label(step, failed ? "waiting" : status);
		const last = step.fiscal_year === state.fiscal_year && status !== "done" && year;
		return make_year_close_step(
			last
				? __("{0} · {1} to {2}", [label, profit_words(frm, year.profit), state.closing_account])
				: label,
			failed ? "waiting" : status
		);
	});
	return frappe.ui.alert({
		theme: state.error ? "red" : "blue",
		icon: state.error ? undefined : "loader-circle",
		title: state.error
			? __("Closing {0} stopped", [state.fiscal_year])
			: __("Closing {0} in the background", [state.fiscal_year]),
		description: state.error || __("You can keep working; this updates by itself."),
		footer: () =>
			$(`<div class="flex flex-col gap-2"></div>`).append(
				steps,
				state.error
					? $(`<div class="flex gap-2 pt-1"></div>`).append(
							frappe.ui.button({
								label: __("Try again"),
								onclick: () =>
									call_method(
										frm,
										"close_year",
										{
											fiscal_year: state.fiscal_year,
											closing_account: state.closing_account,
										},
										{ freeze: true }
									).then(() => reload_fresh(frm)),
							})
					  )
					: []
			),
	});
}

// The Overview row of Check your books while a year closes: which step of how many
export function get_year_close_summary(frm) {
	const state = frm.year_close;
	const step = state.steps[state.done];
	return __("Closing {0} in the background · step {1} of {2}: {3}", [
		state.fiscal_year,
		state.done + 1,
		state.steps.length,
		step ? year_close_step_label(step, "running").toLowerCase() : "",
	]);
}

// The year close as the server has it now: the card and the Overview row show it in place, and
// the page reloads once it is done
export function update_year_close(frm, state) {
	if (cur_frm !== frm) return;
	const was = frm.year_close;
	frm.year_close = state;
	if (!state) {
		frappe.ui.toast({
			type: "success",
			message: __("{0} is closed. The Balance Sheet shows it in a few minutes.", [was?.fiscal_year]),
		});
		return frm.reload_doc();
	}
	const steps = get_steps(frm);
	$("[data-company-setup-checklist]").replaceWith(make_checklist(frm, steps));
	const books = steps.find((step) => step.name === "check_books");
	if (books) $("[data-company-setup-card='check_books']").replaceWith(make_task_card(frm, books));
	watch_year_close(frm);
}

// the realtime event can be missed: while a year closes, look again now and then
export function watch_year_close(frm) {
	if (!frm.year_close || frm.year_close.error || frm.year_close_timer) return;
	frm.year_close_timer = setTimeout(() => {
		get_data(frm, "get_year_close").then((r) => {
			frm.year_close_timer = null;
			if (cur_frm !== frm || !frm.year_close) return;
			if (JSON.stringify(r.message) !== JSON.stringify(frm.year_close)) {
				return update_year_close(frm, r.message);
			}
			watch_year_close(frm);
		});
	}, 15000);
}
