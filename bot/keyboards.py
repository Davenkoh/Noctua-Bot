"""Keyboards and the single source of truth for Noctua's callback data.

Callback-data grammar
=====================

Fields are ``:``-separated ASCII, always well under Telegram's 64-byte limit.
``<mid>`` is a machine id (``w1``/``w2``/``d1``/``d2``), ``<sid>`` a
``sessions.id``, ``<min>`` whole minutes, ``<L>`` a suite unit letter A-F.

===========================  ==================================================
``noop``                     inert button: a day already past, a grid
                             heading, an arrow with nowhere to go
``hub``                      render the laundry hub (start / status / ping)
``menu``                     render the machine list ("Start a machine")
``m:<mid>``                  render the machine detail view
``dur:<mid>:<min>``          duration tapped — starts a cycle, or opens the
                             "paid twice" confirm when the tapper already owns
                             the active session
``extc:<sid>:<min>``         confirmed extension: add <min> to session <sid>
``stop:<sid>``               ask to free own running machine early ("collected")
``stopc:<sid>``              confirmed early finish
``ping:<mid>``               nudge whoever used the free machine <mid> last
``pingpick``                 render the nudge picker (free machines with a load)
``reset``                    leaders: ask to reset every machine to 🟢 free
``resetok``                  leaders: confirmed reset
``st``                       render the status board
``st:refresh``               re-render the status board in place
``sl:<L>``                   suite unit letter (room-entry conversations)
``reg:ok`` / ``reg:redo``    onboarding: save / start over — on a roster-
                             prefilled confirm the same two mean "That's me" /
                             "Fix name" (only the name is re-asked)
``bc:send`` / ``bc:cancel``  announcement composer: fan out now / abort
``bc:prev`` / ``bc:undo``    announcement composer: preview / drop last message
``bc:when``                  announcement composer: open "send it later"
``bc:cal:<YYYYMM>``          date picker: draw that month
``bc:d:<YYYYMMDD>``          date picker: day chosen, ask for the hour
``bc:h:<YYYYMMDD>:<HH>``     date picker: hour chosen, ask for the minutes
``bc:m:<YYYYMMDD>:<HHMM>``   date picker: the whole moment, go to confirm
``bc:at:<key>``              a quick time button (``1h``/``3h``/``eve``/``am``)
``bc:ok`` / ``bc:redo``      confirm the parsed time / pick a different one
``bcx:<aid>``                cancel scheduled announcement <aid>, from any
                             message still carrying the button
``pl:in:<pid>``              count me in on poll <pid>
``pl:out:<pid>``             can't make it on poll <pid>
``pl:re:<pid>``              refresh my copy of poll <pid>'s card
``pl:send`` / ``pl:cancel``  poll composer: fan out / abort
``rc:all``                   recall every announcement still in reach
``rc:one``                   recall only the most recent one
``rc:no``                    close the recall card, delete nothing
===========================  ==================================================

Every "No"/"Cancel" button on a machine dialog reuses ``m:<mid>``, so backing
out always lands on the machine view it came from.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from . import config, util

# --- reply-keyboard labels (handlers match these exactly) -----------------
MENU_LAUNDRY = "🧺 Laundry menu"
MENU_PROFILE = "👤 Profile"
MENU_HELP = "❓ Help"
MENU_ANNOUNCE = "📢 Announce (now or scheduled)"
MENU_POLL = "📋 Poll"
MENU_RECALL = "♻️ Recall"

# v1 labels: reply keyboards live in the client until the resident triggers a
# new one, so the old captions must keep routing somewhere sensible.
LEGACY_USE = "🧺 Use a machine"
LEGACY_LAUNDRY = "🧺 Laundry"
LEGACY_STATUS = "📊 Status"
LEGACY_PROFILE = "👤 My profile"
LEGACY_BROADCAST = "📢 Broadcast"
LEGACY_ANNOUNCE = "📢 Announce"
# The second announcement door, retired once 📢 Announce started saying it
# covers both. The caption still routes to the timed entry, so a leader whose
# keyboard predates the merge gets what the button promised them.
LEGACY_SCHEDULE = "📅 Scheduled Announce"

# Receiving any of these is proof the sender's reply keyboard predates the
# current one — Telegram caches it client-side until the bot replaces it, so
# a resident who hasn't run /start since a rename taps the old caption forever.
LEGACY_CAPTIONS = frozenset(
    {
        LEGACY_USE,
        LEGACY_LAUNDRY,
        LEGACY_STATUS,
        LEGACY_PROFILE,
        LEGACY_BROADCAST,
        LEGACY_ANNOUNCE,
        LEGACY_SCHEDULE,
    }
)

# --- inline labels shared across keyboards --------------------------------
# "Nudge" everywhere a resident can see: "ping" survives only in callback data
# and the /ping command, where nobody reads it.
HUB_PING = "🔔 Nudge last user"
HUB_RESET = "🔄 Reset all machines"
BACK_HUB = "⬅️ Laundry menu"


def _exact(*labels: str) -> str:
    """Regex matching any of ``labels`` exactly (reply-button captions)."""
    return "^(?:" + "|".join(re.escape(label) for label in labels) + ")$"


RX_LAUNDRY = _exact(MENU_LAUNDRY, LEGACY_LAUNDRY, LEGACY_USE)
RX_STATUS = _exact(LEGACY_STATUS)
RX_PROFILE = _exact(MENU_PROFILE, LEGACY_PROFILE)
RX_HELP = _exact(MENU_HELP)
RX_ANNOUNCE = _exact(MENU_ANNOUNCE, LEGACY_ANNOUNCE, LEGACY_BROADCAST)
RX_SCHEDULE = _exact(LEGACY_SCHEDULE)
RX_POLL = _exact(MENU_POLL)
RX_RECALL = _exact(MENU_RECALL)

# --- callback patterns (used verbatim by CallbackQueryHandler) ------------
PAT_HUB = r"^hub$"
PAT_MENU = r"^menu$"
PAT_MACHINE = r"^m:([a-z0-9]+)$"
PAT_DURATION = r"^dur:([a-z0-9]+):(\d+)$"
PAT_EXTEND_OK = r"^extc:(\d+):(\d+)$"
PAT_STOP = r"^stop:(\d+)$"
PAT_STOP_OK = r"^stopc:(\d+)$"
PAT_PING = r"^ping:([a-z0-9]+)$"
PAT_PING_PICK = r"^pingpick$"
PAT_RESET = r"^reset$"
PAT_RESET_OK = r"^resetok$"
PAT_STATUS = r"^st(?::refresh)?$"
PAT_SUITE_LETTER = r"^sl:([A-F])$"
PAT_REG_OK = r"^reg:ok$"
PAT_REG_REDO = r"^reg:redo$"
PAT_BC_SEND = r"^bc:send$"
PAT_BC_PREVIEW = r"^bc:prev$"
PAT_BC_UNDO = r"^bc:undo$"
PAT_BC_CANCEL = r"^bc:cancel$"
PAT_BC_WHEN = r"^bc:when$"
PAT_BC_AT = r"^bc:at:([a-z0-9]+)$"
PAT_BC_OK = r"^bc:ok$"
PAT_BC_REDO = r"^bc:redo$"
PAT_BC_DROP = r"^bcx:(\d+)$"
PAT_BC_CAL = r"^bc:cal:(\d{6})$"
PAT_BC_DAY = r"^bc:d:(\d{8})$"
PAT_BC_HOUR = r"^bc:h:(\d{8}):(\d{2})$"
PAT_BC_MIN = r"^bc:m:(\d{8}):(\d{4})$"
PAT_NOOP = r"^noop$"
PAT_POLL_ANSWER = r"^pl:(in|out):(\d+)$"
PAT_POLL_REFRESH = r"^pl:re:(\d+)$"
PAT_POLL_SEND = r"^pl:send$"
PAT_POLL_CANCEL = r"^pl:cancel$"
PAT_RC_ALL = r"^rc:all$"
PAT_RC_ONE = r"^rc:one$"
PAT_RC_CANCEL = r"^rc:no$"

# Every machine dialog is reached from the machine list, so "back" always means
# "the laundry menu" — spelling that out beats a bare "⬅️ Back".
_BTN_BACK = InlineKeyboardButton(BACK_HUB, callback_data="menu")
_BTN_HUB = InlineKeyboardButton(BACK_HUB, callback_data="hub")


def main_menu(is_leader: bool = False) -> ReplyKeyboardMarkup:
    """Persistent menu; the leaders' rows exist only for dorm leaders.

    Announcing is one door, full width, and its caption says so. It was two
    for a while, which meant a leader had to decide whether tonight's notice
    was a "📢 Announce" or a "📅 Scheduled Announce" before they had written
    a word of it. They are the same tool with different timing, the composer
    offers ✅ Send and 🕒 Send it later side by side once you are inside, and
    the timing is the last thing you should have to commit to, not the first.
    ``/schedule`` still opens it straight on the timed step.

    ♻️ Recall sits beside Poll rather than under Announce, which is where it
    belongs by meaning. A thumb reaching for the announcement door should not
    be able to land on the button that deletes the last one instead, and the
    recall card asks before it touches anything, so a stray tap costs a glance.
    """
    rows = [[MENU_LAUNDRY], [MENU_PROFILE, MENU_HELP]]
    if is_leader:
        rows.append([MENU_ANNOUNCE])
        rows.append([MENU_POLL, MENU_RECALL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def hub_only_keyboard() -> InlineKeyboardMarkup:
    """A lone ``⬅️ Laundry`` button for end-of-flow messages."""
    return InlineKeyboardMarkup([[_BTN_HUB]])


def reset_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Yes, reset", callback_data="resetok")],
            [InlineKeyboardButton("❌ No", callback_data="hub")],
        ]
    )


def nudge_picker_keyboard(entries: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """``entries`` are ``(machine id, caption)`` for free machines with a load."""
    rows = [
        [InlineKeyboardButton(caption, callback_data=f"ping:{machine_id}")]
        for machine_id, caption in entries
    ]
    rows.append([_BTN_HUB])
    return InlineKeyboardMarkup(rows)


def duration_buttons(
    machine: config.Machine, *, extend: bool = False
) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            f"➕ {minutes} min" if extend else f"▶️ {minutes} min",
            callback_data=f"dur:{machine.id}:{minutes}",
        )
        for minutes in machine.durations
    ]


def machine_view_keyboard(
    machine: config.Machine,
    *,
    durations: bool = False,
    extend: bool = False,
    stop_sid: int | None = None,
    ping_name: str | None = None,
    back: bool = True,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if durations:
        rows.append(duration_buttons(machine, extend=extend))
    if stop_sid is not None:
        # Early finish: the only "collected" left, and only for your own load.
        # The callback keeps its v1 `stop:` name; residents never see the word.
        rows.append([InlineKeyboardButton("✅ Collecting now", callback_data=f"stop:{stop_sid}")])
    if ping_name:
        rows.append(
            [
                InlineKeyboardButton(
                    f"🔔 Nudge {util.shorten(ping_name)}", callback_data=f"ping:{machine.id}"
                )
            ]
        )
    if back:
        rows.append([_BTN_BACK])
    return InlineKeyboardMarkup(rows)


def started_keyboard(
    machine: config.Machine, session_id: int | None = None
) -> InlineKeyboardMarkup:
    """After a start/extension: durations now mean "add time".

    ``session_id`` adds ✅ Collecting now, so the resident can free the machine
    from the very message that confirmed their cycle instead of navigating back
    into the machine view to find it.
    """
    return machine_view_keyboard(
        machine, durations=True, extend=True, stop_sid=session_id
    )


def extend_confirm_keyboard(machine_id: str, session_id: int, minutes: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Yes, I paid twice", callback_data=f"extc:{session_id}:{minutes}")],
            [InlineKeyboardButton("❌ No", callback_data=f"m:{machine_id}")],
        ]
    )


def stop_confirm_keyboard(machine_id: str, session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Yes, taking it out", callback_data=f"stopc:{session_id}")],
            [InlineKeyboardButton("❌ Not yet", callback_data=f"m:{machine_id}")],
        ]
    )


def suite_letters_keyboard(letters: list[str]) -> InlineKeyboardMarkup:
    """A-F in rows of three: [A][B][C] / [D][E][F]."""
    rows = [
        [InlineKeyboardButton(letter, callback_data=f"sl:{letter}") for letter in letters[i : i + 3]]
        for i in range(0, len(letters), 3)
    ]
    return InlineKeyboardMarkup(rows)


def registration_confirm_keyboard(*, roster: bool = False) -> InlineKeyboardMarkup:
    """Onboarding confirm. ``roster``: details came prefilled from the sheet,
    so "start over" only means "the name is wrong" (the room is fixed)."""
    if roster:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ That's me", callback_data="reg:ok")],
                [InlineKeyboardButton("✏️ Fix name", callback_data="reg:redo")],
            ]
        )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ All good", callback_data="reg:ok")],
            [InlineKeyboardButton("🔄 Start over", callback_data="reg:redo")],
        ]
    )


def composer_keyboard(
    recipients: int, *, schedule_first: bool = False
) -> InlineKeyboardMarkup:
    """Buttons under the announcement composer's status message.

    Send and Schedule sit on rows of their own rather than side by side. They
    are the two irreversible buttons here, they read almost alike at a glance,
    and a mis-tap either broadcasts a half-written draft or sits on a finished
    one until tomorrow.

    ``schedule_first`` puts the timed exit on top, for a leader who came in
    through 📅 Scheduled Announce. The other exit stays: arriving through the
    scheduling door and then deciding to send now is a change of mind, not a
    reason to start the draft again.
    """
    plural = "" if recipients == 1 else "s"
    now = InlineKeyboardButton(
        f"✅ Send to {recipients} resident{plural} now", callback_data="bc:send"
    )
    later = InlineKeyboardButton("🕒 Send it later", callback_data="bc:when")
    return InlineKeyboardMarkup(
        [
            [later] if schedule_first else [now],
            [now] if schedule_first else [later],
            [
                InlineKeyboardButton("👀 Preview", callback_data="bc:prev"),
                InlineKeyboardButton("↩️ Undo last message", callback_data="bc:undo"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="bc:cancel")],
        ]
    )


def schedule_when_keyboard(
    presets: list[tuple[str, str]], month: str
) -> InlineKeyboardMarkup:
    """The "when?" step. ``presets`` are ``(key, label)``, two to a row.

    Three ways to answer one question, because they suit different answers.
    The presets cover the times a house notice actually goes out; the picker
    handles any other moment without typing; typing stays for whoever finds
    "fri 6:30pm" faster than four taps. ``month`` (``YYYYMM``) is where the
    picker opens.
    """
    rows = [
        [
            InlineKeyboardButton(label, callback_data=f"bc:at:{key}")
            for key, label in presets[index : index + 2]
        ]
        for index in range(0, len(presets), 2)
    ]
    rows.append(
        [InlineKeyboardButton("📅 Pick a date and time", callback_data=f"bc:cal:{month}")]
    )
    rows.append([InlineKeyboardButton("↩️ Back to the draft", callback_data="bc:redo")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="bc:cancel")])
    return InlineKeyboardMarkup(rows)


def schedule_confirm_keyboard() -> InlineKeyboardMarkup:
    """Last step before an announcement is armed: the resolved time, confirmed.

    Every route into this card, typed or tapped, passes through here. What the
    bot understood is spelled out with the weekday and the date, so the one
    kind of mistake this feature can make in silence has to be tapped past.
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Schedule it", callback_data="bc:ok")],
            [
                InlineKeyboardButton("🕒 Different time", callback_data="bc:when"),
                InlineKeyboardButton("❌ Cancel", callback_data="bc:cancel"),
            ],
        ]
    )


# --- the date and time picker ---------------------------------------------
# Three taps, no typing: a month grid, then an hour, then the minutes. Every
# step is drawn against "now", so a moment that has already gone is rendered
# as an inert · rather than offered and then refused. Nothing here decides
# anything on its own; the last tap lands on the same confirmation card a
# typed time does, which is where the leader reads the day back in words.

_WEEK_HEADS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
MINUTE_STEP = 5
_INERT = InlineKeyboardButton("·", callback_data="noop")


def month_stamp(moment: date) -> str:
    return f"{moment:%Y%m}"


def _shift_month(month: date, delta: int) -> date:
    """The first of the month ``delta`` months away."""
    index = (month.year * 12 + month.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def _picker_tail(back: InlineKeyboardButton) -> list[list[InlineKeyboardButton]]:
    return [[back], [InlineKeyboardButton("❌ Cancel", callback_data="bc:cancel")]]


def calendar_keyboard(
    month: date, *, earliest: datetime, latest: date
) -> InlineKeyboardMarkup:
    """A month grid. ``earliest`` is now; days with no slot left are inert.

    The arrows stop at the ends of the range rather than wrapping into months
    with nothing selectable in them, so every page the leader can reach has at
    least one day they can actually tap.
    """
    first = month.replace(day=1)
    previous = _shift_month(first, -1)
    following = _shift_month(first, 1)
    has_previous = previous >= earliest.date().replace(day=1)
    has_next = following <= latest.replace(day=1)

    rows = [
        [
            InlineKeyboardButton(
                "‹", callback_data=f"bc:cal:{month_stamp(previous)}"
            ) if has_previous else _INERT,
            InlineKeyboardButton(
                f"{calendar.month_name[first.month]} {first.year}", callback_data="noop"
            ),
            InlineKeyboardButton(
                "›", callback_data=f"bc:cal:{month_stamp(following)}"
            ) if has_next else _INERT,
        ],
        [InlineKeyboardButton(head, callback_data="noop") for head in _WEEK_HEADS],
    ]

    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(
        first.year, first.month
    ):
        row = []
        for day in week:
            # The padding days belong to the neighbouring month, and a day
            # whose last slot (23:55) is gone has nothing left to offer.
            usable = (
                day.month == first.month
                and day <= latest
                and datetime.combine(day, time(23, 55), tzinfo=earliest.tzinfo)
                > earliest
            )
            row.append(
                InlineKeyboardButton(str(day.day), callback_data=f"bc:d:{day:%Y%m%d}")
                if usable
                else _INERT
            )
        rows.append(row)

    return InlineKeyboardMarkup(
        rows + _picker_tail(
            InlineKeyboardButton("↩️ Back to the times", callback_data="bc:when")
        )
    )


def hours_keyboard(day: date, *, earliest: datetime) -> InlineKeyboardMarkup:
    """Hours 00 to 23, six to a row.

    A 24-hour grid rather than a 12-hour one with AM/PM: it halves the rows,
    and the confirmation card that follows reads the choice back as "9:00 AM",
    so nobody commits to 18:00 without seeing "6:00 PM" first.
    """
    rows = []
    for start in range(0, 24, 6):
        row = []
        for hour in range(start, start + 6):
            last_slot = datetime.combine(
                day, time(hour, 60 - MINUTE_STEP), tzinfo=earliest.tzinfo
            )
            row.append(
                InlineKeyboardButton(
                    f"{hour:02d}", callback_data=f"bc:h:{day:%Y%m%d}:{hour:02d}"
                )
                if last_slot > earliest
                else _INERT
            )
        rows.append(row)

    return InlineKeyboardMarkup(
        rows + _picker_tail(
            InlineKeyboardButton(
                "↩️ Back to the calendar", callback_data=f"bc:cal:{month_stamp(day)}"
            )
        )
    )


def minutes_keyboard(day: date, hour: int, *, earliest: datetime) -> InlineKeyboardMarkup:
    """Minutes in steps of five, four to a row.

    Five is the finest a house notice has ever needed and it fits three rows.
    Anyone who really wants 9:07 can still type it at the step before this.
    """
    rows = []
    slots = list(range(0, 60, MINUTE_STEP))
    for start in range(0, len(slots), 4):
        row = []
        for minute in slots[start : start + 4]:
            moment = datetime.combine(day, time(hour, minute), tzinfo=earliest.tzinfo)
            row.append(
                InlineKeyboardButton(
                    f":{minute:02d}",
                    callback_data=f"bc:m:{day:%Y%m%d}:{hour:02d}{minute:02d}",
                )
                if moment > earliest
                else _INERT
            )
        rows.append(row)

    return InlineKeyboardMarkup(
        rows + _picker_tail(
            InlineKeyboardButton(
                "↩️ Back to the hours", callback_data=f"bc:d:{day:%Y%m%d}"
            )
        )
    )


def scheduled_keyboard(announcement_id: int) -> InlineKeyboardMarkup:
    """❌ Cancel, for a message about one announcement that is still waiting.

    It rides both the "scheduled" receipt and every ``/scheduled`` listing, so
    it is a stateless callback: the receipt outlives the conversation that
    produced it, and a leader who scrolls back to it days later must still be
    able to call the announcement off from there.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ Cancel this announcement", callback_data=f"bcx:{announcement_id}"
                )
            ]
        ]
    )


def broadcast_cancel_keyboard() -> InlineKeyboardMarkup:
    """Just ❌ Cancel, for the announcement composer's intro.

    The intro has no other action yet — the next step is the leader sending
    their messages — so a lone Cancel spares them remembering /cancel.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="bc:cancel")]]
    )


def _messages(count: int) -> str:
    return f"{count} message" if count == 1 else f"{count} messages"


def recall_keyboard(
    announcements: int, messages: int, latest: int
) -> InlineKeyboardMarkup | None:
    """The recall card's buttons, or ``None`` when there is nothing to undo.

    Counted in the leader's own units. ``announcements`` is how many of their
    sends are still inside Telegram's delete window, ``messages`` how many
    messages those add up to, ``latest`` how many are in the newest one. A
    leader thinks "the two messages I just sent", not "238 delivered copies",
    so the copy count stays out of the buttons and turns up in the receipt.

    With one announcement left there is no distinction to draw, so it gets a
    single button: offering "all" and "just the latest" for the same thing
    invites a leader to work out whether they differ.
    """
    if announcements <= 0:
        return None
    if announcements == 1:
        rows = [
            [
                InlineKeyboardButton(
                    f"♻️ Recall it ({_messages(latest)})", callback_data="rc:all"
                )
            ]
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(
                    f"♻️ Recall all {announcements} announcements "
                    f"({_messages(messages)})",
                    callback_data="rc:all",
                )
            ],
            [
                InlineKeyboardButton(
                    f"♻️ Just the latest ({_messages(latest)})", callback_data="rc:one"
                )
            ],
        ]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="rc:no")])
    return InlineKeyboardMarkup(rows)


def poll_card_keyboard(poll_id: int, answer: str | None = None) -> InlineKeyboardMarkup:
    """Buttons under a resident's copy of a "count me in" card.

    The tapper's current answer is ticked so their own choice is obvious even
    though the card lists everyone. Tapping the other option switches sides;
    the same button never has to mean "undo".
    """
    yes = "✅ I'm in" if answer != "in" else "✅ I'm in  ·  your answer"
    no = "❌ Can't" if answer != "out" else "❌ Can't  ·  your answer"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(yes, callback_data=f"pl:in:{poll_id}"),
                InlineKeyboardButton(no, callback_data=f"pl:out:{poll_id}"),
            ],
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"pl:re:{poll_id}")],
        ]
    )


def poll_composer_keyboard(recipients: int) -> InlineKeyboardMarkup:
    """Buttons under the poll composer's draft."""
    plural = "" if recipients == 1 else "s"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"✅ Send to {recipients} resident{plural}", callback_data="pl:send"
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="pl:cancel")],
        ]
    )


def poll_cancel_keyboard() -> InlineKeyboardMarkup:
    """Just ❌ Cancel, for the "send me the question" prompt.

    The prompt has no other action yet — the next step is the leader typing —
    so a lone Cancel spares them remembering /cancel.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="pl:cancel")]]
    )


def laundry_home_keyboard(
    rows: list[tuple[str, str, tuple[str, str] | None]], is_admin: bool = False
) -> InlineKeyboardMarkup:
    """The merged laundry home: one row per machine, nudge beside it.

    ``rows`` is ``(machine id, caption, (nudge caption, machine id) | None)``.
    A machine that is running has nobody to nudge — its owner's load is not
    finished — so that row is the machine button alone, full width.
    """
    keyboard = []
    for machine_id, caption, nudge in rows:
        row = [InlineKeyboardButton(caption, callback_data=f"m:{machine_id}")]
        if nudge is not None:
            nudge_caption, nudge_id = nudge
            row.append(
                InlineKeyboardButton(nudge_caption, callback_data=f"ping:{nudge_id}")
            )
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="hub")])
    if is_admin:
        keyboard.append([InlineKeyboardButton(HUB_RESET, callback_data="reset")])
    return InlineKeyboardMarkup(keyboard)
