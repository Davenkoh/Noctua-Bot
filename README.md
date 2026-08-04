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

By default, anyone can register with `/start`. To restrict the bot to actual residents, keep the master resident list in **`roster/master.txt`** and load it into the bot. That file is the single source of truth: to change anything about who lives where, edit the line there and sync.

One line per room, columns separated by `|` (see `roster/master.example.txt` for a fake sample; the real file holds resident details and stays out of git):

```
; room   | full name             | display name | telegram handle | note
#06-27   | TAN XIAO MING, ALICE  | Alice        | @alicetan81     | leader
#08-21   | NG WEI                | Wei          |                 | no handle yet
```

- **room** — the key. Suite rooms (`06-01`, `06-11`, `06-12`, `07-01`, `07-11`, `07-12`, `08-01`, `08-12`) must include their unit letter, e.g. `#08-01C`.
- **full name** — the resident as the dorm's records know them. For people, never stored by the bot.
- **display name** — what the bot shows: first name, plus surname whenever two residents share one (Chloe Oon / Chloe Ong / Chloe Ng...). Must be unique across the file.
- **telegram handle** — what registration matches on. Blank means none known yet, so that resident cannot register until one is filled in.
- **note** — free text for humans (exchange student, leader, open questions). Never stored. Leaders are configured by `LEADER_USERNAMES` in `.env`, so a leadership change means updating both.

Then load it:

```bash
.venv/bin/python -m bot.roster_sync            # show what would change, write nothing
.venv/bin/python -m bot.roster_sync --apply    # make the database match the file
.venv/bin/python -m bot.roster_sync --show     # print the roster as the bot stores it
```

The sync makes the database's roster table **match the file exactly**: new handles are added, changed rooms and names are updated, and handles no longer in the file are removed, all in one transaction, safe to run while the bot is polling. The plan is printed either way and nothing is written without `--apply`. A file with any bad line writes nothing at all, and an empty file is refused (an empty roster would open registration to anyone).

Remember the live bot runs on the server, so the loop for a real change is: edit `roster/master.txt` on the Mac, `./deploy/push.sh` (the roster folder rides along), then run the sync **on the server**.

A few rules worth knowing:

- **Matching is by Telegram username**, not name. A resident without a `@username` set is told to add one (Telegram → Settings → Username) and tap `/start` again.
- **An empty or never-synced roster means open registration**, so nothing changes until the first sync.
- Residents who **registered before a roster was synced keep their access**; they're not retroactively removed just because they're missing from a later version of the file.
- After a sync, a new resident's `/start` skips the name and room questions entirely: the file is the source of truth, so they land straight on the menu.
- **Syncing does not un-register anyone who already tapped `/start`.** Registration copies the name and room across once, and residents keep their access afterwards, by design. The plan prints a warning whenever that gap applies, and `/resetme` (leaders) or a fresh registration is what actually moves someone's stored details.

## Data

All **live state** — registered residents, machine sessions, and the loaded roster — lives in a single SQLite file, `noctua.db`, created automatically at the project root the first time the bot runs. Back it up by copying that file (e.g. before an upgrade, or on a regular schedule if it's on a VPS). Deleting it resets the bot to a blank slate, and everyone would need to register again.

The roster table inside it is just the loaded copy of `roster/master.txt`; the file is the one to edit, the database is what the bot reads at runtime.

## Troubleshooting

- **"Invalid token" / bot won't start** — double-check `BOT_TOKEN` in `.env` was pasted in full, with no extra spaces or missing characters.
- **`Conflict: terminated by other getUpdates request`** — Telegram only allows one running copy of a bot to poll for updates at a time. This means two instances are running (e.g. one left over on a VPS and one on your laptop). Find and stop the extra process (e.g. `ps aux | grep bot.main`) before starting a new one.
