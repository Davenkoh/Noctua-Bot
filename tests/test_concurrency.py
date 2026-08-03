"""Tests for the contested-action paths in bot.db and bot.jobs — what happens
when two residents act on the same machine at the same moment.
Run with: ./.venv/bin/python -m tests.test_concurrency

Same DB_PATH/BOT_TOKEN stubbing as tests/test_roster.py, so this never touches
the real noctua.db even while the live bot is polling against it.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

# --- must happen before any `bot.*` import below ---------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="noctua_test_concurrency_")
_DB_PATH = str(Path(_TMP_DIR) / "test_concurrency.db")
os.environ["DB_PATH"] = _DB_PATH
os.environ["BOT_TOKEN"] = "test-dummy-token"

from bot import config, db, util  # noqa: E402

ALICE, BOB = 1001, 1002


def _fresh_db() -> None:
    """Empty every table so each test starts from a known state."""
    db.init_db()
    with db._tx() as conn:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM users")
    db.upsert_user(ALICE, "alice", "Alice", "#06-27")
    db.upsert_user(BOB, "bob", "Bob", "#07-11A")


def _set(session_id: int, column: str, value: str | None) -> None:
    """Move a timestamp directly, so tests don't have to wait real minutes."""
    with db._tx() as conn:
        conn.execute(f"UPDATE sessions SET {column} = ? WHERE id = ?", (value, session_id))


def _shift(minutes: int) -> str:
    return util.to_iso(util.now_utc() + timedelta(minutes=minutes))


# --------------------------------------------------------------------------
# two residents, one free machine
# --------------------------------------------------------------------------


def test_only_one_of_two_starts_wins_the_same_machine() -> int:
    _fresh_db()
    first = db.start_session("w1", ALICE, 30)
    second = db.start_session("w1", BOB, 30)

    assert first is not None, "the first resident to tap a free machine must get it"
    assert second is None, "the second resident must be turned away, not given a 2nd session"

    active = db.get_active("w1")
    assert active["user_id"] == ALICE, "the winner must be the one holding the machine"
    rows = db.connection().execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE machine = 'w1' AND status = 'active'"
    ).fetchone()
    assert rows["n"] == 1, f"exactly one active session expected on w1, found {rows['n']}"
    return 3


def test_a_machine_frees_up_for_the_next_resident() -> int:
    """Losing the race is not a lockout: once the winner's load is out, the
    machine is claimable again by anyone."""
    _fresh_db()
    first = db.start_session("d1", ALICE, 30)
    assert db.start_session("d1", BOB, 30) is None
    assert db.finish_early(first["id"]) is True

    second = db.start_session("d1", BOB, 45)
    assert second is not None, "a freed machine must be claimable by the resident who lost"
    assert second["user_id"] == BOB
    return 2


def test_a_second_connection_waits_instead_of_erroring() -> int:
    """busy_timeout: a roster import (or any second process) holding the write
    lock must make the bot wait its turn, not raise "database is locked"."""
    _fresh_db()
    held = threading.Event()
    failed: list[BaseException] = []

    def hold_write_lock() -> None:
        # The connection must be opened in this thread — sqlite3 objects can't
        # cross threads, and a holder that never actually locks tests nothing.
        holder = sqlite3.connect(_DB_PATH)
        try:
            holder.execute("BEGIN IMMEDIATE")
            held.set()
            time.sleep(0.25)
            holder.rollback()
        except BaseException as exc:  # surfaced below, not swallowed
            failed.append(exc)
            held.set()
        finally:
            holder.close()

    thread = threading.Thread(target=hold_write_lock)
    thread.start()
    try:
        assert held.wait(timeout=5), "the lock holder never started"
        assert not failed, f"the lock holder itself failed: {failed[0]!r}"
        started = time.monotonic()
        session = db.start_session("w2", ALICE, 30)
        waited = time.monotonic() - started
    finally:
        thread.join()

    assert session is not None, "the write should have waited for the lock, then succeeded"
    assert waited > 0.05, f"the write returned in {waited:.3f}s — it never hit the lock"
    return 2


# --------------------------------------------------------------------------
# two residents, one 🔔 nudge
# --------------------------------------------------------------------------


def test_two_simultaneous_nudges_only_send_one_dm() -> int:
    _fresh_db()
    session = db.start_session("d2", ALICE, 30)
    db.finish_early(session["id"])

    claimed_a, previous_a = db.claim_nudge(session["id"], config.PING_COOLDOWN_MIN)
    claimed_b, previous_b = db.claim_nudge(session["id"], config.PING_COOLDOWN_MIN)

    assert claimed_a is True, "the first nudge must go through"
    assert previous_a is None, "nothing had nudged this session before"
    assert claimed_b is False, "the second nudge inside the cooldown must be refused"
    assert previous_b is not None, "the loser needs the winning stamp to say 'nudged X ago'"
    return 4


def test_a_nudge_is_allowed_again_once_the_cooldown_passes() -> int:
    _fresh_db()
    session = db.start_session("d2", ALICE, 30)
    db.finish_early(session["id"])
    db.claim_nudge(session["id"], config.PING_COOLDOWN_MIN)

    _set(session["id"], "last_ping_at", _shift(-(config.PING_COOLDOWN_MIN + 1)))
    claimed, _ = db.claim_nudge(session["id"], config.PING_COOLDOWN_MIN)
    assert claimed is True, "past the cooldown the machine must be nudgeable again"
    return 1


def test_releasing_a_failed_nudge_lets_the_next_person_try() -> int:
    """When the DM bounces (owner blocked the bot) the slot goes back, rather
    than blocking everyone else for the full cooldown."""
    _fresh_db()
    session = db.start_session("d2", ALICE, 30)
    db.finish_early(session["id"])

    _, previous = db.claim_nudge(session["id"], config.PING_COOLDOWN_MIN)
    db.release_nudge(session["id"], previous)

    claimed, _ = db.claim_nudge(session["id"], config.PING_COOLDOWN_MIN)
    assert claimed is True, "a released slot must be claimable straight away"
    return 1


# --------------------------------------------------------------------------
# an extension landing as the timer fires
# --------------------------------------------------------------------------


def test_an_extension_stops_the_timer_freeing_a_running_machine() -> int:
    _fresh_db()
    session = db.start_session("w1", ALICE, 30)
    _set(session["id"], "ends_at", _shift(-1))  # the timer is due

    db.extend_session(session["id"], 30)  # ...and "paid twice" lands right now
    assert db.mark_done(session["id"]) is False, (
        "mark_done must refuse while ends_at is still in the future"
    )
    assert db.get_active("w1") is not None, "the machine must stay 🔴 running"
    return 2


def test_the_timer_still_frees_a_machine_that_is_genuinely_done() -> int:
    _fresh_db()
    session = db.start_session("w1", ALICE, 30)
    _set(session["id"], "ends_at", _shift(-1))

    assert db.mark_done(session["id"]) is True, "an expired cycle must still end"
    assert db.get_active("w1") is None, "the machine must go 🟢 free"
    assert db.mark_done(session["id"]) is False, "a finished cycle must not end twice"
    return 3


def test_an_early_collect_and_the_timer_cannot_both_win() -> int:
    _fresh_db()
    session = db.start_session("w1", ALICE, 30)
    assert db.finish_early(session["id"]) is True
    assert db.mark_done(session["id"]) is False, (
        "the timer must not re-finish a session the owner already collected"
    )
    return 2


def main() -> None:
    tests = (
        test_only_one_of_two_starts_wins_the_same_machine,
        test_a_machine_frees_up_for_the_next_resident,
        test_a_second_connection_waits_instead_of_erroring,
        test_two_simultaneous_nudges_only_send_one_dm,
        test_a_nudge_is_allowed_again_once_the_cooldown_passes,
        test_releasing_a_failed_nudge_lets_the_next_person_try,
        test_an_extension_stops_the_timer_freeing_a_running_machine,
        test_the_timer_still_frees_a_machine_that_is_genuinely_done,
        test_an_early_collect_and_the_timer_cannot_both_win,
    )
    total_cases = 0
    try:
        for test in tests:
            total_cases += test()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)  # best-effort cleanup

    summary = f"{len(tests)}/{len(tests)} test functions, {total_cases} cases"
    print(f"PASS: {summary} — tests/test_concurrency.py")


if __name__ == "__main__":
    main()
