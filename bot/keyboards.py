"""Keyboards and the single source of truth for Noctua's callback data.

Callback-data grammar
=====================

Fields are ``:``-separated ASCII, always well under Telegram's 64-byte limit.
``<mid>`` is a machine id (``w1``/``w2``/``d1``/``d2``), ``<sid>`` a
``sessions.id``, ``<min>`` whole minutes, ``<L>`` a suite unit letter A-F.

===========================  ==================================================
``noop``                     inert button (never registered, reserved)
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
``bc:send`` / ``bc:cancel``  announcement composer: fan out / abort
``bc:prev`` / ``bc:undo``    announcement composer: preview / drop last message
``pl:in:<pid>``              count me in on poll <pid>
``pl:out:<pid>``             can't make it on poll <pid>
``pl:re:<pid>``              refresh my copy of poll <pid>'s card
``pl:send`` / ``pl:cancel``  poll composer: fan out / abort
===========================  ==================================================

Every "No"/"Cancel" button on a machine dialog reuses ``m:<mid>``, so backing
out always lands on the machine view it came from.
"""

from __future__ import annotations

import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from . import config, util

# --- reply-keyboard labels (handlers match these exactly) -----------------
MENU_LAUNDRY = "🧺 Laundry menu"
MENU_PROFILE = "👤 Profile"
MENU_HELP = "❓ Help"
MENU_ANNOUNCE = "📢 Announce"
MENU_POLL = "📋 Poll"

# v1 labels: reply keyboards live in the client until the resident triggers a
# new one, so the old captions must keep routing somewhere sensible.
LEGACY_USE = "🧺 Use a machine"
LEGACY_LAUNDRY = "🧺 Laundry"
LEGACY_STATUS = "📊 Status"
LEGACY_PROFILE = "👤 My profile"
LEGACY_BROADCAST = "📢 Broadcast"

# Receiving any of these is proof the sender's reply keyboard predates the
# current one — Telegram caches it client-side until the bot replaces it, so
# a resident who hasn't run /start since a rename taps the old caption forever.
LEGACY_CAPTIONS = frozenset(
    {LEGACY_USE, LEGACY_LAUNDRY, LEGACY_STATUS, LEGACY_PROFILE, LEGACY_BROADCAST}
)

# --- inline labels shared across keyboards --------------------------------
HUB_START = "▶️ Start a machine"
HUB_STATUS = "📊 Machine status"
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
RX_ANNOUNCE = _exact(MENU_ANNOUNCE, LEGACY_BROADCAST)
RX_POLL = _exact(MENU_POLL)

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
PAT_POLL_ANSWER = r"^pl:(in|out):(\d+)$"
PAT_POLL_REFRESH = r"^pl:re:(\d+)$"
PAT_POLL_SEND = r"^pl:send$"
PAT_POLL_CANCEL = r"^pl:cancel$"

# Every machine dialog is reached from the machine list, so "back" always means
# "the laundry menu" — spelling that out beats a bare "⬅️ Back".
_BTN_BACK = InlineKeyboardButton(BACK_HUB, callback_data="menu")
_BTN_HUB = InlineKeyboardButton(BACK_HUB, callback_data="hub")
_BTN_STATUS = InlineKeyboardButton(HUB_STATUS, callback_data="st")


def main_menu(is_leader: bool = False) -> ReplyKeyboardMarkup:
    """Persistent menu; the leaders' row exists only for dorm leaders.

    Announce and Poll share a row: both are "reach every resident" tools, and
    keeping them together stops the residents' three rows from growing.
    """
    rows = [[MENU_LAUNDRY], [MENU_PROFILE, MENU_HELP]]
    if is_leader:
        rows.append([MENU_ANNOUNCE, MENU_POLL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def laundry_hub_keyboard(is_leader: bool = False) -> InlineKeyboardMarkup:
    """Sections of the laundry feature — the one entry point residents see.

    The reset row is a leaders-only escape hatch, so it is rendered only for
    them (``cb_reset`` re-checks: a stale keyboard must not be a back door).
    """
    rows = [
        [InlineKeyboardButton(HUB_START, callback_data="menu")],
        [InlineKeyboardButton(HUB_STATUS, callback_data="st")],
        [InlineKeyboardButton(HUB_PING, callback_data="pingpick")],
    ]
    if is_leader:
        rows.append([InlineKeyboardButton(HUB_RESET, callback_data="reset")])
    return InlineKeyboardMarkup(rows)


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


def machine_list_keyboard(
    labels: dict[str, str], is_leader: bool = False
) -> InlineKeyboardMarkup:
    """The laundry home: a machine per row, then the secondary actions.

    Machines come first because starting a cycle is what residents open this
    for; status and nudge sit underneath so they cost the same two taps they
    did when they had their own menu. The reset row is leaders-only, and
    ``cb_reset`` re-checks that (a stale keyboard must not be a back door).
    """
    rows = [
        [InlineKeyboardButton(caption, callback_data=f"m:{machine_id}")]
        for machine_id, caption in labels.items()
    ]
    rows.append(
        [
            InlineKeyboardButton(HUB_STATUS, callback_data="st"),
            InlineKeyboardButton(HUB_PING, callback_data="pingpick"),
        ]
    )
    if is_leader:
        rows.append([InlineKeyboardButton(HUB_RESET, callback_data="reset")])
    return InlineKeyboardMarkup(rows)


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
    status: bool = False,
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
    tail = [button for button, on in ((_BTN_STATUS, status), (_BTN_BACK, back)) if on]
    if tail:
        rows.append(tail)
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
        machine, durations=True, extend=True, stop_sid=session_id, status=True
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


def status_keyboard(ping_machines: list[config.Machine]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Refresh", callback_data="st:refresh")]]
    for machine in ping_machines:
        rows.append(
            [InlineKeyboardButton(f"🔔 Nudge {machine.label}", callback_data=f"ping:{machine.id}")]
        )
    rows.append([_BTN_HUB])
    return InlineKeyboardMarkup(rows)


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


def composer_keyboard(recipients: int) -> InlineKeyboardMarkup:
    """Buttons under the announcement composer's status message."""
    plural = "" if recipients == 1 else "s"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"✅ Send to {recipients} resident{plural}", callback_data="bc:send"
                )
            ],
            [
                InlineKeyboardButton("👀 Preview", callback_data="bc:prev"),
                InlineKeyboardButton("↩️ Undo last", callback_data="bc:undo"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="bc:cancel")],
        ]
    )


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
