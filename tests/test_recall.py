"""Tests for recall: what a send writes down, and what taking it back deletes.

Run with: ./.venv/bin/python -m tests.test_recall
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

# --- must happen before any `bot.*` import below ---------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="noctua_test_recall_")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "test_recall.db")
os.environ["BOT_TOKEN"] = "test-dummy-token"
os.environ["LEADER_USERNAMES"] = "lead,other"
os.environ["TIMEZONE"] = "Asia/Singapore"

from telegram.error import BadRequest, Forbidden, NetworkError  # noqa: E402

from bot import db, keyboards, util  # noqa: E402
from bot.handlers import broadcast, recall  # noqa: E402

broadcast.SEND_DELAY_S = 0  # the rate-limit pause is not what these test
recall.DELETE_DELAY_S = 0

LEAD, OTHER, RESIDENT = 1, 2, 3


def _reset() -> None:
    db.init_db()
    conn = db.connection()
    for table in ("announcement_copies", "announcements", "users"):
        conn.execute(f"DELETE FROM {table}")
    db.upsert_user(LEAD, "lead", "Daven", "#08-27")
    db.upsert_user(OTHER, "other", "Lydia", "#08-01C")
    db.upsert_user(RESIDENT, "kai", "Kai Jin", "#07-25")


def _backdate(announcement_id: int, hours: float) -> None:
    """Move a send into the past, to reach the edges of the delete window."""
    when_sent = util.to_iso(util.now_utc() - timedelta(hours=hours))
    conn = db.connection()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE announcements SET sent_at = ? WHERE id = ?", (when_sent, announcement_id)
    )
    conn.commit()


class FakeBot:
    """Records deletes. ``missing``/``blocked``/``flaky`` are chat ids that fail."""

    def __init__(self, *, missing=(), blocked=(), flaky=()) -> None:
        self.copies: list[tuple[int, int]] = []
        self.deleted: list[tuple[int, int]] = []
        self.next_id = 900
        self._missing = set(missing)
        self._blocked = set(blocked)
        self._flaky = set(flaky)

    async def copy_message(self, chat_id: int, from_chat_id: int, message_id: int):
        self.copies.append((chat_id, message_id))
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id)

    async def delete_message(self, chat_id: int, message_id: int):
        if chat_id in self._missing:
            raise BadRequest("message to delete not found")
        if chat_id in self._blocked:
            raise Forbidden("bot was blocked by the user")
        if chat_id in self._flaky:
            raise NetworkError("connection reset")
        self.deleted.append((chat_id, message_id))


def _send(bot: FakeBot, draft: list[int], by: int = LEAD):
    return asyncio.run(broadcast.deliver(bot, from_chat_id=by, draft=draft, created_by=by))


# --------------------------------------------------------------------------
# what a send writes down
# --------------------------------------------------------------------------


def test_a_send_records_where_every_copy_landed() -> int:
    _reset()
    bot = FakeBot()
    report = _send(bot, [100, 101])

    # Three residents, two messages each. Without these rows the announcement
    # would be undeletable the moment copy_message returned.
    copies = db.live_copies(report.announcement_id)
    assert len(copies) == 6, copies
    assert {row["chat_id"] for row in copies} == {LEAD, OTHER, RESIDENT}
    # The ids stored are the ones Telegram handed back, not the draft's.
    assert {row["message_id"] for row in copies} == set(range(901, 907))
    assert report.sent == 3
    return 4


def test_a_resident_who_blocked_the_bot_leaves_no_copy_to_recall() -> int:
    _reset()

    class Blocked(FakeBot):
        async def copy_message(self, chat_id, from_chat_id, message_id):
            if chat_id == OTHER:
                raise Forbidden("bot was blocked by the user")
            return await super().copy_message(chat_id, from_chat_id, message_id)

    bot = Blocked()
    report = _send(bot, [100])
    chats = {row["chat_id"] for row in db.live_copies(report.announcement_id)}
    assert chats == {LEAD, RESIDENT}, chats
    assert report.unreachable == 1
    return 2


# --------------------------------------------------------------------------
# whose stack, and how far back
# --------------------------------------------------------------------------


def test_the_stack_is_your_own_announcements_newest_first() -> int:
    _reset()
    first = _send(FakeBot(), [100], by=LEAD).announcement_id
    second = _send(FakeBot(), [200, 201], by=LEAD).announcement_id
    theirs = _send(FakeBot(), [300], by=OTHER).announcement_id

    now = util.now_utc()
    mine = recall.stack(LEAD, now)
    assert [row["id"] for row in mine] == [second, first], "newest first"
    assert [row["id"] for row in recall.stack(OTHER, now)] == [theirs]
    # Undoing "the last announcement" must never reach somebody else's.
    assert theirs not in [row["id"] for row in mine]
    return 3


def test_the_counts_are_in_the_units_a_leader_thinks_in() -> int:
    _reset()
    _send(FakeBot(), [100], by=LEAD)
    _send(FakeBot(), [200, 201], by=LEAD)

    announcements, messages, latest = recall.totals(recall.stack(LEAD, util.now_utc()))
    # Three messages written, nine copies delivered. The leader wrote three.
    assert (announcements, messages, latest) == (2, 3, 2)
    return 1


def test_anything_past_the_window_is_not_offered() -> int:
    _reset()
    fresh = _send(FakeBot(), [100], by=LEAD).announcement_id
    stale = _send(FakeBot(), [200], by=LEAD).announcement_id
    _backdate(stale, recall.WINDOW_H + 1)

    ids = [row["id"] for row in recall.stack(LEAD, util.now_utc())]
    assert ids == [fresh], ids

    # Just inside it is still fair game, so the boundary is the stated one.
    _backdate(stale, recall.WINDOW_H - 1)
    assert stale in [row["id"] for row in recall.stack(LEAD, util.now_utc())]
    return 2


# --------------------------------------------------------------------------
# taking it back
# --------------------------------------------------------------------------


def test_recalling_everything_clears_the_stack() -> int:
    _reset()
    _send(FakeBot(), [100], by=LEAD)
    _send(FakeBot(), [200, 201], by=LEAD)

    bot = FakeBot()
    rows = recall.stack(LEAD, util.now_utc())
    result = asyncio.run(recall.delete_copies(bot, rows))

    assert result.announcements == 2
    assert result.deleted == 9, result  # (1 + 2) messages x 3 residents
    assert len(bot.deleted) == 9
    assert recall.stack(LEAD, util.now_utc()) == [], "nothing left to recall"
    return 4


def test_recalling_just_the_latest_leaves_the_one_before_it() -> int:
    _reset()
    first = _send(FakeBot(), [100], by=LEAD).announcement_id
    second = _send(FakeBot(), [200, 201], by=LEAD).announcement_id

    bot = FakeBot()
    top = recall.stack(LEAD, util.now_utc())[:1]
    result = asyncio.run(recall.delete_copies(bot, top))

    assert result.deleted == 6, result  # only the two-message one
    left = recall.stack(LEAD, util.now_utc())
    assert [row["id"] for row in left] == [first], left
    assert db.live_copies(second) == []
    # And tapping again walks one further back, which is the whole idea.
    asyncio.run(recall.delete_copies(FakeBot(), left))
    assert recall.stack(LEAD, util.now_utc()) == []
    return 4


def test_a_copy_the_resident_already_deleted_is_not_a_failure() -> int:
    _reset()
    report = _send(FakeBot(), [100], by=LEAD)
    bot = FakeBot(missing={RESIDENT})
    result = asyncio.run(recall.delete_copies(bot, recall.stack(LEAD, util.now_utc())))

    assert (result.deleted, result.missing, result.failed) == (2, 1, 0), result
    # Gone is gone, however it went: the announcement still closes.
    assert recall.stack(LEAD, util.now_utc()) == []
    assert db.live_copies(report.announcement_id) == []
    assert "already gone" in recall.outcome_text(result)
    return 4


def test_a_blocked_chat_is_admitted_to_rather_than_left_jamming_the_stack() -> int:
    _reset()
    _send(FakeBot(), [100], by=LEAD)
    bot = FakeBot(blocked={RESIDENT})
    result = asyncio.run(recall.delete_copies(bot, recall.stack(LEAD, util.now_utc())))

    assert (result.deleted, result.unreachable) == (2, 1), result
    # That copy can never be deleted, so retrying it forever would only keep
    # offering a recall that cannot finish.
    assert recall.stack(LEAD, util.now_utc()) == []
    assert "blocked the bot" in recall.outcome_text(result)
    return 3


def test_a_copy_telegram_balked_at_stays_recallable() -> int:
    _reset()
    _send(FakeBot(), [100], by=LEAD)
    flaky = FakeBot(flaky={RESIDENT})
    result = asyncio.run(recall.delete_copies(flaky, recall.stack(LEAD, util.now_utc())))

    assert (result.deleted, result.failed) == (2, 1), result
    assert "again to retry" in recall.outcome_text(result)

    # Still on the stack, holding exactly the copy that did not go.
    left = recall.stack(LEAD, util.now_utc())
    assert len(left) == 1 and left[0]["live"] == 1, left

    # A second pass on a working connection finishes the job, and does not
    # re-delete the two that already went.
    retry = FakeBot()
    again = asyncio.run(recall.delete_copies(retry, left))
    assert again.deleted == 1 and len(retry.deleted) == 1, again
    assert recall.stack(LEAD, util.now_utc()) == []
    return 6


# --------------------------------------------------------------------------
# the card
# --------------------------------------------------------------------------


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_one_announcement_gets_one_button_not_two_ways_to_say_it() -> int:
    _reset()
    _send(FakeBot(), [100, 101], by=LEAD)
    now = util.now_utc()
    text, markup = recall.card(recall.stack(LEAD, now), now)

    labels = _labels(markup)
    assert labels == ["♻️ Recall it (2 messages)", "❌ Cancel"], labels
    assert "6</b> copies" in text, text  # what it will actually delete
    assert "already read it" in text, "the warning has to be on the card"
    return 3


def test_several_announcements_offer_all_or_just_the_latest() -> int:
    _reset()
    _send(FakeBot(), [100], by=LEAD)
    _send(FakeBot(), [200, 201], by=LEAD)
    now = util.now_utc()
    labels = _labels(recall.card(recall.stack(LEAD, now), now)[1])

    assert labels[0] == "♻️ Recall all 2 announcements (3 messages)", labels
    assert labels[1] == "♻️ Just the latest (2 messages)", labels
    return 2


def test_an_empty_stack_says_so_and_offers_no_buttons() -> int:
    _reset()
    now = util.now_utc()
    text, markup = recall.card([], now)
    assert markup is None, "nothing to press when there is nothing to undo"
    assert "Nothing to recall" in text
    assert str(recall.WINDOW_H) in text, "say why, not just no"

    # After a recall that emptied the stack, the receipt leads and the card
    # closes off rather than re-asking.
    done, markup = recall.card([], now, heading="✅ <b>Recalled 1 announcement.</b>")
    assert markup is None and recall.DONE_TAIL in done, done
    return 4


def test_recalling_one_of_several_re_asks_with_the_receipt_on_top() -> int:
    _reset()
    _send(FakeBot(), [100], by=LEAD)
    _send(FakeBot(), [200, 201], by=LEAD)

    top = recall.stack(LEAD, util.now_utc())[:1]
    result = asyncio.run(recall.delete_copies(FakeBot(), top))
    now = util.now_utc()
    text, markup = recall.card(
        recall.stack(LEAD, now), now, heading=recall.outcome_text(result)
    )

    assert "Recalled 1 announcement" in text, text
    assert "Recall the next one?" in text, text
    assert markup is not None and _labels(markup)[0] == "♻️ Recall it (1 message)"
    return 3


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def _captions(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.keyboard]


def test_leaders_get_the_button_and_residents_do_not() -> int:
    resident = _captions(keyboards.main_menu(is_leader=False))
    leader = _captions(keyboards.main_menu(is_leader=True))
    assert keyboards.MENU_RECALL in [c for row in leader for c in row], leader
    assert keyboards.MENU_RECALL not in [c for row in resident for c in row], resident
    # It must not sit next to Send, where a slip would be expensive.
    announce_row = next(row for row in leader if keyboards.MENU_ANNOUNCE in row)
    assert keyboards.MENU_RECALL not in announce_row, announce_row
    return 3


def main() -> None:
    tests = (
        test_a_send_records_where_every_copy_landed,
        test_a_resident_who_blocked_the_bot_leaves_no_copy_to_recall,
        test_the_stack_is_your_own_announcements_newest_first,
        test_the_counts_are_in_the_units_a_leader_thinks_in,
        test_anything_past_the_window_is_not_offered,
        test_recalling_everything_clears_the_stack,
        test_recalling_just_the_latest_leaves_the_one_before_it,
        test_a_copy_the_resident_already_deleted_is_not_a_failure,
        test_a_blocked_chat_is_admitted_to_rather_than_left_jamming_the_stack,
        test_a_copy_telegram_balked_at_stays_recallable,
        test_one_announcement_gets_one_button_not_two_ways_to_say_it,
        test_several_announcements_offer_all_or_just_the_latest,
        test_an_empty_stack_says_so_and_offers_no_buttons,
        test_recalling_one_of_several_re_asks_with_the_receipt_on_top,
        test_leaders_get_the_button_and_residents_do_not,
    )
    total_cases = 0
    try:
        for test in tests:
            total_cases += test()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)  # best-effort cleanup

    summary = f"{len(tests)}/{len(tests)} test functions, {total_cases} cases"
    print(f"PASS: {summary} — tests/test_recall.py")


if __name__ == "__main__":
    main()
