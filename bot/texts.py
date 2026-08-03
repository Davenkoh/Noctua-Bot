"""The one card residents read.

``/start`` and ❓ Help show the same thing on purpose: a resident who taps
Help is usually looking for something they half-remember from onboarding, and
keeping two versions of that in sync by hand never lasts. Only the greeting
differs, so this module owns the text and both handlers render it.

Button labels are pulled from :mod:`bot.keyboards` rather than retyped, so
renaming a button can't leave the instructions pointing at a caption that no
longer exists.
"""

from __future__ import annotations

from . import config, util

# How long a finished load may sit in a machine before anyone may move it.
# The rule is written from both sides: the card tells the person waiting for a
# machine, the nudge tells the person whose load is in it. Same number, so it
# lives here rather than being typed into each message.
COLLECT_GRACE_MIN = 15

COLLECT_RULE_WAITING = (
    f"If a machine finished more than {COLLECT_GRACE_MIN} minutes ago and the "
    "load is still sitting in it, you have the right to take it out."
)
COLLECT_RULE_OWNER = (
    f"If a load is left more than {COLLECT_GRACE_MIN} minutes after it "
    "finishes, the next user has the right to take it out."
)

# Button names as the leader wrote them in the card. They are prose here, so
# they are title-cased rather than pulled from bot.keyboards, whose captions
# are lower case ("🧺 Laundry menu"). Rename a button and this needs the same
# edit.
BTN_LAUNDRY = "🧺 Laundry Menu"
BTN_STATUS = "📊 Machine Status"
BTN_NUDGE = "🔔 Nudge last user"
BTN_ANNOUNCE = "📢 Announce"

GREETING = "🦉 Hi Owlet, <b>{name}</b>!"

# Leaders get their own headed block rather than one more numbered line under
# announcements. The card is what a leader skims once and half-remembers, and
# a house leader who reads past this never uses the broadcast at all.
LEADER_BLOCK = (
    "<b>🔑 You're a Noctua leader</b>",
    "You hold a leadership position in the house, so you can send an "
    "announcement to every resident from this chat.",
    f"1. Tap “{BTN_ANNOUNCE}” (or /announce), then write your announcement "
    "here. Text, photos, videos and files all work, and you can send several "
    "messages in one go.",
    "2. Edit or undo anything you sent, and preview the whole thing, before it "
    "goes anywhere.",
    "3. Tap ✅ <b>Send</b> and it reaches every resident, headed "
    "“📢 Noctua Announcement” rather than your name.",
    "Nothing leaves this chat until you tap Send.",
)


def _contact() -> str:
    return f"@{util.esc(config.CONTACT_HANDLE)}"


def greeting(name: str) -> str:
    return GREETING.format(name=util.esc(name))


def overview(row, *, is_leader: bool = False, greeting: str | None = None) -> str:
    """The full card. ``row`` is a users row, or None if they aren't in yet."""
    lines: list[str] = []
    if greeting:
        lines += [greeting, ""]

    if row is not None:
        lines += [
            "<b>Your profile</b>",
            f"👤 {util.esc(row['name'])} · 🏠 {util.esc(row['room'])}",
            f"Wrong? Text {_contact()}.",
            f"Changed your Telegram tag? Text {_contact()} or you lose access.",
            "",
        ]

    lines += [
        "This is the official Noctua bot, for announcements and laundry.",
        "",
        "<b>📢 Announcements</b>",
        "1. Any official Noctua announcement is sent through here, so keep "
        "this chat unmuted.",
    ]
    if is_leader:
        lines += ["", *LEADER_BLOCK]

    lines += [
        "",
        "<b>🧺 Laundry Queue System</b>",
        f"1. Tap “{BTN_LAUNDRY}” to see the laundry options.",
        "2. When you put a load in, tap the machine you're using, then how "
        "long the cycle is. This bot will message you when your load is done.",
        f"3. Tap “{BTN_STATUS}” to see which machines are in use, when it was "
        "last used, and by who.",
        f"4. To nudge the person before you, tap “{BTN_NUDGE}” and this bot "
        "will send them a notification.",
        "",
        COLLECT_RULE_WAITING,
        "",
        f"Anything wrong with the bot, or questions? Text {_contact()}.",
    ]
    return "\n".join(lines)
