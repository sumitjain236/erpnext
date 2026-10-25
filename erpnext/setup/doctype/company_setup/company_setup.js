frappe.ui.form.on("Company Setup", {
	async setup(frm) {
		await frappe.require("company_setup.bundle.js");
		const { update_posting, listen_for_postings, update_year_close } = erpnext.company_setup;
		// invoices made in the background: how far they are, then done
		frappe.realtime.off("company_setup_invoices_progress");
		frappe.realtime.on("company_setup_invoices_progress", (data) => {
			if (data.company !== frm.doc.company) return;
			frm.posting = { ...frm.posting, [data.party_type]: { done: data.done, total: data.total } };
			update_posting(frm, data.party_type);
		});
		listen_for_postings(frm);
		frappe.realtime.off("company_setup_year_close");
		frappe.realtime.on("company_setup_year_close", (data) => {
			if (data.company === frm.doc.company) update_year_close(frm, data.state);
		});
	},

	async refresh(frm) {
		await frappe.require("company_setup.bundle.js");
		const {
			CHECK_COUNTRIES,
			set_says_check,
			watch_stock_posting,
			watch_year_close,
			show_tabs_cue,
			call_method,
		} = erpnext.company_setup;
		if (frm.is_new()) {
			// one setup per company: only companies without one can be picked
			frappe.db.get_list("Company Setup", { fields: ["company"], limit: 0 }).then((rows) => {
				const taken = rows.map((row) => row.company);
				frm.set_query("company", () => ({ filters: { name: ["not in", taken] } }));
				if (taken.includes(frm.doc.company)) frm.set_value("company", null);
			});
			return;
		}

		set_says_check(CHECK_COUNTRIES.includes(frm.doc.__onload?.company_details?.country));
		// every step saves from its own window
		frm.disable_save();
		// one setup per company: nothing to page through
		frm.page.clear_icons();
		frm.posting = Object.fromEntries(
			(frm.doc.__onload?.posting || []).map((party_type) => [
				party_type,
				frm.posting?.[party_type] || {},
			])
		);
		frm.year_close = frm.doc.__onload?.year_close;
		frm.events.render(frm);
		watch_stock_posting(frm);
		watch_year_close(frm);
		show_tabs_cue(frm);
		// a step done or undone since the last save, from the company's data, is saved now
		// once at a time: two reloads close together would save the steps twice over each other
		if (frm.doc.__onload?.steps_changed && !frm.checking_steps) {
			frm.checking_steps = true;
			call_method(frm, "check_steps")
				.then((r) => r.message && frm.reload_doc())
				.always(() => (frm.checking_steps = false));
		}
	},

	async render(frm) {
		await frappe.require(["desk_charts.bundle.js", "desk_charts.bundle.css", "company_setup.bundle.js"]);
		const {
			TABS,
			get_steps,
			make_page,
			make_company_card,
			make_missing_roles,
			make_overview_stats,
			make_live_note,
			make_checklist,
			make_task_card,
		} = erpnext.company_setup;
		const steps = get_steps(frm);
		make_page(frm, "onboarding_html").append(
			make_company_card(frm),
			make_missing_roles(frm),
			make_overview_stats(frm),
			frm.doc.status === "Live"
				? $(`<div class="contact-box mb-0"></div>`).append(make_live_note(frm))
				: [],
			make_checklist(frm, steps)
		);
		for (const tab of TABS) {
			const field = tab.replace("_tab", "_html");
			make_page(frm, field).append(
				steps.filter((step) => step.tab === tab).map((step) => make_task_card(frm, step))
			);
		}
	},
});
