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

from . import config, keyboards, util

# How long a finished load may sit in a machine before anyone may move it.
COLLECT_GRACE_MIN = 15


def _contact() -> str:
    return f"@{util.esc(config.CONTACT_HANDLE)}"


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
            # Access survives a rename (residents are keyed by their Telegram
            # account, not their tag), but the resident list is matched by tag,
            # so it goes stale until a leader updates it.
            f"Changed your Telegram tag? Text {_contact()} so the resident "
            "list stays right.",
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
        lines.append(
            f"2. <b>{keyboards.MENU_ANNOUNCE}</b> (or /announce) sends a "
            "message to every resident."
        )

    lines += [
        "",
        "<b>🧺 Laundry</b>",
        f"1. Tap <b>{keyboards.MENU_LAUNDRY}</b> to see the laundry options.",
        "2. When you put a load in, tap the machine you're using, then how "
        "long the cycle is. I'll message you when it should be done.",
        f"3. Tap <b>{keyboards.HUB_STATUS}</b> to see which machines are in "
        "use, and by who.",
        f"4. To nudge the person before you, tap <b>{keyboards.HUB_PING}</b> "
        "and I'll send them a notification.",
        "",
        f"If a machine finished more than {COLLECT_GRACE_MIN} minutes ago and "
        "the load is still sitting in it, you have the right to take it out.",
        "",
        f"Anything wrong with the bot, or questions? Text {_contact()}.",
    ]
    return "\n".join(lines)
