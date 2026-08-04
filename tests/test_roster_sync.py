"""Tests for bot.roster_sync (master file -> roster table) and for bot.db's
roster functions. Run with: ./.venv/bin/python -m tests.test_roster_sync

Sets DB_PATH to a throwaway temp file *before* anything imports bot.db /
bot.config, so this never touches the real noctua.db even while the live
bot is running against it.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
from pathlib import Path

# --- must happen before any `bot.*` import below ---------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="noctua_test_roster_sync_")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "test_roster_sync.db")
os.environ["BOT_TOKEN"] = "test-dummy-token"

from bot import db, roster_sync  # noqa: E402


def _quietly(call):
    with contextlib.redirect_stdout(io.StringIO()):
        return call()


def _write(name: str, text: str) -> Path:
    path = Path(_TMP_DIR) / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# bot.roster_sync.parse_line / parse_file
# --------------------------------------------------------------------------


def test_parse_line_reads_all_five_columns() -> int:
    entry, error = roster_sync.parse_line(
        "#06-22 | YAU LE HAN | Le Han | @Start_Strong | leader", "line 1"
    )
    assert error is None, error
    assert entry is not None
    assert entry.room == "#06-22"
    assert entry.full_name == "YAU LE HAN"
    assert entry.display == "Le Han"
    assert entry.handle == "start_strong", "handles are stored lowercase, no @"
    assert entry.note == "leader"
    return 1


def test_parse_line_allows_a_blank_handle_and_a_missing_note() -> int:
    entry, error = roster_sync.parse_line("#08-21 | WU, DI | Di |  | ", "line 1")
    assert error is None and entry is not None
    assert entry.handle is None, "blank handle means the resident cannot register yet"

    entry, error = roster_sync.parse_line("#08-21 | WU, DI | Di | @wudi55", "line 2")
    assert error is None and entry is not None, "the note column may be left off"
    assert entry.note == ""
    return 2


def test_parse_line_rejects_bad_rows() -> int:
    cases = [
        "#08-01 | FU, CADEE | Cadee | @cadee | ",     # suite without its letter
        "#99-01 | NOBODY | Nobody | @nobody | ",      # no such floor
        "#06-22 | YAU LE HAN | | @lehan | ",          # display name empty
        "just some text",                             # not enough columns
    ]
    for line in cases:
        entry, error = roster_sync.parse_line(line, "line 1")
        assert entry is None and error is not None, f"expected a rejection: {line!r}"
    return len(cases)


def test_parse_file_skips_comments_and_rejects_duplicates() -> int:
    path = _write(
        "dup.txt",
        "; a comment line\n"
        "\n"
        "#06-22 | YAU LE HAN | Le Han | @lehan | \n"
        "#06-22 | SOMEONE ELSE | Other | @other | \n"     # duplicate room
        "#07-13 | CHOO RAYNER | Rayner | @lehan | \n"     # duplicate handle
        "#07-14 | AMBAR | Le Han | @i_ambar | \n",        # duplicate display
    )
    entries, errors, _warnings = roster_sync.parse_file(path)
    assert len(entries) == 4, "duplicate checks reject lines, they do not drop them"
    assert len(errors) == 3, errors
    assert "same room" in errors[0] and "same handle" in errors[1], errors
    assert "same display name" in errors[2], errors
    return 1


def test_parse_file_warns_when_the_full_name_is_missing() -> int:
    path = _write("gap.txt", "#08-01E |  | Denise | @deniseayq | full name tbc\n")
    entries, errors, warnings = roster_sync.parse_file(path)
    assert not errors and len(entries) == 1
    assert len(warnings) == 1 and "#08-01E" in warnings[0], warnings
    return 1


# --------------------------------------------------------------------------
# bot.db roster functions (ported from the retired test_roster.py)
# --------------------------------------------------------------------------


def test_db_roster_replace_and_lookup() -> int:
    db.init_db()
    assert db.roster_count() == 0, "expected a fresh temp DB to start with an empty roster"

    count = db.roster_replace([("alice", "Alice Tan", "#06-27"), ("bob", "Bob Lee", "#08-01C")])
    assert count == 2 and db.roster_count() == 2

    found = db.roster_lookup("alice")
    assert found is not None and found["name"] == "Alice Tan" and found["room"] == "#06-27"
    assert db.roster_lookup("@Alice") is not None, "lookup should be case/'@' insensitive"
    assert db.roster_lookup("nobody") is None, "unknown handles must return None"

    assert db.roster_replace([("carol", "Carol Ng", "#07-11A")]) == 1
    assert db.roster_count() == 1, "roster_replace wipes before it loads"
    assert db.roster_lookup("alice") is None and db.roster_lookup("carol") is not None
    return 4


# --------------------------------------------------------------------------
# bot.roster_sync end to end: plan, then apply
# --------------------------------------------------------------------------


def test_sync_makes_the_database_match_the_file() -> int:
    db.init_db()
    db.roster_replace(
        [
            ("alice", "Alice Tan", "#06-27"),   # stays, but moves room in the file
            ("bob", "Bob Lee", "#08-01C"),      # not in the file -> removed
            ("carol", "Carol", "#07-11A"),      # matches the file exactly -> untouched
        ]
    )
    path = _write(
        "master.txt",
        "; comment\n"
        "#06-01A | TAN, ALICE | Alice Tan | @alice | moved here\n"
        "#07-11A | NG CAROL | Carol | @carol | \n"
        "#07-13  | CHOO RAYNER | Rayner | @rayner | new this term\n"
        "#08-21  | WU, DI | Di |  | no handle yet\n",
    )

    code = _quietly(lambda: roster_sync.main([str(path)]))
    assert code == 0, "a clean plan run exits 0"
    assert db.roster_lookup("bob") is not None, "plan mode must not write"
    assert db.roster_lookup("alice")["room"] == "#06-27", "plan mode must not write"

    code = _quietly(lambda: roster_sync.main([str(path), "--apply"]))
    assert code == 0
    assert db.roster_count() == 3, "three rows carry handles (Di has none)"
    assert db.roster_lookup("alice")["room"] == "#06-01A", "alice moved"
    assert db.roster_lookup("bob") is None, "bob removed: file is the source of truth"
    assert db.roster_lookup("carol")["room"] == "#07-11A", "carol untouched"
    assert db.roster_lookup("rayner") is not None, "rayner added"
    return 4


def test_sync_refuses_an_empty_file_and_bad_files() -> int:
    db.init_db()
    db.roster_replace([("alice", "Alice Tan", "#06-27")])

    empty = _write("empty.txt", "; only comments in here\n")
    assert _quietly(lambda: roster_sync.main([str(empty), "--apply"])) == 1
    assert db.roster_count() == 1, "an empty file must never wipe the roster"

    bad = _write("bad.txt", "#06-22 | YAU LE HAN | Le Han | @lehan | \nnot a row\n")
    assert _quietly(lambda: roster_sync.main([str(bad), "--apply"])) == 1
    assert db.roster_lookup("lehan") is None, "a file with errors writes nothing at all"

    assert _quietly(lambda: roster_sync.main([str(Path(_TMP_DIR) / "nope.txt")])) == 1
    return 3


def main() -> None:
    tests = (
        test_parse_line_reads_all_five_columns,
        test_parse_line_allows_a_blank_handle_and_a_missing_note,
        test_parse_line_rejects_bad_rows,
        test_parse_file_skips_comments_and_rejects_duplicates,
        test_parse_file_warns_when_the_full_name_is_missing,
        test_db_roster_replace_and_lookup,
        test_sync_makes_the_database_match_the_file,
        test_sync_refuses_an_empty_file_and_bad_files,
    )
    total_cases = 0
    try:
        for test in tests:
            total_cases += test()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)  # best-effort cleanup

    summary = f"{len(tests)}/{len(tests)} test functions, {total_cases} cases"
    print(f"PASS: {summary} — tests/test_roster_sync.py")


if __name__ == "__main__":
    main()
