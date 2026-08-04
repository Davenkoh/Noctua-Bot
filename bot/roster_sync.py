"""Load roster/master.txt into the bot's database.

    python -m bot.roster_sync                     # plan against roster/master.txt
    python -m bot.roster_sync --apply             # write the plan
    python -m bot.roster_sync --show              # print the roster as stored
    python -m bot.roster_sync path/to/file.txt    # sync some other file

The master file is the single source of truth: one line per room, columns
separated by "|" (room | full name | display name | handle | note). This tool
makes the database's roster table match the file exactly. Rows with a handle
are added or updated; handles in the database but missing from the file are
removed. The full name and note columns are for people, not the bot: they are
read and checked, never stored.

Nothing is written without ``--apply``, and the plan is printed either way.
Safe to run while the bot is polling: SQLite is in WAL mode, and the whole
sync is one transaction.

Syncing never un-registers anyone who already tapped /start. Registration
copies the name and room across once and residents keep access afterwards,
by design; the plan warns whenever that gap applies.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from . import config, db, rooms, util

NAME_LIMIT = 60
COMMENT = ";"
DEFAULT_FILE = Path(config.PROJECT_ROOT) / "roster" / "master.txt"
USAGE = __doc__


def _clean_handle(raw: str) -> str:
    return "".join(raw.split()).lstrip("@").lower()


@dataclass
class Entry:
    """One data line of the master file."""

    room: str                  # normalized, e.g. "#06-01A"
    full_name: str             # leader's records; may be empty (warned about)
    display: str               # what the bot shows for this resident
    handle: str | None         # lowercase, no "@"; None = cannot register yet
    note: str
    source: str                # "line 24", for error messages


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse_line(line: str, source: str) -> tuple[Entry | None, str | None]:
    fields = [field.strip() for field in line.split("|")]
    if len(fields) == 4:
        fields.append("")          # note column left off entirely
    if len(fields) != 5:
        return None, (
            f"{source}: expected room | full name | display | handle | note, "
            f"got {len(fields)} column(s)"
        )
    raw_room, full_name, raw_display, raw_handle, note = fields

    result = rooms.validate_room(raw_room)
    if result.needs_letter:
        return None, f"{source}: room #{result.base} needs its unit letter A-F"
    if not result.ok or result.room is None:
        return None, f"{source}: {result.error}"

    handle = _clean_handle(raw_handle) or None

    display = util.clean_name(raw_display, limit=NAME_LIMIT)
    if display is None:
        return None, f"{source}: display name is empty or over {NAME_LIMIT} characters"

    return Entry(result.room, full_name, display, handle, note, source), None


def parse_file(path: Path) -> tuple[list[Entry], list[str], list[str]]:
    """All data lines of ``path`` -> (entries, errors, warnings)."""
    entries: list[Entry] = []
    errors: list[str] = []
    warnings: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(COMMENT):
            continue
        entry, error = parse_line(line, f"line {number}")
        if error is not None or entry is None:
            errors.append(error or f"line {number}: could not read that line")
            continue
        if not entry.full_name:
            warnings.append(f"{entry.source}: {entry.room} has no full name on file")
        entries.append(entry)

    _reject_duplicates(entries, errors)
    return entries, errors, warnings


def _reject_duplicates(entries: list[Entry], errors: list[str]) -> None:
    """Rooms, handles, and display names must each be unique in the file.

    Rooms and handles because the database is keyed on them; display names
    because two residents shown identically cannot be told apart in the
    laundry queue, which is why the master carries surnames for the Chloes.
    """
    seen: dict[str, dict[str, str]] = {"room": {}, "handle": {}, "display name": {}}
    for entry in entries:
        keys = {"room": entry.room, "display name": entry.display.lower()}
        if entry.handle is not None:
            keys["handle"] = entry.handle
        for kind, key in keys.items():
            clash = seen[kind].get(key)
            if clash is not None:
                errors.append(
                    f"{entry.source}: same {kind} as {clash} ({entry.room})"
                )
            else:
                seen[kind][key] = entry.source


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


@dataclass
class Plan:
    adds: list[Entry]
    changes: list[tuple[Entry, str, str]]   # entry, old name, old room
    removes: list[tuple[str, str, str]]     # handle, name, room (db rows)
    unchanged: int
    warnings: list[str]


def plan(entries: list[Entry]) -> Plan:
    """Compare the file against the roster table as it stands right now."""
    target = {e.handle: e for e in entries if e.handle is not None}
    current = {row["handle"]: row for row in db.roster_all()}

    adds, changes, removes, unchanged, warnings = [], [], [], 0, []

    for handle, entry in target.items():
        row = current.get(handle)
        if row is None:
            adds.append(entry)
        elif (row["name"], row["room"]) != (entry.display, entry.room):
            changes.append((entry, row["name"], row["room"]))
        else:
            unchanged += 1
        _warn_registered(warnings, handle, entry.room)

    for handle, row in current.items():
        if handle not in target:
            removes.append((handle, row["name"], row["room"]))
            registered = db.user_by_handle(handle)
            if registered is not None:
                warnings.append(
                    f"@{handle} is already registered as {registered['name']} "
                    f"({registered['room']}) and keeps access until purged"
                )

    return Plan(adds, changes, removes, unchanged, warnings)


def _warn_registered(warnings: list[str], handle: str, room: str) -> None:
    row = db.user_by_handle(handle)
    if row is not None and row["room"] != room:
        warnings.append(
            f"@{handle} registered earlier as {row['name']} ({row['room']}), "
            f"so the bot still shows that room"
        )


# --------------------------------------------------------------------------
# reporting + applying
# --------------------------------------------------------------------------


def report(result: Plan, *, applied: bool) -> None:
    mark = "✅" if applied else "·"
    for entry in result.adds:
        print(f"  {mark} add     @{entry.handle} ({entry.display}) {entry.room}")
    for entry, old_name, old_room in result.changes:
        was = old_room if old_room != entry.room else old_name
        now = entry.room if old_room != entry.room else entry.display
        print(f"  {mark} update  @{entry.handle} ({entry.display}): {was} -> {now}")
    for handle, name, room in result.removes:
        print(f"  {mark} remove  @{handle} ({name}) {room}")
    if result.unchanged:
        print(f"  = {result.unchanged} already match")
    for warning in result.warnings:
        print(f"       ⚠️  {warning}")


def apply(result: Plan) -> None:
    removals = [handle for handle, _name, _room in result.removes]
    upserts = [
        (entry.handle, entry.display, entry.room)
        for entry in result.adds + [change[0] for change in result.changes]
        if entry.handle is not None
    ]
    if removals or upserts:
        db.roster_apply(removals, upserts)


def show_roster() -> None:
    rows = db.roster_all()
    if not rows:
        print("Roster is empty (registration is open to everyone).")
        return
    print(f"Roster ({len(rows)} resident(s)):")
    for row in rows:
        print(f"  @{row['handle']:<24} {row['name']:<20} {row['room'] or 'no room'}")


def run(path: Path, *, write: bool) -> int:
    entries, errors, warnings = parse_file(path)
    for error in errors:
        print(f"  ❌ {error}")
    if errors:
        print(f"\n{len(errors)} problem line(s); nothing compared or written.")
        return 1
    if not entries:
        print(f"❌ {path} lists nobody; refusing to wipe the roster.")
        print("   (an empty roster would open registration to anyone)")
        return 1

    result = plan(entries)
    if write:
        apply(result)

    with_handle = sum(1 for e in entries if e.handle is not None)
    print("Applied:" if write else "Plan (nothing written yet):")
    report(result, applied=write)
    for warning in warnings:
        print(f"       ⚠️  {warning}")

    print()
    touched = len(result.adds) + len(result.changes) + len(result.removes)
    print(
        f"{path.name}: {len(entries)} rooms, {with_handle} with a handle, "
        f"{len(entries) - with_handle} without."
    )
    if write:
        print(f"Database updated: {touched} row(s) touched, now in sync.")
    elif touched:
        print(f"{touched} row(s) would change. Re-run with --apply to write it.")
    else:
        print("Database already matches the file; nothing to write.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-h", "--help"}:
        print(USAGE)
        return 0

    db.init_db()

    if "--show" in args:
        show_roster()
        return 0

    write = "--apply" in args
    args = [arg for arg in args if arg != "--apply"]
    if len(args) > 1:
        print(USAGE)
        return 2

    path = Path(args[0]).expanduser() if args else DEFAULT_FILE
    if not path.is_file():
        print(f"❌ No such file: {path}")
        return 1

    code = run(path, write=write)
    if write and code == 0:
        print()
        show_roster()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
