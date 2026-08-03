"""Room number parsing and validation for Noctua Bot.

Pure stdlib, no project imports — this module (and its tests) must import
and run with plain system python3 and no third-party dependencies.

Room data below is kept as plain constants so the dorm leader can add a
floor or a suite unit without touching the parsing logic.
"""

import re
from dataclasses import dataclass

# --- Room data (edit here to add/remove floors, rooms, or suites) ----------

SUITE_LETTERS = "ABCDEF"

FLOOR_ROOMS: dict[str, list[str]] = {
    "06": [f"{n:02d}" for n in range(1, 28)],
    "07": [f"{n:02d}" for n in range(1, 28)],
    "08": [f"{n:02d}" for n in range(1, 28) if n != 11],
}

# Numbers inside a floor's range that are not student rooms. Worth naming
# rather than just rejecting: "there is no #08-11" reads as a bug to whoever
# typed it, so the message says what the space actually is.
NOT_A_ROOM: dict[str, str] = {
    "08-11": "that's the RF's home",
}

# Suite rooms (subdivided into units A-F; a unit letter is required for
# these). Base form is "FF-RR", no leading "#".
SUITE_ROOMS: set[str] = {
    "06-01", "06-11", "06-12",
    "07-01", "07-11", "07-12",
    "08-01", "08-12",
}

_FORMAT_HINT = "e.g. #06-27"
_SEPARATOR_RE = re.compile(r"[-–\s]+")  # hyphen, en dash, or whitespace


@dataclass
class RoomResult:
    ok: bool
    room: str | None = None      # normalized "#06-27" / "#08-01C" when ok
    needs_letter: bool = False   # valid suite base, letter missing
    base: str | None = None      # "08-01" when needs_letter
    error: str | None = None     # friendly message when not ok and not needs_letter


def validate_room(text: str) -> RoomResult:
    """Parse and validate a free-text room number.

    Accepts case-insensitive input with an optional leading '#', floor and
    room separated by '-', an en dash, a space, or nothing, and an optional
    trailing suite unit letter (A-F). Floor and room numbers may be given as
    1 or 2 digits and are zero-padded. A bare 3-digit blob (e.g. "627") is
    ambiguous — it could split 1+2 or 2+1 digits — so it is rejected rather
    than guessed.
    """
    if not isinstance(text, str):
        return _invalid_format()

    raw = text.strip()
    if raw.startswith("#"):
        raw = raw[1:].strip()
    if not raw:
        return _invalid_format()

    letter: str | None = None
    if raw[-1].isalpha():
        letter = raw[-1].upper()
        raw = raw[:-1].rstrip()

    parsed = _split_floor_room(raw)
    if parsed is None:
        return _invalid_format()
    floor, room = parsed

    if floor not in FLOOR_ROOMS:
        return _bad_floor()
    if room not in FLOOR_ROOMS[floor]:
        return _bad_room(floor, f"{floor}-{room}")

    base = f"{floor}-{room}"
    is_suite = base in SUITE_ROOMS

    if letter is not None:
        if not is_suite:
            return _letter_not_allowed(base)
        if letter not in SUITE_LETTERS:
            return _bad_letter(base)
        return RoomResult(ok=True, room=f"#{base}{letter}")

    if is_suite:
        return RoomResult(ok=False, needs_letter=True, base=base)

    return RoomResult(ok=True, room=f"#{base}")


def suite_letters(base: str) -> list[str]:
    """Return the valid unit letters (A-F) for a suite room base."""
    return list(SUITE_LETTERS)


def _split_floor_room(raw: str) -> tuple[str, str] | None:
    """Split a '#'-free, letter-free room body into (floor, room), or None."""
    raw = raw.strip()
    if not raw:
        return None

    parts = _SEPARATOR_RE.split(raw)

    if len(parts) == 2:
        floor_raw, room_raw = parts
        if not (floor_raw.isdigit() and room_raw.isdigit()):
            return None
        if not (1 <= len(floor_raw) <= 2) or not (1 <= len(room_raw) <= 2):
            return None
        return floor_raw.zfill(2), room_raw.zfill(2)

    if len(parts) == 1:
        blob = parts[0]
        if not blob.isdigit():
            return None
        if len(blob) == 4:
            return blob[0:2], blob[2:4]
        if len(blob) == 2:
            return blob[0:1].zfill(2), blob[1:2].zfill(2)
        return None  # length 1, 3 (ambiguous), or >4 — not parseable

    return None


def _invalid_format() -> RoomResult:
    msg = f"Sorry, I couldn't read that as a room number ({_FORMAT_HINT})."
    return RoomResult(ok=False, error=msg)


def _bad_floor() -> RoomResult:
    return RoomResult(ok=False, error=f"Noctua rooms are on floors 06–08 ({_FORMAT_HINT}).")


def _bad_room(floor: str, base: str) -> RoomResult:
    reason = NOT_A_ROOM.get(base)
    if reason is not None:
        return RoomResult(ok=False, error=f"There's no #{base}, {reason} ({_FORMAT_HINT}).")
    valid_rooms = FLOOR_ROOMS[floor]
    low, high = valid_rooms[0], valid_rooms[-1]
    msg = f"Floor {floor} only has rooms {low}–{high} ({_FORMAT_HINT})."
    return RoomResult(ok=False, error=msg)


def _letter_not_allowed(base: str) -> RoomResult:
    return RoomResult(ok=False, error=f"#{base} has no units, just send #{base}.")


def _bad_letter(base: str) -> RoomResult:
    valid = ", ".join(SUITE_LETTERS)
    return RoomResult(
        ok=False,
        error=f"Unit letter must be one of {valid} for #{base} (e.g. #{base}A).",
    )
