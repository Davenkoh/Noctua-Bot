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
# Refusal shown for the two admin-only actions, resetting the machines and
# /resetme. Both wipe state, so they answer with the same words wherever the
# refusal happens: a slash command, a button tap, or a confirm on a keyboard
# that was sent before the tier changed.
ADMIN_ONLY = "🔒 Admin only."

COLLECT_GRACE_MIN = 15

COLLECT_RULE_WAITING = (
    f"If a machine finished more than {COLLECT_GRACE_MIN} minutes ago and the "
    "load is still sitting in it, you have the right to take it out."
)
COLLECT_RULE_OWNER = (
    f"If a load is left more than {COLLECT_GRACE_MIN} minutes after it "
    "finishes, the next user has the right to take it out."
)

BTN_LAUNDRY = "🧺 Laundry menu"
BTN_ANNOUNCE = "📢 Announce (now or scheduled)"
BTN_POLL = "📋 Poll"
BTN_RECALL = "♻️ Recall"

GREETING = "🦉 Hi Owlet, <b>{name}</b>!"

# What a leader can do, not how. The buttons are two taps away and say what
# they are; a numbered walkthrough here was read once and never again. Same
# shape as the resident sections: a heading, then the lines under it.
LEADER_BLOCK = (
    "<b>🔑 You're a Noctua leader, you get announcement privileges</b>",
    "",
    f"1. “{BTN_ANNOUNCE}” writes a message to every resident. Send it "
    "straight away, or pick a time and let it go out then.",
    f"2. “{BTN_POLL}” lets you poll the house.",
    f"3. “{BTN_RECALL}” deletes an announcement you sent back out of "
    "everyone's chat, for up to 48 hours after it went.",
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
        ]
    if lines:  # ❓ Help has no greeting, so it must not open with a blank line
        lines.append("")

    lines += [
        "The official Noctua bot, for laundry and house announcements.",
        "",
        "<b>🧺 Laundry</b>",
        f"“{BTN_LAUNDRY}” shows what's free right now. Start a machine and the "
        "bot messages you when your load is done, or nudge whoever left their "
        "laundry sitting in one.",
        "",
        # Sits with laundry rather than at the end: it is the rule that settles
        # an argument in the laundry room, not a footnote about the bot.
        COLLECT_RULE_WAITING,
        "",
        "<b>📢 Announcements</b>",
        "House announcements arrive here, so keep this chat unmuted.",
    ]
    if is_leader:
        lines += ["", *LEADER_BLOCK]

    lines += ["", f"Anything wrong, or questions? Text {_contact()}."]
    return "\n".join(lines)
