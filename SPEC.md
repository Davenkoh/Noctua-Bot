# Noctua Bot — Technical Spec (v1)

Official Telegram bot for the **Noctua** dorm (~120 residents).

V1 features:
1. **Laundry machine tracker** — residents register machine usage via buttons, timers ping them, everyone can see live status.
2. **Broadcast** — designated leaders can push announcements to all registered residents.

Future features will be added, so keep the architecture modular (one handler module per feature).

## Stack

- Python 3.11 (venv at `.venv`, managed by the orchestrator — agents must NOT create venvs or pip install)
- `python-telegram-bot[job-queue]~=21.9` (async API: `Application`, `ConversationHandler`, `JobQueue`, `CallbackQueryHandler`)
- `python-dotenv` for `.env`
- SQLite via stdlib `sqlite3`, single file `noctua.db` at project root
- Long polling (no webhook)
- All timestamps stored as **UTC ISO-8601 strings**; displayed in `config.TIMEZONE` (default `Asia/Singapore`)
- Parse mode: **HTML everywhere** (`Defaults(parse_mode=ParseMode.HTML)`). ALWAYS `html.escape()` user-provided strings (names, broadcast text) before embedding.

## File layout & ownership

```
Noctua Bot/
  SPEC.md                  (this file — do not edit)
  requirements.txt         SONNET
  .env.example             SONNET
  .gitignore               SONNET
  README.md                SONNET
  tests/
    __init__.py            SONNET
    test_rooms.py          SONNET
  bot/
    __init__.py            SONNET (empty file)
    config.py              SONNET
    rooms.py               SONNET
    db.py                  OPUS
    util.py                OPUS (time formatting, mention helpers — optional but recommended)
    keyboards.py           OPUS
    jobs.py                OPUS
    main.py                OPUS
    handlers/
      __init__.py          OPUS (exposes register_all(application))
      registration.py      OPUS
      laundry.py           OPUS
      status.py            OPUS
      broadcast.py         OPUS
      help.py              OPUS
```

Each agent creates ONLY its own files. Never edit the other agent's files.

## Machines

| id | label | kind | emoji | durations (min) |
|----|-------|------|-------|-----------------|
| w1 | Washer 1 | washer | 🫧 | 30 |
| w2 | Washer 2 | washer | 🫧 | 30 |
| d1 | Dryer 1 | dryer | 💨 | 30, 45, 60 |
| d2 | Dryer 2 | dryer | 💨 | 30, 45, 60 |

Rules:
- **One ACTIVE session per machine.** A user MAY hold sessions on multiple machines at once (wash + dry is legit).
- Session lifecycle: `active` → `done` (timer fires) → `collected` (owner taps ✅ Collected, or someone else starts the machine). Also `cancelled` (owner stops own active timer early).
- **Extension ("paid twice")**: while a user's OWN session is ACTIVE on a machine, tapping a duration button again = request to ADD those minutes. MUST show a confirm dialog first: "You already have a timer on Washer 1 (ends 3:05 PM). Add 30 min → new end 3:35 PM?" → [✅ Yes, I paid twice] [❌ No]. On yes: `ends_at += minutes`, `duration_min += minutes`, reschedule the done job, cancel any pending reminder. Multiple extensions allowed, each confirmed. **Never any free-text time entry.**
- Machine with someone ELSE's active session (time remaining): blocked — show who (name, room, handle) and est. time left.
- Machine whose latest session is `done` (uncollected): anyone may start it. Starting marks the old session `collected` and the confirmation notes "♻️ Previous load by NAME was still inside — it's now marked collected."

## Config contract (`bot/config.py`) — exact API

```python
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

@dataclass(frozen=True)
class Machine:
    id: str
    label: str                  # "Washer 1"
    kind: str                   # "washer" | "dryer"
    emoji: str                  # "🫧" | "💨"
    durations: tuple[int, ...]  # minutes

PROJECT_ROOT: Path              # parent of the bot/ package
MACHINES: dict[str, Machine]    # insertion order: w1, w2, d1, d2
BOT_TOKEN: str | None           # from .env / env; main() validates non-empty
LEADER_USERNAMES: set[str]      # lowercase, no @; parsed from env LEADER_USERNAMES="alice, bob" ("@" and whitespace stripped)
TIMEZONE: ZoneInfo              # env TIMEZONE, default "Asia/Singapore"
DB_PATH: str                    # env DB_PATH, default str(PROJECT_ROOT / "noctua.db")
PING_COOLDOWN_MIN: int = 3      # min minutes between "ping previous user" nudges per machine
REMINDER_AFTER_MIN: int = 10    # auto-reminder after done if not collected

def is_leader(username: str | None) -> bool  # None-safe, case-insensitive
```

`load_dotenv(PROJECT_ROOT / ".env")` at import (does not override real env vars). Importing config must never raise on missing token — only `main()` exits with a friendly message.

## Rooms contract (`bot/rooms.py`) — exact API

**Pure stdlib, no project imports** (tests must run without third-party deps).

Room data (keep as easily editable constants at the top of the file — the dorm leader may tweak):
- Floors **06, 07, 08**, each with rooms **01–27**.
- **Suite rooms** (subdivided into units A–F; unit letter REQUIRED):
  `06-01, 06-11, 06-12, 07-01, 07-11, 07-12, 08-01, 08-12`
- Normalized display form: `#06-27`, `#08-01C`.

```python
SUITE_LETTERS = "ABCDEF"
FLOOR_ROOMS: dict[str, list[str]]   # {"06": ["01",...,"27"], "07": [...], "08": [...]}
SUITE_ROOMS: set[str]               # {"06-01", ...} base form, no '#'

@dataclass
class RoomResult:
    ok: bool
    room: str | None = None      # normalized "#06-27" / "#08-01C" when ok
    needs_letter: bool = False   # valid suite base, letter missing → caller shows A–F buttons
    base: str | None = None      # "08-01" when needs_letter
    error: str | None = None     # friendly message when not ok and not needs_letter

def validate_room(text: str) -> RoomResult
def suite_letters(base: str) -> list[str]    # ["A", ..., "F"]
```

Accepted input (case-insensitive, `#` optional, separators `-`, `–`, space, or none): `#06-27`, `06-27`, `06 27`, `0627`, `6-27`, `08-01C`, `#08-01 c`. Floor and room numbers may be 1–2 digits → zero-pad. 3-digit blobs like `627` are rejected as ambiguous.

Error cases (all with friendly messages that include the expected format, e.g. "e.g. #06-27"):
- unparseable text → invalid format
- floor not in FLOOR_ROOMS → "Noctua rooms are on floors 06–08"
- room number not on that floor
- letter given for a non-suite room → "…#06-27 has no units — just send #06-27"
- letter outside A–F for a suite room → list valid letters
- suite room without letter → NOT an error: `needs_letter=True`, `base` set

## Database (`bot/db.py`)

Single module-level `sqlite3` connection (`check_same_thread=False`), WAL mode, `Row` factory, idempotent `CREATE TABLE IF NOT EXISTS` schema, sync calls (fine at 120 users on one event loop).

```sql
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  username TEXT,               -- telegram handle without @, nullable, refreshed on interactions
  name TEXT NOT NULL,          -- self-reported display name
  room TEXT NOT NULL,          -- normalized "#06-27" / "#08-01C"
  registered_at TEXT NOT NULL  -- UTC ISO
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine TEXT NOT NULL,       -- w1/w2/d1/d2
  user_id INTEGER NOT NULL REFERENCES users(user_id),
  started_at TEXT NOT NULL,    -- UTC ISO
  duration_min INTEGER NOT NULL, -- total incl. extensions
  ends_at TEXT NOT NULL,       -- UTC ISO
  status TEXT NOT NULL DEFAULT 'active',  -- active|done|collected|cancelled
  done_notified INTEGER NOT NULL DEFAULT 0,
  reminder_sent INTEGER NOT NULL DEFAULT 0,
  last_ping_at TEXT            -- UTC ISO, throttles ping button
);
```

Suggested functions (Opus may adjust signatures, keep coherent): `init_db()`, `upsert_user`, `get_user`, `update_user_fields`, `touch_username(user_id, username)`, `all_user_ids()`, `count_users()`, `get_active(machine)`, `get_latest(machine)` (newest session any status, joined with user name/room/username), `start_session(machine, user_id, minutes)` (atomically: fail→None if an active exists; auto-collect a latest `done` session), `extend_session(id, minutes)`, `mark_done(id)`, `mark_collected(id)`, `cancel_session(id)`, `set_last_ping(id)`, `get_session(id)` (joined), `active_sessions()` (for job restore).

## UX flows

### Onboarding (`/start`, ConversationHandler)

Unregistered: "🦉 Welcome to <b>Noctua Bot</b>! Let's get you set up — takes 20 seconds. What's your <b>name</b>?"
→ name (strip, collapse spaces, 1–40 chars, re-ask on empty/too long)
→ "Hi {name}! What's your <b>room</b>? (e.g. #06-27)"
→ `rooms.validate_room`; on error: show the friendly error, re-ask; on `needs_letter`: "Room #08-01 has units A–F — tap yours:" inline buttons `[A][B][C] / [D][E][F]`
→ confirm: "Name: <b>{name}</b>\nRoom: <b>{room}</b>\nAll correct?" [✅ All good] [🔄 Start over]
→ save, "You're set! 🎉", show main menu reply keyboard.

Registered `/start`: greet + show menu. `/cancel` works inside all conversations.

**Unregistered gate**: any other command/button/callback → "Please register first — tap /start". For callbacks, also `answer()` the query.

**Username refresh**: keep `users.username` current (e.g. a `TypeHandler` in group -1 that calls `touch_username` for registered users, or refresh at each main interaction — Opus's choice).

### Main menu (persistent `ReplyKeyboardMarkup`, `resize_keyboard=True`)

```
[🧺 Use a machine] [📊 Status]
[👤 My profile]    [❓ Help]
[📢 Broadcast]                  ← this row ONLY for leaders
```

Text handlers match these exact labels. `/status`, `/profile`, `/help`, `/broadcast` commands do the same things.

### Use a machine

"🧺 Use a machine" → inline keyboard, one machine per row, live state suffix:
`🫧 Washer 1 · 🟢 free` / `· 🔴 ~18 min left` / `· 🟡 done, uncollected` — plus a last row [📊 Status].

Tap a machine → machine view (edit same message), state-dependent:
- **Free** (or never used / cancelled / collected): "🫧 <b>Washer 1</b> is free 🟢\nChoose your cycle — tap when your stuff is in and paid:" + duration buttons (`30 min` for washers; `30 min / 45 min / 60 min` for dryers) + [⬅️ Back]
- **Someone else's, done-uncollected**: same duration buttons, plus warning line "🟡 {Name}'s load finished {X} min ago and is still inside." + [🔔 Ping {Name}] + [⬅️ Back]
- **Someone else's, active**: "🔴 In use by {Name} ({room}) — ~{X} min left (est. done {time})." + [⬅️ Back] (no durations)
- **Own active**: "🌀 Your cycle — ~{X} min left (est. done {time}).\n💸 Paid twice? Tap a duration to add time." + duration buttons (as extension) + [🛑 Stop my timer] + [⬅️ Back]
- **Own done-uncollected**: "⏰ Your load is done — please collect!" + [✅ Collected] + duration buttons (start a new cycle) + [⬅️ Back]

Tap a duration:
- Machine free → start session, schedule done job, edit to: "🫧 <b>Washer 1 started</b> · 30 min\nEst. done <b>3:05 PM</b> — I'll ping you (times are estimates).\n\n💸 Paid twice? Tap a duration below to add time." + duration buttons (now meaning extend) + [📊 Status]
- Own session active on that machine → **extension confirm dialog** (see Machines rules). On ✅: "Extended! Washer 1 · total 60 min · est. done <b>3:35 PM</b>." On ❌: back to machine view.
- Race (someone grabbed it between render and tap) → `answer(show_alert=True)`: "Oops — Washer 1 was just taken."

[🛑 Stop my timer] → confirm [Yes, stop] [No] → `cancelled`, jobs cancelled, machine free.

### Status (`📊 Status` / `/status`)

One message, all four machines, e.g.:

```
🧺 Noctua Laundry — Status

🫧 Washer 1 — 🟢 Free
    Last: Alice (#06-27) · finished 2:14 PM

🫧 Washer 2 — 🔴 In use — Bob (#08-01C) @bob
    ~18 min left · est. done 3:05 PM

💨 Dryer 1 — 🟡 Done, not collected — Carol (#07-11A) @carol
    finished 12 min ago (2:50 PM)

💨 Dryer 2 — 🟢 Free · not used yet

Updated 3:02 PM · times are estimates
```

- Handle display: `@username` if set, else an HTML mention link `<a href="tg://user?id=...">Name</a>` (helper in `util.py`).
- Inline buttons: [🔄 Refresh] (edit in place; catch and ignore "message is not modified") + one [🔔 Ping <machine>] row per 🟡 machine + [🧺 Use a machine].
- 🟢 Free lines show last user name+room+finish time (that's the "last used" requirement); 🔴/🟡 lines include the handle.

### Ping previous user

Any registered user taps [🔔 Ping …] on a done-uncollected machine:
- Throttle: if `last_ping_at` within `PING_COOLDOWN_MIN` → `answer(show_alert=True)`: "Already pinged {X} min ago — you can PM them: @handle".
- Else DM the owner: "🔔 <b>Ping!</b> Someone needs <b>Dryer 1</b> — your laundry finished {X} min ago. Please collect it 🙏" + [✅ Collected] button.
- Ack to the pinger (alert or message): "✅ Pinged Carol (#07-11A). You can also PM them: @carol" (mention link if no handle).
- If DM fails (owner blocked the bot): tell the pinger, still give the handle.

### Timer jobs (`bot/jobs.py`)

- On session start: `job_queue.run_once(done_job, when=ends_at_utc, name=f"done:{session_id}", data=session_id)`.
- `done_job`: if session still `active`: mark `done`, set `done_notified`, DM owner "⏰ <b>Washer 1 is done!</b> (30 min cycle, {time}). Please collect your laundry 🙏" + [✅ Collected]; schedule `rem:{session_id}` at `+REMINDER_AFTER_MIN` min.
- `reminder_job`: if still not collected: DM "🧺 Reminder — your laundry is still in Washer 1 (done {X} min ago)!" + [✅ Collected], set `reminder_sent`.
- [✅ Collected] → mark collected, cancel `rem:` job, edit the tapped message to "✅ Collected — thanks!"
- Extension: cancel+reschedule `done:` job, cancel pending `rem:`.
- **Restore on startup** (`post_init`): `init_db()`, for every `active` session schedule its done job (overdue → run ~now), `set_my_commands`.

### Broadcast (leaders only)

`📢 Broadcast` / `/broadcast` → `config.is_leader(update.effective_user.username)` else "🔒 Leaders only." ConversationHandler:
1. "📢 Send me the announcement now — text, photo, or video (one message). /cancel to abort."
2. Preview: text → send back formatted `📢 <b>Noctua Announcement</b>\n\n{escaped text}`; media → `copy_message` back as-is. Then "Send this to <b>{N}</b> registered residents?" [✅ Send] [❌ Cancel]
3. Send loop over `all_user_ids()`: text → `send_message` with the formatted template; media → `copy_message`. `await asyncio.sleep(0.05)` between sends; catch `Forbidden`/`BadRequest` per user and count failures. Skip sending to the leader themself is NOT needed (include everyone).
4. Report: "📤 Sent to 117/120. 3 unreachable (blocked/never started the bot)."

### Help (`❓ Help` / `/help`)

Short guide: what the bot does, machine flow (put stuff in → tap machine → tap duration; paid twice → tap again), status/ping etiquette ("collect promptly 🙏"), leaders' broadcast note, /profile to fix name/room.

### `/profile` (`👤 My profile`)

"👤 <b>{name}</b> · {room}" + [✏️ Change name] [✏️ Change room] — short conversations reusing the same validation as onboarding.

### Misc, quality

- `set_my_commands` in `post_init`: start, status, profile, help, cancel, broadcast ("Leaders: send an announcement").
- Global error handler: log the traceback; on callback queries try a generic `answer("Something went wrong 😵 — try again")`.
- Always `await query.answer()` on every callback path (avoid endless spinners).
- Logging: `logging.basicConfig(level=INFO)`, silence `httpx` to WARNING.
- PTB `ConversationHandler` "per_message" warnings are acceptable — don't contort the design to silence them.
- `main.py`: `build_application() -> Application` (Defaults HTML, token from config — build must work with any non-empty token string so a dummy token can be used for an offline smoke test) and `main()` (validate token with friendly exit message, `run_polling(allowed_updates=Update.ALL_TYPES)`). Entry: `python -m bot.main`.

## Callback data grammar

Opus owns this. Keep prefixes short, consistent between `keyboards.py` and handlers, and documented in one place (module docstring in `keyboards.py`). Suggested shape: `menu`, `m:{id}`, `dur:{id}:{min}`, `extc:{id}:{min}` (extend confirm), `col:{sid}`, `ping:{id}`, `stop:{id}`, `st:refresh`, registration/broadcast confirms, `noop`.

## Copy & tone

Friendly, concise, light emoji (🦉 is the mascot). Residents see "Noctua Bot". Never blame users; timers are "estimates" (washers aren't precise). All copy in English.

## requirements.txt (exact)

```
python-telegram-bot[job-queue]~=21.9
python-dotenv~=1.0
```

## .env.example (exact)

```
# Telegram bot token from @BotFather (required)
BOT_TOKEN=

# Comma-separated Telegram usernames (without @) allowed to /broadcast
LEADER_USERNAMES=

# Optional overrides
# TIMEZONE=Asia/Singapore
# DB_PATH=/full/path/to/noctua.db
```

## Tests (`tests/test_rooms.py`)

Stdlib-only, runnable as `python3 -m tests.test_rooms` (assert-based, print a PASS summary; no pytest). Cover at least: `#06-27`, `06-27`, `06 27`, `0627`, `6-27` → `#06-27`; `08-01C`, `#08-01 c`, `0801f` → normalized with letter; `08-01`/`8-1` → `needs_letter` with `base="08-01"`; `09-01`, `06-28`, `06-00`, `627`, `hello`, empty → errors; `06-27A` → letter-not-allowed error; `08-01G` → invalid letter; `07-11b` → `#07-11B`; `suite_letters` returns A–F.

## README.md outline

Audience: semi-technical dorm leader on macOS (later maybe a VPS). Sections: What it does (feature tour incl. leader broadcast) · Create the bot with @BotFather (/newbot, name "Noctua Bot", username ending in `bot`, copy token) · Setup (`python3.11 -m venv .venv`, `source .venv/bin/activate`, `pip install -r requirements.txt`, `cp .env.example .env`, fill token + leaders) · Run (`python -m bot.main`) & keep it running (nohup/screen note, VPS suggestion) · Configuration (leaders in `.env`, rooms in `bot/rooms.py`, machines/timings in `bot/config.py`, timezone) · Data (`noctua.db`, back it up) · Troubleshooting (token invalid, "Conflict: terminated by other getUpdates" = two instances running).

## Style

PEP 8, type hints on public functions, f-strings, no over-engineering, comments only for non-obvious constraints. Match this spec's naming. Verify your own files with `python3 -m py_compile <file>` (do NOT try to run the bot or install anything — third-party deps may not be installed yet while you work).

---

# v1.1 changes (2026-07-31, user feedback)

The bot is now live as **@rc4noctuabot**. Three changes below. Keep everything else (timers, sessions, extension, collected, status board internals, jobs, onboarding validation) as-is unless a change below requires touching it.

## 1. Menu restructure — general-purpose bot, intuitive short labels

This is a dorm-wide bot that will grow beyond laundry, so top-level labels must say *which* feature they belong to, stay short, and use sectioning (inline hubs) rather than long captions.

New persistent reply keyboard:

```
[🧺 Laundry]
[👤 Profile] [❓ Help]
[📢 Announce]        ← leaders only
```

`🧺 Laundry` opens the **laundry hub** (inline, one message):

```
🧺 Laundry — what do you need?
[▶️ Start a machine]
[📊 Machine status]
[🔔 Ping a machine]
```

- `▶️ Start a machine` → the existing machine list (unchanged flows beneath).
- `📊 Machine status` → the existing status board. Keep `/status` command working. The status board's bottom button row should link back to the hub (`⬅️ Laundry`) instead of directly to the machine list.
- `🔔 Ping a machine` → **new ping picker**: lists each machine whose latest session is done-uncollected as a button `🔔 Dryer 1 · Carol` (shortened name), reusing the existing `ping:<mid>` callback; if none, show "Nothing waiting to be collected 🎉" (edit in place, with `⬅️ Laundry` back button). This makes ping a first-class user option rather than something you stumble on.
- Machine list's `⬅️ Back`/bottom row routes to the hub; machine views keep `⬅️ Back` → machine list.
- Callback for hub: `hub` (add to grammar). Old callbacks keep working.
- Rename reply labels: `👤 Profile`, `❓ Help`, `📢 Announce` (update RX_ constants + /help text + registered-greeting copy accordingly). Commands unchanged (`/broadcast` stays, alias of Announce).

## 2. Broadcast → multi-message composer (leaders)

Replace the single-message flow with a draft composer:

- Entry (`📢 Announce` / `/broadcast`, leaders only): explain the composer: "Send me the announcement — as many messages as you like (text, photos, videos, files). You can keep editing a sent message in this chat until you hit Send; edits are included. When you're ready, hit ✅ Send."
- Every non-command message from the leader while composing is appended to the draft: store `(message_id)` list in `user_data` (chat is the leader's own DM). After each append, the bot posts/refreshes a short **composer status** message at the bottom: "📝 Draft: 3 message(s)" + buttons `[✅ Send to N residents] [👀 Preview] [↩️ Undo last] [❌ Cancel]`. Delete the previous composer status message first so the buttons stay at the bottom (best-effort; ignore delete failures).
- `👀 Preview`: send the header + copy the whole chain back to the leader, then re-post the composer status.
- `↩️ Undo last`: pop the last draft id, refresh composer status ("Draft: 2 message(s)"; if 0 → "Draft is empty — send me something or ❌ Cancel").
- `✅ Send`: for each registered user: send header `📢 <b>Noctua Announcement</b>` then `copy_message` each draft id **in order** (copies happen at send time, so in-chat edits made before sending are naturally included). Per-user: if the header send fails → count user unreachable, skip their chain. Individual draft copies that fail (e.g. leader deleted that message) are skipped silently for everyone (log once). Keep `asyncio.sleep(0.05)` between every API send. Report `📤 Sent to X/N residents. …`.
- `❌ Cancel` / `/cancel`: clear draft.
- Empty-draft Send → alert "Draft is empty".
- Keep `allow_reentry=True`; re-entering resets the draft.

## 3. Roster whitelist (Excel sheet of residents)

Only people on a leader-provided roster may use the bot. The user will supply an `.xlsx` with columns for name, room and telegram tag; import happens on the server via CLI, not through Telegram.

- New table:
  ```sql
  CREATE TABLE IF NOT EXISTS roster (
    handle TEXT PRIMARY KEY,   -- telegram username, lowercase, no @
    name TEXT NOT NULL,
    room TEXT NOT NULL         -- normalized "#06-27" form
  );
  ```
  db functions: `roster_count()`, `roster_lookup(handle) -> Row | None`, `roster_replace(rows: list[tuple[handle, name, room]])` (wipe + insert in one tx).
- **Enforcement — only when the roster is non-empty** (empty roster = open registration, current behaviour, so nothing breaks before the sheet arrives):
  - In the `/start` onboarding entry: user has no `@username` → "This bot matches residents by Telegram username. Add a username in Telegram Settings → Username, then tap /start again." · username not in roster → "🚫 This bot is for Noctua residents. If you live here, ask a dorm leader to add your handle (@X) to the resident list." (Do NOT end with a dangling state; conversation ends.)
  - On a roster hit, skip the name/room questions: "🦉 Found you on the resident list:\nName: <b>{name}</b>\nRoom: <b>{room}</b>\nAll correct?" `[✅ That's me]` `[✏️ Fix name]` — Fix name asks only the name (room always comes from the roster). Save with `db.upsert_user`.
  - Already-registered users are **not** retroactively kicked when a roster is imported (their timers/history stay); note this in README later.
  - The group -1 gate stays as-is (it only blocks unregistered users).
- **Import CLI** — new file `bot/roster_import.py`, run as `python -m bot.roster_import <path.xlsx>`:
  - Uses `openpyxl` (already installed in the venv; requirements.txt update happens separately — do not edit requirements.txt).
  - Reads the first worksheet. Header row detection: first row containing a cell matching (case-insensitive substring) `name`, one matching `room`, and one matching `tele|tag|handle|username`. Data rows below it.
  - Handle: strip `@`, whitespace, lowercase; skip + report rows with an empty handle.
  - Room: run through `rooms.validate_room`; `needs_letter` or error → the row is **reported as invalid and skipped** (suite rooms need their letter in the sheet).
  - Name: `util.clean_name`-style tidy (but allow up to 60 chars here).
  - Duplicate handles: last row wins, report it.
  - Prints a summary: imported N, skipped rows with reasons, then `roster_replace`. Exit code 1 if zero valid rows.
  - Must run fine while the bot is running (WAL) and must not import telegram (no PTB dependency — db/config/rooms/util only; guard: `util` imports nothing from telegram either — check).
- `/start` for an already-registered user: unchanged greeting + new menu.

## v1.1 callback grammar additions (superseded in part by v1.2)

`hub` (laundry hub) · `pingpick` (ping picker) · `bc:prev` / `bc:undo` (composer) — reuse `bc:send` / `bc:cancel`. Update keyboards.py docstring table.


---

# v1.2 changes (2026-07-31, user feedback) — BINARY MACHINE STATE

Residents should never have to tell the bot they took their clothes out. A machine is either **🟢 Free** or **🔴 Running** — nothing else. If a machine is free but still has someone's load sitting in it, the "last used by / when" line is what tells you whose it is, and the ping button is how you nudge them. Plus a leader-only "reset everything to green" escape hatch.

## 1. Kill the collected state

- **Delete** the `✅ Collected` button everywhere (machine view, done DM, reminder DM), `col:<sid>` from the grammar, `cb_collected`, `collected_keyboard`, `db.mark_collected`, and the `🟡` state from every render.
- **Delete the auto-reminder entirely**: `reminder_job`, `schedule_reminder`, `cancel_reminder`, the `rem:` job name, `db.sessions_awaiting_reminder`, `config.REMINDER_AFTER_MIN`, and the reminder half of `restore_jobs`. (With no collected signal the bot cannot know whether a nag is warranted; nagging everyone is worse than not nagging.) `sessions.reminder_sent` / `done_notified` columns stay (harmless, schema untouched).
- `done_job` keeps its purpose: when the timer ends, mark the session `done` and DM the owner — text becomes: `⏰ <b>Washer 1 is done!</b> ({N} min cycle, {time}). Grab your laundry when you can 🙏\n\nThe machine now shows as free — someone else may move your load if you leave it.` No buttons on that DM.
- `db.start_session` no longer "displaces" anything: it fails only if an **active** session exists on that machine. Remove `StartResult.displaced`, the ♻️ copy, and the auto-collect. (`StartResult` may collapse back to returning `Row | None`.)
- Legacy rows with status `collected` behave exactly like `done` (free machine, valid "last used"). Nothing to migrate.

## 2. Two-state rendering

**Machine free** = no `active` session. **Machine running** = an `active` session exists.

Machine list buttons: `🫧 Washer 1 · 🟢 free` / `· 🔴 ~18 min left`.

Machine view, only two shapes now:
- **Free**: `🫧 <b>Washer 1</b> — 🟢 Free` + (when a last session exists) `Last used by {Name} ({room}) · finished {clock} ({X} min ago)` + a line `Someone's load still inside? Tap 🔔 below to nudge them.` + duration buttons + `[🔔 Ping {Name}]` (only when a last user exists and it isn't you) + `[⬅️ Back]`.
- **Running, someone else**: `🔴 In use by {Name} ({room}) — ~{X} min left (est. done {clock}).` + `[⬅️ Back]`.
- **Running, yours**: `🌀 Your cycle — ~{X} min left (est. done {clock}).` + `💸 Paid twice? Tap a duration to add time.` + duration buttons (extend) + `[🛑 Stop my timer]` + `[⬅️ Back]`.

Status board — every machine is one of two lines:
```
🫧 Washer 1 — 🟢 Free
    Last: Alice (#06-27) @alice · finished 2:14 PM (23 min ago)

🫧 Washer 2 — 🔴 In use — Bob (#08-01C) @bob
    ~18 min left · est. done 3:05 PM

💨 Dryer 1 — 🟢 Free · not used yet
```
Free-with-history lines now DO include the handle (that's the point — you may need to PM them). Keep `Updated {clock} · times are estimates`. Buttons: `[🔄 Refresh]`, one `[🔔 Ping <machine>]` row per free machine that has a last user (max 4), `[⬅️ Laundry]`.

## 3. Ping targets the last user

`ping:<mid>` now works whenever the machine's **latest session** has a user and that user isn't the tapper — regardless of status (`done`, `cancelled`, legacy `collected`). Running machines: reject with `That machine is still running 🔴`.
- Same `PING_COOLDOWN_MIN` throttle on `sessions.last_ping_at`, same "already pinged X min ago — PM them: @handle" alert, same handle reveal in the success alert, same blocked-user fallback.
- DM text: `🔔 <b>Ping!</b> Someone needs <b>Dryer 1</b> — your laundry finished {X} min ago. Please clear it when you can 🙏` (no buttons).
- **Ping picker** (`pingpick`) lists every machine that is free AND has a last user, captioned `🔔 Dryer 1 · Carol · 12 min ago` (shorten the name). Machines never used, currently running, or last used by the tapper are omitted. Empty → `Nothing to nudge — every machine is free or running 🎉`.

## 4. Leader-only reset

New capability, gated exactly like broadcast (`config.is_leader(username)`):
- Button `🔄 Reset all machines` appended to the **laundry hub** keyboard, rendered ONLY for leaders (`laundry_hub_keyboard(is_leader: bool)`; `render_hub(is_leader)`), plus command `/resetmachines`.
- Tap → confirm dialog: `🔄 <b>Reset all machines?</b>\nThis ends every running timer and sets all four machines to 🟢 free. Residents keep their registrations; the "last used" history stays.` `[✅ Yes, reset]` `[❌ No]` (No → back to hub).
- Confirm → `db.reset_machines()`: mark every `active` session `cancelled`, return their ids; handler cancels each one's done job. Then edit to `✅ All machines reset — everything shows 🟢 free.` + `[⬅️ Laundry]`, and log `INFO` who did it.
- Non-leader who somehow taps it: `answer("🔒 Leaders only.", show_alert=True)`.
- Grammar: `reset` (ask) and `resetok` (confirm).

## 5. Copy updates

- `/help`: replace the collect/etiquette wording. New etiquette section: `Machines are just 🟢 free or 🔴 running. When your timer ends the machine shows free again, so clear your load quickly 🙏 — if you find a load sitting in a free machine, check 📊 <b>Machine status</b> to see whose it is and 🔔 nudge them (or just move it aside).` Ping section: nudging the last user of a free machine. Leaders' line mentions `🔄 Reset all machines`.
- Hub text unchanged; hub keyboard grows the leader row.
- `main.py` `set_my_commands`: keep the existing list (do not advertise `/resetmachines`).

## v1.2 grammar

Added: `reset`, `resetok`. Removed: `col:<sid>`. Everything else unchanged. Update the `keyboards.py` docstring table.

## v1.2 AMENDMENT (same session, user clarification) — early "Collected", auto-green on timer end

Supersedes the parts of v1.2 §1–§2 that conflict.

**A. The owner's running-machine button becomes an early-finish "Collected".**
Residents never confirm collection *after* the timer ends — the machine auto-frees. But if they finish/collect **early**, they can free the machine immediately.

- In the machine view for the owner's own ACTIVE session, replace `[🛑 Stop my timer]` with `[✅ Collected — free it now]` (callback stays `stop:<sid>`; `stopc:<sid>` confirms).
- Confirm dialog text: `✅ <b>Free up Washer 1 now?</b>\nYour timer stops and the machine shows 🟢 free straight away.` Buttons `[✅ Yes, I've taken it out]` `[❌ No]`.
- On confirm call **`db.finish_early(session_id)`** (replaces the old `cancel_session` call here): sets `status='done'` AND `ends_at = now` where `status='active'`, so every "finished {clock} ({X} min ago)" renderer keeps working off `ends_at` with no special cases. Cancel the session's done job. Result message: `✅ <b>Washer 1</b> is free again — thanks for clearing it! 🙏` + `[⬅️ Back]`.
- Keep the same ownership guard (only the session's owner may do it) and the stale-session alert.
- `db.cancel_session` is no longer used by the laundry flow (reset uses its own path); it may stay or go.

**B. No "stopped early" state in any render.** A finished-early session is just `done`. Legacy `cancelled` (and `collected`) rows must still render as a normal free machine with a valid last user (`finished {clock}`), never as a special case.

**C. Last-used info is mandatory on every free machine**, in both the status board and the machine view, and must carry all three facts: **who** (name + room), **their handle**, and **when** (`finished {clock} ({X} min ago)`). This is separate from the board's `Updated {clock}` footer, which stays. Machines never used show `🟢 Free · not used yet`.
