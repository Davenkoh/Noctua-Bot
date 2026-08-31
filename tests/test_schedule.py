"""Tests for scheduled announcements: the clock, the claim, and the restart.

Run with: ./.venv/bin/python -m tests.test_schedule
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

# --- must happen before any `bot.*` import below ---------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="noctua_test_schedule_")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "test_schedule.db")
os.environ["BOT_TOKEN"] = "test-dummy-token"
os.environ["LEADER_USERNAMES"] = "lead"
os.environ["TIMEZONE"] = "Asia/Singapore"

from telegram.error import BadRequest, Forbidden  # noqa: E402

from bot import db, keyboards, util, when  # noqa: E402
from bot.handlers import broadcast  # noqa: E402

SG = ZoneInfo("Asia/Singapore")
MONDAY = datetime(2026, 8, 31, 14, 5, tzinfo=SG)  # a Monday afternoon

broadcast.SEND_DELAY_S = 0  # the rate-limit pause is not what these test


def _reset() -> None:
    db.init_db()
    conn = db.connection()
    conn.execute("DELETE FROM scheduled_announcements")
    conn.execute("DELETE FROM announcement_copies")
    conn.execute("DELETE FROM announcements")
    conn.execute("DELETE FROM users")
    db.upsert_user(1, "lead", "Daven", "#08-27")
    db.upsert_user(2, "lydia", "Lydia", "#08-01C")
    db.upsert_user(3, "kai", "Kai Jin", "#07-25")


# --------------------------------------------------------------------------
# reading a typed time
# --------------------------------------------------------------------------


def test_the_shapes_a_leader_actually_types_all_resolve() -> int:
    cases = {
        # bare clock, in every spelling a notice gets written in
        "18:30": "Mon 2026-08-31 18:30",
        "6:30pm": "Mon 2026-08-31 18:30",
        "6.30pm": "Mon 2026-08-31 18:30",
        "6pm": "Mon 2026-08-31 18:00",
        "1830": "Mon 2026-08-31 18:30",
        # a clock that has passed today means tomorrow, never the past
        "9am": "Tue 2026-09-01 09:00",
        "12am": "Tue 2026-09-01 00:00",
        "12pm": "Tue 2026-09-01 12:00",
        # day words
        "tomorrow 9am": "Tue 2026-09-01 09:00",
        "tmr 9am": "Tue 2026-09-01 09:00",
        "9am tomorrow": "Tue 2026-09-01 09:00",
        "tonight 8pm": "Mon 2026-08-31 20:00",
        "today 3pm": "Mon 2026-08-31 15:00",
        # weekdays, and the filler a human puts around them
        "wed 9am": "Wed 2026-09-02 09:00",
        "on saturday at 10am": "Sat 2026-09-05 10:00",
        "friday, 9am": "Fri 2026-09-04 09:00",
        "next friday 9am": "Fri 2026-09-04 09:00",
        # dates
        "1 sep 9am": "Tue 2026-09-01 09:00",
        "sep 1 9am": "Tue 2026-09-01 09:00",
        "sept 5 2pm": "Sat 2026-09-05 14:00",
        "1/9 0900": "Tue 2026-09-01 09:00",
        "2026-09-01 09:00": "Tue 2026-09-01 09:00",
        # relative
        "in 30 min": "Mon 2026-08-31 14:35",
        "in 90 minutes": "Mon 2026-08-31 15:35",
        "in 2h": "Mon 2026-08-31 16:05",
        "in 1 hour 30 mins": "Mon 2026-08-31 15:35",
        "in 3 days": "Thu 2026-09-03 14:05",
    }
    for text, expected in cases.items():
        got = when.parse(text, MONDAY)
        assert got is not None, f"{text!r} did not parse"
        assert got.strftime("%a %Y-%m-%d %H:%M") == expected, f"{text!r} -> {got}"
    return len(cases)


def test_a_weekday_always_means_the_coming_one() -> int:
    # Typed on a Monday, "mon" is next Monday. Six hours from now would be a
    # very expensive thing to guess wrong, and the confirm card spells the
    # date out either way.
    got = when.parse("mon 6:30pm", MONDAY)
    assert got.strftime("%a %Y-%m-%d %H:%M") == "Mon 2026-09-07 18:30", got
    return 1


def test_a_date_with_no_year_rolls_forward_rather_than_landing_in_the_past() -> int:
    september = datetime(2026, 9, 15, 9, 0, tzinfo=SG)
    assert when.parse("1 sep 9am", september).year == 2027
    assert when.parse("20 sep 9am", september).year == 2026
    return 2


def test_anything_it_cannot_read_is_refused_rather_than_guessed() -> int:
    # The dangerous failure is not "I don't understand", it is quietly
    # dropping the word that carried the meaning.
    refused = (
        "",                 # nothing at all
        "banana",           # not a time
        "9am blah",         # a leftover word could have been the day
        "banana 9am",
        "tomorrow",         # a day with no clock: breakfast, or midnight?
        "next week",
        "25:00",            # not a clock
        "31 feb 9am",       # not a date
        "in 30",            # 30 what?
        "in 0 min",
        "in 2 fortnights",
    )
    for text in refused:
        assert when.parse(text, MONDAY) is None, f"{text!r} should not have parsed"

    # And the word that carries the meaning survives the words around it.
    assert when.parse("please friday 9am", MONDAY).strftime("%a") == "Fri"
    return len(refused) + 1


def test_the_confirmation_line_always_names_the_day() -> int:
    now = MONDAY
    assert when.fmt_full(when.parse("6pm", now), now) == "Today (Mon 31 Aug), 6:00 PM"
    assert when.fmt_full(when.parse("9am", now), now) == "Tomorrow (Tue 1 Sep), 9:00 AM"
    assert when.fmt_full(when.parse("sep 5 2pm", now), now) == "Sat 5 Sep, 2:00 PM"
    # A year away is the shape of a typo, so the year is shown.
    assert "2027" in when.fmt_full(datetime(2027, 1, 2, 9, 0, tzinfo=SG), now)

    assert when.fmt_lead(when.parse("in 30 min", now), now) == "in 30 minutes"
    assert when.fmt_lead(when.parse("in 2h", now), now) == "in about 2 hours"
    assert when.fmt_lead(when.parse("sep 5 2pm", now), now) == "in about 5 days"
    return 7


# --------------------------------------------------------------------------
# the quick-time buttons
# --------------------------------------------------------------------------


def test_a_quick_time_button_means_exactly_what_it_says() -> int:
    options = dict(broadcast.presets(MONDAY))
    assert "3:05 PM" in options["1h"], options["1h"]
    assert "8:00 PM" in options["eve"], options["eve"]
    assert "9:00 AM" in options["am"], options["am"]

    # The label and the tap come from one function, so they cannot drift.
    assert broadcast.preset_moment("1h", MONDAY) == MONDAY + timedelta(hours=1)
    assert broadcast.preset_moment("am", MONDAY).hour == broadcast.MORNING_HOUR
    return 5


def test_tonight_stops_being_offered_once_tonight_has_passed() -> int:
    late = MONDAY.replace(hour=21, minute=30)
    assert broadcast.preset_moment("eve", late) is None
    assert "eve" not in dict(broadcast.presets(late)), "a button that cannot fire"
    # Tomorrow morning is still perfectly good at half nine at night.
    assert "am" in dict(broadcast.presets(late))
    return 3


def test_a_time_in_the_past_or_too_far_out_is_named_as_such() -> int:
    assert broadcast._problem(MONDAY - timedelta(hours=1), MONDAY) == broadcast.IN_THE_PAST
    far = MONDAY + timedelta(days=when.MAX_DAYS_AHEAD + 1)
    assert broadcast._problem(far, MONDAY) == broadcast.TOO_FAR
    assert broadcast._problem(MONDAY + timedelta(hours=1), MONDAY) is None
    return 3


# --------------------------------------------------------------------------
# the database: an announcement goes out once, or not at all
# --------------------------------------------------------------------------


def test_an_announcement_can_only_be_claimed_once() -> int:
    _reset()
    announcement_id = db.schedule_announcement(1, 1, [10, 11, 12], util.now_utc())
    row = db.get_announcement(announcement_id)
    assert db.draft_ids(row) == [10, 11, 12], "draft order is the send order"
    assert len(db.pending_announcements()) == 1

    # A duplicate timer, or a restore racing the job it is restoring, must not
    # broadcast the same messages to the whole dorm twice.
    assert db.claim_announcement(announcement_id) is not None
    assert db.claim_announcement(announcement_id) is None
    assert db.get_announcement(announcement_id)["status"] == db.SENT
    assert db.pending_announcements() == []
    return 6


def test_cancelling_and_sending_cannot_both_win() -> int:
    _reset()
    first = db.schedule_announcement(1, 1, [10], util.now_utc())
    assert db.settle_announcement(first, db.CANCELLED, 1) is True
    assert db.claim_announcement(first) is None, "a cancelled one must never fire"

    second = db.schedule_announcement(1, 1, [20], util.now_utc())
    assert db.claim_announcement(second) is not None
    assert db.settle_announcement(second, db.CANCELLED, 1) is False, "already gone out"
    assert db.get_announcement(second)["status"] == db.SENT
    return 5


# --------------------------------------------------------------------------
# the fan-out, shared by ✅ Send and by the timer
# --------------------------------------------------------------------------


class FakeBot:
    """Just enough Bot to watch what a fan-out actually does."""

    def __init__(self, forbidden: set[int] = frozenset(), broken: set[int] = frozenset()):
        self.copies: list[tuple[int, int]] = []
        self.sent: list[tuple[int, str]] = []
        self.next_id = 500
        self._forbidden = forbidden
        self._broken = broken

    async def copy_message(self, chat_id: int, from_chat_id: int, message_id: int):
        if chat_id in self._forbidden:
            raise Forbidden("bot was blocked by the user")
        if message_id in self._broken:
            raise BadRequest("message to copy not found")
        self.copies.append((chat_id, message_id))
        # deliver() records what this returns, which is the whole basis of
        # recall — a fake that returned None would pass here and lose every
        # copy's address in the process.
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id)

    async def send_message(self, chat_id: int, text: str, reply_markup=None):
        self.sent.append((chat_id, text))
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id, chat_id=chat_id)

    async def delete_message(self, chat_id: int, message_id: int):
        pass


def test_every_resident_gets_the_whole_draft_in_order() -> int:
    _reset()
    bot = FakeBot()
    report = asyncio.run(broadcast.deliver(bot, from_chat_id=1, draft=[100, 101], created_by=1))
    assert report.sent == 3 and report.total == 3, report
    assert bot.copies == [(1, 100), (1, 101), (2, 100), (2, 101), (3, 100), (3, 101)]
    return 3


def test_one_blocked_resident_does_not_stop_the_rest() -> int:
    _reset()
    bot = FakeBot(forbidden={2})
    report = asyncio.run(broadcast.deliver(bot, from_chat_id=1, draft=[100], created_by=1))
    assert (report.sent, report.unreachable) == (2, 1), report
    assert (2, 100) not in bot.copies
    assert "1 unreachable" in broadcast.report_text(report)
    return 4


def test_a_draft_message_deleted_before_it_sends_is_skipped_for_everyone() -> int:
    _reset()
    # This is the cost of never copying the draft until send time, and the
    # report has to own up to it rather than quietly sending less.
    bot = FakeBot(broken={101})
    report = asyncio.run(broadcast.deliver(bot, from_chat_id=1, draft=[100, 101, 102], created_by=1))
    assert report.sent == 3 and report.broken == 1, report
    assert [m for _chat, m in bot.copies] == [100, 102] * 3, bot.copies
    assert "1 draft message(s) couldn't be copied" in broadcast.report_text(report)
    return 4


def test_a_resident_who_registers_after_scheduling_still_gets_it() -> int:
    _reset()
    announcement_id = db.schedule_announcement(1, 1, [100], util.now_utc())
    db.upsert_user(4, "latecomer", "Newbie", "#06-02")

    bot = FakeBot()
    row = db.claim_announcement(announcement_id)
    report = asyncio.run(broadcast.deliver(bot, row["chat_id"], db.draft_ids(row), created_by=row["created_by"]))
    assert report.total == 4, "the audience is read at send time, not at compose time"
    assert (4, 100) in bot.copies
    return 2


# --------------------------------------------------------------------------
# surviving a restart
# --------------------------------------------------------------------------


class FakeJobQueue:
    def __init__(self):
        self.jobs: list[SimpleNamespace] = []

    def get_jobs_by_name(self, name: str):
        return [job for job in self.jobs if job.name == name]

    def run_once(self, callback, when, name, data):
        job = SimpleNamespace(name=name, delay=when, data=data)
        job.schedule_removal = lambda: self.jobs.remove(job)
        self.jobs.append(job)


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()
        self.job_queue = FakeJobQueue()


def test_a_restart_re_arms_what_is_still_due() -> int:
    _reset()
    soon = util.now_utc() + timedelta(hours=2)
    announcement_id = db.schedule_announcement(1, 1, [100], soon)

    app = FakeApp()
    armed, missed = asyncio.run(broadcast.restore_scheduled(app))
    assert (armed, missed) == (1, 0)
    assert app.job_queue.jobs[0].name == broadcast.job_name(announcement_id)
    assert 7000 < app.job_queue.jobs[0].delay <= 7200, "roughly two hours out"
    assert db.get_announcement(announcement_id)["status"] == db.PENDING
    return 4


def test_a_restart_still_sends_one_the_bot_only_just_missed() -> int:
    _reset()
    # A deploy takes seconds. An announcement due during it is still the
    # announcement the leader wanted, so it fires as soon as the bot is up.
    just_missed = util.now_utc() - timedelta(minutes=broadcast.LATE_GRACE_MIN - 5)
    announcement_id = db.schedule_announcement(1, 1, [100], just_missed)

    app = FakeApp()
    armed, missed = asyncio.run(broadcast.restore_scheduled(app))
    assert (armed, missed) == (1, 0)
    assert app.job_queue.jobs[0].delay == 1.0, "overdue fires almost immediately"
    assert db.get_announcement(announcement_id)["status"] == db.PENDING
    assert app.bot.sent == [], "nothing to apologise for yet"
    return 4


def test_a_long_outage_writes_the_announcement_off_and_says_so() -> int:
    _reset()
    stale = util.now_utc() - timedelta(hours=6)
    announcement_id = db.schedule_announcement(1, 1, [100], stale)

    app = FakeApp()
    armed, missed = asyncio.run(broadcast.restore_scheduled(app))
    assert (armed, missed) == (0, 1)
    assert app.job_queue.jobs == [], "six hours late is not worth sending"
    assert db.get_announcement(announcement_id)["status"] == db.MISSED

    chat_id, text = app.bot.sent[0]
    assert chat_id == 1, "the leader who wrote it hears about it"
    assert "did not go out" in text, text

    # And a second restart must not apologise all over again.
    app = FakeApp()
    assert asyncio.run(broadcast.restore_scheduled(app)) == (0, 0)
    assert app.bot.sent == []
    return 7


# --------------------------------------------------------------------------
# what the leader sees
# --------------------------------------------------------------------------


class FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.answers: list[str | None] = []
        self.edits: list[str] = []
        self.message = SimpleNamespace(message_id=None, chat_id=1)

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append(text)


def _tap(data: str, context) -> SimpleNamespace:
    return SimpleNamespace(
        callback_query=FakeQuery(data),
        effective_user=SimpleNamespace(id=1, username="lead", first_name="Daven"),
        effective_chat=SimpleNamespace(id=1),
        effective_message=None,
    )


def test_a_broken_timer_is_admitted_to_rather_than_papered_over() -> int:
    _reset()
    # No JobQueue means nothing can ever fire. Storing the announcement and
    # reporting "✅ Scheduled" would leave a leader believing the dorm will be
    # told something it will never be told.
    bot = FakeBot()
    context = SimpleNamespace(
        bot=bot,
        job_queue=None,
        user_data={
            broadcast.DRAFT_KEY: [100],
            broadcast.CHAT_KEY: 1,
            broadcast.WHEN_KEY: util.to_local(util.now_utc()) + timedelta(hours=2),
        },
    )
    update = _tap("bc:ok", context)
    state = asyncio.run(broadcast.cb_confirm(update, context))

    assert state == broadcast.SCHEDULING, "the leader stays on the time step"
    posted = "\n".join(text for _chat, text in bot.sent)
    assert "can't set timers" in posted, posted
    assert "Scheduled" not in posted, "no receipt for something that will not happen"
    assert db.pending_announcements() == [], "and nothing left pending to inherit"
    return 4


def test_the_waiting_list_says_when_what_and_who() -> int:
    _reset()
    announcement_id = db.schedule_announcement(
        1, 1, [100, 101], util.now_utc() + timedelta(hours=3)
    )
    line = broadcast.describe(db.get_announcement(announcement_id), util.now_utc())
    assert "2 message(s)" in line, line
    assert "@lead" in line, line
    assert "in about 3 hours" in line, line
    return 3


def test_the_composer_offers_scheduling_and_every_step_offers_a_way_out() -> int:
    composer = keyboards.composer_keyboard(118).inline_keyboard
    data = [button.callback_data for row in composer for button in row]
    assert data == ["bc:send", "bc:when", "bc:prev", "bc:undo", "bc:cancel"], data
    # Send and Schedule are the two irreversible ones, so neither shares a row.
    assert [len(row) for row in composer] == [1, 1, 2, 1]

    when_step = keyboards.schedule_when_keyboard(
        [("1h", "in 1 hour"), ("am", "tomorrow")], "202608"
    ).inline_keyboard
    data = [button.callback_data for row in when_step for button in row]
    assert data == [
        "bc:at:1h", "bc:at:am", "bc:cal:202608", "bc:redo", "bc:cancel"
    ], data

    confirm = keyboards.schedule_confirm_keyboard().inline_keyboard
    data = [button.callback_data for row in confirm for button in row]
    assert data == ["bc:ok", "bc:when", "bc:cancel"], data

    assert keyboards.scheduled_keyboard(9).inline_keyboard[0][0].callback_data == "bcx:9"
    return 5


def test_arriving_through_the_scheduled_door_leads_with_the_timed_exit() -> int:
    front = keyboards.composer_keyboard(118, schedule_first=True).inline_keyboard
    data = [button.callback_data for row in front for button in row]
    assert data == ["bc:when", "bc:send", "bc:prev", "bc:undo", "bc:cancel"], data
    # Send now survives the reorder: changing your mind is not a reason to
    # rewrite the draft.
    assert front[1][0].callback_data == "bc:send"

    # And the button that opens that door is on the leaders' own menu. There
    # is one announcement button now, and its caption promises both timings.
    rows = [[button.text for button in row] for row in keyboards.main_menu(True).keyboard]
    assert ["📢 Announce (now or scheduled)"] in rows, rows
    resident = [
        [button.text for button in row] for row in keyboards.main_menu(False).keyboard
    ]
    assert resident == [["🧺 Laundry menu"], ["👤 Profile", "❓ Help"]], resident
    return 4


def _grid(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _live(markup) -> list[str]:
    """Callback data of every button that actually does something."""
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data != "noop"
    ]


def test_the_calendar_never_offers_a_day_that_has_gone() -> int:
    from datetime import date

    august = keyboards.calendar_keyboard(
        date(2026, 8, 1), earliest=MONDAY, latest=date(2026, 10, 30)
    )
    days = [d for d in _live(august) if d.startswith("bc:d:")]
    # MONDAY is the 31st, so August has exactly one day left in it.
    assert days == ["bc:d:20260831"], days
    assert "August 2026" in " ".join(_grid(august)[0]), _grid(august)[0]

    september = keyboards.calendar_keyboard(
        date(2026, 9, 1), earliest=MONDAY, latest=date(2026, 10, 30)
    )
    days = [d for d in _live(september) if d.startswith("bc:d:")]
    assert len(days) == 30, f"all of September is ahead, got {len(days)}"
    assert days[0] == "bc:d:20260901"
    return 5


def test_the_calendar_arrows_stop_at_the_ends_of_the_range() -> int:
    from datetime import date

    # August is the current month, so there is no way back from it.
    august = _live(
        keyboards.calendar_keyboard(
            date(2026, 8, 1), earliest=MONDAY, latest=date(2026, 10, 30)
        )
    )
    assert "bc:cal:202607" not in august, "cannot page into a month already gone"
    assert "bc:cal:202609" in august

    # October holds the last schedulable day, so forward stops there.
    october = _live(
        keyboards.calendar_keyboard(
            date(2026, 10, 1), earliest=MONDAY, latest=date(2026, 10, 30)
        )
    )
    assert "bc:cal:202609" in october
    assert "bc:cal:202611" not in october, "cannot page past the limit"
    return 4


def test_the_hour_and_minute_grids_hide_what_has_already_passed() -> int:
    from datetime import date

    # MONDAY is 14:05, so today starts at 14 and 14:00 is gone.
    today = _live(keyboards.hours_keyboard(date(2026, 8, 31), earliest=MONDAY))
    hours = [h.split(":")[-1] for h in today if h.startswith("bc:h:")]
    assert hours == [f"{h:02d}" for h in range(14, 24)], hours

    minutes = _live(
        keyboards.minutes_keyboard(date(2026, 8, 31), 14, earliest=MONDAY)
    )
    slots = [m.split(":")[-1] for m in minutes if m.startswith("bc:m:")]
    assert slots[0] == "1410", slots[:3]
    assert "1400" not in slots and "1405" not in slots, "14:05 is now, not later"

    # A day that is wholly ahead offers the entire clock.
    tomorrow = _live(keyboards.hours_keyboard(date(2026, 9, 1), earliest=MONDAY))
    assert len([h for h in tomorrow if h.startswith("bc:h:")]) == 24
    return 5


def test_the_three_taps_add_up_to_the_moment_they_showed() -> int:
    from datetime import date

    # The grids hand the next step everything it needs, so the last tap alone
    # says which moment was chosen. This is the whole contract between them.
    day = [
        d for d in _live(
            keyboards.calendar_keyboard(
                date(2026, 9, 1), earliest=MONDAY, latest=date(2026, 10, 30)
            )
        ) if d.endswith("20260903")
    ][0]
    assert day == "bc:d:20260903"

    hour = [
        h for h in _live(keyboards.hours_keyboard(date(2026, 9, 3), earliest=MONDAY))
        if h.endswith(":18")
    ][0]
    assert hour == "bc:h:20260903:18"

    minute = [
        m for m in _live(
            keyboards.minutes_keyboard(date(2026, 9, 3), 18, earliest=MONDAY)
        ) if m.endswith("1830")
    ][0]
    assert minute == "bc:m:20260903:1830"

    # And every one of those is a shape the router will actually route.
    import re

    assert re.match(keyboards.PAT_BC_DAY, day)
    assert re.match(keyboards.PAT_BC_HOUR, hour)
    assert re.match(keyboards.PAT_BC_MIN, minute)
    return 6


def test_every_dead_button_in_a_grid_is_answerable() -> int:
    from datetime import date

    # A callback with no handler spins in the client until it times out, which
    # reads as a crash. The grids are full of inert buttons, so `noop` has to
    # be a handler, not a convention.
    from telegram.ext import CallbackQueryHandler
    import re

    handlers = broadcast.broadcast_handler().states[broadcast.SCHEDULING]
    patterns = [
        h.pattern.pattern for h in handlers if isinstance(h, CallbackQueryHandler)
    ]
    assert keyboards.PAT_NOOP in patterns, patterns

    grids = (
        keyboards.calendar_keyboard(
            date(2026, 8, 1), earliest=MONDAY, latest=date(2026, 10, 30)
        ),
        keyboards.hours_keyboard(date(2026, 8, 31), earliest=MONDAY),
        keyboards.minutes_keyboard(date(2026, 8, 31), 14, earliest=MONDAY),
    )
    for markup in grids:
        for row in markup.inline_keyboard:
            for button in row:
                assert any(
                    re.match(pattern, button.callback_data) for pattern in patterns
                ), f"{button.callback_data} has no handler"
    return 4


def test_picking_a_time_does_not_swallow_messages_into_the_draft() -> int:
    from telegram.ext import MessageHandler

    states = broadcast.broadcast_handler().states
    composing = [h for h in states[broadcast.COMPOSING] if isinstance(h, MessageHandler)]
    scheduling = [h for h in states[broadcast.SCHEDULING] if isinstance(h, MessageHandler)]
    # One message handler each, and they are different: a message typed while
    # the bot is asking for a time must be read as a time, never appended to
    # an announcement the leader has already finished writing.
    assert len(composing) == 1 and composing[0].callback is broadcast.collect
    assert len(scheduling) == 1 and scheduling[0].callback is broadcast.collect_time
    return 4


def main() -> None:
    tests = (
        test_the_shapes_a_leader_actually_types_all_resolve,
        test_a_weekday_always_means_the_coming_one,
        test_a_date_with_no_year_rolls_forward_rather_than_landing_in_the_past,
        test_anything_it_cannot_read_is_refused_rather_than_guessed,
        test_the_confirmation_line_always_names_the_day,
        test_a_quick_time_button_means_exactly_what_it_says,
        test_tonight_stops_being_offered_once_tonight_has_passed,
        test_a_time_in_the_past_or_too_far_out_is_named_as_such,
        test_an_announcement_can_only_be_claimed_once,
        test_cancelling_and_sending_cannot_both_win,
        test_every_resident_gets_the_whole_draft_in_order,
        test_one_blocked_resident_does_not_stop_the_rest,
        test_a_draft_message_deleted_before_it_sends_is_skipped_for_everyone,
        test_a_resident_who_registers_after_scheduling_still_gets_it,
        test_a_restart_re_arms_what_is_still_due,
        test_a_restart_still_sends_one_the_bot_only_just_missed,
        test_a_long_outage_writes_the_announcement_off_and_says_so,
        test_a_broken_timer_is_admitted_to_rather_than_papered_over,
        test_the_waiting_list_says_when_what_and_who,
        test_the_composer_offers_scheduling_and_every_step_offers_a_way_out,
        test_arriving_through_the_scheduled_door_leads_with_the_timed_exit,
        test_the_calendar_never_offers_a_day_that_has_gone,
        test_the_calendar_arrows_stop_at_the_ends_of_the_range,
        test_the_hour_and_minute_grids_hide_what_has_already_passed,
        test_the_three_taps_add_up_to_the_moment_they_showed,
        test_every_dead_button_in_a_grid_is_answerable,
        test_picking_a_time_does_not_swallow_messages_into_the_draft,
    )
    total_cases = 0
    try:
        for test in tests:
            total_cases += test()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)  # best-effort cleanup

    summary = f"{len(tests)}/{len(tests)} test functions, {total_cases} cases"
    print(f"PASS: {summary} — tests/test_schedule.py")


if __name__ == "__main__":
    main()
