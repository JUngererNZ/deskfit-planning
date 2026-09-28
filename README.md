# Office Desk Planning

Internal web app for managing office desk allocation. Office admins sign in and
move people between desks; everyone else sees a live, read-only floor plan that
updates within a fraction of a second.

- **Public seating view** — read-only floor plan, auto-refreshing
- **Admin login** — Django session authentication, staff accounts only
- **Admin board** — drag-and-drop or click-to-move, plus a people panel for
  renaming and entering department / role / employment status
- **Real-time** — Django Channels WebSocket, with 5-second HTTP polling as an
  automatic fallback if the socket cannot connect

Layout comes from `DEMO-SEATING-PLAN.xlsx` (grid A–I × rows 1–41). Colours come
from `SEATING-PLAN-DEMO.png`.

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Built and tested on 3.14.4 |
| `python3-venv` | May need `sudo apt install python3-venv` |
| Network access | Needed once, to install dependencies |

**If your machine has no `pip` and no `ensurepip`** (common on Debian/Ubuntu
without `python3-venv`), the commands below bootstrap pip manually. This is
already done in this repository's `.venv`.

```bash
# 1. Create the venv without pip
python3 -m venv --without-pip .venv

# 2. Download the pip bootstrap script
curl -o /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py
# (Windows: curl -o %TEMP%\get-pip.py https://bootstrap.pypa.io/get-pip.py)

# 3. Install pip into the venv
.venv/bin/python3 /tmp/get-pip.py
```

Verify:

```bash
.venv/bin/python3 -m pip --version
```

> On Windows, use `.venv\Scripts\python3.exe` in place of `.venv/bin/python3`.

---

## 2. Install dependencies

```bash
.venv/bin/python3 -m pip install -r requirements.txt
```

Optional extras (production only):

```bash
.venv/bin/python3 -m pip install channels-redis psycopg[binary]
```

---

## 3. Initialise the database

```bash
.venv/bin/python3 manage.py migrate
```

Creates `db.sqlite3` in the project root. This is the default; see
[PostgreSQL](#postgresql) to switch.

---

## 4. Load the seating plan

```bash
.venv/bin/python3 manage.py import_seating
```

Expected output:

```
Imported from DEMO-SEATING-PLAN.xlsx:
  desks          : 150
  fixed areas    : 39
  people         : 67
  assigned       : 67
  unassigned     : 0
  placeholders   : 62
  empty desks    : 44
  freed 43 duplicate cells (16 repeated names); first occurrence kept its seat.
```

The command is **idempotent** — running it again produces identical counts and
does not disturb manual desk moves. Use `--reset` to wipe and re-import from
scratch, and `--file <path>` to load a different spreadsheet.

### How the source spreadsheet is interpreted

- Grid is columns **A–I**, rows **1–41**.
- The file assigns **the same person to multiple desks** — 16 names appear in
  43 cells, so `HUMAN48` alone occupies 8 desks. A person cannot sit in two
  places, so the importer keeps the **first occurrence in reading order**
  (top-down, then left-to-right) and frees the remaining 43 cells to *Spare*.
  Every freed cell is reported in the command output and in the log.
- `SPARE` is treated as a **desk status**, not a person, so cell `H2` starts
  empty.
- `PERSON` is imported as a person literally named `PERSON`, flagged as a
  placeholder so it is easy to find and rename.
- `HUMAN01`–`HUMAN61` are **placeholders** pending real staff names. They are
  flagged `is_placeholder` and shown in grey italics on the floor plan so they
  are not mistaken for real occupants.

### Fixed areas

All nine required fixed areas are present and non-assignable:

| Area | Source |
|---|---|
| BACK WALL, DOOR ×2, WINDOW, MEETING ROOM, HUDDLE | Cell positions in the spreadsheet |
| KITCHENETTE, BREAKOUT ROOM, PHOTOCOPIER, SALES STANDUP AREA | Placed in unoccupied floor space (rows 6, 9, 12, 15, 18, 21, 31 and the C/F/I corridors) |

---

## 5. Create an admin account

No account is seeded. Until you create one, the app runs read-only.

```bash
.venv/bin/python3 manage.py createsuperuser
```

Use a **staff/superuser** account — non-staff accounts are rejected by the
login endpoint even with correct credentials.

---

## 6. Run it

```bash
.venv/bin/daphne -b 0.0.0.0 -p 8000 deskplan.asgi:application
```

Daphne is required: `runserver` does not support WebSockets.

| URL | Purpose |
|---|---|
| `http://<host>:8000/` | Public seating view (read-only) |
| `http://<host>:8000/admin-login/` | Admin sign in |
| `http://<host>:8000/admin-board/` | Admin board |
| `http://<host>:8000/django-admin/` | Django data admin |

To reach it from another machine, bind to `0.0.0.0` (as above) and allow the
port through the firewall.

---

## 7. Using the app

**Public view.** Open `/`. Desks are colour-coded by pod, matching the reference
PNG palette. Fixed areas render as solid grey blocks. Spare desks show a dashed
border. The header shows a live connection indicator and the time of the last
update.

**Moving a person** (admin board), either:

- **Drag** an occupied desk onto a spare desk, or
- **Click** an occupied desk to select the person, then click the destination.
  Clicking an *occupied* desk performs a **swap**. `Esc` cancels a selection.

**Editing a person.** Click an occupied desk (or a roster entry) to open the
details panel, then set name, department, role and employment status. Replacing
a `HUMANnn` placeholder name with a real name automatically clears the
placeholder flag. **Unassign** frees a seat without deleting the person.

---

## 8. API reference

All responses are JSON. Errors use a consistent envelope:

```json
{"error": {"code": "DESK_OCCUPIED", "message": "A3 is already occupied by BOB.", "field": "desk_id"}}
```

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| GET | `/api/floorplan` | public | Full plan snapshot + stats |
| POST | `/api/auth/login` | public | `{username, password}` → session cookie |
| POST | `/api/auth/logout` | public | Ends the session |
| GET | `/api/auth/session` | public | Current auth state |
| GET | `/api/people` | admin | Roster |
| POST | `/api/people/update` | admin | `{person_id, name?, department?, role?, employment_status?}` |
| POST | `/api/assignments/move` | admin | `{person_id, desk_id, version?, swap?}` — `desk_id: null` unassigns |
| GET | `/api/audit` | admin | Last 100 assignment events |
| WS | `/ws/floorplan/` | public | Push channel for live updates |

### Error codes

| Code | Status | Meaning |
|---|---|---|
| `INVALID_CREDENTIALS` | 401 | Wrong username or password |
| `UNAUTHENTICATED` | 401 | Admin endpoint called without signing in |
| `FORBIDDEN` | 403 | Account is not staff |
| `NAME_REQUIRED` | 400 | Blank name submitted |
| `NAME_TOO_LONG` | 400 | Name/department/role over 100 characters |
| `INVALID_REQUEST` | 400 | Malformed body or bad enum value |
| `DESK_NOT_ASSIGNABLE` | 400 | Target is a fixed area (wall, door, room) |
| `NO_CHANGE` | 400 | Person already at that desk / not seated |
| `NAME_TAKEN` | 409 | Another person already has that name |
| `DESK_OCCUPIED` | 409 | Target desk is taken; retry with `swap: true` |
| `STALE_VERSION` | 409 | Desk changed since the client last read it |
| `DESK_NOT_FOUND` | 404 | No such desk |
| `PERSON_NOT_FOUND` | 404 | No such person, or the person is inactive |

### Concurrency

Every move runs inside a transaction with row-level locks
(`SELECT … FOR UPDATE`) on the target desk, so two admins cannot claim the same
desk simultaneously. Each desk carries a `version` counter; a client may send
the version it last saw and a mismatch is rejected with `STALE_VERSION` rather
than silently overwriting a newer change.

---

## 9. Logging

Errors and activity are written to `logs/deskplan.log` (rotating, 5 MB × 5) and
the console. Every HTTP response includes an `X-Request-ID` header, and the same
ID appears in the log for that request — quote it to an administrator to find
the exact log lines.

Failed login attempts and rejected moves are logged with the requesting address.

---

## 10. Tests

```bash
.venv/bin/python3 manage.py test floorplan
```

40 tests covering the importer's duplicate resolution, move/swap/unassign logic,
every validation error, authentication and authorisation, concurrent-move
protection, and snapshot integrity.

---

## Production deployment

### Required environment variables

```bash
export DJANGO_SECRET_KEY='<50+ random characters>'
export DJANGO_DEBUG=False
export DJANGO_ALLOWED_HOSTS='deskplan.example.internal'
export DJANGO_CSRF_TRUSTED_ORIGINS='https://deskplan.example.internal'
export DJANGO_TIME_ZONE='Australia/Sydney'   # optional
export DJANGO_LOG_LEVEL='INFO'               # optional
```

Generate a secret key:

```bash
.venv/bin/python3 -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
```

With `DJANGO_DEBUG=False` the app enables HSTS, SSL redirect, clickjacking and
content-type protections automatically. Serve it behind a TLS-terminating
reverse proxy.

### Redis (required for multi-worker)

The default in-memory channel layer only broadcasts within a single process. If
you run more than one worker, set `REDIS_URL` or clients on other workers will
never receive updates.

```bash
export REDIS_URL='redis://127.0.0.1:6379/0'
.venv/bin/python3 -m pip install channels-redis
```

### Collect static files

```bash
.venv/bin/python3 manage.py collectstatic --noinput
```

WhiteNoise serves them, so a separate static file server is not required.

### systemd unit

```ini
[Unit]
Description=Office Desk Planning
After=network.target redis-server.service

[Service]
Type=simple
User=www-data
WorkingDirectory=/srv/deskplan
EnvironmentFile=/srv/deskplan/.env
ExecStart=/srv/deskplan/.venv/bin/daphne -b 127.0.0.1 -p 8000 deskplan.asgi:application
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### PostgreSQL (optional)

```bash
export DATABASE_URL='postgres://deskplan:password@127.0.0.1:5432/deskplan'
.venv/bin/python3 -m pip install "psycopg[binary]"
.venv/bin/python3 manage.py migrate
```

Back up the database with `manage.py dumpdata` / `loaddata`, or a normal
`pg_dump`.

---

## Project layout

```
deskplan/           Django project (settings, urls, asgi)
floorplan/
  models.py         Desk, Person, Assignment, AssignmentEvent
  grid.py           Grid geometry and fixed-area placement
  importer.py       Spreadsheet parsing and duplicate resolution
  services.py       Move/swap/update logic, snapshot, broadcast
  views.py          REST API
  pages.py          HTML page views
  consumers.py      WebSocket consumer
  routing.py        WebSocket routes
  middleware.py     Request-ID correlation
  tests.py          40 tests
  management/commands/import_seating.py
static/css/deskplan.css
static/js/deskplan.js   API client, live updates, renderer
templates/floorplan/    public.html, login.html, admin.html, forbidden.html
```

---

## Known limitations

- **62 of 67 occupants are placeholders** (`HUMAN01`–`HUMAN61`, `PERSON`) and
  43 desks sit empty until real names are entered. Editing is manual, one
  person at a time.
- **Department and role ship empty.** The pod colours are derived from grid
  position, not from a stored department, because the source data did not
  contain departments. Once you populate the `department` field, colours can be
  switched to be department-driven — see `podFor()` in `static/js/deskplan.js`.
- **No bulk rename.** 62 placeholder renames means 62 form submissions. A CSV
  import was deliberately out of scope; `GET /api/people` returns the full
  roster if you later want to script it.
- **Single-process channel layer** unless `REDIS_URL` is set (see above).
- **No pagination** on `/api/people` or `/api/audit`; fine at this scale, but
  revisit past a few thousand people.
