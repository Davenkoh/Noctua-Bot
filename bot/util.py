"""Time, text and mention helpers shared by the Noctua handlers.

Everything is stored in UTC and rendered in ``config.TIMEZONE``.
"""

from __future__ import annotations

import html
import math
from datetime import datetime, timezone

from . import config


def now_utc() -> datetime:
    """Timezone-aware 'now' in UTC — the only clock the DB ever sees."""
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Serialise to the UTC ISO-8601 string stored in SQLite."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    """Read back a stored timestamp; naive values are assumed to be UTC."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def to_local(moment: datetime) -> datetime:
    return moment.astimezone(config.TIMEZONE)


def fmt_clock(moment: datetime) -> str:
    """Local wall clock, e.g. ``3:05 PM`` (strftime '%-I' is not portable)."""
    local = to_local(moment)
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{local.minute:02d} {suffix}"


def remaining_min(end: datetime, now: datetime | None = None) -> int:
    """Whole minutes left until ``end``, rounded up, never negative."""
    seconds = (end - (now or now_utc())).total_seconds()
    return max(0, math.ceil(seconds / 60))


def fmt_remaining(end: datetime, now: datetime | None = None) -> str:
    """e.g. ``~18 min``. Plain text so it is safe in callback alerts too."""
    minutes = remaining_min(end, now)
    return "under 1 min" if minutes <= 0 else f"~{minutes} min"


def elapsed_min(since: datetime, now: datetime | None = None) -> int:
    """Whole minutes since ``since``, rounded down, never negative."""
    seconds = ((now or now_utc()) - since).total_seconds()
    return max(0, int(seconds // 60))


def fmt_ago(since: datetime, now: datetime | None = None) -> str:
    """e.g. ``12 min ago`` / ``just now``."""
    minutes = elapsed_min(since, now)
    return "just now" if minutes <= 0 else f"{minutes} min ago"


def finished_at(row) -> datetime:
    """When a non-active session stopped being a machine's live load.

    A stopped-early session has no cancellation timestamp (the schema is
    unchanged since v1), so its *scheduled* ``ends_at`` — which may still be in
    the future — is clamped to now rather than shown as a future "finished".
    """
    ends = parse_iso(row["ends_at"])
    now = now_utc()
    return ends if ends <= now else now


def finished_line(row) -> str:
    """``finished 2:14 PM (23 min ago)`` for a machine's last load."""
    moment = finished_at(row)
    return f"finished {fmt_clock(moment)} ({fmt_ago(moment)})"


def esc(value: str | None) -> str:
    """HTML-escape anything a resident typed before embedding it."""
    return html.escape(value or "")


def mention_html(user_id: int, username: str | None, name: str) -> str:
    """``@handle`` when known, otherwise a clickable ``tg://user`` mention."""
    if username:
        return f"@{esc(username)}"
    return f'<a href="tg://user?id={user_id}">{esc(name)}</a>'


def who_html(row) -> str:
    """``Bob (#08-01C) @bob`` — a tappable mention when they have no handle."""
    room = esc(row["room"])
    if row["username"]:
        return f"{esc(row['name'])} ({room}) @{esc(row['username'])}"
    return f"{mention_html(row['user_id'], None, row['name'])} ({room})"


def nudger_html(row) -> str:
    """``Daven @daven_koh`` for the person sending a nudge.

    Deliberately named and tagged rather than "Someone": the nudged resident
    should be able to reply to a human, and a nudge you have to sign is a
    nudge people think twice about sending.
    """
    if row is None:
        return "Someone"
    if row["username"]:
        return f"{esc(row['name'])} @{esc(row['username'])}"
    return mention_html(row["user_id"], None, row["name"])


def handle_plain(username: str | None, name: str) -> str:
    """Handle for callback alerts / button labels, which are never parsed."""
    return f"@{username}" if username else name


def clean_name(raw: str | None, limit: int = 40) -> str | None:
    """Strip + collapse whitespace; ``None`` when empty or over ``limit`` chars.

    Residents type their own name (40), the roster import is more generous (60).
    """
    name = " ".join((raw or "").split())
    return name if 1 <= len(name) <= limit else None


def shorten(text: str, limit: int = 16) -> str:
    """Trim a name so it still fits on an inline button."""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
