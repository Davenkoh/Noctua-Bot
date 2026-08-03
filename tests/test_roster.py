"""Tests for the v1.1 roster whitelist: bot.roster_import + bot.db's roster
functions. Run with: ./.venv/bin/python -m tests.test_roster

Sets DB_PATH to a throwaway temp file *before* anything imports bot.db /
bot.config, so this never touches the real noctua.db even while the live
bot is running against it. BOT_TOKEN is also stubbed since bot.config reads
it at import time (bot.config never raises on a missing/dummy token — only
bot.main.main() validates it, which this test never calls or imports).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from openpyxl import Workbook

# --- must happen before any `bot.*` import below ---------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="noctua_test_roster_")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "test_roster.db")
os.environ["BOT_TOKEN"] = "test-dummy-token"

from bot import db  # noqa: E402
from bot.roster_import import _clean_handle, find_header, import_roster  # noqa: E402


# --------------------------------------------------------------------------
# bot.roster_import._clean_handle
# --------------------------------------------------------------------------


def test_clean_handle_strips_at_and_whitespace_and_lowercases() -> int:
    cases = {
        "@Daven_Koh ": "daven_koh",
        "": "",
        # internal whitespace is removed entirely (not just trimmed), matching
        # _clean_handle's "".join(raw.split()) behaviour.
        " @ a B ": "ab",
    }
    for raw, expected in cases.items():
        got = _clean_handle(raw)
        assert got == expected, f"_clean_handle({raw!r}) expected {expected!r}, got {got!r}"
    return len(cases)


# --------------------------------------------------------------------------
# bot.roster_import.find_header
# --------------------------------------------------------------------------


def test_find_header_skips_junk_row_and_handle_claims_telegram_name() -> int:
    rows = [
        ["Noctua Residents 2026", "", ""],  # junk title row - no header here
        ["Telegram Name", "Room Number", "Full Name"],
        ["alice", "06-27", "Alice Tan"],
    ]
    result = find_header(rows)
    assert result is not None, "expected a header row to be found"
    index, columns = result
    assert index == 1, f"expected the header on row index 1 (junk row first), got {index}"
    # "Telegram Name" matches both the handle pattern ("tele") and the name
    # pattern ("name"), but handle is resolved first and claims column 0 -
    # leaving "Full Name" (column 2) for the name key.
    assert columns == {"handle": 0, "room": 1, "name": 2}, columns
    return 1


def test_find_header_returns_none_when_a_column_is_missing() -> int:
    rows = [
        ["Name", "Telegram handle"],  # no column matches "room" anywhere
        ["Alice", "alice"],
    ]
    assert find_header(rows) is None
    return 1


# --------------------------------------------------------------------------
# bot.db roster functions
# --------------------------------------------------------------------------


def test_db_roster_replace_and_lookup() -> int:
    db.init_db()
    assert db.roster_count() == 0, "expected a fresh temp DB to start with an empty roster"

    first = [
        ("alice", "Alice Tan", "#06-27"),
        ("bob", "Bob Lee", "#08-01C"),
    ]
    count = db.roster_replace(first)
    assert count == 2, f"expected roster_replace to report 2 rows, got {count}"
    assert db.roster_count() == 2, f"expected roster_count()==2, got {db.roster_count()}"

    found = db.roster_lookup("alice")
    assert found is not None, "expected to find 'alice' in the roster"
    assert found["name"] == "Alice Tan"
    assert found["room"] == "#06-27"
    # lookup normalises "@" / case / whitespace, same as a stored handle would be
    assert db.roster_lookup("@Alice") is not None, "lookup should be case/'@' insensitive"
    assert db.roster_lookup("nobody") is None, "unknown handles must return None"

    second = [("carol", "Carol Ng", "#07-11A")]
    count2 = db.roster_replace(second)
    assert count2 == 1, f"expected roster_replace to report 1 row, got {count2}"
    assert db.roster_count() == 1, f"expected the old rows wiped, got {db.roster_count()}"
    assert db.roster_lookup("alice") is None, "alice should have been wiped by the re-import"
    assert db.roster_lookup("carol") is not None, "carol should be in the new roster"
    return 4


# --------------------------------------------------------------------------
# bot.roster_import.import_roster, end-to-end via a real .xlsx
# --------------------------------------------------------------------------


def _write_workbook(path: Path, rows: list[list[str]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def test_import_roster_skips_bad_rows_and_imports_the_rest() -> int:
    sheet_rows = [
        ["Name", "Room", "Telegram handle"],
        ["Dave Ong", "06-27", "@dave"],  # valid, plain (non-suite) room
        ["Eve Sim", "08-01", "@eve"],  # suite room missing its unit letter -> skipped
    ]
    xlsx_path = Path(_TMP_DIR) / "roster.xlsx"
    _write_workbook(xlsx_path, sheet_rows)

    exit_code = import_roster(xlsx_path)
    assert exit_code == 0, f"expected exit code 0 for one valid + one skipped row, got {exit_code}"
    assert db.roster_count() == 1, f"expected exactly 1 imported resident, got {db.roster_count()}"

    dave = db.roster_lookup("dave")
    assert dave is not None, "expected the valid row (dave) to be imported"
    assert dave["room"] == "#06-27"
    assert db.roster_lookup("eve") is None, "the suite row missing its letter must be skipped"
    return 1


def main() -> None:
    tests = (
        test_clean_handle_strips_at_and_whitespace_and_lowercases,
        test_find_header_skips_junk_row_and_handle_claims_telegram_name,
        test_find_header_returns_none_when_a_column_is_missing,
        test_db_roster_replace_and_lookup,
        test_import_roster_skips_bad_rows_and_imports_the_rest,
    )
    total_cases = 0
    try:
        for test in tests:
            total_cases += test()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)  # best-effort cleanup

    summary = f"{len(tests)}/{len(tests)} test functions, {total_cases} cases"
    print(f"PASS: {summary} — tests/test_roster.py")


if __name__ == "__main__":
    main()
