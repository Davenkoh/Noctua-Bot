"""Leaders-only announcements: a multi-message draft composer.

The draft is just a list of the leader's own message ids. Nothing is copied
until the announcement goes out, so anything the leader edits in this chat
beforehand goes out in its edited form.

That also makes "send it later" almost free: a scheduled announcement stores
the same list of ids and copies them when its timer fires, which is why a
leader can keep fixing a typo in tonight's notice all afternoon. The cost is
that the draft has to survive in the leader's own chat until then. A message
deleted before it fires is skipped, and the delivery report says so.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from telegram import Bot, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    JobQueue,
    MessageHandler,
    filters,
)

from .. import config, db, keyboards, util, when

logger = logging.getLogger(__name__)

COMPOSING = 20
SCHEDULING = 21

DRAFT_KEY = "bc_draft"      # list[int]: message ids, in send order
STATUS_KEY = "bc_status"    # message id of the composer's own message
CHAT_KEY = "bc_chat"        # the leader's DM chat id
WHEN_KEY = "bc_when"        # the resolved send time, awaiting confirmation
MODE_KEY = "bc_scheduled"   # came in through 📅 Scheduled Announce

SEND_DELAY_S = 0.05  # gentle on Telegram's per-bot rate limit

JOB_PREFIX = "announce:"

# How late a scheduled announcement may fire and still be worth sending. The
# bot only misses one while it is down, and a deploy is seconds. Past this the
# announcement is written off rather than sent: "the laundry room shuts at 2"
# arriving at six is worse than not arriving, and only the leader can tell
# which of the two theirs is.
LATE_GRACE_MIN = 30

# The one instruction, verbatim on the intro and again on the status line: a
# leader who taps Send too early has already broadcast a half announcement.
# Scheduling names both buttons because it is the worse half of the mistake.
# A premature ✅ Send is at least visible; a premature 🕒 Send it later goes
# out at nine tomorrow morning with nobody watching.
COMPOSE_RULE = (
    "SEND ALL YOUR MULTIPLE MESSAGES BEFORE PRESSING ✅ <b>Send</b> "
    "OR 🕒 <b>Send it later</b>."
)

INTRO = (
    "📢 <b>New announcement</b>\n\n"
    f"{COMPOSE_RULE}\n\n"
    "Text, photos, videos, files are supported."
)

# Same composer, same rule, different promise at the end of it. A leader who
# came in through 📅 Scheduled Announce has already decided this is for later,
# so the intro says what happens next instead of making them find the button.
SCHEDULED_INTRO = (
    "📅 <b>Scheduled announcement</b>\n\n"
    f"{COMPOSE_RULE}\n\n"
    "Text, photos, videos, files are supported. I'll ask when it should go "
    "out once the draft is ready."
)

# The examples are the grammar: bot.when accepts more than this, but a leader
# who copies one of these four shapes never meets the parser's limits.
TIME_HINT = (
    "<i>tomorrow 9am</i> · <i>fri 6:30pm</i> · "
    "<i>1 sep 0900</i> · <i>in 90 minutes</i>"
)

WHEN_PROMPT = (
    "🕒 <b>When should this go out?</b>\n\n"
    f"Tap a time, pick a date below, or type one: {TIME_HINT}\n\n"
    "Leave the draft messages in this chat until then. They are only copied "
    "when it sends, so an edit you make before that still counts."
)

TIMER_BROKEN = (
    "⚠️ I can't set timers right now, so nothing was scheduled. Send it with "
    "✅ <b>Send</b> instead, and tell whoever runs the bot."
)
NOT_A_TIME = "🤔 I couldn't read that as a time."
IN_THE_PAST = "⏳ That moment has already passed."
TOO_FAR = f"📅 That is more than {when.MAX_DAYS_AHEAD} days out."
DRAFT_ONLY = (
    "📎 I'm waiting for a time, so that was not added to the draft. Tap "
    "↩️ <b>Back to the draft</b> first if you want to add to the announcement."
)

# Only fresh messages: an edit must not append the same id twice.
COMPOSE_FILTER = filters.UpdateType.MESSAGE & ~filters.COMMAND & ~filters.StatusUpdate.ALL


# --------------------------------------------------------------------------
# draft + composer status message
# --------------------------------------------------------------------------


def _draft(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    return context.user_data.setdefault(DRAFT_KEY, [])


def _clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (DRAFT_KEY, STATUS_KEY, CHAT_KEY, WHEN_KEY, MODE_KEY):
        context.user_data.pop(key, None)


async def _delete_status(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.user_data.get(CHAT_KEY)
    message_id = context.user_data.pop(STATUS_KEY, None)
    if not chat_id or not message_id:
        return
    try:
        await context.bot.delete_message(chat_id, message_id)
    except TelegramError as exc:  # already gone, or too old to delete
        logger.debug("Composer status %s not deleted: %s", message_id, exc)


async def _post(context: ContextTypes.DEFAULT_TYPE, text: str, markup) -> None:
    """Move the composer's own message to the bottom of the chat.

    Deleted and re-posted rather than edited, because everything the leader
    does here (drafting a message, typing a time) pushes the previous copy up
    the chat, and buttons stranded above the fold get scrolled past. There is
    only ever one of these alive, whichever step it is showing.
    """
    await _delete_status(context)
    chat_id = context.user_data.get(CHAT_KEY)
    if chat_id is None:
        return
    message = await context.bot.send_message(chat_id, text, reply_markup=markup)
    context.user_data[STATUS_KEY] = message.message_id


async def _show_status(context: ContextTypes.DEFAULT_TYPE) -> None:
    count = len(_draft(context))
    if count:
        text = f"📝 Drafted: <b>{count}</b> message(s)\n\n{COMPOSE_RULE}"
    else:
        text = "📝 Draft is empty. Send me something, or tap ❌ Cancel."
    await _post(
        context,
        text,
        keyboards.composer_keyboard(
            db.count_users(), schedule_first=bool(context.user_data.get(MODE_KEY))
        ),
    )


# --------------------------------------------------------------------------
# composing
# --------------------------------------------------------------------------


async def _open(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, scheduled: bool
) -> int:
    if not config.is_leader(update.effective_user.username):
        await update.effective_message.reply_text("🔒 Leaders only.")
        return ConversationHandler.END

    await _delete_status(context)  # a composer left over from a previous run
    _clear(context)
    context.user_data[CHAT_KEY] = update.effective_chat.id
    context.user_data[DRAFT_KEY] = []
    context.user_data[MODE_KEY] = scheduled
    await update.effective_message.reply_text(
        SCHEDULED_INTRO if scheduled else INTRO,
        reply_markup=keyboards.broadcast_cancel_keyboard(),
    )
    return COMPOSING


async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _open(update, context, scheduled=False)


async def announce_later(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """📅 Scheduled Announce: the same composer, with the timed exit on top."""
    return await _open(update, context, scheduled=True)


async def collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    context.user_data[CHAT_KEY] = message.chat_id
    _draft(context).append(message.message_id)
    await _show_status(context)
    return COMPOSING


async def preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    draft = _draft(context)
    if not draft:
        await query.answer("Draft is empty", show_alert=True)
        return COMPOSING

    await query.answer("Preview below 👀")
    chat_id = context.user_data.get(CHAT_KEY, update.effective_chat.id)
    missing = 0
    for message_id in draft:
        try:
            await context.bot.copy_message(
                chat_id=chat_id, from_chat_id=chat_id, message_id=message_id
            )
        except TelegramError as exc:
            missing += 1
            logger.info("Preview copy of %s failed: %s", message_id, exc)
    if missing:
        await context.bot.send_message(
            chat_id,
            f"⚠️ {missing} draft message(s) can't be copied any more "
            "(deleted?), so they'll be skipped.",
        )
    await _show_status(context)
    return COMPOSING


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    draft = _draft(context)
    if not draft:
        await query.answer("Nothing to undo, the draft is empty", show_alert=True)
        return COMPOSING

    draft.pop()
    await query.answer("Removed the last message ↩️")
    await _show_status(context)
    return COMPOSING


# --------------------------------------------------------------------------
# send / cancel
# --------------------------------------------------------------------------


@dataclass
class Report:
    """What one fan-out achieved, for the line the leader reads afterwards."""

    total: int
    sent: int = 0
    unreachable: int = 0
    broken: int = 0
    announcement_id: int | None = None


async def deliver(
    bot: Bot, from_chat_id: int, draft: list[int], *, created_by: int
) -> Report:
    """Copy a draft to every registered resident, in order.

    Shared by ✅ Send and by the scheduled timer, so a leader who schedules an
    announcement gets exactly the delivery an immediate one would have had,
    including which failures are survivable.

    The audience is read here rather than when the announcement was written:
    somebody who registers this afternoon is a resident by tonight, and the
    house notice they are missing is the one thing they most need.

    Every copy's id is written down as it is made. That is the whole basis of
    ``recall``: ``copy_message`` hands the id back exactly once and Telegram
    will never tell the bot again where its own messages went, so a copy that
    is not recorded here cannot be deleted for the rest of its life.
    ``created_by`` is whose stack the announcement joins.
    """
    user_ids = db.all_user_ids()
    report = Report(total=len(user_ids))
    report.announcement_id = db.open_announcement(created_by, from_chat_id, len(draft))
    broken: set[int] = set()  # draft ids Telegram refuses to copy, skip for all

    for user_id in user_ids:
        # Nothing is sent ahead of the draft, so the first copy is also the
        # reachability check. Telegram tells the two apart: Forbidden is about
        # this resident, anything else is about this draft message.
        delivered: list[tuple[int, int]] = []
        unreachable = False
        for message_id in draft:
            if message_id in broken:
                continue
            try:
                copy = await bot.copy_message(
                    chat_id=user_id, from_chat_id=from_chat_id, message_id=message_id
                )
            except Forbidden as exc:
                unreachable = True
                logger.info("Announcement to %s failed: %s", user_id, exc)
                break
            except TelegramError as exc:
                broken.add(message_id)
                logger.warning(
                    "Draft message %s can't be copied, skipping it for everyone: %s",
                    message_id,
                    exc,
                )
                continue
            delivered.append((user_id, copy.message_id))
            await asyncio.sleep(SEND_DELAY_S)

        # Flushed per resident rather than per copy: one write instead of one
        # for every message, and a fan-out that dies mid-flight loses at most
        # this resident's chain from the recall.
        db.record_copies(report.announcement_id, delivered)

        if unreachable:
            report.unreachable += 1
            await asyncio.sleep(SEND_DELAY_S)
        elif delivered:
            report.sent += 1

    report.broken = len(broken)
    return report


def report_text(report: Report) -> str:
    text = f"📤 Sent to {report.sent}/{report.total} residents."
    if report.unreachable:
        text += f"\n{report.unreachable} unreachable (blocked/never started the bot)."
    if report.broken:
        text += (
            f"\n{report.broken} draft message(s) couldn't be copied and were skipped."
        )
    return text


async def send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    draft = list(_draft(context))
    if not draft:
        await query.answer("Draft is empty", show_alert=True)
        return COMPOSING

    await query.answer()
    chat_id = context.user_data.get(CHAT_KEY, update.effective_chat.id)

    # The composer's message turns into the progress line, so drop its buttons
    # and stop tracking it: it must survive as the record of the send.
    context.user_data.pop(STATUS_KEY, None)
    try:
        await query.edit_message_text(
            f"📤 Sending {len(draft)} message(s) to {db.count_users()} residents…",
            reply_markup=None,
        )
    except TelegramError as exc:
        logger.debug("Could not repurpose the composer status: %s", exc)

    report = await deliver(
        context.bot, chat_id, draft, created_by=update.effective_user.id
    )
    _clear(context)
    await context.bot.send_message(chat_id, report_text(report))
    return ConversationHandler.END


async def abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    # Cancel sits on the intro as well as the status message. Tapping the
    # intro's copy has to take the status message down too, or its Send button
    # outlives the cancelled draft.
    if query.message.message_id != context.user_data.get(STATUS_KEY):
        await _delete_status(context)
    _clear(context)  # the tapped message survives as the record — edit it
    try:
        await query.edit_message_text(
            "❌ Announcement cancelled. Nothing was sent.", reply_markup=None
        )
    except TelegramError as exc:
        logger.debug("Could not edit the cancelled composer: %s", exc)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _delete_status(context)
    _clear(context)
    await update.effective_message.reply_text("❌ Announcement cancelled. Nothing was sent.")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# scheduling: picking a time
# --------------------------------------------------------------------------

EVENING_HOUR = 20
MORNING_HOUR = 9

_PRESET_LABELS = {
    "1h": "⏱ In 1 hour ({clock})",
    "3h": "⏱ In 3 hours ({clock})",
    "eve": "🌙 Tonight, {clock}",
    "am": "☀️ Tomorrow, {clock}",
}


def preset_moment(key: str, now: datetime) -> datetime | None:
    """Resolve a quick-time button against ``now`` (local, aware).

    The same function draws the label and answers the tap, so a button can
    never mean something other than what it says. ``None`` means the option
    does not apply any more, which is how 🌙 Tonight disappears after 8 PM
    and how a tap that arrives a second too late is turned away.
    """
    if key == "1h":
        return (now + timedelta(hours=1)).replace(second=0, microsecond=0)
    if key == "3h":
        return (now + timedelta(hours=3)).replace(second=0, microsecond=0)
    if key == "eve":
        evening = now.replace(hour=EVENING_HOUR, minute=0, second=0, microsecond=0)
        return evening if evening > now else None
    if key == "am":
        return (now + timedelta(days=1)).replace(
            hour=MORNING_HOUR, minute=0, second=0, microsecond=0
        )
    return None


def presets(now: datetime) -> list[tuple[str, str]]:
    """The quick-time buttons that still make sense at ``now``."""
    options = []
    for key, label in _PRESET_LABELS.items():
        moment = preset_moment(key, now)
        if moment is not None:
            options.append((key, label.format(clock=util.fmt_clock(moment))))
    return options


def _problem(moment: datetime, now: datetime) -> str | None:
    """Why this moment cannot be scheduled, in words, or ``None`` if it can."""
    if moment <= now:
        return IN_THE_PAST
    if moment - now > timedelta(days=when.MAX_DAYS_AHEAD):
        return TOO_FAR
    return None


async def _ask_when(context: ContextTypes.DEFAULT_TYPE, problem: str = "") -> None:
    """(Re-)show the "when?" step, optionally led by what went wrong."""
    now = util.to_local(util.now_utc())
    text = f"{problem}\n\n{WHEN_PROMPT}" if problem else WHEN_PROMPT
    await _post(
        context,
        text,
        keyboards.schedule_when_keyboard(
            presets(now), keyboards.month_stamp(now.date())
        ),
    )


async def _take(context: ContextTypes.DEFAULT_TYPE, moment: datetime) -> int:
    """One gate for every route into the confirmation card.

    Typed, tapped as a preset, or built three taps at a time in the picker, a
    moment reaches the leader's confirmation the same way and is refused for
    the same reasons. A picker slot can go stale between drawing the grid and
    tapping it, so the check is here rather than only on the typed path.
    """
    now = util.to_local(util.now_utc())
    problem = _problem(moment, now)
    if problem is not None:
        await _ask_when(context, f"{problem} ({when.fmt_full(moment, now)})")
        return SCHEDULING
    await _offer(context, moment)
    return SCHEDULING


async def _offer(context: ContextTypes.DEFAULT_TYPE, moment: datetime) -> None:
    """Show the resolved time for confirmation. Nothing is armed yet."""
    now = util.now_utc()
    context.user_data[WHEN_KEY] = moment
    await _post(
        context,
        "🕒 <b>Send this later?</b>\n\n"
        f"📅 {when.fmt_full(moment, now)}\n"
        f"⏳ {when.fmt_lead(moment, now)}\n"
        f"📝 {len(_draft(context))} message(s) in the draft.\n\n"
        "It goes to everyone registered at that moment, so residents who join "
        "between now and then get it too.",
        keyboards.schedule_confirm_keyboard(),
    )


async def cb_when(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """🕒 Send it later, from the composer or from the confirmation card."""
    query = update.callback_query
    if not _draft(context):
        await query.answer("Draft is empty", show_alert=True)
        return COMPOSING

    await query.answer()
    context.user_data.pop(WHEN_KEY, None)
    await _ask_when(context)
    return SCHEDULING


async def cb_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    moment = preset_moment(query.data.split(":")[2], util.to_local(util.now_utc()))
    if moment is None:
        await query.answer("That time has just passed, pick another", show_alert=True)
        await _ask_when(context)
        return SCHEDULING

    await query.answer()
    await _offer(context, moment)
    return SCHEDULING


async def collect_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A typed time. Anything else here is a draft message sent a step too late."""
    message = update.effective_message
    context.user_data[CHAT_KEY] = message.chat_id
    if not message.text:
        await _ask_when(context, DRAFT_ONLY)
        return SCHEDULING

    now = util.to_local(util.now_utc())
    moment = when.parse(message.text, now)
    if moment is None:
        await _ask_when(context, NOT_A_TIME)
        return SCHEDULING

    # A time the parser understood perfectly but cannot act on is a different
    # problem from one it could not read, and saying so is the difference
    # between the leader rephrasing and the leader picking another day.
    return await _take(context, moment)


# --------------------------------------------------------------------------
# scheduling: the date and time picker
# --------------------------------------------------------------------------

CALENDAR_PROMPT = "📅 <b>Pick a date</b>\n\nThen the hour, then the minutes."
HOURS_PROMPT = "📅 <b>{day}</b>\n\nPick the hour. This grid is a 24-hour clock."
MINUTES_PROMPT = "📅 <b>{day}, {hour:02d}:00</b>\n\nPick the minutes."
STALE_SLOT = "⏳ That moment has just passed."


def _latest_day(now: datetime) -> date:
    return (now + timedelta(days=when.MAX_DAYS_AHEAD)).date()


async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A day already gone, a grid heading, an arrow at the end of the range.

    Answered rather than left unhandled: an unanswered callback spins in the
    client until it times out, which reads as the bot having crashed.
    """
    await update.callback_query.answer()
    return SCHEDULING


async def cb_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    now = util.to_local(util.now_utc())

    stamp = query.data.split(":")[2]
    month = date(int(stamp[:4]), int(stamp[4:]), 1)
    # Clamped rather than trusted: the button came from a keyboard that may be
    # older than today, so its arrows may point outside the range by now.
    month = min(max(month, now.date().replace(day=1)), _latest_day(now).replace(day=1))

    await _post(
        context,
        CALENDAR_PROMPT,
        keyboards.calendar_keyboard(month, earliest=now, latest=_latest_day(now)),
    )
    return SCHEDULING


async def cb_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    now = util.to_local(util.now_utc())
    day = datetime.strptime(query.data.split(":")[2], "%Y%m%d").date()

    await _post(
        context,
        HOURS_PROMPT.format(day=when.fmt_date(day)),
        keyboards.hours_keyboard(day, earliest=now),
    )
    return SCHEDULING


async def cb_hour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    now = util.to_local(util.now_utc())
    _, _, stamp, hour = query.data.split(":")
    day = datetime.strptime(stamp, "%Y%m%d").date()

    await _post(
        context,
        MINUTES_PROMPT.format(day=when.fmt_date(day), hour=int(hour)),
        keyboards.minutes_keyboard(day, int(hour), earliest=now),
    )
    return SCHEDULING


async def cb_minute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The last tap. Three grids have narrowed it down to one moment."""
    query = update.callback_query
    await query.answer()
    now = util.to_local(util.now_utc())
    _, _, stamp, clock = query.data.split(":")
    day = datetime.strptime(stamp, "%Y%m%d").date()

    moment = datetime.combine(
        day, time(int(clock[:2]), int(clock[2:])), tzinfo=now.tzinfo
    )
    return await _take(context, moment)


async def cb_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """↩️ Back to the draft: the announcement is untouched, only the step changes."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop(WHEN_KEY, None)
    await _show_status(context)
    return COMPOSING


async def cb_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    moment = context.user_data.get(WHEN_KEY)
    draft = list(_draft(context))
    if moment is None or not draft:
        await query.answer("Nothing to schedule", show_alert=True)
        return SCHEDULING

    now = util.to_local(util.now_utc())
    # A confirm card can sit unanswered. Inside the grace window the leader
    # still gets what they asked for (the timer simply fires at once); past it
    # they are sent back to pick a time rather than surprised by a send.
    if now - moment > timedelta(minutes=LATE_GRACE_MIN):
        await query.answer()
        await _ask_when(context, IN_THE_PAST)
        return SCHEDULING

    await query.answer()
    chat_id = context.user_data.get(CHAT_KEY, update.effective_chat.id)
    announcement_id = db.schedule_announcement(
        update.effective_user.id, chat_id, draft, moment
    )
    if not schedule_job(context.job_queue, announcement_id, moment):
        # Nothing can fire it, so it is settled here rather than left pending
        # for a restart to inherit and apologise for hours later.
        db.settle_announcement(announcement_id, db.MISSED)
        await _ask_when(context, TIMER_BROKEN)
        return SCHEDULING

    await _delete_status(context)
    _clear(context)
    await context.bot.send_message(
        chat_id,
        "✅ <b>Scheduled.</b>\n\n"
        f"📅 {when.fmt_full(moment, now)}\n"
        f"⏳ {when.fmt_lead(moment, now)}\n"
        f"📝 {len(draft)} message(s).\n\n"
        "Leave those messages in this chat, they are copied when it sends. "
        "/waiting lists everything still going out.",
        reply_markup=keyboards.scheduled_keyboard(announcement_id),
    )
    return ConversationHandler.END


# --------------------------------------------------------------------------
# scheduling: the timer
# --------------------------------------------------------------------------


def job_name(announcement_id: int) -> str:
    return f"{JOB_PREFIX}{announcement_id}"


def cancel_job(job_queue: JobQueue | None, announcement_id: int) -> None:
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(job_name(announcement_id)):
        job.schedule_removal()


def schedule_job(
    job_queue: JobQueue | None, announcement_id: int, send_at: datetime
) -> bool:
    """(Re)arm the timer. An overdue one fires almost immediately.

    Returns whether it is actually armed. Without a JobQueue a stored
    announcement would sit pending forever, and the one thing worse than not
    being able to schedule is telling a leader you did.
    """
    if job_queue is None:
        logger.warning("No JobQueue: announcement %s will not fire.", announcement_id)
        return False
    cancel_job(job_queue, announcement_id)
    delay = max(1.0, (send_at - util.now_utc()).total_seconds())
    job_queue.run_once(
        announcement_job, when=delay, name=job_name(announcement_id), data=announcement_id
    )
    return True


async def _tell(bot: Bot, chat_id: int, text: str) -> None:
    """Report back to a leader. Their chat failing must not fail the send."""
    try:
        await bot.send_message(chat_id, text)
    except TelegramError as exc:
        logger.warning("Could not report to %s: %s", chat_id, exc)


async def announcement_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """A scheduled announcement's moment has come."""
    announcement_id = int(context.job.data)
    # Claiming is what makes this safe to reach twice: a restore that races
    # the timer it is restoring, or a duplicate job, finds nothing to send.
    row = db.claim_announcement(announcement_id)
    if row is None:
        return

    draft = db.draft_ids(row)
    if not draft:
        logger.warning("Scheduled announcement %s has an empty draft", announcement_id)
        return

    report = await deliver(
        context.bot, row["chat_id"], draft, created_by=row["created_by"]
    )
    await _tell(
        context.bot,
        row["chat_id"],
        f"🕒 <b>Your scheduled announcement just went out.</b>\n\n{report_text(report)}",
    )


def _missed_text(send_at: datetime, now: datetime) -> str:
    return (
        "⚠️ <b>A scheduled announcement did not go out.</b>\n\n"
        f"📅 It was due {when.fmt_full(send_at, now)}, and the bot was not "
        "running then.\n\n"
        f"More than {LATE_GRACE_MIN} minutes late an announcement is as likely "
        "to be wrong as useful, so this one was dropped rather than sent now. "
        "The draft messages are still in this chat if you want to send it again."
    )


async def restore_scheduled(application: Application) -> tuple[int, int]:
    """Re-arm scheduled announcements after a restart.

    Returns ``(armed, missed)``. Anything the bot slept through by more than
    the grace window is written off here and its author told, because the
    alternative is a dorm waking up to last night's notice.
    """
    job_queue = application.job_queue
    now = util.now_utc()
    armed = missed = 0

    for row in db.pending_announcements():
        send_at = util.parse_iso(row["send_at"])
        if now - send_at > timedelta(minutes=LATE_GRACE_MIN):
            if db.settle_announcement(row["id"], db.MISSED):
                missed += 1
                await _tell(application.bot, row["chat_id"], _missed_text(send_at, now))
            continue
        if schedule_job(job_queue, row["id"], send_at):
            armed += 1

    return armed, missed


# --------------------------------------------------------------------------
# scheduling: the waiting list
# --------------------------------------------------------------------------


def describe(row, now: datetime) -> str:
    """One waiting announcement, as the leaders' list shows it."""
    send_at = util.parse_iso(row["send_at"])
    author = db.get_user(row["created_by"])
    who = util.handle_plain(author["username"], author["name"]) if author else "a leader"
    return (
        "🕒 <b>Waiting to go out</b>\n"
        f"📅 {when.fmt_full(send_at, now)}\n"
        f"⏳ {when.fmt_lead(send_at, now)}\n"
        f"📝 {len(db.draft_ids(row))} message(s), from {util.esc(who)}."
    )


LIST_LIMIT = 10

NOTHING_WAITING = (
    "🕒 Nothing is scheduled.\n\n"
    "Compose one with 📢 <b>Announce</b>, then tap 🕒 <b>Send it later</b>."
)

_SETTLED_ALERTS = {
    db.SENT: "Too late, that one has already gone out.",
    db.CANCELLED: "That one was already cancelled.",
    db.MISSED: "That one never went out, the bot was down when it was due.",
}


async def scheduled_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/scheduled``: what is still waiting, each with its own ❌ Cancel."""
    if not config.is_leader(update.effective_user.username):
        await update.effective_message.reply_text("🔒 Leaders only.")
        return

    rows = db.pending_announcements()
    if not rows:
        await update.effective_message.reply_text(NOTHING_WAITING)
        return

    now = util.now_utc()
    for row in rows[:LIST_LIMIT]:
        await update.effective_message.reply_text(
            describe(row, now), reply_markup=keyboards.scheduled_keyboard(row["id"])
        )
    # Each one needs its own ❌ Cancel, so each is its own message. Saying how
    # many were left out beats a listing that quietly stops at the tenth.
    if len(rows) > LIST_LIMIT:
        await update.effective_message.reply_text(
            f"…and {len(rows) - LIST_LIMIT} more, not listed. Cancel some of "
            "these and run /scheduled again to see them."
        )


async def cb_drop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """❌ Cancel on a waiting announcement, from wherever the button is.

    Any leader may cancel any of them, not only whoever wrote it: a notice
    that has turned out to be wrong should not have to wait for its author to
    wake up. The author is told when it was somebody else who called it off.
    """
    query = update.callback_query
    user = update.effective_user
    if not config.is_leader(user.username):
        await query.answer("🔒 Leaders only.", show_alert=True)
        return

    announcement_id = int(query.data.split(":")[1])
    row = db.get_announcement(announcement_id)
    if row is None:
        await query.answer("That announcement is gone.", show_alert=True)
        return

    if not db.settle_announcement(announcement_id, db.CANCELLED, user.id):
        settled = db.get_announcement(announcement_id)
        await query.answer(
            _SETTLED_ALERTS.get(settled["status"], "Nothing to cancel."), show_alert=True
        )
        return

    cancel_job(context.job_queue, announcement_id)
    await query.answer("Cancelled ❌")
    try:
        await query.edit_message_text(
            "❌ <b>Cancelled.</b> That announcement will not go out.", reply_markup=None
        )
    except TelegramError as exc:
        logger.debug("Could not edit the cancelled announcement notice: %s", exc)

    if row["created_by"] != user.id:
        await _tell(
            context.bot,
            row["chat_id"],
            "❌ <b>Your scheduled announcement was cancelled</b> by "
            f"{util.esc(util.handle_plain(user.username, user.first_name or 'a leader'))}"
            f".\n\n📅 It was due {when.fmt_full(util.parse_iso(row['send_at']), util.now_utc())}.",
        )


def broadcast_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            # /announce matches the button; /broadcast kept for muscle memory.
            # /schedule is the same composer opened straight on the timed step,
            # which is all the retired 📅 Scheduled Announce button ever did.
            CommandHandler(["announce", "broadcast"], announce),
            MessageHandler(filters.TEXT & filters.Regex(keyboards.RX_ANNOUNCE), announce),
            CommandHandler("schedule", announce_later),
            MessageHandler(
                filters.TEXT & filters.Regex(keyboards.RX_SCHEDULE), announce_later
            ),
        ],
        states={
            COMPOSING: [
                CallbackQueryHandler(send, pattern=keyboards.PAT_BC_SEND),
                CallbackQueryHandler(cb_when, pattern=keyboards.PAT_BC_WHEN),
                CallbackQueryHandler(preview, pattern=keyboards.PAT_BC_PREVIEW),
                CallbackQueryHandler(undo, pattern=keyboards.PAT_BC_UNDO),
                CallbackQueryHandler(abort, pattern=keyboards.PAT_BC_CANCEL),
                MessageHandler(COMPOSE_FILTER, collect),
            ],
            # Same draft, different question. The collector is swapped for the
            # time reader, so a message that arrives here is read as a time
            # and never silently appended to an announcement already finished.
            SCHEDULING: [
                CallbackQueryHandler(cb_preset, pattern=keyboards.PAT_BC_AT),
                CallbackQueryHandler(cb_confirm, pattern=keyboards.PAT_BC_OK),
                CallbackQueryHandler(cb_when, pattern=keyboards.PAT_BC_WHEN),
                CallbackQueryHandler(cb_calendar, pattern=keyboards.PAT_BC_CAL),
                CallbackQueryHandler(cb_day, pattern=keyboards.PAT_BC_DAY),
                CallbackQueryHandler(cb_hour, pattern=keyboards.PAT_BC_HOUR),
                CallbackQueryHandler(cb_minute, pattern=keyboards.PAT_BC_MIN),
                CallbackQueryHandler(cb_noop, pattern=keyboards.PAT_NOOP),
                CallbackQueryHandler(cb_back, pattern=keyboards.PAT_BC_REDO),
                CallbackQueryHandler(abort, pattern=keyboards.PAT_BC_CANCEL),
                MessageHandler(COMPOSE_FILTER, collect_time),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        name="announce",
    )
