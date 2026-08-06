"""Tests for "count me in" polls: the tally, the card, and who may answer.

Run with: ./.venv/bin/python -m tests.test_poll
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

# --- must happen before any `bot.*` import below ---------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="noctua_test_poll_")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "test_poll.db")
os.environ["BOT_TOKEN"] = "test-dummy-token"
os.environ["LEADER_USERNAMES"] = "lead"
os.environ["ADMIN_USERNAMES"] = "boss"

from bot import db  # noqa: E402
from bot.handlers import poll as poll_handler  # noqa: E402


def _fresh() -> tuple[int, int, int, int]:
    """A poll sent to three residents. Returns (poll_id, *user_ids)."""
    db.init_db()
    conn = db.connection()
    for table in ("poll_recipients", "polls", "users"):
        conn.execute(f"DELETE FROM {table}")
    db.upsert_user(1, "boss", "Daven", "#08-27")
    db.upsert_user(2, "lead", "Jeshua", "#08-25")
    db.upsert_user(3, "someone", "Sarah", "#06-27")

    poll_id = db.create_poll("Block cleanup Saturday 10am?", created_by=1)
    for user_id in (1, 2, 3):
        db.add_poll_recipient(poll_id, user_id, chat_id=user_id, message_id=100 + user_id)
    return poll_id, 1, 2, 3


# --------------------------------------------------------------------------
# tally
# --------------------------------------------------------------------------


def test_answers_tally_by_side_and_track_who_has_not_replied() -> int:
    poll_id, a, b, c = _fresh()
    assert db.poll_answers(poll_id) == {"in": [], "out": []}
    assert len(db.poll_no_reply(poll_id)) == 3, "nobody has answered yet"

    db.set_poll_answer(poll_id, a, db.IN)
    db.set_poll_answer(poll_id, b, db.OUT)
    tally = db.poll_answers(poll_id)
    assert tally["in"] == ["Daven"], tally
    assert tally["out"] == ["Jeshua"], tally

    waiting = db.poll_no_reply(poll_id)
    assert [row["name"] for row in waiting] == ["Sarah"], "only Sarah is outstanding"
    return 4


def test_changing_your_mind_moves_you_and_never_double_counts() -> int:
    poll_id, a, _b, _c = _fresh()
    db.set_poll_answer(poll_id, a, db.IN)
    db.set_poll_answer(poll_id, a, db.OUT)
    tally = db.poll_answers(poll_id)
    assert tally["in"] == [], "the old answer must not linger"
    assert tally["out"] == ["Daven"], tally
    return 2


def test_only_a_recipient_can_answer() -> int:
    poll_id, *_ = _fresh()
    db.upsert_user(99, "gatecrasher", "Nobody", "#06-01A")
    # 99 was never sent this poll, so the write must not land. This is what
    # stops a forwarded card being used to vote.
    assert db.set_poll_answer(poll_id, 99, db.IN) is False
    assert db.poll_answers(poll_id) == {"in": [], "out": []}
    return 2


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_card_shows_names_and_counts_but_never_rooms() -> int:
    poll_id, a, b, _c = _fresh()
    db.set_poll_answer(poll_id, a, db.IN)
    db.set_poll_answer(poll_id, b, db.IN)
    card = poll_handler.render_card(poll_id)

    assert "Daven" in card and "Jeshua" in card, card
    assert "In (2)" in card, card
    assert "nobody yet" in card, "the empty side should say so, not be blank"
    # Names sit on their own line under each label, not beside it.
    assert "In (2):</b>\nDaven, Jeshua" in card, card
    assert "Can\'t (0):</b>\nnobody yet" in card, card
    for room in ("#08-27", "#08-25", "#06-27"):
        assert room not in card, f"a resident card must never leak {room}"
    return 4


def test_summary_is_the_leaders_view_and_does_carry_rooms() -> int:
    poll_id, a, _b, _c = _fresh()
    db.set_poll_answer(poll_id, a, db.IN)
    summary = poll_handler.render_summary(poll_id)

    assert "No reply (2)" in summary, summary
    # Rooms are wanted here: this message only goes to the poll's creator.
    assert "#08-25" in summary and "#06-27" in summary, summary
    assert "Sent to 3 resident(s)" in summary, summary
    return 3


def test_question_is_escaped_so_a_leader_cannot_break_the_card() -> int:
    db.init_db()
    conn = db.connection()
    for table in ("poll_recipients", "polls", "users"):
        conn.execute(f"DELETE FROM {table}")
    db.upsert_user(1, "boss", "Daven", "#08-27")
    poll_id = db.create_poll("<b>bold</b> & loud, who's in?", created_by=1)
    card = poll_handler.render_card(poll_id)
    assert "&lt;b&gt;bold&lt;/b&gt;" in card, card
    assert "&amp;" in card, card
    # Apostrophes must survive intact: "who's in?" is the single most likely
    # thing a leader types, and &#x27; would read back to them raw.
    assert "who's in?" in card, card
    return 3


def test_a_missing_poll_renders_as_none_rather_than_crashing() -> int:
    assert poll_handler.render_card(9999) is None
    assert poll_handler.render_summary(9999) is None
    return 2


def test_long_lists_are_truncated_not_dropped() -> int:
    names = [f"Person{i}" for i in range(70)]
    joined = poll_handler._names(names, limit=60)
    assert "Person0" in joined and "Person59" in joined
    assert "and 10 more" in joined, joined
    assert poll_handler._names([]) == "nobody yet"
    return 3


def main() -> None:
    tests = (
        test_answers_tally_by_side_and_track_who_has_not_replied,
        test_changing_your_mind_moves_you_and_never_double_counts,
        test_only_a_recipient_can_answer,
        test_card_shows_names_and_counts_but_never_rooms,
        test_summary_is_the_leaders_view_and_does_carry_rooms,
        test_question_is_escaped_so_a_leader_cannot_break_the_card,
        test_a_missing_poll_renders_as_none_rather_than_crashing,
        test_long_lists_are_truncated_not_dropped,
    )
    total_cases = 0
    try:
        for test in tests:
            total_cases += test()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)  # best-effort cleanup

    summary = f"{len(tests)}/{len(tests)} test functions, {total_cases} cases"
    print(f"PASS: {summary} — tests/test_poll.py")


if __name__ == "__main__":
    main()
