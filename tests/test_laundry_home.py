"""Tests for the merged laundry home screen.

Run with: ./.venv/bin/python -m tests.test_laundry_home
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

# --- must happen before any `bot.*` import below ---------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="noctua_test_home_")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "test_home.db")
os.environ["BOT_TOKEN"] = "test-dummy-token"
os.environ["ADMIN_USERNAMES"] = "boss"

from bot import config, db, keyboards  # noqa: E402
from bot.handlers import laundry  # noqa: E402


def _reset() -> None:
    db.init_db()
    conn = db.connection()
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM users")
    db.upsert_user(1, "boss", "Daven", "#08-27")
    db.upsert_user(2, "lydia", "Lydia", "#08-01C")


def _buttons(markup) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.inline_keyboard]


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# --------------------------------------------------------------------------
# the screen itself
# --------------------------------------------------------------------------


def test_a_free_machine_gets_a_nudge_button_naming_its_last_user() -> int:
    _reset()
    session = db.start_session("w2", 2, 30)
    # mark_done refuses while the timer is still running, by design.
    db.finish_early(session["id"])

    text, markup = laundry.render_home()
    assert "🟢 Free" in text
    assert "Last: Lydia" in text, text
    rows = _buttons(markup)
    machine_row = next(r for r in rows if "Washer 2" in r[0])
    assert len(machine_row) == 2, f"expected machine + nudge, got {machine_row}"
    assert machine_row[1] == "🔔 Nudge Lydia", machine_row
    assert f"ping:w2" in _callbacks(markup)
    return 4


def test_a_running_machine_has_no_nudge_button() -> int:
    _reset()
    db.start_session("w1", 2, 30)

    text, markup = laundry.render_home()
    assert "🔴 In use by Lydia" in text, text
    rows = _buttons(markup)
    machine_row = next(r for r in rows if "Washer 1" in r[0])
    # Its owner's load isn't finished, so there is nothing to nudge about.
    assert len(machine_row) == 1, f"a running machine must not offer a nudge: {machine_row}"
    assert "ping:w1" not in _callbacks(markup)
    return 3


def test_an_unused_machine_has_no_nudge_button() -> int:
    _reset()
    text, markup = laundry.render_home()
    assert "not used yet" in text, text
    assert not [c for c in _callbacks(markup) if c.startswith("ping:")]
    return 2


def test_the_footer_reads_last_updated_and_drops_the_estimates_note() -> int:
    _reset()
    text, _ = laundry.render_home()
    assert re.search(r"Last updated \d", text), text
    assert "times are estimates" not in text, "that note was dropped"
    assert text.strip().splitlines()[-1].startswith("<i>Last updated"), text
    return 3


def test_the_instruction_sits_under_the_title_not_at_the_foot() -> int:
    _reset()
    lines = laundry.render_home()[0].splitlines()
    # It tells you what to do with the buttons, so it must be read first.
    assert lines[0] == laundry.HOME_TITLE, lines[0]
    assert lines[1] == laundry.HOME_HINT, lines[1]
    assert laundry.HOME_HINT not in lines[-1], "it should no longer be the footer"
    return 3


def test_every_machine_appears_once_with_a_refresh_at_the_end() -> int:
    _reset()
    _text, markup = laundry.render_home()
    callbacks = _callbacks(markup)
    for machine_id in config.MACHINES:
        assert callbacks.count(f"m:{machine_id}") == 1, machine_id
    assert callbacks[-1] == "hub", "Refresh sits last and re-renders the home"
    assert _buttons(markup)[-1] == ["🔄 Refresh"]
    return 6


def test_admins_still_get_reset_and_residents_do_not() -> int:
    _reset()
    _t, admin_markup = laundry.render_home(is_admin=True)
    _t, plain_markup = laundry.render_home(is_admin=False)
    assert "reset" in _callbacks(admin_markup), "admins keep the escape hatch"
    assert "reset" not in _callbacks(plain_markup), "residents must not see reset"
    return 2


def test_every_button_matches_a_registered_handler_pattern() -> int:
    _reset()
    db.finish_early(db.start_session("d1", 2, 30)["id"])
    _text, markup = laundry.render_home(is_admin=True)
    patterns = [
        keyboards.PAT_MACHINE, keyboards.PAT_PING,
        keyboards.PAT_HUB, keyboards.PAT_RESET,
    ]
    for data in _callbacks(markup):
        assert any(re.match(p, data) for p in patterns), f"no handler matches {data!r}"
    return 1


def main() -> None:
    tests = (
        test_a_free_machine_gets_a_nudge_button_naming_its_last_user,
        test_a_running_machine_has_no_nudge_button,
        test_an_unused_machine_has_no_nudge_button,
        test_the_footer_reads_last_updated_and_drops_the_estimates_note,
        test_the_instruction_sits_under_the_title_not_at_the_foot,
        test_every_machine_appears_once_with_a_refresh_at_the_end,
        test_admins_still_get_reset_and_residents_do_not,
        test_every_button_matches_a_registered_handler_pattern,
    )
    total_cases = 0
    try:
        for test in tests:
            total_cases += test()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)  # best-effort cleanup

    summary = f"{len(tests)}/{len(tests)} test functions, {total_cases} cases"
    print(f"PASS: {summary} — tests/test_laundry_home.py")


if __name__ == "__main__":
    main()
