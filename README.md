# Noctua Bot

🦉 A Telegram bot for the Noctua dorm — tracks the shared laundry machines and lets dorm leaders broadcast announcements to everyone who's registered.

## What it does

The persistent menu has three rows:

```
[🧺 Laundry menu]
[👤 Profile] [❓ Help]
[📢 Announce]        ← dorm leaders only
```

- **🧺 Laundry menu** lists all four machines with their live state (🟢 free or 🔴 running), plus two sections underneath:
  - **▶️ Start a machine** — pick Washer 1/2 or Dryer 1/2, then tap a duration (30 min for washers; 30/45/60 min for dryers). The bot times the cycle and DMs the resident when it's done, and the machine frees itself. Paid twice? Tap a duration again to add the time. Out early? **✅ Collecting now** frees the machine straight away.
  - **📊 Machine status** — all four machines at a glance, so nobody needs to walk down just to check. Free machines also show who used them last and when. (`/status` still works as a shortcut.)
  - **🔔 Nudge last user** — lists every free machine that may still hold someone's load, one tap to nudge its owner (throttled to once every few minutes per machine).
- **📢 Announce** (dorm leaders only, see [Configuration](#configuration)) opens a draft composer: send as many messages as you like — text, photos, videos, files — and keep editing them in the chat right up until you hit Send. **👀 Preview** shows the draft as residents will see it, **↩️ Undo last** drops the most recent message, **❌ Cancel** drops the whole draft. Sending delivers a header followed by every draft message, in order, to each registered resident, then reports how many were reached and how many were unreachable.
- Registration (`/start`) normally takes about 20 seconds: name, then room number. If a dorm leader has imported a [resident whitelist](#resident-whitelist-roster), matching residents just confirm their prefilled name and room instead. Everything else is buttons, so residents never have to type a machine name or timer by hand.

More features may be added later; the code is organized so each feature lives in its own file under `bot/handlers/`.

## Create the bot with @BotFather

Noctua Bot is already live on Telegram as **[@rc4noctuabot](https://t.me/rc4noctuabot)** — for day-to-day use (running it on a new machine, recovering a lost token, etc.) skip ahead to [Setup](#setup) and reuse that bot's existing token rather than creating a new one. The steps below are kept for reference, e.g. if you ever need a second/test instance:

1. Open a chat with [@BotFather](https://t.me/BotFather) on Telegram.
2. Send `/newbot` and follow the prompts.
3. When asked for a name, use **Noctua Bot** — this is the display name residents will see.
4. When asked for a username, pick anything that ends in `bot`, e.g. `NoctuaDormBot` (the live bot already uses `rc4noctuabot`).
5. BotFather replies with a token that looks like `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. Copy it — you'll paste it into `.env` in the next step. Treat it like a password: anyone who has it can control the bot.

## Setup

Requires **Python 3.11** on macOS (the same steps work later on a Linux VPS).

```bash
cd "Noctua Bot"                 # this project folder
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then open `.env` in any text editor and fill in:

- `BOT_TOKEN` — the token you copied from @BotFather above.
- `LEADER_USERNAMES` — comma-separated Telegram usernames (no `@`) allowed to send broadcasts, e.g. `LEADER_USERNAMES=alice,bob`.

## Run

```bash
source .venv/bin/activate       # if not already active
python -m bot.main
```

You should see log output and no errors — the bot is now polling Telegram for updates. Message it on Telegram to try it. `Ctrl+C` stops it.

### Keeping it running

Closing the terminal (or `Ctrl+C`) stops the bot. To leave it running in the background on the same machine:

```bash
nohup python -m bot.main > noctua.log 2>&1 &
```

or start it inside a `screen`/`tmux` session so it keeps going after you log out. For anything more permanent than "my Mac stays on", moving the bot to a small VPS and running it under `systemd` (or `screen`/`tmux` there) is more reliable than relying on a laptop or desktop staying powered on and awake.

## Configuration

Everything below is a plain file — edit it and restart the bot (`Ctrl+C` then `python -m bot.main` again) to apply changes.

- **Leaders** — `LEADER_USERNAMES` in `.env` (comma-separated Telegram usernames, no `@`).
- **Rooms** — floors, room numbers, and suite units are constants near the top of `bot/rooms.py` (`FLOOR_ROOMS`, `SUITE_ROOMS`). Add a floor or mark a room as a suite by editing those.
- **Machines & timings** — the machine list (id, label, emoji, cycle durations) and the nudge throttle (`PING_COOLDOWN_MIN`) live in `bot/config.py`.
- **Timezone** — defaults to `Asia/Singapore` (used for all displayed times); override with `TIMEZONE=` in `.env` using any IANA timezone name.

## Resident whitelist (roster)

By default, anyone can register with `/start`. If a dorm leader wants to restrict the bot to actual residents, they can import an Excel sheet listing everyone who's allowed in.

The sheet needs one header row with a **name** column, a **room** column, and a **Telegram** column (anything with "tele", "tag", "handle", or "username" in the header works) — data rows underneath. Then, on the server:

```bash
cd "Noctua Bot"                                        # this project folder
.venv/bin/python -m bot.roster_import residents.xlsx   # path to the sheet
```

The importer prints a summary: how many residents were imported, plus a list of any rows it had to skip (with the reason) so they can be fixed and re-imported.

A few rules worth knowing:

- **Matching is by Telegram username**, not name. A resident without a `@username` set is told to add one (Telegram → Settings → Username) and tap `/start` again.
- **Suite rooms** (`06-01`, `06-11`, `06-12`, `07-01`, `07-11`, `07-12`, `08-01`, `08-12`) must include the unit letter in the sheet, e.g. `#08-01C` — a suite room listed without a letter is treated as a bad row and skipped.
- Rows with problems (no handle, bad room, etc.) are **listed and skipped**; every other valid row still imports.
- **Re-importing replaces the whole list** — it's not additive, so always upload the complete, current resident list rather than just the new additions.
- **An empty or never-imported roster means open registration** — the current, pre-whitelist behaviour — so nothing changes until the first sheet is imported.
- Residents who **registered before a roster was imported keep their access** — they're not retroactively removed just because they're missing from a later sheet.
- After a successful import, a new resident's `/start` skips the name and room questions entirely: the sheet is the source of truth, so they land straight on the menu.

### Fixing the roster afterwards

Re-importing the whole sheet is a big hammer for "this one room has the wrong tag". Two smaller tools handle day-to-day corrections, and both are safe to run while the bot is polling.

**By room**, which is the usual case (the handle listed for a room is wrong, or the room changed hands):

```bash
.venv/bin/python -m bot.roster_fix roster/corrections.txt           # show the plan
.venv/bin/python -m bot.roster_fix roster/corrections.txt --apply   # write it
.venv/bin/python -m bot.roster_fix --apply "#06-22" @lehan "Le Han" # or one room inline
```

Each line of the file is a room, then a handle (or `empty` / `unknown`), then an optional name. A `;` comments out the rest of a line, and `roster/corrections.example.txt` is a template to copy:

```
#06-22   @startstrongendstronger   Le Han
#06-25   @laurelite                ; handle was wrong, keep the name on file
#08-01D  empty
#08-21   unknown
```

Real correction files hold resident details, so they're kept out of git (`.gitignore` covers `roster/*.txt` apart from the example).

- **Leave the name out** to keep whatever name the roster already has for that room, which is what you want when only the handle was wrong.
- **The room is the key**, so anyone else listed at that room is removed in the same transaction. That is the point: adding the right handle without dropping the wrong one would leave both whitelisted.
- `empty` means nobody lives there; `unknown` means the entry on file is wrong and the right handle isn't known yet. Both clear the room.
- **Nothing is written without `--apply`.** The plan is printed either way, including warnings when a handle you're changing has already registered (roster edits don't touch anyone who is already in, see below).
- A room or a handle may only appear **once per run**; a repeat is reported and skipped rather than guessed at.

**By handle**, for adding or removing one person:

```bash
.venv/bin/python -m bot.roster_add @derrick8765 "Derrick" "#07-10"
.venv/bin/python -m bot.roster_add @yiwennt "Test User 1" none    # whitelisted, no room
.venv/bin/python -m bot.roster_add --remove @derrick8765
.venv/bin/python -m bot.roster_add --list
```

A room of `none` (or `-`, `tbd`, `?`) whitelists someone **without claiming a room** for them: test accounts, guests, or a resident whose room isn't settled. They get in, and `/start` asks them for a name and room the way it does when no roster exists at all.

One thing neither tool does: **fixing the roster does not un-register anyone who already tapped `/start`.** Registration copies the name and room across once, and residents keep their access afterwards, by design. Both tools print a warning when that applies, and `/resetme` (leaders) or a fresh registration is what actually moves someone's stored details.

## Data

All state — registered residents, machine sessions, and the resident whitelist (if imported) — lives in a single SQLite file, `noctua.db`, created automatically at the project root the first time the bot runs. Back it up by copying that file (e.g. before an upgrade, or on a regular schedule if it's on a VPS). Deleting it resets the bot to a blank slate, and everyone (and the roster) would need to be set up again.

## Troubleshooting

- **"Invalid token" / bot won't start** — double-check `BOT_TOKEN` in `.env` was pasted in full, with no extra spaces or missing characters.
- **`Conflict: terminated by other getUpdates request`** — Telegram only allows one running copy of a bot to poll for updates at a time. This means two instances are running (e.g. one left over on a VPS and one on your laptop). Find and stop the extra process (e.g. `ps aux | grep bot.main`) before starting a new one.
