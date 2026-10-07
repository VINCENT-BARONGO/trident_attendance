## Trident Attendance

Frappe app backing the Trident Plumbers field attendance Android app: national ID
scanning, face verification against the employee photo, geofenced check-ins, and the
review workflow that turns those check-ins into `Attendance`.

Targets Frappe / ERPNext / HRMS v15.

### Install

```bash
bench get-app <repo-url>
bench --site <site> install-app trident_attendance
```

Fixtures (custom fields, roles, permissions, workspace) apply on install. Then open
**Trident Attendance Settings** and run the **Setup Audit** report — it lists everything
that would silently stop the flow (supervisors without Employee records or the app role,
projects without GPS, employees without photos, …).

### How attendance is marked

There are **no shifts** in this flow. A day is one employee's check-ins on one calendar
date; working hours run from the **first IN to the last OUT** (hrms's own
`calculate_working_hours`).

1. The app posts `Employee Checkin` rows. Every app punch is staged
   (`custom_review_status = Pending Review`) and annotated with hold reasons: face
   result, distance from the project's geofence, whether the posting user is in the
   project's Users table, and so on.
2. Once the day is complete (any earlier day, or today after **Day Cutoff Time**) the
   hourly job `trident_attendance.tasks.finalise_days` runs the day rules: IN/OUT
   pairing, "supervisor checked in/out first", mixed projects, an Attendance that
   already exists.
3. A day with no blocking reason is released and **marked immediately**: one submitted
   `Attendance` with `working_hours`, `in_time`, `out_time` and `custom_project` (the
   project of the last OUT), and the check-ins linked to it.
4. Anything else waits on **/attendance-review**, where an `Attendance Admin` can
   release, reject, move a punch to another project, add a missing OUT, or cancel and
   rebuild a day.

App punches keep `skip_auto_attendance = 1` for their whole life, so hrms's Shift-Type
auto-attendance never touches them; this app sets the `attendance` link itself.
Attendance marked manually by HR is left alone — if a day already has one, the punches
are held with `Attendance already marked` and the reviewer decides.

### Supervisor rules

- The user taking attendance must be listed in the project's **Users** table
  (`Project.users`); `get_my_projects` only returns those projects.
- Workers can only be checked in after the supervisor has checked **themselves** in at the
  same project (and out before workers are checked out). By default the supervisor's own
  punch must have `Face Match Result = Matched`.
- The supervisor is identified from the session user that posted the punch, never from
  what the client sent. A supervisor therefore needs an active `Employee` with
  `User ID` set.

All of these are toggles in Trident Attendance Settings.

### Endpoints used by the app

| Method | Purpose |
|---|---|
| `trident_attendance.api.sync_checkin` | Single-call, idempotent ingestion (`client_uid`), optional base64 photo. Returns `name`, `review_status`, `hold_reasons`. |
| `trident_attendance.api.get_my_projects` | Open projects the user is listed on, with GPS and radius. |
| `trident_attendance.api.get_my_history` | The user's own punches with review outcome and Attendance, plus a per-day summary. |

The plain REST path (`POST /api/resource/Employee Checkin`) still works: a controller
override repairs the two values older app builds send that Frappe would otherwise
reject (`custom_logged_by` as an email, face labels outside the Select options).

### What the app provides

**Custom fields** — `Employee.custom_id_number / custom_verified / custom_project /
custom_app_access`;
`Project.custom_site_latitude / custom_site_longitude / custom_geofence_radius_meters`;
`Employee Checkin.custom_site_project / custom_id_number_scanned / custom_mrz_raw /
custom_face_match_result / custom_face_match_score / custom_logged_by /
custom_app_source / custom_attendance_photo / custom_review_status /
custom_hold_reasons / custom_reviewed_by / custom_reviewed_on /
custom_distance_from_site / custom_client_uid`; `Attendance.custom_project /
custom_mixed_projects`; `HR Settings.custom_enforce_facial_recognition`.

**Roles** — `Attendance Marking` for the mobile app, `Attendance Admin` for whoever
reviews the day's punches. Reviewing is gated on the role, not on an Attendance
permission, because the app role may legitimately hold Attendance create for the office
Attendance Tool.

**A permlevel-1 grant that matters.** `Employee Checkin.time` is `permlevel: 1`. Frappe
silently discards writes to permlevel-1 fields from roles lacking write access at that
level and substitutes `Now` — no error, HTTP 200, wrong data. Because the app queues
check-ins offline, a role without this grant records every synced check-in at *sync*
time instead of *scan* time. The fixture grants it to both roles.

**Settings single** — `Trident Attendance Settings`: staged sources, day cutoff,
half-day / absent thresholds, which reasons hold a day, supervisor rules, geofence
tolerance, optional auto-checkout, optional absent marking, photo retention.

**Reports** — *Attendance by Project*, *Check-in Staging Log*, *Setup Audit*.

**Monitoring** — every processing run (hourly job, reviewer, manual) writes a
`Trident Attendance Run` row; the Settings page opens with pipeline health (pending
days, top hold reasons, punches today, scheduler status, recent runs, warnings) and
Run / Setup Audit / Run Log buttons. The review page shows the same numbers in a strip.

**Security notes** — the review page is for `Attendance Admin` / HR roles only;
mutating endpoints are POST-only; review fields on Employee Checkin are permlevel 2 so
the app cannot change a punch's review status; the supervisor is always the posting
user; `after_install` / `after_migrate` copy standard permissions into Custom DocPerm
so the fixture never locks HR or System Manager out of a doctype.

**Daily photo retention** — `tasks.purge_attendance_photos` deletes attendance face
photos older than the configured number of days; the check-in row is kept. The window
is keyed on the check-in's own `time`, so a late-syncing handset earns no extra
retention.

Both jobs are `scheduler_events` hooks rather than Server Scripts: Server Scripts need
`server_script_enabled` in `common_site_config.json`, which Frappe Cloud disables on
shared plans.

### Employee app access (sign-in without an ERP password)

Employees open the app and My HR with their ID number, a 6-digit PIN and a one-time code
sent to their phone. The attendance hub passes each step on to this site, which checks the
PIN and the code and then hands the hub an ERP OAuth token for that employee's own user.
Supervisors keep their ERP sign-in.

**Settings** (Trident Attendance Settings > Employee App Access):

| Setting | Meaning |
|---|---|
| Hub Service User | The hub's own login. Only this user can call the hub-only methods below, and it must hold `Attendance Admin` (or System Manager). Empty = nobody can. |
| Employee Token OAuth Client | The OAuth Client the tokens are issued for. It needs "Skip Authorization" ticked. Empty = no tokens are issued. |
| Helper Token (minutes) | Life of a borrowed-phone token. Default 10, at most 60. |
| Allow Face Sign-in | Off by default; leave it off. On, the older methods that issue a token after the hub's face check answer again, with no PIN and no code. |

Only a System Manager can change the first two and the last. `Employee > App Access`
(`custom_app_access`) is HR's switch per employee; no code is sent and no token is issued
without it.

**Where the code goes.** By SMS to `Employee > Mobile` (`cell_number`), through the site's
own `SMS Settings` gateway. Kenyan numbers may be written `07..`, `01..`, `2547..` or
`+2547..`; they are sent as `2547..`. With no usable mobile number, or no SMS gateway on the
site, the code goes by e-mail to the employee's own address: `prefered_email`, else
`personal_email`, else `company_email`, never the `@staff.` address of a provisioned user.
The message is not kept: no SMS Log row and no Email Queue row.

**The rules.**

- PIN: exactly 6 digits; not one digit repeated, not a straight run up or down (`123456`,
  `987654`, `012345`, also `890123`), not the last 6 digits of the ID number. Kept as a hash
  in `__Auth` on the Employee. It is not the user's password, which stays random and unknown.
- Code: 6 digits, good for 5 minutes, for one use and one purpose, 5 wrong tries. Asking
  again replaces it. At most 3 codes in 15 minutes and 10 in 24 hours per employee.
- 5 wrong PINs in an hour lock that employee's PIN for an hour. While it is locked the right
  PIN is refused too. Choosing a new PIN, or HR's reset, lifts the lock.

**What HR does.** Tick `App Access` on the Employee, check the mobile number, and tell the
employee: "Open the app, choose *I am an employee*, enter your ID number, then *First time
here, or forgot your PIN?* You get a code on your phone; enter it and choose a PIN." From
then on they sign in with the ID number, the PIN and a new code each time. The Employee form
shows whether an app PIN has been chosen, and a **Reset App PIN** button (HR Manager, HR
User, Attendance Admin, System Manager; only when App Access is ticked). The reset clears
the PIN and any code on its way, lifts a lock and signs the employee out everywhere; they
choose a new PIN the same way as the first time. An employee who forgot the PIN does not
need HR for that, only a phone that still gets the code.

**One user per employee.** Each employee needs an ERP user holding only the self-service
roles, linked on the Employee. `provision_ess_users` creates
`<employee id>@staff.<domain>` for every Active employee without a user: no welcome
e-mail, a random password nobody is told, the Employee link and user permissions, then
the `Employee Self Service` role. Employees who already have a user are skipped and that
user is not changed, so it can be run again.

```bash
# Dry run (the default): changes nothing, lists what it would create
bench --site <site> execute trident_attendance.provision.provision_ess_users \
    --kwargs "{'domain': 'example.co.ke'}"

# Create them, for one company, and tick App Access on those employees
bench --site <site> execute trident_attendance.provision.provision_ess_users \
    --kwargs "{'domain': 'example.co.ke', 'dry_run': 0, 'company': 'Trident Plumbers Ltd', 'enable_app_access': 1}"

# Only some employees
bench --site <site> execute trident_attendance.provision.provision_ess_users \
    --kwargs "{'domain': 'example.co.ke', 'dry_run': 0, 'employees': ['HR-EMP-00001', 'HR-EMP-00002']}"
```

It returns one row per employee: `would create`, `created`, `skipped: ...` or
`failed: ...`. A failure undoes that employee only. Run it on a copy of the site first.

**Hub-only methods** (`trident_attendance.employee_access`). None is open to guests. Any
caller other than the Hub Service User gets HTTP 403 with `exc_type: HubOnlyError`. Every
other refusal is a normal answer: `{"ok": false, "reason": "<code>", "message": "..."}`.
The hub sends the PIN and the code in the POST body, never in the URL.

| Method | Purpose |
|---|---|
| `begin_employee_sign_in(id_number, purpose, pin)` (POST) | `purpose=sign_in`: checks the PIN, then sends a code. `purpose=set_pin` (first time, or forgot PIN): sends a code; `pin` is ignored. Returns `employee`, `employee_name`, `channel` (`sms` or `email`), `destination` (masked: `07•• ••• 123`, `v•••@gmail.com`), `expires_in` (300). |
| `complete_employee_sign_in(employee, purpose, code, kind, new_pin, device)` (POST) | Takes the code and issues the token. `kind=own`: a 3600 s access token and a refresh token, after revoking every token the user holds. `kind=helper`: a short access token, no refresh token. `purpose=set_pin` needs `new_pin`, saves it and revokes every earlier token whatever the `kind`. Returns `access_token`, `refresh_token` (null for helper), `expires_in`, `user`, `employee`, `employee_name`, `pin_set` (true when this call saved a PIN). |
| `revoke_employee_tokens(employee)` (POST) | Revokes every Active OAuth token of the employee's user, whichever client holds it, and any sign-in code not yet exchanged. Returns `revoked`. |

`begin_employee_sign_in` refuses with: `id_number_required`, `invalid_purpose`,
`not_found`, `ambiguous` (two Active employees share the number), `employee_inactive`,
`app_access_off`, `no_user`, `user_disabled`, `user_has_other_roles` (the user holds
anything beyond `Employee` and `Employee Self Service`: a supervisor, a manager or a System
Manager is never signed in this way), `oauth_client_not_set`, `oauth_client_missing`,
`oauth_client_no_scopes`, `pin_required`, `pin_not_set`, `wrong_pin`, `pin_locked` (with
`retry_after` seconds; the fifth wrong PIN answers this too), `no_contact`,
`too_many_codes` (with `retry_after`), `send_failed` (the gateway or the mail server
refused; no code is left behind and it does not count toward the limit).

`complete_employee_sign_in` refuses with: `invalid_kind`, `invalid_purpose`,
`employee_not_found`, `no_code` (none pending for that purpose, or it expired),
`wrong_code` (with `tries_left`), `too_many_code_tries` (the fifth wrong code; ask for a new
one), `pin_invalid`, `pin_too_simple`, and the employee, user and OAuth client reasons
above. `pin_invalid` and `pin_too_simple` leave the code usable, so the same code can come
again with a better PIN.

An error nobody planned for answers HTTP 500 with `reason: server_error` and is logged
without the request's values. Raised the usual way, Frappe would write the PIN and the code
to the Error Log.

**For HR, not the hub** (a role decides: HR Manager, HR User, Attendance Admin or System
Manager, and an employee the caller may open; anyone else gets HTTP 403):

| Method | Purpose |
|---|---|
| `reset_employee_pin(employee)` (POST) | Behind the Reset App PIN button. Clears the PIN and any pending code, lifts the PIN lock, revokes every token. Returns `had_pin`, `revoked`. |
| `get_employee_pin_status(employee)` | For the Employee form: `pin_set`, `pin_locked`. Never the PIN or its hash. |

**Face methods, switched off.** `get_employee_for_verification(id_number)`,
`get_verification_photo(employee)` and `issue_employee_token(employee, kind, device)` are
the earlier sign-in by ID card and face. Unless `Allow Face Sign-in` is ticked each answers
`{"ok": false, "reason": "face_sign_in_off"}` (the photo method too, in place of the
image).

**Where the state lives.** The PIN hash is in `__Auth`. The pending code (a keyed hash), the
PIN lock and the times codes were sent are one row per employee in `Employee App Sign In`,
a doctype no role can read. Not in the cache: `bench migrate` and `bench clear-cache` empty
it, which would lift every lock. Both hashes are mixed with the site's `encryption_key`, so
a copy of the database alone does not give the PINs away; a site restored without its
`site_config.json` key has every employee choose a PIN again.

Tests: `bench --site <site> run-tests --app trident_attendance`. On a bench whose
installed copy of the app is another branch, `scripts/run_tests_from_src.py` runs this
checkout's tests instead (see the file).

### Site requirements

- `System Settings > Time Zone` must be `Africa/Nairobi` (the app posts handset-local
  times without a zone).
- `HR Settings > Allow Geolocation Tracking` must stay **off** (hrms would reject punches
  without GPS).
- Do not enable auto attendance on a Shift Type for employees who use the app while
  *Staged Sources* is "All sources".
- If the site already keeps the national ID number on Employee in another field (a CSF KE
  site has a mandatory `national_id`), name it in `Trident Attendance Settings > Employee
  ID Number Field`. The app is still handed the value as `custom_id_number`. A fresh install
  picks `national_id` by itself when `custom_id_number` is empty on every Active Employee.

### Before pushing: check API usage against the site's frappe version

There is no bench in the development environment, so every `frappe.*` name and keyword
argument is checked statically against the frappe source of the version running on the
site (see "App Versions" in any error report):

```bash
git clone --depth 1 --branch v15.120.1 https://github.com/frappe/frappe.git /tmp/frappe_src
python scripts/check_frappe_api.py /tmp/frappe_src
```

It exits non-zero and lists unknown functions or keywords. Update the tag when the site
is upgraded.

### Regenerating fixtures

After changing custom fields, roles or permissions on a site:

```bash
bench --site <site> export-fixtures --app trident_attendance
```

The fixture filter excludes the legacy `Employee Checkin-custom_project` and
`custom_mobile_app_data` fields that exist on the production site.

### License

MIT
