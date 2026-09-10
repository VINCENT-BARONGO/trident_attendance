frappe.ui.form.on("Trident Attendance Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Open Review Page"), () => window.open("/attendance-review", "_blank"));
		frm.add_custom_button(__("Setup Audit"), () => frappe.set_route("query-report", "Setup Audit"));
		frm.add_custom_button(__("Run Log"), () => frappe.set_route("List", "Trident Attendance Run"));
		frm.add_custom_button(__("Run Processing Now"), () => run_processing(frm, 0), __("Actions"));
		frm.add_custom_button(__("Run Processing (include today)"), () => run_processing(frm, 1), __("Actions"));
		frm.add_custom_button(__("Refresh Stats"), () => render_stats(frm), __("Actions"));
		render_stats(frm);
	},
});

function run_processing(frm, include_today) {
	frappe.call({
		method: "trident_attendance.api.process_attendance",
		args: { include_today },
		freeze: true,
		freeze_message: __("Processing check-ins…"),
		callback(r) {
			const m = r.message || {};
			frappe.msgprint({
				title: __("Processing finished"),
				indicator: m.errors ? "orange" : "green",
				message: __("Up to {0}: {1} employee-day(s) evaluated, {2} marked, {3} held, {4} error(s).", [
					m.up_to, m.days, m.marked, m.held, m.errors,
				]),
			});
			render_stats(frm);
		},
	});
}

function render_stats(frm) {
	const wrapper = frm.get_field("monitor_html").$wrapper;
	wrapper.html(`<div class="text-muted">${__("Loading stats…")}</div>`);
	frappe.call({
		method: "trident_attendance.monitor.get_pipeline_stats",
		args: { days: 7 },
		callback(r) {
			wrapper.html(build_stats_html(r.message || {}));
		},
		error() {
			wrapper.html(`<div class="text-danger">${__("Could not load stats.")}</div>`);
		},
	});
}

function build_stats_html(s) {
	const esc = frappe.utils.escape_html;
	const fmt_dt = (v) => (v ? frappe.datetime.str_to_user(v) : "—");
	const p = s.punches || {}, rv = s.review || {}, sc = s.scheduler || {}, st = s.settings || {};
	const last = sc.last_run, lastSched = sc.last_scheduler_run;

	const tile = (label, value, sub, color) => `
		<div class="tas-tile">
			<div class="tas-value ${color || ""}">${value}</div>
			<div class="tas-label">${esc(label)}</div>
			${sub ? `<div class="tas-sub">${sub}</div>` : ""}
		</div>`;

	const warnings = (s.warnings || []).length
		? `<div class="tas-warn">${(s.warnings || []).map((w) => `<div>&#9888; ${esc(w)}</div>`).join("")}</div>`
		: `<div class="tas-ok">&#10003; ${__("Pipeline looks healthy.")}</div>`;

	const reasons = (rv.top_reasons || []).length
		? `<table class="table table-sm tas-table"><tbody>${rv.top_reasons
				.map(([reason, n]) => `<tr><td>${esc(reason)}</td><td class="text-right">${n}</td></tr>`)
				.join("")}</tbody></table>`
		: `<div class="text-muted">${__("Nothing pending.")}</div>`;

	const runs = (sc.runs || []).length
		? `<table class="table table-sm tas-table"><thead><tr><th>${__("Run")}</th><th>${__("Trigger")}</th><th class="text-right">${__("Days")}</th><th class="text-right">${__("Marked")}</th><th class="text-right">${__("Held")}</th><th class="text-right">${__("Errors")}</th><th class="text-right">${__("Sec")}</th></tr></thead><tbody>${sc.runs
				.map(
					(x) => `<tr><td><a href="/app/trident-attendance-run/${encodeURIComponent(x.name)}">${fmt_dt(x.run_at)}</a></td><td>${esc(x.trigger || "")}</td><td class="text-right">${x.days_evaluated}</td><td class="text-right">${x.marked}</td><td class="text-right">${x.held}</td><td class="text-right ${x.errors ? "text-danger" : ""}">${x.errors}</td><td class="text-right">${x.duration_seconds}</td></tr>`
				)
				.join("")}</tbody></table>`
		: `<div class="text-muted">${__("No runs recorded yet. The hourly job records one each time it runs.")}</div>`;

	const jobs = (sc.jobs || [])
		.map((j) => `<div>${esc(j.method.split(".").pop())}: ${j.stopped ? `<span class="text-danger">${__("stopped")}</span>` : `<span class="text-success">${__("active")}</span>`} · ${__("last")} ${fmt_dt(j.last_execution)}</div>`)
		.join("") || `<div class="text-danger">${__("No scheduled job types found — run bench migrate.")}</div>`;

	return `
	<style>
		.tas-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: .6rem; margin-bottom: .75rem; }
		.tas-tile { border: 1px solid var(--border-color); border-radius: 8px; padding: .6rem .75rem; background: var(--card-bg); }
		.tas-value { font-size: 1.4rem; font-weight: 600; line-height: 1.2; }
		.tas-label { color: var(--text-muted); font-size: .8rem; }
		.tas-sub { color: var(--text-muted); font-size: .72rem; margin-top: .15rem; }
		.tas-warn { border-left: 3px solid var(--orange-500, #f59e0b); padding: .4rem .7rem; margin-bottom: .75rem; background: var(--bg-orange, rgba(245,158,11,.08)); border-radius: 6px; }
		.tas-ok { border-left: 3px solid var(--green-500, #22c55e); padding: .4rem .7rem; margin-bottom: .75rem; background: var(--bg-green, rgba(34,197,94,.08)); border-radius: 6px; }
		.tas-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
		.tas-table { margin: .25rem 0 0; font-size: .82rem; }
		.tas-h { font-weight: 600; margin: .25rem 0; }
		.tas-meta { color: var(--text-muted); font-size: .78rem; margin-top: .5rem; }
		@media (max-width: 900px) { .tas-cols { grid-template-columns: 1fr; } }
	</style>
	${warnings}
	<div class="tas-grid">
		${tile(__("Pending employee-days"), rv.pending_days ?? 0, rv.oldest_pending ? __("oldest {0}", [frappe.datetime.str_to_user(rv.oldest_pending)]) : "", rv.pending_days ? "text-warning" : "text-success")}
		${tile(__("Pending punches"), rv.pending_punches ?? 0)}
		${tile(__("Punches today"), p.today ?? 0, __("{0} supervisor(s) active", [p.supervisors_today ?? 0]))}
		${tile(__("Punches, {0} days", [s.window_days]), p.window ?? 0, p.last_received ? __("last {0}", [fmt_dt(p.last_received)]) : __("none received"))}
		${tile(__("Marked, {0} days", [s.window_days]), rv.attendance_from_app_window ?? 0, __("{0} punches linked", [rv.marked_punches_window ?? 0]), "text-success")}
		${tile(__("Rejected, {0} days", [s.window_days]), rv.rejected_window ?? 0)}
		${tile(__("Errors, 24h"), s.errors_24h ?? 0, "", s.errors_24h ? "text-danger" : "")}
		${tile(__("Last run"), last ? fmt_dt(last.run_at) : "—", last ? `${esc(last.trigger)} · ${last.marked} ${__("marked")}, ${last.held} ${__("held")}` : "", last && last.errors ? "text-danger" : "")}
	</div>
	<div class="tas-cols">
		<div>
			<div class="tas-h">${__("Why days are held")}</div>
			${reasons}
			<div class="tas-h" style="margin-top:.75rem">${__("Scheduler")}</div>
			<div>${sc.disabled ? `<span class="text-danger">${__("Site scheduler is DISABLED")}</span>` : `<span class="text-success">${__("Site scheduler enabled")}</span>`}</div>
			${jobs}
			<div class="tas-meta">${__("Hourly job last recorded")}: ${lastSched ? fmt_dt(lastSched.run_at) : "—"}</div>
		</div>
		<div>
			<div class="tas-h">${__("Recent runs")}</div>
			${runs}
		</div>
	</div>
	<div class="tas-meta">
		${__("Scope")}: ${esc(st.staged_sources || "")} · ${__("cutoff")} ${esc(st.day_cutoff_time || "")} · ${__("complete up to")} ${frappe.datetime.str_to_user(st.complete_up_to)} ·
		${__("auto-release")} ${st.auto_release ? __("on") : `<span class="text-warning">${__("off")}</span>`} · ${__("auto-checkout")} ${st.auto_checkout ? __("on") : __("off")} · ${__("absent marking")} ${st.mark_absent ? __("on") : __("off")}
		· ${__("as of")} ${fmt_dt(s.as_of)}
	</div>`;
}
