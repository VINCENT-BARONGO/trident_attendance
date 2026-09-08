## Trident Attendance

Frappe app backing the Trident Plumbers field attendance Android app: national ID
scanning, face verification against the employee photo, and geofenced check-ins.

The Android app posts to the stock `Employee Checkin` doctype. This app supplies the
custom fields it writes to, the roles it authenticates as, and the scheduled
maintenance those records need — so a site is provisioned by installing this rather
than by recreating seventeen custom fields and two roles by hand.

### Install

```bash
bench get-app <repo-url>
bench --site <site> install-app trident_attendance
```

Fixtures apply on install. Nothing else is required.

### What it provides

**Custom fields (17)** — `Employee.custom_id_number` / `custom_verified` /
`custom_project`; `Project.custom_site_latitude` / `custom_site_longitude` /
`custom_geofence_radius_meters`; `Employee Checkin.custom_site_project` /
`custom_id_number_scanned` / `custom_mrz_raw` / `custom_face_match_result` /
`custom_face_match_score` / `custom_logged_by` / `custom_app_source` /
`custom_attendance_photo`; `HR Settings.custom_enforce_facial_recognition`.

**Roles** — `Attendance Marking` for the mobile app, `Attendance Admin` for the
person reviewing the day's punches in the Desk.

**A permlevel-1 grant that matters.** `Employee Checkin.time` is declared
`permlevel: 1`. Frappe silently discards writes to permlevel-1 fields from roles
lacking write access at that level and substitutes the field default (`Now`) — no
error, HTTP 200, wrong data. Because the Android app queues check-ins offline and
replays them later, a role without this grant records every synced check-in at
*sync* time instead of *scan* time: someone scanned at 06:15 and synced at 13:05 is
recorded at 13:05. The fixture grants it, so installing this app closes that gap.

**Daily photo retention** — `trident_attendance.tasks.purge_attendance_photos`
deletes attendance face photos older than 30 days (`PHOTO_RETENTION_DAYS` in
`tasks.py`). The `Employee Checkin` row itself is kept forever; only the JPEG is
dropped. The window is keyed on the check-in's own `time`, not upload time, so a
handset that syncs late does not earn a longer retention.

This runs as a `scheduler_events` hook rather than a Server Script deliberately:
Server Scripts require `server_script_enabled` in `common_site_config.json`, which
is bench-level config that Frappe Cloud controls and disables on shared plans. An
app hook has no such dependency and behaves identically on local and hosted sites.

### Regenerating fixtures

After changing custom fields, roles or permissions on a site:

```bash
bench --site <site> export-fixtures --app trident_attendance
```

### Shift configuration is not shipped

Auto-attendance depends on a `Shift Type` (start/end times, grace periods, the
half-day and absent thresholds, and how multiple punches are paired). Those are
business decisions and are deliberately left out of the fixtures — configure them
per site. Two settings drive how a day's punches become hours:
`determine_check_in_and_check_out` and `working_hours_calculation_based_on`.

### License

MIT
