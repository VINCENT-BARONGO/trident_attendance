frappe.ui.form.on("Payment Certificate", {
	setup(frm) {
		// Only invoices of this certificate's own project can claim its retention.
		frm.set_query("retention_invoice", () => ({
			filters: { docstatus: 1, ...(frm.doc.project ? { project: frm.doc.project } : {}) },
		}));
	},
	refresh(frm) {
		if (!frm.is_new() && frm.doc.status === "Certified" && frappe.model.can_create("Sales Invoice")) {
			frm.add_custom_button(__("Raise Invoice"), () => {
				frappe.new_doc("Sales Invoice", {
					customer: frm.doc.customer,
					project: frm.doc.project,
					custom_payment_certificate: frm.doc.name,
				});
			});
		}
	},
	project(frm) {
		if (!frm.doc.project || frm.doc.valuation_no) return;
		// Next valuation number on the project.
		frappe.db
			.get_list("Payment Certificate", {
				filters: { project: frm.doc.project },
				fields: ["valuation_no"],
				order_by: "valuation_no desc",
				limit: 1,
			})
			.then((rows) => frm.set_value("valuation_no", (rows.length ? rows[0].valuation_no : 0) + 1));
	},
});
