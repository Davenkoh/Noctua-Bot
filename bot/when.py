"""Reading a time a dorm leader typed, and writing one back.

Scheduled announcements are the only place Noctua accepts a moment in time as
free text, so the grammar here is deliberately small: a day, a clock time, or
a plain "in 90 minutes". Anything it cannot resolve comes back as ``None`` and
the handler shows the leader what it does accept, rather than guessing.

Everything is local (``config.TIMEZONE``): ``parse`` takes the current local
time and returns a local one, so callers convert to UTC at the database edge
the same way every other timestamp does.

Nothing here is ever armed on the parser's word alone. Whatever it resolves is
rendered back with :func:`fmt_full` and confirmed by the leader first, which
is what makes the two guesses below safe to make:

* A bare clock time means the next time that clock reads, so ``9am`` typed at
  lunchtime is tomorrow morning.
* A weekday name always means the coming one, so ``mon`` typed on a Monday is
  in seven days, not in six hours.
* A slashed date is day-first, so ``1/9`` is September. ``1 sep`` is the form
  the prompt suggests precisely because it cannot be read the other way.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from . import util

# How far out a leader may schedule. Not a technical limit: a draft that has
# to survive two months in the leader's own chat, unedited and undeleted, is
# far likelier to be a typo in the year than a real plan.
MAX_DAYS_AHEAD = 60

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Words a leader naturally types around a time and which carry no meaning of
# their own. Anything left over after the date and the clock have been taken
# out must be one of these, or the whole string is rejected: silently ignoring
# a word we did not understand is how "monday 9am, not sunday" becomes Sunday.
_FILLER = frozenset({"at", "on", "the", "by", "this", "sharp", "please", ",", "-", "."})

_DAY_WORDS = {
    "today": 0, "tonight": 0, "tonite": 0,
    "tomorrow": 1, "tmr": 1, "tmrw": 1, "tomo": 1, "tmw": 1,
}

_UNITS = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 1440, "day": 1440, "days": 1440,
}

_RELATIVE_RE = re.compile(r"^in\b(?P<rest>.*)$")
_AMOUNT_RE = re.compile(r"(\d+)\s*([a-z]+)")

def _any_of(names) -> str:
    """A ``\b``-anchored alternation of literal words, longest first.

    Every date pattern below matches the *names* it knows rather than "some
    word, checked afterwards". With the loose version, the first word-shaped
    thing in "please friday 9am" is "please", the weekday never gets looked
    at, and the announcement quietly goes out today instead of on Friday.
    """
    return "|".join(sorted(names, key=len, reverse=True))


_ISO_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + _any_of(_MONTHS) + r")\b"
)
_MONTH_DAY_RE = re.compile(
    r"\b(" + _any_of(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b"
)
_WEEKDAY_RE = re.compile(
    r"\b(?:next\s+|coming\s+)?(" + _any_of(_WEEKDAYS) + r")\b"
)
_DAY_WORD_RE = re.compile(r"\b(" + _any_of(_DAY_WORDS) + r")\b")

# Clock forms, tried in this order so the most specific one wins. "6.30pm" is
# as common here as "6:30pm", and "1830" is what a leader writing a notice
# types, so all three resolve rather than being turned back at the door.
_TIME_RES = (
    re.compile(r"\b(\d{1,2})[:.](\d{2})\s*([ap])\.?m\.?\b"),
    re.compile(r"\b(\d{1,2})[:.](\d{2})\b"),
    re.compile(r"\b(\d{1,2})\s*([ap])\.?m\.?\b"),
    re.compile(r"\b(\d{4})\s*(?:h|hrs)?\b"),
)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse(text: str | None, now: datetime) -> datetime | None:
    """Resolve what a leader typed against ``now`` (a local, aware datetime).

    Returns an aware local datetime on the same clock as ``now``, or ``None``
    when the string is not a time this module understands. A moment already in
    the past is a resolution, not an error: the caller says so in words the
    leader can act on, which beats "I don't understand" when the time was
    perfectly clear and merely too late.
    """
    raw = " ".join((text or "").lower().split())
    if not raw:
        return None
    return _parse_relative(raw, now) or _parse_absolute(raw, now)


def _parse_relative(raw: str, now: datetime) -> datetime | None:
    """``in 90 minutes`` / ``in 2h`` / ``in 1 hour 30 mins``."""
    match = _RELATIVE_RE.match(raw)
    if match is None:
        return None

    rest = match.group("rest")
    total = 0
    for amount, unit in _AMOUNT_RE.findall(rest):
        minutes = _UNITS.get(unit)
        if minutes is None:
            return None
        total += int(amount) * minutes
        rest = rest.replace(f"{amount}{unit}", " ", 1).replace(f"{amount} {unit}", " ", 1)

    if total <= 0 or not _only_filler(rest):
        return None
    return (now + timedelta(minutes=total)).replace(second=0, microsecond=0)


def _parse_absolute(raw: str, now: datetime) -> datetime | None:
    """``tomorrow 9am`` / ``mon 6:30pm`` / ``1 sep 0900`` / ``18:30``."""
    rest, day = _take_date(raw, now)
    rest, clock = _take_time(rest)

    if not _only_filler(rest):
        return None
    # A day with no clock time is the one ambiguity worth refusing outright.
    # "tomorrow" could be breakfast or midnight, and picking one for the
    # leader means picking one for all 120 residents.
    if clock is None:
        return None

    if day is None:
        # A bare clock means the next time it comes round, which is today
        # while it is still ahead and tomorrow once it has passed.
        moment = _combine(now.date(), clock, now)
        return moment if moment > now else moment + timedelta(days=1)
    return _combine(day, clock, now)


def _combine(on: date, clock: time, now: datetime) -> datetime:
    return datetime.combine(on, clock, tzinfo=now.tzinfo)


def _only_filler(text: str) -> bool:
    return all(token in _FILLER for token in text.replace(",", " ").split())


def _take_date(text: str, now: datetime) -> tuple[str, date | None]:
    """Pull a day out of ``text``, returning what is left and what it meant."""
    today = now.date()

    match = _ISO_RE.search(text)
    if match is not None:
        found = _safe_date(int(match[1]), int(match[2]), int(match[3]))
        if found is not None:
            return _cut(text, match), found

    match = _SLASH_RE.search(text)
    if match is not None:
        year = _full_year(match[3], today)
        found = _safe_date(year, int(match[2]), int(match[1]))
        if found is not None:
            return _cut(text, match), _roll_forward(found, today, explicit=match[3])

    for pattern, day_group, month_group in (
        (_DAY_MONTH_RE, 1, 2), (_MONTH_DAY_RE, 2, 1)
    ):
        match = pattern.search(text)
        if match is not None:
            found = _safe_date(today.year, _MONTHS[match[month_group]], int(match[day_group]))
            if found is not None:
                return _cut(text, match), _roll_forward(found, today)

    match = _DAY_WORD_RE.search(text)
    if match is not None:
        return _cut(text, match), today + timedelta(days=_DAY_WORDS[match[1]])

    match = _WEEKDAY_RE.search(text)
    if match is not None:
        ahead = (_WEEKDAYS[match[1]] - today.weekday()) % 7 or 7
        return _cut(text, match), today + timedelta(days=ahead)

    return text, None


def _take_time(text: str) -> tuple[str, time | None]:
    """Pull a clock time out of ``text``, returning what is left."""
    for pattern in _TIME_RES:
        match = pattern.search(text)
        if match is None:
            continue
        clock = _read_clock(match)
        if clock is not None:
            return _cut(text, match), clock
    return text, None


def _read_clock(match: re.Match[str]) -> time | None:
    groups = match.groups()
    if len(groups) == 3:                                  # 6:30 pm
        hour, minute, meridiem = int(groups[0]), int(groups[1]), groups[2]
    elif len(groups) == 2 and groups[1] in ("a", "p"):    # 6 pm
        hour, minute, meridiem = int(groups[0]), 0, groups[1]
    elif len(groups) == 2:                                # 18:30
        hour, minute, meridiem = int(groups[0]), int(groups[1]), None
    else:                                                 # 1830
        digits = groups[0]
        hour, minute, meridiem = int(digits[:2]), int(digits[2:]), None

    if meridiem is not None:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def _cut(text: str, match: re.Match[str]) -> str:
    return f"{text[: match.start()]} {text[match.end() :]}"


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:  # 31 Feb, month 13, and friends
        return None


def _full_year(raw: str | None, today: date) -> int:
    if raw is None:
        return today.year
    year = int(raw)
    return year if year >= 100 else 2000 + year


def _roll_forward(found: date, today: date, explicit: str | None = None) -> date:
    """A date with no year means the next one, so 1 Jan typed in December works."""
    if explicit is not None or found >= today:
        return found
    return found.replace(year=found.year + 1)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def fmt_full(moment: datetime, now: datetime) -> str:
    """``Tomorrow (Mon 1 Sep), 9:00 AM`` — the line the leader confirms.

    Always carries the weekday and the date, even for today: the whole point
    of the confirm step is that a leader who typed the wrong day sees it.
    """
    local = util.to_local(moment)
    today = util.to_local(now).date()
    stamp = f"{_DAY_NAMES[local.weekday()]} {local.day} {_MONTH_NAMES[local.month - 1]}"
    if local.year != today.year:
        stamp += f" {local.year}"

    days = (local.date() - today).days
    if days == 0:
        stamp = f"Today ({stamp})"
    elif days == 1:
        stamp = f"Tomorrow ({stamp})"
    return f"{stamp}, {util.fmt_clock(local)}"


def fmt_date(day: date) -> str:
    """``Tue 1 Sep`` — the picker's heading, in the same words as fmt_full."""
    return f"{_DAY_NAMES[day.weekday()]} {day.day} {_MONTH_NAMES[day.month - 1]}"


def fmt_lead(moment: datetime, now: datetime) -> str:
    """``in about 3 hours`` — how long the leader has to change their mind."""
    minutes = max(0, int((moment - now).total_seconds() // 60))
    if minutes < 1:
        return "in under a minute"
    if minutes < 60:
        return f"in {minutes} minute{'' if minutes == 1 else 's'}"
    hours = minutes / 60
    if hours < 24:
        rounded = round(hours * 2) / 2
        shown = int(rounded) if rounded == int(rounded) else rounded
        return f"in about {shown} hour{'' if shown == 1 else 's'}"
    days = round((moment - now).total_seconds() / 86400)
    return f"in about {days} day{'' if days == 1 else 's'}"
