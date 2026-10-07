// Employee form: whether the employee has chosen an app PIN, and HR's "Reset App PIN".
// The server checks the role again; this only decides what is shown.

frappe.ui.form.on("Employee", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.custom_app_access) return;
		const roles = ["HR Manager", "HR User", "Attendance Admin", "System Manager"];
		if (!roles.some((role) => frappe.user.has_role(role))) return;
		show_app_pin_status(frm);
		frm.add_custom_button(__("Reset App PIN"), () => reset_app_pin(frm));
	},
});

function show_app_pin_status(frm) {
	frappe.call({
		method: "trident_attendance.employee_access.get_employee_pin_status",
		args: { employee: frm.doc.name },
		callback(r) {
			const m = r.message || {};
			if (!m.ok || frm.doc.name !== m.employee) return;
			if (m.pin_locked) {
				frm.dashboard.add_indicator(__("App PIN: locked after wrong tries"), "red");
			} else if (m.pin_set) {
				frm.dashboard.add_indicator(__("App PIN: set"), "green");
			} else {
				frm.dashboard.add_indicator(__("App PIN: not chosen yet"), "orange");
			}
		},
	});
}

function reset_app_pin(frm) {
	frappe.confirm(
		__(
			"Reset the app PIN of {0}? They are signed out of the employee app and choose a new PIN the next time they open it.",
			[frappe.utils.escape_html(frm.doc.employee_name || frm.doc.name)]
		),
		() => {
			frappe.call({
				method: "trident_attendance.employee_access.reset_employee_pin",
				args: { employee: frm.doc.name },
				freeze: true,
				callback(r) {
					if (!(r.message || {}).ok) return;
					frappe.show_alert({ message: __("App PIN reset."), indicator: "green" });
					frm.reload_doc();
				},
			});
		}
	);
}
