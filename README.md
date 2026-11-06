# Dr.Prepper

Offline medication and overdose reference for austere and delayed-care settings, on a
Raspberry Pi 5 with an Elecrow 7" (1024x600) touchscreen, plus a lightweight field EHR
served over the Pi's own Wi-Fi hotspot. Nothing needs the internet once it's set up.

| Piece | What it is |
|---|---|
| `drprepper-ingest` | Builds `drugs.db` (SQLite + FTS5) from the openFDA drug-label bulk export (DailyMed SPL) plus 20 curated overdose/toxicology cards. Dedupes thousands of manufacturer labels to one per generic/route/Rx-OTC. |
| `drprepper-touch` | Kivy kiosk app for the touchscreen: search with a built-in keyboard, Rx/OTC filter, A-Z browse, label sections as tabs, overdose cards, dose-math helper. Read-only; never touches patient data. |
| `drprepper-manage serve` | FastAPI web console: triage board, assignment and handoff, patient charts, encounters, vitals, medications with allergy checks, notes, handoff printout, audit trail with syslog forwarding, trust center, scheduled backups and restore. |
| `pi/setup.sh` | Turns a fresh Pi OS Bookworm install into the appliance: venv, service user, TLS, encrypted EHR, hotspot, systemd, kiosk autostart, display mode, backup folder. |

## 1. Build and test in Docker (no Pi needed)

```bash
docker compose up --build
```

* Web console: `https://localhost:8443`. The first admin's username (`admin`) and one-time
  password are printed in `docker compose logs web`. To use your own name for that account, set
  `DRP_ADMIN_LAST` / `DRP_ADMIN_FIRST` in `docker-compose.yml` before the first start.
* Touchscreen preview: `http://localhost:6080/vnc.html` (1024x600, same Kivy app as the Pi).
* Backups go to the `backups/` folder next to `docker-compose.yml` (a stand-in for a USB drive).
* Tests: `docker compose run --rm web test`

### Drug data: sample first, full FDA database in the background

On first start the console comes up within seconds on a small **sample** database (placeholder
text, about a dozen drugs) and, in the background, downloads the full FDA label set (about 1.5 GB)
and builds the real database. When the build finishes it replaces the sample in one step; the web
console and touchscreen switch over by themselves, no restart needed.

* While the sample is in use, an amber **SAMPLE DRUG DATA – not a real drug reference** banner is
  shown on every page and on the touchscreen, with download progress.
* Progress: `/data/ingest-status.json`; full log: `docker compose exec web cat /data/ingest.log`.
* No internet on first start? The banner says the download failed. Connect and run
  `docker compose run --rm ingest`, or just restart the web container.
* Downloads are cached in the volume, so an interrupted download resumes where it left off.
* To refresh the data later (FDA labels change): `docker compose run --rm ingest`.
* Set `DRP_AUTO_INGEST: "0"` in `docker-compose.yml` to turn off the automatic download.

Copy the finished database out for the Pi (optional; the Pi can also download its own):

```bash
docker compose cp web:/data/drugs.db ./drugs.db
```

### Try syslog forwarding in Docker

```bash
docker compose --profile syslog up -d syslog-test
docker compose logs -f syslog-test
```

In **Admin > Syslog**: server `syslog-test`, port `6514`, transport TLS. Paste the contents of
`syslog-test/syslog-test.crt` (created on the receiver's first start) as the server CA, save, then
**Send test message**. Every audit entry then shows up in the receiver's log.

## 2. Deploy to the Pi 5

1. Flash **Raspberry Pi OS (64-bit, Desktop, Bookworm)**; create your user; boot with the Elecrow panel attached.
2. Copy this repo and `drugs.db` to the Pi (USB stick or `scp`).
3. Run:

```bash
sudo ./pi/setup.sh --drugs-db ./drugs.db --ssid DrPrepper --wifi-pass 'choose-a-long-one' \
     --admin-last Rivera --admin-first Sam --admin-credential EMT-P
sudo reboot
```

Without `--drugs-db`, setup installs the sample and then downloads and builds the full FDA database
before it switches Wi-Fi over to the hotspot (the download needs that internet connection; allow
15-45 minutes on a Pi 5). If it fails, the unit keeps the sample and shows the warning banner; connect
Ethernet and run `sudo systemctl start drprepper-ingest`.

**SSH is turned off at the end of setup** and a firewall is enabled. If you install over SSH and want to
keep it, add `--ssh keys` (key-only login; put your public key in `~/.ssh/authorized_keys` first).
Setup also creates the `drprepper-kiosk` screen account (no sudo); your own account keeps its rights.

Before real patient data: set an **exit PIN** (Admin > Touchscreen) and **seal the encryption key**
(Admin > Encryption). See "Data protection and security" below.

Setup prints the first admin password **once** and writes the hotspot details to
`/etc/drprepper/hotspot.txt`. After reboot the touchscreen opens the reference fullscreen;
devices that join the `DrPrepper` Wi-Fi open `https://10.42.0.1:8443` for the EHR.

Useful commands on the Pi (prefix each with
`sudo -u drprepper --preserve-env=DRP_STATE_DIR,DRP_SECRETS_DIR`, after `set -a; . /etc/default/drprepper; set +a`):

```bash
drprepper-manage create-user hsmith --last Smith --first Harold --credential EMT-B --role provider
drprepper-manage reset-password hsmith
drprepper-manage list-users
drprepper-manage verify-audit
drprepper-manage backup /mnt/drp-backup
systemctl status drprepper-web ; journalctl -u drprepper-web -f
```

`--no-hotspot` keeps the Pi on your normal Wi-Fi instead (console then at the Pi's LAN address).

## Users and roles

People are shown everywhere as **Last, First Credential** (e.g. *Smith, Harold EMT-B*). Admins add
users in **Admin > Users**; each gets a one-time password and chooses their own at first login.

| Role | Opens charts | Changes charts | Assigns | Admin pages |
|---|---|---|---|---|
| Triage | All, no reason needed | Yes | Any patient, from the triage board | No |
| Provider | Their assigned patients; others with a reason | Yes | Hands off their own patients | No |
| Viewer | Any, with a reason | No | No | No |
| Admin | Any, with a reason | Yes | Any patient | Yes |

* **Patient numbers:** every patient gets a random 8-digit number at creation.
* **Assignment:** triage registers arrivals and assigns each to a provider. Patients a provider
  creates themselves are assigned to them. At the end of a shift a provider hands off from the chart
  with a note; every assignment and handoff is shown on the chart and in the audit trail.
* **Reason for access:** opening a chart that isn't assigned to you asks for a reason (pick-list plus
  details). It lasts 30 minutes for that patient (`DRP_GRANT_MINUTES`) and is logged as a notice.
* **Last seen by:** the top of every chart shows who last recorded care and when (vitals, medication,
  note, encounter change).
* **Allergy badges:** wherever a patient's name appears, a badge shows **ALLERGIES** (red),
  **NKDA** (gray) or **ALLERGIES UNKNOWN** (amber). Clicking it opens the list for any signed-in
  role, with no reason needed. Each click is recorded. The allergy check on medications applies to
  every provider, assigned or not.
* **Sessions:** sign-out after 30 minutes idle (`DRP_IDLE_MINUTES`) or 12 hours total; 5 failed
  logins in 5 minutes lock that username and device for 5 minutes.

## Touchscreen intake (incoming patients)

Whoever is on comms can report an incoming patient from the unit's touchscreen without a laptop:
tap **INCOMING PATIENT** on the home screen, or **SEND INCOMING PATIENT TO TRIAGE** at the top of the
Unknown Overdose card, then tap age band, sex, concern, responsiveness, breathing, naloxone given and
ETA (optional: approximate age, location/unit, notes, name or call sign) and **SEND TO TRIAGE**.

* The triage board shows a red **Incoming** section with an ETA countdown, the full radio report and
  **Arrived**, **Pre-assign**, **Open chart** and **Didn't arrive** buttons. Triage and admin users see
  a pulsing **Incoming** badge on every page; **Alert sound** on the board turns on a chime.
* The touchscreen can only send reports. It never opens the EHR or reads any patient data; it gets
  back only the new patient number to read out on arrival.
* It authenticates with a device token and checks the server by certificate fingerprint. Both live in
  the kiosk folder (`/var/lib/drprepper/kiosk` on the Pi, readable by the kiosk user; no patient data).
* **Admin > Touchscreen:** turn intake off, require a 4-8 digit PIN, replace the token, see recent reports.
* Every report and refusal is in the audit trail as *Touchscreen intake (call sign)*.
* If the server can't be reached, the screen says **NOT SENT** and keeps the form so it can be resent.

## Branding and themes

* **Admin > Branding:** replace "Dr.Prepper" with your own title and an optional tagline, pick one of
  eight built-in icons (medical cross, heartbeat, capsule, stethoscope, first-aid kit, Rod of
  Asclepius, field tent, shield) or upload your own (PNG, JPEG or WebP up to 512 KB; SVG is refused
  because it can carry scripts). An uploaded icon is kept, so you can switch to a built-in and back
  without uploading it again. The built-ins are original artwork (regenerate them with
  `python tools/make_icons.py`); the red cross, red crescent and Star of Life are deliberately left
  out because they're protected emblems. Then choose
  icon and title, title only or icon only. The title also names browser tabs and the sign-in page, and
  the touchscreen shows the same title and icon.
* Pick the unit's default theme and light/dark setting, optionally a brand accent colour (adjusted
  automatically to stay readable), and whether users may choose their own theme.
* **Themes:** Field, Clinic, Desert, Night ops (red on black in dark mode, to protect night vision),
  High contrast (bright sun, low vision) and Slate. Each has a light and a dark version; **Auto**
  follows each device. Every palette is checked for WCAG AA contrast by the test suite, and allergy,
  overdose and warning colours keep their meaning in every theme.
* **Account menu > Display & theme:** each user's own theme and light/dark choice, kept with their
  account. The **Auto / Light / Dark** button in the header switches mode in one tap (also on the
  pages you can open without signing in).

## Patient flow: triage time, locations and leaving care

* **Triage time** runs from arrival (or record creation) until the patient is first assigned to a
  provider. The triage board shows a live timer per waiting patient (amber at 15 min, red at 30; change
  under Admin > Locations), the median and longest triage time over the last 24 h, and each assigned
  patient's triage time. Incoming patients start the clock when marked as arrived. The assignment
  history and audit entry record it ("after 14 min in triage").
* **Locations** (Admin > Locations): you build the layout; nothing is created for you (there's an optional
  "Start from a sample layout" button). Add triage areas, rooms, ORs, other areas and a morgue, name them
  anything, and give rooms **beds** ("Room 1" with Bed 1-6 shows as "Room 1 · Bed 3"), or add beds on their
  own. "How many" adds numbered sets (Bed x 12 -> Bed 1 ... Bed 12). Patients go in a bed, or in a room or
  area that has no beds. Unused locations can be removed; ones with patient history are turned off so the
  record still reads correctly. New and arriving patients go to the first triage spot with space (none set
  up: no location, and the triage clock still runs). Every move is kept in the movement history and audit
  trail. **Census** shows who is where.
* **Leaving care** (chart > Location and status): Discharged, Left AMA, Transferred / evacuated, Left
  without being seen, or Record death. AMA requires the capacity / risks / alternatives / may-return
  checks, the specific risks explained, signed-or-refused, and a witness when refused. Death requires
  time of death, who pronounced, and a confirmation; the patient moves to the morgue. Each outcome
  closes open encounters, takes the patient off the boards (they appear under "Left care in the last
  24 h") and prints with signature lines. Triage/admin can **readmit** (new triage) or **correct** a
  disposition recorded in error, with a reason; corrections are logged as warnings.

## Status board (second screen)

A read-only wall display at `/board`: every room and OR as a tile (Occupied with time, Empty · ready,
Needs cleaning, Cleaning, Out of service) plus active counts: incoming with next ETA, waiting for a
provider with the longest wait (amber/red at your triage targets), in the triage area, in rooms, in OR,
and total active patients. It refreshes every 10 s and shows NO CONNECTION if the unit stops answering.

* **Sanitized by default** — no names, numbers, ages or complaints. Admin > Status board can turn on
  names for screens inside the facility: "M. Hayes", their provider, and ALLERGY / DNR flags; every
  screen then shows "NAMES SHOWN". The morgue tile is off unless turned on. Changes are audited.
* **Cleaning:** a room or OR becomes *Needs cleaning* automatically when its last patient leaves (moved,
  discharged, died). Anyone signed in — including viewer accounts for cleaning staff — taps *Start
  cleaning* / *Ready* on the Census page. Triage/admin can take a room out of service (moves into it are
  blocked). Moving a patient into a room that isn't ready is allowed but flagged.
* **Who can open it:** the Pi itself (the HDMI screen, no login), anyone signed in, or a screen holding a
  **display key** (Admin > Status board > Create key; open the link once on that screen). A key opens
  the board and nothing else; revoke it any time.
* **On the Pi:** `setup.sh` installs Chromium and starts the board full screen on the second HDMI port
  (`--board-output HDMI-A-2`), keeps the touchscreen app on the 7" panel (`--touch-output HDMI-A-1`) and
  maps touch input to the panel only so taps never land on the TV. `--no-board` skips it. *Not yet
  tested on hardware:* if the windows land on the wrong screens, swap the two output names.
* **In Docker:** sign in and open https://localhost:8443/board, or create a display key and open its link
  on another device on your network.

## History and home medications

* Chart > History: past medical and surgical history, family and social history (tobacco, alcohol,
  other substances), implants/devices, usual doctor, code status and advance directive, blood type,
  pregnancy/LMP, tetanus and immunizations. DNR, DNR/DNI and comfort-care status shows as a purple
  badge next to the allergy badge on boards and charts.
* Chart > Home medications: one row per drug (searchable against the drug database), with dose, route,
  frequency, indication, last dose, status and source. Rows are never deleted ("entered in error"
  instead). Recording a medication that matches a current home med warns about doubling up.
* Encounters now have an Exam field; the handoff summary prints history, code status and home meds.

## Date and time

Admin > Date & time sets the unit's time zone (default `DRP_TZ`, else UTC). It applies to the console,
audit trail, printouts, backup schedule and the touchscreen clock; earlier entries are converted for
display. The page also compares your device's clock with the unit's. In Docker you can set the
starting zone with `DRP_TZ=America/Chicago docker compose up -d`.

## Audit trail and syslog

Entries read like `2026-10-22 14:05:12 - Williams, Abigail MD accessed patient record 48213907`.
Logged: sign-ins (and failures, lockouts, timeouts, sign-outs), every chart and encounter view,
reasons for access, allergy badge views, patient searches, every clinical entry, allergy alerts and
overrides, assignments and handoffs, user changes, certificate changes, backups, restores and
settings changes.

* **Admin > Audit trail:** filter by user, patient number, action, level and date; export CSV.
* **Tamper evidence:** each entry is chained to the one before it with SHA-256. **Verify chain**
  (or `drprepper-manage verify-audit`) reports the first entry that was altered or deleted.
* **Admin > Syslog:** RFC 5424 over UDP, TCP or TCP+TLS (default, port 6514), octet-counting or
  newline framing, optional CA certificate. Each message carries structured data (entry number,
  user, action, patient number, client address, chain hash). Entries queue while the server is
  unreachable and are sent when it returns.

## Trust center (Admin > Trust center)

* Shows the installed certificate: names, validity, days left, SHA-256 fingerprint.
* Create a new self-signed certificate, or a signing request (CSR) for your organization's CA and
  upload the signed certificate with its chain. The key is checked against the certificate before
  anything is installed, the previous pair is kept for one-click rollback, and the new certificate
  is in use immediately without a restart.
* Devices download the certificate from the sign-in page (`/trust/drprepper.crt`) to stop the browser warning.

## Backups and restore (Admin > Backup & restore)

A backup bundle (`.drpbak`) holds the encrypted EHR, audit trail, users and certificate. The EHR
key travels inside it, sealed with your **backup passphrase** (scrypt + AES-256-GCM). Without the
passphrase a bundle is unreadable, so it's safe on a USB drive, and also **unrecoverable**: write
the passphrase down and keep it off the unit.

* **Schedules:** daily, weekly and monthly, each with a time and how many copies to keep. A missed
  run (unit was off) runs at next start; a missing drive is retried every 30 minutes and logged.
* **Destination:** an off-device folder. The page warns if it's on the same disk as the live data.
* **Verify** any bundle (checksums, decryption, database integrity) without changing anything.
* **Restore** from the destination or an uploaded file. Type RESTORE to confirm; the current data
  is kept in `/var/lib/drprepper/pre-restore-<time>/`.

### USB drive on the Pi

```bash
lsblk -f                                    # find the drive, e.g. /dev/sda1, note its UUID
echo 'UUID=<uuid> /mnt/drp-backup exfat defaults,nofail,uid=drprepper,gid=drprepper,umask=077 0 0' | sudo tee -a /etc/fstab
sudo mount /mnt/drp-backup
```

Use `ext4` instead of `exfat` (and drop the uid/gid/umask options, then `sudo chown drprepper /mnt/drp-backup`)
if the drive is Linux-only. A network share works too (`cifs` or `nfs` mount at the same path).

### Moving to new hardware

1. Set up the new Pi with `pi/setup.sh` as above.
2. Plug in the backup drive, then:

```bash
sudo systemctl stop drprepper-web
set -a; . /etc/default/drprepper; set +a
sudo -u drprepper --preserve-env=DRP_STATE_DIR,DRP_SECRETS_DIR drprepper-manage restore /mnt/drp-backup/<file>.drpbak
sudo systemctl start drprepper-web
```

Users, patients, the audit chain and the certificate all come across, so devices that already
trust the old unit keep working.

## Data protection and security

Hardening from the 2026-10-22 security audit (HIPAA / NIST CSF 2.0 / ISO 27001 review):

* **Encryption at rest.** `ehr.db` is AES-256 encrypted with SQLCipher. **Seal the key before real
  patient data:** Admin > Encryption (or `drprepper-manage seal-key`) wraps it with a passphrase
  (scrypt + AES-GCM) and removes the plain key file. The unit then starts **locked**: the drug and
  overdose reference works, but no records open until someone enters the passphrase at
  `https://<unit>:8443/unlock`. Wrong passphrases are throttled and recorded. Backups, syslog and
  restores keep working after the unlock (a restored key is re-sealed under the same passphrase).
  `drprepper-manage unseal-key` goes back to unattended start.
* **Kiosk.** The screen logs in as `drprepper-kiosk`, an account with no password and no sudo.
  "Exit to desktop" needs the exit PIN set in Admin > Touchscreen (only its salted hash is in the
  kiosk folder); with no PIN the button is disabled.
* **Sessions** live on the server: logout, a password change (other devices), an admin password reset,
  disabling an account or changing a role all end sessions at once. Admin > Users shows signed-in
  sessions and has "Sign out everywhere". Idle sign-out (30 min) is enforced by the server.
* **Sign-in.** Unknown usernames take as long as wrong passwords; lockout is per username + device
  (20 failures per device across usernames); growing delay after failures; common passwords and
  passwords containing the username are refused; PBKDF2-SHA256 at 600,000 rounds (older hashes are
  upgraded at the next sign-in).
* **Logs.** The web server's access log drops query strings, so patient searches and display keys
  never reach the system log. The syslog copy withholds search text (the local, encrypted audit keeps it).
  The audit page summarises the last 24 h and flags anyone opening 5+ unassigned charts in an hour.
* **Record integrity.** Edits to encounter text, demographics and history keep every earlier value
  ("Edit history" on the chart and encounter).
* **Restores** keep the replaced data as a safety copy whose key sits in the secrets folder (sealed if
  the unit is sealed); delete it from Admin > Backup & restore once you're sure.
* **Network (Pi).** nftables allows only the console (8443) plus DHCP/DNS for hotspot clients; SSH is
  off unless setup is run with `--ssh keys` (key-only); hotspot clients are isolated from each other.
  TLS 1.2+ only, HSTS, no server banner, upload size caps.
* **Docker (dev).** Ports bound to 127.0.0.1 (change to `"8443:8443"` to test from phones); services run
  as an unprivileged user.
* **Dependencies** are pinned in `constraints.txt` (used by Docker and the Pi installer). Before a
  release run `tools/security_check.sh` (pip-audit, Bandit, tests).
* The Pi 5 keeps time off-grid only with its RTC battery (connector J5); the console warns if the
  clock looks wrong because audit timestamps depend on it.
* Still to do: optional two-factor sign-in for admins; unlocking from the touchscreen.

## Content and review

* Drug pages are FDA label text: indications, dosing, contraindications, overdosage, interactions.
  Coverage is US prescription and OTC products in openFDA.
* `src/drprepper/data/toxicology.yaml` holds the overdose cards: recognition, life threats,
  field management, antidotes and rescue dosing, what to avoid, observation times. Street names are
  searchable. Cards contain no recreational-use dosing.
* **Every card ships as `review_status: draft`.** Before deployment, have a physician medical
  director review each and set `review_status: "reviewed YYYY-MM-DD <initials>"`. Rebuild `drugs.db` after editing.
* This is a reference aid for trained responders, not a substitute for medical direction.
  Poison Control (US): 1-800-222-1222.

## Layout

```
src/drprepper/
  core/      shared read-only drug DB access, search, text cleanup
  ingest/    openFDA download/stream, dedupe, curated cards -> drugs.db
  server/    app (clinical routes), admin (users/audit/syslog/trust/backups), web (roles, access),
             ehr (schema, migrations), auditlog, syslog, trust, backup, templates, static
  touch/     Kivy touchscreen app
  data/      toxicology.yaml, sample fixture
pi/          setup.sh, systemd unit, kiosk launcher
docker/      container entrypoint
tests/       pytest suite
```
