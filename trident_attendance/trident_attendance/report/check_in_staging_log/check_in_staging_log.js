frappe.query_reports["Check-in Staging Log"] = {
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
		{ fieldname: "employee", label: __("Employee"), fieldtype: "Link", options: "Employee" },
		{
			fieldname: "review_status",
			label: __("Review Status"),
			fieldtype: "Select",
			options: ["", "Pending Review", "Auto-Released", "Released", "Marked", "Rejected"],
		},
		{
			fieldname: "reason",
			label: __("Hold Reason contains"),
			fieldtype: "Data",
			description: __("e.g. Missing OUT, Face, geofence, Supervisor"),
		},
		{ fieldname: "supervisor", label: __("Posted By (User)"), fieldtype: "Link", options: "User" },
		{
			fieldname: "include_all_sources",
			label: __("Include device punches"),
			fieldtype: "Check",
			default: 0,
		},
	],
};
