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
	onload(report) {
		// The weekly attendance & pay register as a landscape PDF (the same sheet My HR downloads):
		// sites -> P/A grid -> phone/ID/rate/pay, with site and grand totals. Scoped to the caller
		// (HR/admin: every site; a coordinator: the sites they are assigned to). Uses the report's
		// To Date to choose the Mon-Sat week.
		report.page.add_inner_button(__("Download weekly register (PDF)"), () => {
			const week = report.get_filter_value("to_date") || frappe.datetime.get_today();
			const url =
				"/api/method/trident_attendance.casual_register.download_weekly_register?week=" +
				encodeURIComponent(week);
			window.open(url, "_blank");
		});
	},
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "status" && data && data.status !== "Present") {
			value = `<span style="color: var(--red-600)">${value}</span>`;
		}
		return value;
	},
};
