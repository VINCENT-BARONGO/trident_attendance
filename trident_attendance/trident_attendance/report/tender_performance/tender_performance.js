frappe.query_reports["Tender Performance"] = {
	filters: [
		{ fieldname: "year", label: __("Year"), fieldtype: "Int", default: new Date().getFullYear(), reqd: 1 },
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company", default: frappe.defaults.get_user_default("Company") },
		{ fieldname: "prepared_by", label: __("Prepared By"), fieldtype: "Link", options: "User" },
		{ fieldname: "submitted_target", label: __("Target: value submitted in the year"), fieldtype: "Currency", default: 3000000000 },
		{ fieldname: "won_target", label: __("Target: value won in the year"), fieldtype: "Currency", default: 700000000 },
	],
};
