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

### Announcements and push notifications

The employee app's My HR shows company announcements under **News** and the phone gets a
push notification (Firebase Cloud Messaging) for a new announcement, a leave decision and a
new payslip, sent by this app from the ERP.

**Announcements are Newsletters.** HR writes a Newsletter (Rich Text, Markdown or HTML) and
ticks `Show in employee app` (`custom_app_announcement`, next to the subject). Nothing else
about the newsletter matters: it need not be sent or published. The first time the box is
ticked the time is kept on the newsletter (`custom_app_announced_at`, hidden); it is the
announcement's date in the app and the reason the box can be unticked and ticked again
without a second notification. Untick it to take the announcement down.

`trident_attendance.announcements`, for any signed-in user (Guest gets 403). The box is the
permission: Newsletter's own permissions are bypassed on purpose, so every employee reads
every ticked newsletter and nothing else.

| Method | Answer |
|---|---|
| `list_announcements(limit=50, offset=0)` | `{"ok": true, "items": [{"name", "subject", "shown_at", "summary"}]}`, newest first. `summary` is the first ~160 characters of the plain text. `limit` is at most 100. |
| `get_announcement(name)` | `{"ok": true, "name", "subject", "shown_at", "html"}`. `html` is the message rendered on the ERP (Markdown rendered, the rest as written), cleaned with Frappe's HTML sanitiser (scripts and event handlers removed), with `/files/...` and `/private/files/...` links made absolute to this site's URL. A newsletter that is not ticked, or does not exist: `{"ok": false, "reason": "not_found"}` (HTTP 200, so the web app can tell it from a site without this backend). |

**Devices.** The phone registers its FCM token after each sign-in and forgets it at
sign-out, through `trident_attendance.push` (POST, any signed-in user; Guest gets 403):

| Method | Answer |
|---|---|
| `register_app_device(token, platform="android", app_version=None)` | `{"ok": true}`. One `App Device` row per token: inserted, or updated when the token is known, with `user` = the caller, `enabled` = 1, `last_used` = now. A token another user registered moves to the caller (the phone changed hands). `token_invalid` (not 20 to 512 characters), `invalid_platform` (not `android` or `ios`). |
| `unregister_app_device(token)` | `{"ok": true}`, whether or not there was a row. Only the caller's own row is deleted. |

`App Device` (`user`, `token`, `platform`, `app_version`, `enabled`, `last_used`,
`last_error`) is readable by System Managers only. A device Firebase no longer knows
(`UNREGISTERED` / `NOT_FOUND`) is disabled with the answer in `last_error`; any other
failure is noted there and the device kept. Registering again re-enables it.

**Settings** (Trident Attendance Settings > Push Notifications):

| Setting | Meaning |
|---|---|
| Push Notifications Enabled | Off (the default) = nothing is sent; phones still register. |
| Firebase Service Account JSON | The Firebase project's service-account key, pasted as downloaded. Kept encrypted (a Password field). Only a System Manager can change it. Empty = the site_config key `fcm_service_account` is used (the JSON as a string, or the object itself). |

**Actions > Send test notification to my devices** sends "Test notification" to the phones
registered for the user who clicks (`trident_attendance.push.send_test_push`, POST, any
signed-in user: `{"ok": true, "devices": <how many were queued>}`, or `push_disabled` /
`no_service_account`). The answer says how many devices were queued, not whether Firebase
accepted the message: that lands in `last_error` on the App Device row.

**What sends a notification** (`hooks.doc_events`; each once per document, and never
twice for the same decision):

| Event | To whom | Title / body | Opens |
|---|---|---|---|
| Newsletter saved with `Show in employee app` turned on for the first time | every enabled device | "New announcement" / the subject | `/ess/announcements/<name>` |
| Leave Application status becomes Approved or Rejected: on save, on submit, or on a submitted application | the employee's user's devices | "Leave approved" or "Leave rejected" / "<from> to <to>, <leave type>" | `/ess/leave` |
| Salary Slip submitted | the employee's user's devices | "Payslip ready" / "<month year>" | `/ess/payslips/<name>` |
| A new Leave Application, status Open | the leave approver named on it | "Leave request" / "<employee>: <from> to <to>, <leave type>" | `/ess/team` |
| A Notification Log row (the ERP's bell: an assignment, a mention, a share, an alert rule; not energy points) | the user it is for | "Assigned to you" and the like / the ERP's own subject as plain text, 180 characters at most | `/ess/` |
| A Workflow Action, status Open | everyone who holds a permitted role and has a registered phone, except whoever caused it | "Waiting for your approval" / "<doctype> <name>: <state>" | `/ess/` |

The status told is kept on the Leave Application (`custom_app_notified_status`, hidden). An
employee without a linked user gets nothing. A document name in a path is URL-encoded as one
segment (`Sal%20Slip%2FHR-EMP-00001%2F00001`). A notification that cannot be queued is
written to the Error Log as its type and never stops the HR document from being saved or
submitted.

**How it is sent.** FCM HTTP v1, `POST https://fcm.googleapis.com/v1/projects/<project_id>/messages:send`,
one request per device with a 10 s timeout, from a background job on the `short` queue
(queued after the HR document's transaction commits; up to 200 devices per job). The
message is

```json
{"message": {"token": "<device token>",
             "notification": {"title": "...", "body": "..."},
             "data": {"url": "/ess/announcements/NL-0007", "kind": "announcement"},
             "android": {"priority": "HIGH", "notification": {"channel_id": "trident_employee"}}}}
```

`data.url` is a path on the web app; `kind` is `announcement`, `leave`, `payslip`, `erp`,
`test`, `shift` or `locate`. The last two are instructions to the app, not notifications:
the message has no `notification` block, so the phone shows nothing (see "Clocked in or
out, and locating a person" below):

| `kind` | Sent when | `data` | `android` |
|---|---|---|---|
| `shift` | the hub reports a punch (`shift.punched`) | `{"kind": "shift", "shift": "in" \| "out", "at": "<the punch's time, ISO 8601>", "employee": "<Employee ID>"}` | `{"priority": "HIGH", "ttl": "600s"}` |
| `locate` | someone asks where the person is (`locate.request_location`) | `{"kind": "locate", "request": "<Location Request name>", "employee": "<Employee ID>", "url": "/ess/"}` | `{"priority": "HIGH", "ttl": "120s"}` |

```json
{"message": {"token": "<device token>",
             "data": {"kind": "locate", "request": "LOC-2026-10-00001", "employee": "HR-EMP-00042", "url": "/ess/"},
             "android": {"priority": "HIGH", "ttl": "120s"}}}
```

`ttl` is how long Firebase keeps the message for a phone it cannot reach: a `shift` or a
`locate` that would arrive late is dropped instead. `at` carries the site's offset from UTC
(`2026-10-13T07:58:00+03:00`). The OAuth2 bearer (scope `https://www.googleapis.com/auth/firebase.messaging`) is
made from the service account with `google-auth`, which Frappe itself depends on, and
cached until five minutes before it expires. The logs hold kinds and counts only: no device
token, no message body, and never the key, which is also kept out of the Error Log when
something fails.

**Firebase set-up** (one-off, by the owner):

1. console.firebase.google.com > Add project "Trident Employee" (Analytics off).
2. Add an Android app, package name `com.tridentplumbers.attendance`; download
   `google-services.json` into the phone app's `apps/trident_employee/android/app/`. The
   same Firebase Android app serves the Trident and Astral builds.
3. Project settings > Service accounts > Generate new private key; paste the JSON into
   Trident Attendance Settings > Firebase Service Account JSON on each ERP, tick Push
   Notifications Enabled, save, then Actions > Send test notification to my devices from a
   user whose phone is signed in to the app.

Whoever holds the service-account key can send any registered phone any notification in
the company's name: treat it like a password, and generate a new key in Firebase (and
delete the old one there) if it leaks.

### Clocked in or out, and locating a person

The employee app reports a person's position to Traccar while they are on shift, and
someone allowed to may ask where a clocked-in person is. Both depend on the ERP knowing
that a person clocked in or out at the moment it happens. Employee Checkins do not say so:
every punch goes to the attendance hub first and the hub writes check-ins to the ERP at
night. The hub therefore tells the ERP about each punch as it accepts it.

**The hub's call.** `POST /api/method/trident_attendance.shift.punched`, with
`Authorization: token <api_key>:<api_secret>` of the hub's ERP user: the `Hub Service User`
of Trident Attendance Settings, holding `Attendance Admin` or System Manager. It is the
check of the hub-only sign-in methods (`employee_access._require_hub`), and anyone else
gets the same 403 `HubOnlyError`; a System Manager may call it too. The JSON body:

```json
{"employee": "HR-EMP-00042", "log_type": "IN", "at": "2026-10-13T08:01:12+03:00", "source": "terminal"}
```

| Field | Meaning |
|---|---|
| `employee` | The Employee ID. |
| `log_type` | `IN` or `OUT` (read in any case). |
| `at` | The time of the punch itself, ISO 8601 with an offset (`Z` and `YYYY-MM-DD HH:MM:SS` are read too; a time without an offset is taken as the site's time). |
| `source` | Optional label of where the punch was made (`terminal`, `phone`), 40 characters at most. |

The hub should call it right after it accepts a punch and not wait on it: a failure must
never affect the punch, and the call is tried again once at most. Calling twice with the
same punch is harmless.

**The answer is HTTP 200 whatever is wrong with the person or the punch.** The hub reads a
404 or a 417 as "this ERP does not have the method" and stops calling for that company for
an hour, and Frappe answers 417 for every validation error, so nothing about one punch is
raised. The only error statuses are 403 (the caller is not the hub) and 500 (something
unexpected, in the Error Log as its type; the body is still `{"ok": false, "reason":
"server_error", "pushed": 0}`).

| Answer | Meaning |
|---|---|
| `{"ok": true, "pushed": n}` | The punch was kept and `n` phones were told. |
| `{"ok": true, "pushed": 0, "reason": "stale"}` | An IN made more than 15 minutes ago: kept, not sent to the phone. |
| `{"ok": false, "reason": "bad_log_type", "pushed": 0}` | `log_type` is not IN or OUT. Nothing kept. |
| `... "unknown_employee"` | No Employee has this ID. Nothing kept. |
| `... "inactive"` | The Employee is not Active. Nothing kept. |
| `... "bad_time"` | `at` is no time, or lies more than 10 minutes in the future. Nothing kept. |
| `... "duplicate"` | The punch kept for this employee already has this time (the same punch again). Nothing changes, nothing is sent. |
| `... "older"` | A punch from before the one kept, arriving late. Nothing changes, nothing is sent. |
| `... "no_user"` | Kept; the employee has no ERP user, so nobody was told. |
| `... "no_device"` | Kept; the employee's user has no registered phone. |
| `... "push_disabled"` | Kept; push notifications are switched off in the settings. |
| `... "push_failed"` | Kept; the push could not be queued (Error Log). |

What the ERP does with it:

- The punch becomes the employee's **Shift Signal** (one row per employee: `employee`,
  `log_type`, `at`, `source`, `received_at`) when its `at` is newer than the one kept. The
  same punch again, or an older punch arriving late, changes nothing and sends nothing.
  System Managers and HR Managers can read the rows; nobody edits them.
- The employee's own phones get the `shift` push (data alone, see the table of kinds above)
  when the punch was made in the last 15 minutes, or is an OUT however late. An IN that
  arrives later than 15 minutes is kept but not sent, so that it does not start the phone's
  reporting after the fact.

**Clocked in now** means: the Shift Signal is an IN whose `at` is within the last 18 hours.
Employee Checkin is not looked at.

**Locating a person.** `POST trident_attendance.locate.request_location(employee)`, from
Employee > Actions > **Locate now** in the desk. The rules:

- Only while the person is clocked in (above).
- Only by the role named in Trident Attendance Settings > Locating People > **Role that may
  locate people** (`locate_role`; Attendance Admin to begin with), or by a System Manager.
  Empty = System Managers only. Only a System Manager can change the setting.
- Every request is recorded as a **Location Request** (`employee`, `employee_name`,
  `requested_by`, `requested_at`, `device_count`): who asked about whom, and when. System
  Managers and HR Managers can read the rows; nobody, whatever their role, can
  create, change or delete one by hand.
- The person is told up front, in the phone app, that the office may ask for their location
  while they are clocked in. A single request shows them nothing: the `locate` push carries
  data alone. The Location Request is the only trace of it.
- One request per employee a minute, whoever asks.

The answer is `{"request": "<Location Request name>", "employee", "employee_name",
"devices": <phones asked>}`. The position goes from the phone to Traccar; the ERP never
holds it. A caller without the role gets a permission error (403). Otherwise a validation
error says which of these is the case: the employee is not active, has no ERP user, has no
phone registered, is not clocked in, was asked about a moment ago, or push notifications are
switched off. `trident_attendance.locate.may_locate()` (GET) answers `true` or `false` for
the signed-in user; the Employee form shows the button by it.

**Where they are, in words.** After Locate now the Employee form opens a box and asks
`GET trident_attendance.locate.location_answer(request)` every 4 seconds, for 90 seconds at
most. Only the one who made the request may ask, for 3 minutes after it. The ERP reads, from
Traccar, the newest position the person's device sent since the request (the device's
Identifier in Traccar is the Employee ID), and answers:

- **At a site:** an open Project whose geofence holds the position (its radius plus the
  Geofence Tolerance, as punches are held; a radius of 0 is no fence), the nearest of several.
- **Elsewhere:** the place's name from OpenStreetMap's geocoder (`nominatim.openstreetmap.org`,
  at most one request a second, with "© OpenStreetMap contributors" shown under it).
- How long ago the position is, and how accurate the phone said it was. **Never the
  coordinates.** Nothing of the position is stored, logged or cached.

`{"status": "found", "site", "site_name", "place", "attribution", "at", "minutes_ago",
"accuracy_m"}`, or `waiting`, `not_set_up` (no Traccar Address), `no_device` or
`traccar_error` (`message`). Settings > Locating People: **Traccar Address** (Traccar's web
address, not the phones' port 5055), **Traccar User** and **Traccar Password or Token** (a
user that can see the employees' devices; read-only is enough; with no user, the password
field holds a Traccar API token). The ERP server must reach both Traccar and OpenStreetMap.
Do not switch on Traccar's own geocoder with OpenStreetMap: it would look up every position
of every phone, which OpenStreetMap's public geocoder does not allow.

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
