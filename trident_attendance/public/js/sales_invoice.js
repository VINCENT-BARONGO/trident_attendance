// Sales Invoice: the Payment Certificate picker offers only this project's certified, un-invoiced certificates.
frappe.ui.form.on("Sales Invoice", {
	setup(frm) {
		if (!frm.fields_dict.custom_payment_certificate) return;
		frm.set_query("custom_payment_certificate", () => ({
			filters: {
				status: "Certified",
				...(frm.doc.project ? { project: frm.doc.project } : {}),
			},
		}));
	},
});
