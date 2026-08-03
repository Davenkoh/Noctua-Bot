"""Correct roster rows by room number.

    python -m bot.roster_fix corrections.txt              # show the plan only
    python -m bot.roster_fix corrections.txt --apply      # write it
    python -m bot.roster_fix --apply "#06-22" @lehan "Le Han"
    python -m bot.roster_fix --apply "#08-01D" empty

``bot.roster_add`` is keyed by Telegram handle, which is the wrong key for the
usual correction: "the handle listed for #06-22 is wrong, here's the right
one". Adding the right handle alone would leave the wrong one whitelisted for
that room, so the wrong person could still register. This tool takes the room
as the key and clears whoever else is listed there, in one transaction.

File format, one room per line. ``;`` comments out the rest of a line:

    #06-22   @startstrongendstronger   Le Han
    #06-25   @laurelite                ; handle was wrong, keep the name
    #08-01D  empty
    #08-21   unknown

The third field (name) is optional: leave it out and the name already on the
roster for that room is kept, which is what you want when only the handle was
wrong. ``empty`` means nobody lives there; ``unknown`` means the row on file is
wrong and the right handle isn't known yet. Both clear the room, the only
difference is what gets printed.

Nothing is written without ``--apply``, and the plan is printed either way.
Safe to run while the bot is polling: SQLite is in WAL mode.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import db, rooms, util
from .roster_add import show_roster

NAME_LIMIT = 60
COMMENT = ";"
CLEAR_WORDS = {"empty": "nobody lives there", "unknown": "handle not known yet"}
USAGE = __doc__


def _clean_handle(raw: str) -> str:
    return "".join(raw.split()).lstrip("@").lower()


@dataclass
class Correction:
    """One line of input: a room, and who (if anyone) belongs to it."""

    room: str                    # normalized, e.g. "#06-22"
    handle: str | None           # None for a clear
    name: str | None             # None means "keep the name already on file"
    clear_word: str | None       # "empty" / "unknown" when clearing
    source: str                  # "line 4" / "arguments", for error messages


@dataclass
class Step:
    """What a single correction turns into once compared with the roster."""

    correction: Correction
    removals: list[str] = field(default_factory=list)
    upsert: tuple[str, str, str] | None = None
    summary: str = ""
    notes: list[str] = field(default_factory=list)
    error: str | None = None


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse_fields(fields: list[str], source: str) -> tuple[Correction | None, str | None]:
    """Turn ``[room, handle_or_word, name...]`` into a Correction or an error."""
    if len(fields) < 2:
        return None, f"{source}: need a room and a handle (or 'empty' / 'unknown')"

    raw_room, token = fields[0], fields[1]
    raw_name = " ".join(fields[2:]).strip()

    result = rooms.validate_room(raw_room)
    if result.needs_letter:
        return None, f"{source}: room #{result.base} needs its unit letter A-F"
    if not result.ok or result.room is None:
        return None, f"{source}: {result.error}"
    room = result.room

    word = token.strip().lower()
    if word in CLEAR_WORDS:
        if raw_name:
            return None, f"{source}: '{word}' takes no name, drop {raw_name!r}"
        return Correction(room, None, None, word, source), None

    handle = _clean_handle(token)
    if not handle:
        return None, f"{source}: no telegram handle"

    name: str | None = None
    if raw_name:
        name = util.clean_name(raw_name, limit=NAME_LIMIT)
        if name is None:
            return None, f"{source}: name is empty or over {NAME_LIMIT} characters"

    return Correction(room, handle, name, None, source), None


def parse_file(path: Path) -> tuple[list[Correction], list[str]]:
    corrections: list[Correction] = []
    errors: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        # ';' comments out the rest of the line, so a file can be annotated
        # both above a block and beside a single room.
        line = raw.split(COMMENT, 1)[0].strip()
        if not line:
            continue
        correction, error = parse_fields(line.split(), f"line {number}")
        if error is not None or correction is None:
            errors.append(error or f"line {number}: could not read that line")
        else:
            corrections.append(correction)
    return corrections, errors


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


def _describe(row: sqlite3.Row) -> str:
    return f"@{row['handle']} ({row['name']})"


def plan_step(correction: Correction) -> Step:
    """Compare one correction with the roster as it stands right now.

    Roster edits never touch the users table: registration copies the name and
    room across once, and residents keep their access afterwards. So a handle
    being corrected here may still be registered under the old details, which
    the plan flags rather than quietly leaving behind.
    """
    step = Step(correction=correction)
    occupants = db.roster_rows_for_room(correction.room)

    if correction.handle is None:
        reason = CLEAR_WORDS[correction.clear_word or "empty"]
        if not occupants:
            step.summary = f"already clear ({reason})"
            return step
        step.removals = [row["handle"] for row in occupants]
        listed = ", ".join(_describe(row) for row in occupants)
        step.summary = f"remove {listed} ({reason})"
        for row in occupants:
            _warn_registered(step, row["handle"], removed=True)
        return step

    existing = db.roster_lookup(correction.handle)
    others = [row for row in occupants if row["handle"] != correction.handle]

    name = correction.name
    if name is None and existing is not None:
        name = existing["name"]
    if name is None and len(others) == 1:
        # Same person, wrong handle on file: their name comes along with the room.
        name = others[0]["name"]
    if name is None:
        if others:
            step.error = (
                f"{correction.source}: {correction.room} lists {len(others)} people, "
                "so add the right name after the handle"
            )
        else:
            step.error = (
                f"{correction.source}: @{correction.handle} is new and "
                f"{correction.room} is unlisted, so add their name after the handle"
            )
        return step

    step.removals = [row["handle"] for row in others]
    step.upsert = (correction.handle, name, correction.room)

    if existing is None:
        action = f"add @{correction.handle} ({name})"
    elif existing["room"] != correction.room:
        action = f"move @{correction.handle} ({name}) from {existing['room'] or 'no room'}"
    elif existing["name"] != name:
        action = f"rename @{correction.handle} to {name}"
    else:
        action = f"keep @{correction.handle} ({name})"
    if others:
        action += ", replacing " + ", ".join(_describe(row) for row in others)
    step.summary = action

    for row in others:
        _warn_registered(step, row["handle"], removed=True)
    _warn_registered(step, correction.handle, removed=False)
    return step


def _warn_registered(step: Step, handle: str, *, removed: bool) -> None:
    row = db.user_by_handle(handle)
    if row is None:
        return
    if removed:
        step.notes.append(
            f"@{handle} is already registered as {row['name']} ({row['room']}) and "
            f"keeps access until purged"
        )
    elif row["room"] != step.correction.room:
        step.notes.append(
            f"@{handle} registered earlier as {row['name']} ({row['room']}), so the "
            f"bot still shows that room"
        )


def plan(corrections: list[Correction]) -> list[Step]:
    steps = [plan_step(correction) for correction in corrections]
    _reject_duplicates(steps)
    return steps


def _reject_duplicates(steps: list[Step]) -> None:
    """A room, and a handle, may each appear only once per run.

    Every step is planned against the roster as it stood before the run, so two
    lines naming the same room would each be planned as though the other did
    not exist and the last one would quietly win. Skipping the repeat and
    saying so beats guessing which line the leader meant.
    """
    rooms_seen: dict[str, str] = {}
    handles_seen: dict[str, str] = {}
    for step in steps:
        correction = step.correction
        clash = rooms_seen.get(correction.room)
        if clash is not None:
            step.error = f"{correction.source}: {correction.room} is already set on {clash}"
            continue
        rooms_seen[correction.room] = correction.source

        if correction.handle is None:
            continue
        clash = handles_seen.get(correction.handle)
        if clash is not None:
            step.error = f"{correction.source}: @{correction.handle} is already used on {clash}"
            continue
        handles_seen[correction.handle] = correction.source


# --------------------------------------------------------------------------
# reporting + applying
# --------------------------------------------------------------------------


def report(steps: list[Step], parse_errors: list[str], *, applied: bool) -> None:
    for error in parse_errors:
        print(f"  ❌ {error}")

    for step in steps:
        room = step.correction.room
        if step.error is not None:
            print(f"  ❌ {room:<9} {step.error}")
            continue
        mark = "✅" if applied else "·"
        print(f"  {mark} {room:<9} {step.summary}")
        for note in step.notes:
            print(f"       ⚠️  {note}")


def apply(steps: list[Step]) -> None:
    removals: list[str] = []
    upserts: list[tuple[str, str, str]] = []
    for step in steps:
        if step.error is not None:
            continue
        removals.extend(step.removals)
        if step.upsert is not None:
            upserts.append(step.upsert)
    if removals or upserts:
        db.roster_apply(removals, upserts)


def run(corrections: list[Correction], parse_errors: list[str], *, write: bool) -> int:
    steps = plan(corrections)
    if write:
        apply(steps)

    print("Applied:" if write else "Plan (nothing written yet):")
    report(steps, parse_errors, applied=write)

    failed = len(parse_errors) + sum(1 for step in steps if step.error is not None)
    changed = sum(1 for step in steps if step.error is None and (step.removals or step.upsert))
    print()
    if write:
        print(f"{changed} room(s) changed, {failed} line(s) skipped.")
    else:
        print(f"{changed} room(s) would change, {failed} line(s) skipped.")
        print("Re-run with --apply to write it.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(USAGE)
        return 0 if args else 2

    write = "--apply" in args
    args = [arg for arg in args if arg != "--apply"]
    if not args:
        print(USAGE)
        return 2

    db.init_db()

    if len(args) == 1:
        path = Path(args[0]).expanduser()
        if not path.is_file():
            print(f"❌ No such file: {path}")
            print("   (a single room on its own needs a handle after it)")
            return 1
        corrections, errors = parse_file(path)
        if not corrections and not errors:
            print(f"❌ {path} has no corrections in it.")
            return 1
    else:
        correction, error = parse_fields(args, "arguments")
        if error is not None or correction is None:
            print(f"❌ {error or 'could not read those arguments'}")
            return 2
        corrections, errors = [correction], []

    code = run(corrections, errors, write=write)
    print()
    show_roster()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
