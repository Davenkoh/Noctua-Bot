"""Tests for bot.roster_fix (room-keyed corrections) and for roster entries
that carry no room. Run with: ./.venv/bin/python -m tests.test_roster_fix

Same DB_PATH/BOT_TOKEN stubbing as tests/test_roster.py, so this never touches
the real noctua.db even while the live bot is polling against it.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

# --- must happen before any `bot.*` import below ---------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="noctua_test_roster_fix_")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "test_roster_fix.db")
os.environ["BOT_TOKEN"] = "test-dummy-token"

from bot import db, roster_add, roster_fix  # noqa: E402
from bot.handlers import registration  # noqa: E402


def _fresh_roster(rows: list[tuple[str, str, str]]) -> None:
    db.init_db()
    db.roster_replace(rows)
    with db._tx() as conn:
        conn.execute("DELETE FROM users")


def _fix(*args: str) -> int:
    """Run the CLI the way the dorm leader would, with its report swallowed."""
    with contextlib.redirect_stdout(io.StringIO()):
        return roster_fix.main(list(args))


def _quietly(call) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return call()


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_parse_fields_reads_rooms_handles_names_and_clear_words() -> int:
    correction, error = roster_fix.parse_fields(["06-22", "@Le_Han", "Le", "Han"], "line 1")
    assert error is None, error
    assert correction.room == "#06-22", correction.room
    assert correction.handle == "le_han", correction.handle
    assert correction.name == "Le Han", correction.name  # spare fields join into the name

    correction, error = roster_fix.parse_fields(["#08-01d", "empty"], "line 2")
    assert error is None, error
    assert correction.room == "#08-01D", correction.room  # unit letter is upper-cased
    assert correction.handle is None and correction.clear_word == "empty"

    correction, error = roster_fix.parse_fields(["#06-25", "@laurelite"], "line 3")
    assert error is None, error
    assert correction.name is None, "an omitted name must stay None, not become ''"
    return 3


def test_parse_fields_rejects_bad_input() -> int:
    cases = [
        (["#06-22"], "need a room"),                       # handle missing
        (["#08-01", "@bob", "Bob"], "unit letter"),        # suite without its letter
        (["#09-22", "@bob", "Bob"], "floors 06"),          # no such floor
        (["#06-22", "empty", "Bob"], "takes no name"),     # clear word plus a name
    ]
    for fields, expected in cases:
        correction, error = roster_fix.parse_fields(fields, "line 1")
        assert correction is None, f"{fields} should not parse"
        assert expected in error, f"{fields}: expected {expected!r} in {error!r}"
    return len(cases)


def test_parse_file_skips_comments_and_blank_lines() -> int:
    path = Path(_TMP_DIR) / "corrections.txt"
    path.write_text(
        "; a comment\n"
        "\n"
        "#06-22  @newhandle  Le Han\n"
        "#07-03  @other              ; a trailing comment is not part of the name\n"
        "#08-01D empty\n",
        encoding="utf-8",
    )
    corrections, errors = roster_fix.parse_file(path)
    assert errors == [], errors
    assert [c.room for c in corrections] == ["#06-22", "#07-03", "#08-01D"], corrections
    assert corrections[1].name is None, f"trailing comment leaked in: {corrections[1].name!r}"
    return 2


# --------------------------------------------------------------------------
# planning + applying
# --------------------------------------------------------------------------


def test_retagging_a_room_drops_the_old_handle_and_keeps_the_name() -> int:
    _fresh_roster([("oldhandle", "Le Han", "#06-22"), ("other", "Other", "#07-03")])

    code = _fix("#06-22", "@startstrongendstronger", "--apply")
    assert code == 0, f"expected a clean run, got exit code {code}"

    assert db.roster_lookup("oldhandle") is None, "the wrong handle must lose the room"
    fixed = db.roster_lookup("startstrongendstronger")
    assert fixed is not None, "the corrected handle must be on the roster"
    assert fixed["room"] == "#06-22", fixed["room"]
    # No name was given, so the one already on file for that room carries over.
    assert fixed["name"] == "Le Han", fixed["name"]
    assert db.roster_lookup("other") is not None, "other rooms must be left alone"
    return 4


def test_empty_and_unknown_clear_the_room() -> int:
    _fresh_roster([("ghost", "Ghost", "#08-01D"), ("wrong", "Wrong", "#08-21")])

    assert _fix("#08-01D", "empty", "--apply") == 0
    assert _fix("#08-21", "unknown", "--apply") == 0

    assert db.roster_lookup("ghost") is None, "'empty' must clear the room"
    assert db.roster_lookup("wrong") is None, "'unknown' must clear the room too"
    assert db.roster_count() == 0, db.roster_count()
    return 3


def test_new_handle_on_an_unlisted_room_needs_a_name() -> int:
    _fresh_roster([("alice", "Alice", "#06-27")])

    code = _fix("#07-11D", "@yuhanger", "--apply")
    assert code == 1, f"expected exit code 1 for a skipped line, got {code}"
    assert db.roster_lookup("yuhanger") is None, "a nameless new entry must not be written"

    # ... and the same line with a name goes straight in.
    assert _fix("#07-11D", "@yuhanger", "Yu Han", "--apply") == 0
    entry = db.roster_lookup("yuhanger")
    assert entry is not None and entry["name"] == "Yu Han", entry
    return 3


def test_a_room_named_twice_in_one_run_is_skipped_not_guessed() -> int:
    _fresh_roster([("first", "First", "#06-22")])
    path = Path(_TMP_DIR) / "duplicate.txt"
    path.write_text("#06-22  @one  One\n#06-22  @two  Two\n", encoding="utf-8")

    code = _fix(str(path), "--apply")
    assert code == 1, f"expected the duplicate line to be reported, got {code}"
    assert db.roster_lookup("one") is not None, "the first line still applies"
    assert db.roster_lookup("two") is None, "the second line must be skipped, not win"
    return 3


def test_without_apply_nothing_is_written() -> int:
    _fresh_roster([("oldhandle", "Le Han", "#06-22")])

    assert _fix("#06-22", "@newhandle") == 0, "a dry run of a valid line is a clean exit"
    assert db.roster_lookup("newhandle") is None, "a dry run must not write"
    assert db.roster_lookup("oldhandle") is not None, "a dry run must not delete"
    return 2


def test_a_move_leaves_the_old_room_empty() -> int:
    _fresh_roster([("mover", "Mover", "#06-05")])

    assert _fix("#07-24", "@mover", "--apply") == 0
    entry = db.roster_lookup("mover")
    assert entry is not None and entry["room"] == "#07-24", entry
    assert db.roster_rows_for_room("#06-05") == [], "the room they left must be empty"
    return 2


# --------------------------------------------------------------------------
# roster entries with no room (test accounts, guests)
# --------------------------------------------------------------------------


def test_roster_add_accepts_a_no_room_entry() -> int:
    _fresh_roster([])
    for word in ("none", "-", "TBD", "?"):
        room, error = roster_add.parse_room(word)
        assert error is None and room == "", f"{word!r} should mean 'no room', got {room!r}"

    assert _quietly(lambda: roster_add.add(["@carol", "Carol", "none"])) == 0
    entry = db.roster_lookup("carol")
    assert entry is not None, "the test user must be whitelisted"
    assert entry["room"] == "", f"expected a blank room, got {entry['room']!r}"
    return 5


def _fake_update(username: str) -> tuple[object, list[str]]:
    """The slice of telegram's Update that onboarding actually touches."""
    sent: list[str] = []

    async def reply_text(text: str, **kwargs: object) -> None:
        sent.append(text)

    message = SimpleNamespace(reply_text=reply_text, text="/start")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=4242, username=username, is_bot=False),
        effective_message=message,
        effective_chat=SimpleNamespace(id=4242),
        message=message,
        callback_query=None,
    )
    return update, sent


def test_a_no_room_entry_is_asked_for_details_instead_of_registered() -> int:
    _fresh_roster([("alice", "Alice", "#06-27"), ("carol", "Carol", "")])
    update, sent = _fake_update("carol")
    context = SimpleNamespace(user_data={}, bot=None)

    state = asyncio.run(registration._start_from_roster(update, context))

    assert state == registration.ASK_NAME, f"expected the name question, got {state}"
    assert "name" in sent[-1].lower(), sent
    assert db.get_user(4242) is None, "nothing is registered until they answer"
    return 3


def test_a_roomed_entry_still_registers_with_zero_questions() -> int:
    _fresh_roster([("alice", "Alice", "#06-27")])
    update, sent = _fake_update("alice")
    context = SimpleNamespace(user_data={}, bot=None)

    state = asyncio.run(registration._start_from_roster(update, context))

    assert state == registration.ConversationHandler.END, state
    row = db.get_user(4242)
    assert row is not None, "a resident with a room is registered straight away"
    assert (row["name"], row["room"]) == ("Alice", "#06-27"), dict(row)
    return 3


def main() -> None:
    tests = (
        test_parse_fields_reads_rooms_handles_names_and_clear_words,
        test_parse_fields_rejects_bad_input,
        test_parse_file_skips_comments_and_blank_lines,
        test_retagging_a_room_drops_the_old_handle_and_keeps_the_name,
        test_empty_and_unknown_clear_the_room,
        test_new_handle_on_an_unlisted_room_needs_a_name,
        test_a_room_named_twice_in_one_run_is_skipped_not_guessed,
        test_without_apply_nothing_is_written,
        test_a_move_leaves_the_old_room_empty,
        test_roster_add_accepts_a_no_room_entry,
        test_a_no_room_entry_is_asked_for_details_instead_of_registered,
        test_a_roomed_entry_still_registers_with_zero_questions,
    )
    total_cases = 0
    try:
        for test in tests:
            total_cases += test()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)  # best-effort cleanup

    summary = f"{len(tests)}/{len(tests)} test functions, {total_cases} cases"
    print(f"PASS: {summary} — tests/test_roster_fix.py")


if __name__ == "__main__":
    main()
