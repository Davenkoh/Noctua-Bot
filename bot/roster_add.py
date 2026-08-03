"""Add, update or remove single residents on the roster.

    python -m bot.roster_add @derrick8765 "Derrick" "#07-10"
    python -m bot.roster_add @a "A" "#06-05" @b "B" "#07-11C"     (several at once)
    python -m bot.roster_add @yiwennt "Test User 1" none          (no room yet)
    python -m bot.roster_add --remove @derrick8765
    python -m bot.roster_add --list

A room of ``none`` (or ``-`` / ``tbd`` / ``?``) whitelists someone without
claiming a room for them: test accounts, guests, or a resident whose room
isn't settled. They get in, and /start asks them for a name and room the
way it does when no roster exists at all.

Unlike ``bot.roster_import`` (which replaces the whole sheet), this only
touches the handles you name, so the rest of the roster is left alone. To fix
a room whose handle is wrong, use ``bot.roster_fix``, which is keyed by room.
Safe to run while the bot is polling: SQLite is in WAL mode.
"""

from __future__ import annotations

import sys

from . import db, rooms, util

NAME_LIMIT = 60
NO_ROOM_WORDS = {"none", "-", "tbd", "?", "n/a"}
NO_ROOM_LABEL = "(no room yet)"
USAGE = __doc__


def _clean_handle(raw: str) -> str:
    return "".join(raw.split()).lstrip("@").lower()


def parse_room(raw: str) -> tuple[str | None, str | None]:
    """Read a room argument as ``(room, error)``.

    An empty room is a real value here, not a failure: it marks a
    whitelist-only entry. Exactly one of the two is ever set.
    """
    if raw.strip().lower() in NO_ROOM_WORDS:
        return "", None

    result = rooms.validate_room(raw)
    if result.needs_letter:
        return None, f"room #{result.base} needs its unit letter A-F"
    if not result.ok:
        return None, result.error
    return result.room, None


def _warn_if_registered(handle: str, room: str | None) -> None:
    """Say so when the roster edit won't reach someone who is already in.

    Registration copies the name and room across once and residents keep their
    access afterwards, so a roster row changed underneath a registered resident
    changes nothing they can see.
    """
    row = db.user_by_handle(handle)
    if row is None:
        return
    if room is None:
        print(f"       ⚠️  @{handle} is already registered and keeps access until purged")
    elif row["room"] != room:
        print(f"       ⚠️  @{handle} registered earlier as {row['name']} ({row['room']}), "
              "so the bot still shows that room")


def show_roster() -> None:
    rows = db.roster_all()
    if not rows:
        print("Roster is empty (registration is open to everyone).")
        return
    print(f"Roster ({len(rows)} resident(s)):")
    for row in rows:
        print(f"  @{row['handle']:<16} {row['name']:<20} {row['room'] or NO_ROOM_LABEL}")


def remove(handles: list[str]) -> int:
    problems = 0
    for raw in handles:
        handle = _clean_handle(raw)
        if db.roster_lookup(handle) is None:
            problems += 1
            print(f"  @{handle} was not on the roster")
            continue
        db.roster_apply([handle], [])
        print(f"  removed @{handle}")
        _warn_if_registered(handle, None)
    return problems


def add(triples: list[str]) -> int:
    if len(triples) % 3 != 0:
        print("Each resident needs three values: handle, name, room.")
        return 2

    problems = 0
    for index in range(0, len(triples), 3):
        raw_handle, raw_name, raw_room = triples[index : index + 3]
        handle = _clean_handle(raw_handle)
        if not handle:
            problems += 1
            print(f"  skipped {raw_name!r}: no telegram handle")
            continue

        name = util.clean_name(raw_name, limit=NAME_LIMIT)
        if name is None:
            problems += 1
            print(f"  skipped @{handle}: name is empty or over {NAME_LIMIT} characters")
            continue

        room, error = parse_room(raw_room)
        if error is not None:
            problems += 1
            print(f"  skipped @{handle}: {error}")
            continue

        existing = db.roster_lookup(handle)
        db.roster_apply([], [(handle, name, room)])
        verb = "updated" if existing else "added"
        print(f"  {verb} @{handle}: {name}, {room or NO_ROOM_LABEL}")
        _warn_if_registered(handle, room)

    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(USAGE)
        return 0 if args else 2

    db.init_db()

    if args[0] == "--list":
        show_roster()
        return 0

    if args[0] == "--remove":
        code = remove(args[1:]) and 1 or 0
        print()
        show_roster()
        return code

    code = add(args)
    print()
    show_roster()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
