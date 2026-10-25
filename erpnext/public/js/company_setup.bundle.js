import { CHECK_COUNTRIES, call_method, set_says_check } from "./company_setup/common";
import "./company_setup/where_to_find";
import "./company_setup/sheet_window";
import {
	TABS,
	get_steps,
	listen_for_postings,
	make_checklist,
	make_missing_roles,
	make_overview_stats,
	make_page,
	make_task_card,
	show_tabs_cue,
	update_posting,
	watch_stock_posting,
} from "./company_setup/overview";
import { make_company_card } from "./company_setup/company";
import "./company_setup/books";
import { update_year_close, watch_year_close } from "./company_setup/close_year";
import { make_live_note } from "./company_setup/go_live";
import "./company_setup/balances";
import "./company_setup/invoices_posted";
import "./company_setup/invoices";
import "./company_setup/invoice_posting";
import "./company_setup/imports";
import "./company_setup/chart";
import "./company_setup/bank";
import "./company_setup/bank_window";
import "./company_setup/taxes";
import "./company_setup/warehouses";
import "./company_setup/stock";
import "./company_setup/posted";
import "./company_setup/orders";
import "./company_setup/assets";

frappe.provide("erpnext.company_setup");

Object.assign(erpnext.company_setup, {
	CHECK_COUNTRIES,
	call_method,
	set_says_check,
	TABS,
	get_steps,
	listen_for_postings,
	make_checklist,
	make_missing_roles,
	make_overview_stats,
	make_page,
	make_task_card,
	show_tabs_cue,
	update_posting,
	watch_stock_posting,
	make_company_card,
	update_year_close,
	watch_year_close,
	make_live_note,
});
