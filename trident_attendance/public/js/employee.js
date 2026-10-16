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
			wait_for_answer(m.request, m.employee_name || m.employee);
		},
	});
}

// How often, and for how long, the ERP is asked whether the phone has answered.
const ANSWER_POLL_MS = 4000;
const ANSWER_GIVE_UP_MS = 90000;

function wait_for_answer(request, who) {
	// The answer is shown here and kept nowhere: closing the box forgets it.
	const name = frappe.utils.escape_html(who);
	const dialog = new frappe.ui.Dialog({
		title: __("Where is {0}?", [name]),
		fields: [{ fieldname: "answer", fieldtype: "HTML" }],
	});
	const show = (html) => dialog.fields_dict.answer.$wrapper.html(`<div style="padding: 4px 0 8px">${html}</div>`);
	show(`<p class="text-muted">${__("Asked {0}'s phone. Waiting for its answer…", [name])}</p>`);
	dialog.show();

	const started = Date.now();
	let open = true;
	dialog.onhide = () => (open = false);

	const ask = () => {
		if (!open) return;
		frappe.call({
			method: "trident_attendance.locate.location_answer",
			type: "GET",
			args: { request },
			callback(r) {
				const m = r.message || {};
				if (!open) return;
				if (m.status === "waiting") {
					if (Date.now() - started < ANSWER_GIVE_UP_MS) {
						setTimeout(ask, ANSWER_POLL_MS);
					} else {
						show(`<p>${__("{0}'s phone did not answer. It may be off, offline, or not allowed to use location in the background.", [name])}</p>`);
					}
					return;
				}
				show(answer_html(m, name));
			},
			error() {
				if (open) show(`<p>${__("The answer could not be read. Press Locate now again.")}</p>`);
			},
		});
	};
	setTimeout(ask, ANSWER_POLL_MS);
}

function answer_html(m, name) {
	const esc = frappe.utils.escape_html;
	if (m.status === "not_set_up") {
		return `<p>${__("The position went to Traccar. Set the Traccar Address in Trident Attendance Settings to see it here.")}</p>`;
	}
	if (m.status === "no_device") {
		return `<p>${__("Traccar has no device for {0}. It is added when they open My HR in the employee app.", [name])}</p>`;
	}
	if (m.status === "traccar_error") {
		return `<p>${esc(m.message || __("Traccar could not be read."))}</p>`;
	}
	let where;
	if (m.site_name) {
		where = __("At {0}", [`<b>${esc(m.site_name)}</b>`]);
	} else if (m.place) {
		where = __("Near {0}", [`<b>${esc(m.place)}</b>`]) + " " + __("(at no site)");
	} else {
		where = __("At no site. The place's name could not be found.");
	}
	const when = [];
	if (m.minutes_ago != null) {
		when.push(m.minutes_ago < 1 ? __("just now") : __("{0} min ago", [m.minutes_ago]));
	}
	if (m.accuracy_m) when.push(__("to within {0} m", [m.accuracy_m]));
	let html = `<p style="font-size: 1.1em">${where}</p>`;
	if (when.length) html += `<p class="text-muted">${when.join(" · ")}</p>`;
	if (m.attribution) html += `<p class="text-muted small">${esc(m.attribution)}</p>`;
	return html;
}
