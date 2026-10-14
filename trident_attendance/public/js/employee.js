// Employee form: whether the employee has chosen an app PIN, HR's "Reset App PIN", and
// "Locate now" for those who may ask where a clocked-in person is.
// The server checks the role again; this only decides what is shown.

frappe.ui.form.on("Employee", {
	refresh(frm) {
		if (frm.is_new()) return;
		show_locate_button(frm);
		if (!frm.doc.custom_app_access) return;
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

// Whether this user may locate people: asked once per page load, not on every refresh.
let may_locate = null;

function show_locate_button(frm) {
	if (frm.doc.status !== "Active" || !frm.doc.user_id) return;
	if (!may_locate) {
		may_locate = new Promise((resolve) => {
			frappe.call({
				method: "trident_attendance.locate.may_locate",
				type: "GET",
				callback: (r) => resolve(Boolean(r.message)),
				error: () => resolve(false),
			});
		});
	}
	const employee = frm.doc.name;
	may_locate.then((allowed) => {
		if (!allowed || frm.doc.name !== employee) return;
		frm.add_custom_button(__("Locate now"), () => locate_now(frm), __("Actions"));
	});
}

function locate_now(frm) {
	// The request is recorded (Location Request: who asked about whom, and when); the
	// employee's phone shows nothing. A refusal (not clocked in,
	// no phone registered, asked a moment ago) is shown by the ERP as the server words it.
	frappe.call({
		method: "trident_attendance.locate.request_location",
		args: { employee: frm.doc.name },
		freeze: true,
		freeze_message: __("Asking the phone…"),
		callback(r) {
			const m = r.message;
			if (!m || !m.request) return;
			frappe.show_alert(
				{
					message: __("Asked {0}'s phone for its location. It appears in Traccar within a minute.", [
						frappe.utils.escape_html(m.employee_name || m.employee),
					]),
					indicator: "green",
				},
				10
			);
		},
	});
}
