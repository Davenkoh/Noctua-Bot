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

## 3. Roster whitelist (Excel sheet of residents) — import CLI superseded in v1.3

> **v1.3 (2026-08-04):** the Excel import never happened; the resident list arrived as
> text instead. `bot/roster_import.py`, `bot/roster_add.py` and `bot/roster_fix.py` are
> replaced by a single source of truth, `roster/master.txt`, loaded by
> `bot/roster_sync.py`. Everything below about *enforcement* still holds exactly (the
> `/start` gate, the db contract, open registration on an empty roster); only the way
> rows get into the roster table changed. See "v1.3 changes" at the end of this file.

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

# v1.3 changes (2026-08-04) — one source of truth for residents

Supersedes the import CLI in "v1.1 §3 Roster whitelist". Enforcement is unchanged:
`/start` still gates on `db.roster_count() > 0`, an empty roster still means open
registration, and residents who registered earlier still keep access.

## 1. `roster/master.txt` is the resident list

One line per room, `|`-separated, `;` comments a whole line:

```
#06-27   | TAN XIAO MING, ALICE  | Alice   | @alicetan81 | leader
#08-21   | NG WEI                | Wei     |             | no handle yet
```

- **room** — the key; suite rooms need their unit letter (`#08-01C`).
- **full name** — as the dorm records it. For people; never stored.
- **display name** — what the bot shows. First name, plus surname whenever two
  residents share one (Chloe Oon / Chloe Ong / Chloe Ng). **Must be unique**: two
  residents rendered identically cannot be told apart in a laundry queue.
- **handle** — what registration matches on. Blank = cannot register yet.
- **note** — free text for humans (exchange, leader, open questions). Never stored.
  Leaders are configured by `LEADER_USERNAMES` in `.env`, so a leadership change
  means editing both, by design: the file is data, `.env` is config.

Real file is gitignored (resident details); `roster/master.example.txt` is the
committed fake sample.

## 2. `bot/roster_sync.py` loads it

```
python -m bot.roster_sync            # plan, writes nothing
python -m bot.roster_sync --apply    # make the db match the file
python -m bot.roster_sync --show     # print the roster as stored
```

Full reconciliation against the roster table: adds new handles, updates changed
names/rooms, **removes handles no longer in the file**, one transaction. Reuses
`db.roster_apply(removals, upserts)` (removals first), so a room changing hands is
never left with both handles whitelisted.

Refusals, both of which would otherwise open registration to everyone:
- a file with **any** malformed line writes nothing at all;
- a file listing **nobody** is rejected rather than wiping the roster.

Duplicate room, duplicate handle, or duplicate display name are all errors.

## 3. Removed

`bot/roster_import.py`, `bot/roster_add.py`, `bot/roster_fix.py`,
`tests/test_roster.py`, `tests/test_roster_fix.py`, `roster/corrections.example.txt`,
the `openpyxl` dependency, and `deploy/push.sh --with-db` (it uploaded the Mac's
`noctua.db` over the server's live one; the database is server-side state and the
rsync already excludes it). Db-level coverage moved into `tests/test_roster_sync.py`.

# v1.4 changes (2026-08-06) — "count me in" polls

Leaders can ask the house who's in, and everyone sees the same list of names.

Not Telegram's native poll. A native poll tallies per message, and each
resident's DM is a different message, so 120 DMs would be 120 unrelated polls
with no combined result. The bot owns the tally instead.

## 1. Schema

```sql
polls (id, question, created_by, created_at, closed_at)
poll_recipients (poll_id, user_id, chat_id, message_id, answer, answered_at,
                 PRIMARY KEY (poll_id, user_id))
```

One `poll_recipients` row per delivered card. `answer` is NULL until they
choose, which is what makes "who hasn't replied" a query rather than a guess.

## 2. Flow

`/poll` or the 📋 Poll button (leader tier, same as `/announce`) → type the
question → confirm → fan out to every registered resident, one card each.

Resident card, names only and deliberately never rooms:

```
📋 <question>

✅ In (2):
Jeshua, Daven

❌ Can't (0):
nobody yet

[✅ I'm in] [❌ Can't]
[🔄 Refresh]
```

The creator additionally gets a summary card carrying the same tally plus
**who has not answered, with rooms**. Rooms appear there and nowhere else,
because that message only ever goes to the leader who made the poll.

## 3. Refresh, not live sync

Keeping N cards current would mean N `editMessageText` calls per tap, which
the rate limit will not carry at 120 residents. So a tap re-renders only the
tapper's own card; everyone else pulls with 🔄 Refresh. `poll_recipients`
stores `chat_id`/`message_id` so any card can be re-rendered on demand.

Refresh distinguishes the two message kinds by `message_id`, not by who
tapped: the creator normally holds both a card and the summary.

## 4. Rules

- `set_poll_answer` returns False when the user is not a recipient, so a
  forwarded card cannot be used to vote by someone the poll never reached.
- Re-tapping overwrites, so changing your mind moves you rather than
  double-counting. The button shows which side you are currently on.
- `POLL_TEST_HANDLES` in `.env` narrows the audience to a few handles for a
  dry run. Empty (the normal state) means every registered resident.
- Name lists are truncated at 60 with "and N more" so a full house cannot
  push a card past Telegram's 4096-character limit.

## 5. util.esc now escapes with quote=False

Escaped text only ever lands in a message body, never an HTML attribute (the
one `href` we build takes an int user id). With the default `quote=True` a
leader's "who's in?" came back as "who&#x27;s in?".

# v1.5 changes (2026-08-08) — the status board becomes the laundry home

Two screens answered one question between them. A resident opening laundry
wants "is anything free" and "can I start mine", and the menu answered only
the second, so seeing the first cost an extra tap and a second screen to
maintain. They are now one screen.

## 1. The home

```
🧺 Noctua Laundry
Tap your machine once your stuff is in and paid.

🫧 Washer 1: 🔴 In use by Lydia (#08-01C) @LydiaChien
    ~9 min left · est. done 4:26 PM

🫧 Washer 2: 🟢 Free
    Last: Kai Jin (#07-25) @KaiJin_11 · finished 3:40 PM (36 min ago)
...
Last updated 4:17 PM

[🫧 Washer 1 🔴]
[🫧 Washer 2 🟢] [🔔 Nudge Kai Jin]
[💨 Dryer 1 🟢] [🔔 Nudge Calvin]
[🔄 Refresh]
[🔄 Reset all machines]        ← admins only
```

The instruction sits under the title, not the foot: it explains the buttons,
so it has to be read before them. The footer is just `Last updated {clock}`;
the old "times are estimates" note is gone.

Nudge sits beside its machine and names the person, not the machine, since
the row already says which machine. A **running** machine has no nudge
button: its owner's load is not finished, so there is nothing to nudge about.

## 2. What went

`render_machine_list`, `render_status`, `machine_list_keyboard`,
`status_keyboard`, `laundry_hub_keyboard`, `HUB_START`, `HUB_STATUS`,
`_BTN_STATUS`, `_suffix`, `LIST_TEXT`, and `machine_view_keyboard(status=)`.

That last one mattered: `started_keyboard` passed `status=True`, so after the
merge the cycle-confirmation screen carried both 📊 Machine status and
⬅️ Laundry menu, which now render the same thing.

`/status` and the `st` callback survive as aliases of the home. Older
messages in residents' chats still carry that button and a dead end there
would be worse than a redirect. Same for `menu` and `hub`.

`render_nudge_picker` / `/ping` / `pingpick` stay: the picker is still how
you nudge without opening the home.

## 3. Rollout

Shipped behind `LAUNDRY_PREVIEW_HANDLES`, a canary env var that gave the new
home to listed handles and the old menu to everyone else, then cleared. The
variable and `config.is_laundry_preview` are removed; the pattern is worth
reusing (see `POLL_TEST_HANDLES`) rather than the specific flag.

Residents needed no action. Inline keyboards are rebuilt per message, so the
next tap after the deploy showed the new screen. Only the *reply* keyboard is
cached client-side, which is why the 📋 Poll button in v1.4 needed a /start.

---

# v1.6 changes (2026-08-31): scheduled announcements

`📢 Announce` could only fire immediately, so a notice written at midnight
either went out at midnight or waited for somebody to remember it at nine.
The composer now has a second exit.

## 1. Two doors, one composer

The leaders' menu grows a second announcement button, and Poll moves down a
row rather than sharing one with either of them.

```
[Laundry menu]
[Profile] [Help]
[Announce] [Scheduled Announce]   <- leaders only
[Poll]                            <- leaders only
```

`Scheduled Announce` (`/schedule`) is not a second flow. It is the same
composer with `MODE_KEY` set, which changes two things: the intro says a time
will be asked for, and `composer_keyboard(schedule_first=True)` puts the timed
exit on top. "Send now" survives the reorder in both directions, because
changing your mind about *when* is not a reason to rewrite *what*.

```
Announce                        Scheduled Announce
   [Send to 118 now]               [Send it later]
   [Send it later]                 [Send to 118 now]
   [Preview] [Undo last]           [Preview] [Undo last]
   [Cancel]                        [Cancel]
```

Both land on the same "when?" step, which answers the question three ways
because they suit different answers. The presets cover the times a house
notice actually goes out; the picker handles any other moment without typing;
typing stays for whoever finds "fri 6:30pm" faster than four taps.

```
When should this go out?
Tap a time, pick a date below, or type one:
   tomorrow 9am / fri 6:30pm / 1 sep 0900 / in 90 minutes
[In 1 hour (3:05 PM)]  [In 3 hours (5:05 PM)]
[Tonight, 8:00 PM]     [Tomorrow, 9:00 AM]
[Pick a date and time]
[Back to the draft]
[Cancel]
```

Every route ends on the same confirmation card. It is the only defence
against a misread time, so it always spells out the weekday and the date even
for today, and nothing is written to the database until it is tapped.

```
Send this later?
  Tomorrow (Tue 1 Sep), 9:00 AM
  in about 19 hours
  2 message(s) in the draft.
[Schedule it]
[Different time] [Cancel]
```

`SCHEDULING` is a real conversation state, not a flag: it swaps the draft
collector for the time reader, so a message that arrives while the bot is
asking for a time is read as a time and never appended to an announcement the
leader has already finished writing.

## 1b. The date and time picker

Three grids, no typing, modelled on Telegram's own scheduler: a month, then
an hour, then the minutes.

```
[.] [September 2026] [>]        Fri 4 Sep                 Fri 4 Sep, 18:00
[Mo][Tu][We][Th][Fr][Sa][Su]    [00][01][02][03][04][05]  [:00][:05][:10][:15]
[ .][ 1][ 2][ 3][ 4][ 5][ 6]    [06][07][08][09][10][11]  [:20][:25][:30][:35]
...                             [12][13][14][15][16][17]  [:40][:45][:50][:55]
[Back to the times]             [18][19][20][21][22][23]  [Back to the hours]
[Cancel]                        [Back to the calendar]    [Cancel]
```

Each step encodes its whole selection in the next step's callback data
(`bc:d:20260904` then `bc:h:20260904:18` then `bc:m:20260904:1830`), so the
picker holds no state of its own and a stale keyboard cannot half-apply.

Everything already past is drawn as an inert cell rather than offered and then
refused: days before today, hours gone by, minutes inside this one. The month
arrows stop at the ends of the range, so every page the leader can reach has a
day they can actually tap. `noop` therefore had to become a registered
handler; an unanswered callback spins in the client until it times out, which
reads as the bot having crashed.

The hour grid is 24-hour, which halves the rows. That is only safe because
`_take()` funnels every route (typed, preset, picked) into the same
confirmation card, and the card reads the choice back as `6:30 PM`. Nobody
commits to `18` without seeing `PM` first. `_take` is also where a slot that
went stale between drawing the grid and tapping it is caught.

Minutes step in fives. Anyone who genuinely wants 9:07 types it.

## 2. `bot/when.py`

A small grammar, not a date library. It takes the current local time and
returns a local one, so callers convert at the database edge like everything
else. Accepts a clock (`18:30`, `6:30pm`, `6.30pm`, `6pm`, `1830`), a day word
(`today`, `tonight`, `tomorrow`, `tmr`), a weekday, a date (`1 sep`, `sep 1`,
`1/9`, `2026-09-01`), and `in 90 minutes` / `in 2h` / `in 3 days`.

Two guesses, both safe only because of the confirm card:

- a bare clock means the next time it reads, so `9am` at lunchtime is tomorrow;
- a weekday always means the coming one, so `mon` on a Monday is in seven days.

It refuses rather than guesses. A day with no clock (`tomorrow`) is ambiguous
between breakfast and midnight, and a leftover word it did not understand
(`banana 9am`) means the word carrying the meaning may have been the one
dropped. The date patterns match the month and weekday names literally, so
the first word of "please friday 9am" cannot shadow the day.

## 3. Schema

```sql
CREATE TABLE scheduled_announcements (
  id, created_by, chat_id, message_ids, created_at,
  send_at, status, settled_at, settled_by
);
```

`message_ids` is the draft: the leader's own message ids, comma-separated in
send order. The draft is never copied here, which is what lets a leader keep
fixing a typo until it fires, and is why a message they delete first is
skipped instead (`Report.broken`, reported to them afterwards).

`status` is `pending` → `sent` / `cancelled` / `missed`.

## 4. Sending once, or not at all

`db.claim_announcement` flips `pending` → `sent` in one transaction **before**
the fan-out, and `db.settle_announcement` only touches a row that is still
`pending`. Between them:

- a duplicate timer, or a restore racing the job it is restoring, finds
  nothing to send;
- a leader tapping ❌ Cancel in the same second the timer fires either stops
  the send or is told it has already gone out.

Marking it sent up front loses the tail of a fan-out that crashes halfway.
That is the cheaper of the two failures: the other one is 120 residents
getting the same announcement twice.

## 5. Restarts and the grace window

`broadcast.restore_scheduled` runs from `post_init` beside
`jobs.restore_jobs`. Anything still due is re-armed. Anything more than
`LATE_GRACE_MIN` (30) late is written off as `missed` and its author told,
because a deploy takes seconds and a real outage does not: "the laundry room
shuts at 2" arriving at six is worse than not arriving, and only the leader
can tell which of the two theirs is.

The same window makes a confirm card that sat unanswered behave sensibly.
Inside it, the leader gets what they asked for and the timer fires at once;
past it they are sent back to pick a time.

## 6. The waiting list

`/waiting` lists what is pending, one message each so each carries its own
Cancel button, capped at ten with a line saying how many were left out. It is
`/waiting` and not `/scheduled` because `/schedule` now opens the composer,
and two commands one letter apart that do different things is a trap;
`/scheduled` survives as an alias, since it is what a leader will guess. That button (`bcx:<id>`) is a stateless handler, like the poll
cards: the receipt outlives the composer that produced it and has to still
work when the leader scrolls back to it days later.

Any leader may cancel any of them, not only the author. A notice that has
turned out to be wrong should not have to wait for whoever wrote it to wake
up, and the author is told when somebody else called it off.

## 7. Audience

`deliver()` reads `db.all_user_ids()` when it runs, not when the announcement
was written. Somebody who registers this afternoon is a resident by tonight,
and the house notice they are missing is the one they most need.

## 8. Rollout

Reply keyboards are cached client-side until the bot sends a new one, so the
Scheduled Announce button appears for a leader only after their next `/start`
(or anything else that re-sends the menu). Same lesson as the Poll button in
v1.4. `/schedule` and `/waiting` work immediately either way, and the command
menu refreshes itself on the next restart.

Nothing needs a migration: `scheduled_announcements` is created by the usual
`CREATE TABLE IF NOT EXISTS` on startup.

## v1.6 grammar

```
bc:when        open "send it later"
bc:at:<key>    quick time button (1h / 3h / eve / am)
bc:cal:<YYYYMM>         picker: draw that month
bc:d:<YYYYMMDD>         picker: day chosen, ask for the hour
bc:h:<YYYYMMDD>:<HH>    picker: hour chosen, ask for the minutes
bc:m:<YYYYMMDD>:<HHMM>  picker: the whole moment, go to confirm
noop           an inert grid cell, now actually registered
bc:ok          confirm the resolved time
bc:redo        back to the draft
bcx:<aid>      cancel scheduled announcement <aid>, from any message
```


---

# v1.7 changes (2026-09-01): recalling an announcement

## 0. Why this could not be done before

A leader sent a two-message announcement and asked for it back minutes later.
It was not possible, and the 48-hour delete window was not the reason.

`deliver()` fanned the draft out with `copy_message` and threw away what it
returned. That return value carries the `message_id` of the copy just created,
and it is the only time Telegram ever names it: there is no API call that asks
a bot what it has sent. Nothing was written to the DB, successful copies were
not logged, and `httpx` is pinned to WARNING so no response body reached
`noctua.log` either. Two messages existed in ~120 chats and the bot could not
name a single one of them.

Everything below follows from that. The feature is mostly bookkeeping; the
deleting is the easy half.

**Announcements sent before this version are not recallable and never will
be.** There is nothing to reconstruct from.

## 1. Schema

```sql
CREATE TABLE announcements (          -- a fan-out that actually happened
  id, created_by, chat_id, drafted, sent_at, recalled_at
);
CREATE TABLE announcement_copies (    -- where each copy landed
  id, announcement_id, chat_id, message_id, deleted_at
);
```

Distinct from `scheduled_announcements`, which is the queue of sends still
waiting. A scheduled announcement that fires writes an `announcements` row
like any other, under the leader who scheduled it.

`drafted` is how many messages the leader wrote. That is the number they
recognise as "the announcement", so it is the number on the buttons; the copy
count (`drafted` x residents) only appears in the card and the receipt.

## 2. `deliver()` records as it goes

`deliver(bot, from_chat_id, draft, *, created_by)` opens the `announcements`
row **before** the first copy, then flushes one batch of `announcement_copies`
per resident. Opening up front means a fan-out interrupted halfway still
leaves every copy that did go out recallable; flushing per resident rather
than per copy is one write instead of N, and costs at most one resident's
chain if the process dies mid-flight.

## 3. The stack

Per leader, newest first, filtered to `recalled_at IS NULL`, `sent_at` inside
48 hours, and at least one copy with `deleted_at IS NULL`. `/recall` acts on
the top of it; recalling again walks one step further back.

Leaders do not share a stack. "Undo the last announcement" turning out to mean
somebody else's notice is a worse surprise than having to ask them to undo
their own.

An announcement drops off the stack on its own once nothing of it is left
standing, which is what makes a partly-failed recall resumable rather than
needing its own retry state.

## 4. The card (`♻️ Recall` / `/recall`)

Stateless, not a conversation: it recomputes the stack on every tap, so a card
scrolled back to an hour later acts on what is true now.

```
♻️ Recall an announcement?

📢 Latest: 2 message(s), sent 14 min ago.
🕒 Today (Tue 1 Sep), 12:05 AM
📬 240 copies of it are still standing.
🗂 3 of your announcements can still be taken back.     ← only when > 1

This deletes the copies out of every resident's chat. Nobody is told it
happened, and anyone who has already read it has already read it.

[ ♻️ Recall all 3 announcements (7 messages) ]
[ ♻️ Just the latest (2 messages) ]
[ ❌ Cancel ]
```

With one announcement in reach the two recall buttons would mean the same
thing, so it collapses to `[ ♻️ Recall it (2 messages) ]`.

Buttons come off before the first delete: the pass takes about as long as the
send did, and a second tap inside that window would start a second sweep over
copies the first is still working through.

After a pass the receipt leads and the card redraws underneath it. If the
stack still has something, it re-asks ("Recall the next one?") with fresh
buttons. If it is empty, the card closes with "Nothing else can be recalled."
and no buttons at all.

## 5. Telegram's three answers

Same split as the send path, and for the same reason: they are three different
situations, not three flavours of failure.

| Answer | Counted as | Copy row | Effect on the stack |
|---|---|---|---|
| deleted | `deleted` | closed | drops off when all are closed |
| `BadRequest` | `missing` (already gone) | closed | drops off |
| `Forbidden` | `unreachable` (blocked the bot) | closed | drops off |
| other `TelegramError` | `failed` | left open | stays, so a retry finds it |

`Forbidden` is closed rather than retried on purpose. That copy can never be
deleted, so leaving it open would keep offering a recall that cannot finish
for two days. The receipt says so instead.

## 6. Rollout

Both tables are created by the usual `CREATE TABLE IF NOT EXISTS` on startup;
no migration. The `♻️ Recall` button reaches a leader on their next `/start`
(reply keyboards are cached client-side, same as the Poll and Scheduled
Announce buttons before it); `/recall` works immediately either way.

## v1.7 grammar

```
rc:all         recall every announcement still in reach, newest first
rc:one         recall only the most recent one, then offer the next
rc:no          close the card, delete nothing
```

---

# v1.8 changes (2026-09-01): one announcement door

`📢 Announce` and `📅 Scheduled Announce` were two buttons onto one composer,
which made a leader decide the timing of a notice before writing a word of it.
The composer already offers `✅ Send` and `🕒 Send it later` side by side once
you are inside, so the choice belongs there and not on the menu.

- `MENU_ANNOUNCE` is now `📢 Announce (now or scheduled)`, full width. The
  caption says what is behind it, which is the point of the rename.
- `📅 Scheduled Announce` leaves the menu and becomes `LEGACY_SCHEDULE`. Its
  caption still routes to `announce_later` via `RX_SCHEDULE`, and it joins
  `LEGACY_CAPTIONS` so a leader tapping it gets the new keyboard once.
- `📢 Announce` does the same, as `LEGACY_ANNOUNCE`, for exactly the same
  reason: reply keyboards live in the client until the bot replaces one, so a
  renamed button is a dead button until then unless the old caption is kept.
- `/schedule` is unchanged and stays in the command menu. It is the door for a
  leader who already knows the notice is for later.

Leader menu after this change:

```
[🧺 Laundry menu]
[📢 Announce (now or scheduled)]
[📋 Poll] [♻️ Recall announcement]
[👤 Profile] [❓ Help]
```

## v1.8 amendment (same night): menu order and the recall caption

`♻️ Recall` became `♻️ Recall announcement`. On its own the word is a verb
with no object, and the one thing it must never be mistaken for is a laundry
action. `LEGACY_RECALL` keeps the one-night-old caption routing.

Profile and Help moved to the bottom row. They are read once; the rows above
are why anybody opens the chat. Residents are unaffected either way, their menu
was already `[🧺 Laundry menu]` over `[👤 Profile] [❓ Help]`.
