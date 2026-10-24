frappe.query_reports["Casual Attendance"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -6),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{ fieldname: "project", label: __("Project"), fieldtype: "Link", options: "Project" },
		{ fieldname: "subcontractor", label: __("Subcontractor"), fieldtype: "Link", options: "Supplier" },
		{ fieldname: "casual_labourer", label: __("Casual"), fieldtype: "Link", options: "Casual Labourer" },
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: ["", "Present", "Missing check-out", "Missing check-in"],
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "status" && data && data.status !== "Present") {
			value = `<span style="color: var(--red-600)">${value}</span>`;
		}
		return value;
	},
};
