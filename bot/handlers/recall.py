"""Leaders-only recall: take an announcement back out of every chat.

Two things make this possible, and both are fragile. Telegram gives a bot 48
hours to delete its own messages, and it will never tell the bot where those
messages went, so the only map is the row ``broadcast.deliver`` writes for
every copy as it makes it. An announcement sent before that recording existed
cannot be recalled, and nothing can be done about it now or later.

The stack is per leader and newest first: ♻️ Recall undoes your last
announcement, tapping it again undoes the one before that. Leaders do not
share a stack. "Undo the last thing" turning out to mean somebody else's
notice is a worse surprise than having to ask them to undo their own.

Nothing here is a conversation. The card recomputes what is recallable on
every tap, so a leader who scrolls back to an hour-old card and taps it gets
what is true now rather than what was true when it was drawn.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from telegram import Bot, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from .. import config, db, keyboards, util, when

logger = logging.getLogger(__name__)

# Telegram's own limit on deleting a bot's own messages. Announcements older
# than this are not offered rather than offered and then refused.
WINDOW_H = 48

DELETE_DELAY_S = 0.05  # gentle on Telegram's per-bot rate limit, as the send is

LEADERS_ONLY = "🔒 Leaders only."

NOTHING = (
    "♻️ <b>Nothing to recall.</b>\n\n"
    f"Either you have not sent an announcement in the last {WINDOW_H} hours, "
    "or the ones you did send have already been taken back.\n\n"
    f"Telegram only lets a bot delete its own messages for {WINDOW_H} hours. "
    "Anything older than that is out of reach for good."
)

WARNING = (
    "This deletes the copies out of every resident's chat. Nobody is told it "
    "happened, and anyone who has already read it has already read it."
)

DONE_TAIL = "Nothing else can be recalled."


# --------------------------------------------------------------------------
# what is still in reach
# --------------------------------------------------------------------------


def stack(user_id: int, now: datetime) -> list:
    """One leader's recallable announcements, newest first."""
    cutoff = util.to_iso(now - timedelta(hours=WINDOW_H))
    return db.recallable_announcements(user_id, cutoff)


def totals(rows: list) -> tuple[int, int, int]:
    """``(announcements, messages across them, messages in the newest)``."""
    if not rows:
        return 0, 0, 0
    return len(rows), sum(row["drafted"] for row in rows), rows[0]["drafted"]


def _age(sent_at: datetime, now: datetime) -> str:
    """How long ago, in the coarsest unit that still reads honestly."""
    minutes = util.elapsed_min(sent_at, now)
    if minutes < 60:
        return util.fmt_ago(sent_at, now)
    hours = minutes // 60
    return "1 hour ago" if hours == 1 else f"{hours} hours ago"


def ask_text(rows: list, now: datetime, *, heading: str = "") -> str:
    """The card that asks. ``heading`` leads with the result of a previous tap."""
    if not rows:
        return f"{heading}\n\n{DONE_TAIL}".strip() if heading else NOTHING

    announcements, _messages, latest = totals(rows)
    sent_at = util.parse_iso(rows[0]["sent_at"])
    lines = [
        "♻️ <b>Recall the next one?</b>" if heading else "♻️ <b>Recall an announcement?</b>",
        "",
        f"📢 Latest: <b>{latest}</b> message(s), sent {_age(sent_at, now)}.",
        # The clock as well as the age: "31 hours ago" is the right unit for
        # deciding, and the wrong one for recognising which notice this was.
        f"🕒 {when.fmt_full(sent_at, now)}",
        f"📬 <b>{rows[0]['live']}</b> copies of it are still standing.",
    ]
    if announcements > 1:
        lines.append(
            f"🗂 <b>{announcements}</b> of your announcements can still be taken back."
        )
    lines += ["", WARNING]
    return "\n".join(([heading, ""] if heading else []) + lines)


def card(rows: list, now: datetime, *, heading: str = "") -> tuple[str, object]:
    """Text and buttons together, so every route shows the same thing."""
    announcements, messages, latest = totals(rows)
    return (
        ask_text(rows, now, heading=heading),
        keyboards.recall_keyboard(announcements, messages, latest),
    )


# --------------------------------------------------------------------------
# doing it
# --------------------------------------------------------------------------


@dataclass
class Outcome:
    """What one recall pass achieved, for the receipt the leader reads."""

    announcements: int = 0
    deleted: int = 0
    missing: int = 0      # not there any more, which is what was wanted anyway
    unreachable: int = 0  # blocked the bot, so this copy can never be deleted
    failed: int = 0       # Telegram balked, so the copy is still standing


def outcome_text(result: Outcome) -> str:
    plural = "" if result.announcements == 1 else "s"
    lines = [
        f"✅ <b>Recalled {result.announcements} announcement{plural}.</b>",
        "",
        f"🗑 <b>{result.deleted}</b> copies deleted from residents' chats.",
    ]
    if result.missing:
        lines.append(f"⚠️ {result.missing} were already gone.")
    if result.unreachable:
        lines.append(
            f"⚠️ {result.unreachable} are in chats that have blocked the bot. "
            "Those copies stay where they are."
        )
    if result.failed:
        # Left standing on purpose: their rows keep deleted_at NULL, so the
        # announcement stays in the stack and tapping ♻️ Recall again retries
        # exactly those. Saying so beats a receipt that implies it is finished.
        lines.append(
            f"⚠️ {result.failed} could not be deleted. Tap ♻️ <b>Recall</b> "
            "again to retry those."
        )
    return "\n".join(lines)


async def delete_copies(bot: Bot, rows: list) -> Outcome:
    """Delete every standing copy of each announcement in ``rows``.

    Telegram's three answers are three different situations, exactly as they
    are on the way out in ``deliver``. Already gone is success by another
    route. Blocked is permanent, so the row is closed rather than left to jam
    the stack for two days with a copy no call will ever remove. Anything else
    is worth another go, so it alone keeps the announcement recallable.
    """
    result = Outcome(announcements=len(rows))
    for row in rows:
        stuck = 0
        for copy in db.live_copies(row["id"]):
            try:
                await bot.delete_message(copy["chat_id"], copy["message_id"])
            except BadRequest as exc:
                # The resident deleted it, or cleared the chat. Either way it
                # is not there, so the row is closed rather than retried.
                result.missing += 1
                db.mark_copy_gone(copy["id"])
                logger.debug("Copy %s was already gone: %s", copy["id"], exc)
            except Forbidden as exc:
                result.unreachable += 1
                db.mark_copy_gone(copy["id"])
                logger.info("Copy %s is in a chat that blocked us: %s", copy["id"], exc)
            except TelegramError as exc:
                stuck += 1
                result.failed += 1
                logger.info("Could not delete copy %s: %s", copy["id"], exc)
            else:
                result.deleted += 1
                db.mark_copy_gone(copy["id"])
            await asyncio.sleep(DELETE_DELAY_S)

        # Only closed when nothing retryable is left. A copy still standing
        # keeps the announcement in the stack, which is what makes a recall
        # that was interrupted or partly refused resumable.
        if not stuck:
            db.close_announcement(row["id"])
    return result


# --------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------


async def recall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/recall`` and the ♻️ Recall menu button: draw the card, touch nothing."""
    user = update.effective_user
    if not config.is_leader(user.username):
        await update.effective_message.reply_text(LEADERS_ONLY)
        return

    now = util.now_utc()
    text, markup = card(stack(user.id, now), now)
    await update.effective_message.reply_text(text, reply_markup=markup)


async def _replace(query, text: str, markup) -> None:
    """Edit the card in place. A card that cannot be edited is not fatal."""
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except TelegramError as exc:
        logger.debug("Could not update the recall card: %s", exc)


async def _run(update: Update, context: ContextTypes.DEFAULT_TYPE, *, every: bool) -> None:
    query = update.callback_query
    user = update.effective_user
    if not config.is_leader(user.username):
        await query.answer(LEADERS_ONLY, show_alert=True)
        return

    now = util.now_utc()
    rows = stack(user.id, now)
    if not rows:
        # A card left open while the window ran out, or a second tap arriving
        # after the first pass cleared everything.
        await query.answer("Nothing left to recall.", show_alert=True)
        await _replace(query, NOTHING, None)
        return

    targets = rows if every else rows[:1]
    await query.answer()
    # The buttons come off before the first delete. The pass takes about as
    # long as the send did, and a second tap inside that window would start a
    # second sweep over copies the first one is already working through.
    await _replace(query, f"♻️ Recalling {len(targets)} announcement(s)…", None)

    result = await delete_copies(context.bot, targets)
    after = util.now_utc()
    text, markup = card(
        stack(user.id, after), after, heading=outcome_text(result)
    )
    await _replace(query, text, markup)


async def cb_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """♻️ Recall all: the whole stack, newest first."""
    await _run(update, context, every=True)


async def cb_one(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """♻️ Just the latest: one step back, then offer the next."""
    await _run(update, context, every=False)


async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _replace(query, "❌ Nothing was recalled.", None)
