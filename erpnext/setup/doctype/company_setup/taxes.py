# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Taxes: ERPNext's own tax step, for countries with no compliance app. One row per tax line; the
rows of one title make one Sales or Purchase Taxes and Charges Template. A compliance app takes its
place with "replaces": "taxes" in its company_setup_steps hook."""

import frappe
from frappe import _
from frappe.utils import cstr, flt

TEMPLATE_DOCTYPES = {
	"Sales": "Sales Taxes and Charges Template",
	"Purchase": "Purchase Taxes and Charges Template",
}


def get_tax_accounts(company: str) -> list[str]:
	"""The ledgers a tax template can post to: this company's accounts of type Tax."""
	return frappe.get_all(
		"Account",
		filters={"company": company, "account_type": "Tax", "is_group": 0, "disabled": 0},
		pluck="name",
		order_by="lft",
	)


def get_taxes(onboarding) -> dict:
	"""What the Taxes window shows: the saved rows, and the accounts it can pick."""
	from erpnext.setup.doctype.company_setup.company_setup import get_account_parent

	# before the first save, the templates already in ERPNext, as rows to review
	rows = (
		onboarding.tax_templates
		if onboarding.taxes_saved or onboarding.tax_templates
		else get_existing_rows(onboarding.company)
	)
	own = {row.template for row in rows if row.template}
	return {
		"rows": [
			{field: row.get(field) for field in ("tax_type", "title", "account", "rate", "template")}
			for row in rows
		],
		"from_existing": bool(rows) and not onboarding.tax_templates,
		"accounts": get_tax_accounts(onboarding.company),
		"saved": onboarding.taxes_saved,
		# the title of this step's default template of each type
		"defaults": {
			tax_type: frappe.db.get_value(
				doctype, {"name": ("in", list(own) or [""]), "is_default": 1, "disabled": 0}, "title"
			)
			for tax_type, doctype in TEMPLATE_DOCTYPES.items()
		},
		# templates made elsewhere, like at setup or by a compliance app: invoices can pick them too
		"other_templates": [
			{"tax_type": tax_type, "doctype": doctype, **template}
			for tax_type, doctype in TEMPLATE_DOCTYPES.items()
			for template in frappe.get_all(
				doctype,
				filters={"company": onboarding.company, "disabled": 0, "name": ("not in", list(own) or [""])},
				fields=["name", "title", "is_default", "tax_category"],
				order_by="title",
			)
		],
		# where a new tax account goes: the group of tax accounts, like Duties and Taxes
		"tax_group": get_account_parent(onboarding.company, "Tax", "Liability"),
		"country": frappe.get_cached_value("Company", onboarding.company, "country"),
		# replacing the chart deletes every tax template, a compliance app's too
		"chart_replaced": onboarding.chart_of_accounts_source == "Imported",
	}


def save_taxes(onboarding, rows: list[dict], defaults: dict | None = None):
	"""Keep the rows and make or update a tax template for each title: its rows are its tax lines, so
	CGST 9% and SGST 9% under one title make one template. No rows means the company charges no tax,
	and the step is done too. defaults: the title to make the default, by Sales or Purchase."""
	accounts = set(get_tax_accounts(onboarding.company))
	own = {(row.tax_type, row.template) for row in onboarding.tax_templates if row.template}
	# templates already in ERPNext that the window showed: kept as they are unless their lines change
	shown = [frappe._dict(row).template for row in rows if frappe._dict(row).get("template")]
	existing = get_existing_rows(onboarding.company, shown) if shown else []
	own |= {(row.tax_type, row.template) for row in existing}
	saved = get_templates(onboarding.tax_templates) | get_templates(existing)
	templates = {}
	for number, row in enumerate(rows, 1):
		row = frappe._dict(row)
		title = " ".join(cstr(row.title).split())
		has_rate = row.rate not in (None, "")
		if not title and not row.account and not has_rate:
			continue
		if row.tax_type not in TEMPLATE_DOCTYPES:
			frappe.throw(
				_("Pick Sales or Purchase for {0}.").format(frappe.bold(title)), title=_("Type missing")
			)
		if not title:
			frappe.throw(
				_("Name the template that uses {0}.").format(
					frappe.bold(row.account or _("the rate {0}%").format(flt(row.rate)))
				),
				title=_("Name missing"),
			)
		if not row.account:
			frappe.throw(
				_("Pick a tax account for {0} (row {1}).").format(frappe.bold(title), number),
				title=_("Tax account missing"),
			)
		if row.account not in accounts:
			frappe.throw(
				_("{0} is not a tax account of {1}. Pick an account of type Tax.").format(
					frappe.bold(row.account), onboarding.company
				),
				title=_("Not a tax account"),
			)
		if not has_rate:
			frappe.throw(
				_("Type the rate of {0} in {1}. Type 0 for a nil rate.").format(
					frappe.bold(row.account), frappe.bold(title)
				),
				title=_("Rate missing"),
			)
		template = templates.setdefault((row.tax_type, title.casefold()), frappe._dict(title=title, lines=[]))
		# one agency may take two taxes, like state and county sales tax, but not the same one twice
		if (row.account, flt(row.rate)) in template.lines:
			frappe.throw(
				_("{0} has {1} at {2}% twice.").format(
					frappe.bold(title), frappe.bold(row.account), flt(row.rate)
				),
				title=_("Same tax line twice"),
			)
		template.lines.append((row.account, flt(row.rate)))
	onboarding.tax_templates = []
	for (tax_type, _key), template in templates.items():
		made = saved.get((tax_type, template.title, tuple(template.lines)))
		if not made or not frappe.db.exists(TEMPLATE_DOCTYPES[tax_type], made):
			made = make_template(
				onboarding.company,
				tax_type,
				template.title,
				template.lines,
				{name for own_type, name in own if own_type == tax_type},
			)
		for account, rate in template.lines:
			onboarding.append(
				"tax_templates",
				{
					"tax_type": tax_type,
					"title": template.title,
					"account": account,
					"rate": rate,
					"template": made,
				},
			)
	kept = {(row.tax_type, row.template) for row in onboarding.tax_templates}
	for tax_type, name in sorted(own - kept):
		disable_template(TEMPLATE_DOCTYPES[tax_type], name)
	for tax_type in TEMPLATE_DOCTYPES:
		set_default_template(onboarding, tax_type, (defaults or {}).get(tax_type))
	onboarding.taxes_saved = 1


def get_existing_rows(company: str, names: list[str] | None = None) -> list[frappe._dict]:
	"""The tax lines of the company's enabled templates, as rows of this step."""
	rows = []
	for tax_type, doctype in TEMPLATE_DOCTYPES.items():
		filters = {"company": company, "disabled": 0}
		if names is not None:
			filters["name"] = ("in", names or [""])
		templates = frappe.get_all(doctype, filters=filters, fields=["name", "title"], order_by="title")
		lines = frappe.get_all(
			"Sales Taxes and Charges" if tax_type == "Sales" else "Purchase Taxes and Charges",
			filters={"parenttype": doctype, "parent": ("in", [t.name for t in templates] or [""])},
			fields=["parent", "account_head", "rate"]
			+ (["add_deduct_tax"] if tax_type == "Purchase" else []),
			order_by="idx",
		)
		for template in templates:
			rows.extend(
				frappe._dict(
					tax_type=tax_type,
					title=template.title,
					account=line.account_head,
					rate=-line.rate if line.get("add_deduct_tax") == "Deduct" else line.rate,
					template=template.name,
				)
				for line in lines
				if line.parent == template.name
			)
	return rows


def get_templates(rows: list) -> dict[tuple, str]:
	"""The template made for each type, title and tax lines saved."""
	lines = {}
	for row in rows:
		if row.template:
			lines.setdefault((row.tax_type, row.title, row.template), []).append((row.account, flt(row.rate)))
	return {
		(tax_type, title, tuple(template_lines)): template
		for (tax_type, title, template), template_lines in lines.items()
	}


def make_template(company: str, tax_type: str, title: str, lines: list[tuple], own: set[str]) -> str:
	"""The template of this title for the company, made, or set to these tax lines when this step
	made it."""
	doctype = TEMPLATE_DOCTYPES[tax_type]
	name = frappe.db.get_value(doctype, {"title": title, "company": company})
	doc = frappe.get_doc(doctype, name) if name else frappe.new_doc(doctype)
	if name and name not in own and not is_disabled_step_template(doc, title):
		frappe.throw(
			_("{0} already exists. Pick another name, or edit it in {1}.").format(
				frappe.bold(title), _(doctype)
			),
			title=_("Template exists"),
		)
	doc.update({"title": title, "company": company, "disabled": 0})
	doc.set(
		"taxes",
		[
			{
				"charge_type": "On Net Total",
				"account_head": account,
				"rate": rate,
				"description": get_line_description(title, account, len(lines), rate),
			}
			for account, rate in lines
		],
	)
	doc.save()
	return doc.name


def get_line_description(title: str, account: str, lines: int, rate: float | None = None) -> str:
	"""The template's title on a single tax line, else the line's account name and rate, so state
	and county tax paid to one agency read apart on an invoice."""
	if lines == 1:
		return title
	name = frappe.get_cached_value("Account", account, "account_name")
	return name if rate is None else _("{0} {1}%").format(name, f"{flt(rate):g}")


def is_disabled_step_template(doc, title: str) -> bool:
	"""A template this step made and disabled when its rows were removed: its tax lines described as
	this step describes them, now or before rates were added to the description."""
	return bool(doc.disabled) and all(
		tax.charge_type == "On Net Total"
		and tax.description
		in (
			get_line_description(title, tax.account_head, len(doc.taxes), tax.rate),
			get_line_description(title, tax.account_head, len(doc.taxes)),
		)
		for tax in doc.taxes
	)


def disable_template(doctype: str, name: str):
	"""A template the step made whose row is gone: no longer offered, so new documents aren't taxed."""
	if not frappe.db.exists(doctype, name):
		return
	doc = frappe.get_doc(doctype, name)
	doc.update({"disabled": 1, "is_default": 0})
	doc.save()


def set_default_template(onboarding, tax_type: str, title: str | None = None):
	"""The template of the title picked becomes the default, and a default made elsewhere, like at
	setup, gives way. None picked: none of this step's templates is the default, as a company may
	sell exempt services too; never one set without asking."""
	doctype = TEMPLATE_DOCTYPES[tax_type]
	own = [row.template for row in onboarding.tax_templates if row.tax_type == tax_type]
	picked = next(
		(row.template for row in onboarding.tax_templates if row.tax_type == tax_type and row.title == title),
		None,
	)
	if picked:
		# saving the default clears the company's other defaults
		doc = frappe.get_doc(doctype, picked)
		doc.is_default = 1
		doc.save()
		return
	for name in frappe.get_all(doctype, filters={"name": ("in", own or [""]), "is_default": 1}, pluck="name"):
		frappe.db.set_value(doctype, name, "is_default", 0)


def get_taxes_status(onboarding) -> frappe._dict:
	return frappe._dict(
		saved=onboarding.taxes_saved,
		templates=len({(row.tax_type, row.template) for row in onboarding.tax_templates if row.template}),
	)
