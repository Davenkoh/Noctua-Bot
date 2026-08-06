"""Leaders-only "count me in" polls: one card per resident, one shared tally.

A poll is not Telegram's native poll. Native polls tally per message, and each
resident's DM is a different message, so 120 DMs would be 120 unrelated polls.
Here the bot owns the tally instead: every card is an ordinary message with
buttons, taps are recorded against one ``polls`` row, and every card renders
that same list. Residents see names only, never rooms.

Keeping 120 cards live would mean editing 120 messages per tap, which the
rate limit will not carry. So a tap re-renders only the tapper's own card, and
everyone else pulls the current list with 🔄 Refresh. The leader gets a
separate summary they can refresh, including who has not answered yet.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .. import config, db, keyboards, texts, util

logger = logging.getLogger(__name__)

ASKING = 30

QUESTION_KEY = "poll_question"
CHAT_KEY = "poll_chat"

SEND_DELAY_S = 0.05  # gentle on Telegram's per-bot rate limit
QUESTION_LIMIT = 300

INTRO = (
    "📋 <b>New poll</b>\n\n"
    "Send me the question, for example:\n"
    "<i>Block cleanup this Saturday 10am, who's in?</i>\n\n"
    "Everyone gets a card with <b>✅ I'm in</b> and <b>❌ Can't</b>, and they "
    "all see the same list of names. /cancel to drop it."
)
TOO_LONG = f"That's over {QUESTION_LIMIT} characters. Send something shorter."
NO_RECIPIENTS = "Nobody has registered yet, so there is nobody to poll."
GONE = "That poll has been closed."


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _names(entries: list[str], limit: int = 60) -> str:
    """Comma-joined names, truncated so a long list can't blow the 4096 cap."""
    if not entries:
        return "nobody yet"
    if len(entries) <= limit:
        return ", ".join(entries)
    return ", ".join(entries[:limit]) + f", and {len(entries) - limit} more"


def render_card(poll_id: int) -> str | None:
    """The card every resident sees. Names only, deliberately no rooms."""
    poll = db.get_poll(poll_id)
    if poll is None:
        return None
    tally = db.poll_answers(poll_id)
    into, out = tally.get(db.IN, []), tally.get(db.OUT, [])
    # Names go on their own line: with 70 residents the "In" list wraps to
    # several lines anyway, and a label sharing the first line makes the two
    # sides hard to tell apart at a glance.
    return (
        f"📋 <b>{util.esc(poll['question'])}</b>\n\n"
        f"✅ <b>In ({len(into)}):</b>\n{util.esc(_names(into))}\n\n"
        f"❌ <b>Can't ({len(out)}):</b>\n{util.esc(_names(out))}"
    )


def render_summary(poll_id: int) -> str | None:
    """The leader's view: the same tally, plus who has not answered."""
    poll = db.get_poll(poll_id)
    if poll is None:
        return None
    tally = db.poll_answers(poll_id)
    into, out = tally.get(db.IN, []), tally.get(db.OUT, [])
    waiting = db.poll_no_reply(poll_id)
    total = len(into) + len(out) + len(waiting)

    text = (
        f"📋 <b>{util.esc(poll['question'])}</b>\n"
        f"<i>Sent to {total} resident(s).</i>\n\n"
        f"✅ <b>In ({len(into)}):</b>\n{util.esc(_names(into))}\n\n"
        f"❌ <b>Can't ({len(out)}):</b>\n{util.esc(_names(out))}\n\n"
        f"⏳ <b>No reply ({len(waiting)}):</b>"
    )
    if waiting:
        # Rooms appear here and nowhere else: this message only ever goes to
        # the leader who created the poll, and chasing needs the room.
        listed = ", ".join(f"{util.esc(row['name'])} {row['room']}" for row in waiting[:40])
        if len(waiting) > 40:
            listed += f", and {len(waiting) - 40} more"
        text += f"\n{listed}"
    return text


async def _rerender(
    context: ContextTypes.DEFAULT_TYPE, poll_id: int, chat_id: int, message_id: int,
    answer: str | None,
) -> None:
    text = render_card(poll_id)
    if text is None:
        return
    try:
        await context.bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=keyboards.poll_card_keyboard(poll_id, answer),
        )
    except TelegramError as exc:
        # "message is not modified" is the common one and is entirely benign.
        logger.debug("Poll %s card %s not re-rendered: %s", poll_id, message_id, exc)


# --------------------------------------------------------------------------
# composing (leaders only)
# --------------------------------------------------------------------------


def _audience() -> list[int]:
    """Who gets the poll. POLL_TEST_HANDLES narrows it while testing."""
    if config.POLL_TEST_HANDLES:
        rows = db.leader_rows(config.POLL_TEST_HANDLES)
        return [row["user_id"] for row in rows]
    return db.all_user_ids()


async def poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not config.is_leader(update.effective_user.username):
        await update.effective_message.reply_text("🔒 Leaders only.")
        return ConversationHandler.END

    context.user_data[CHAT_KEY] = update.effective_chat.id
    context.user_data.pop(QUESTION_KEY, None)
    await update.effective_message.reply_text(INTRO)
    return ASKING


async def collect_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    question = util.clean_name(update.effective_message.text, limit=QUESTION_LIMIT)
    if question is None:
        await update.effective_message.reply_text(TOO_LONG)
        return ASKING

    recipients = _audience()
    if not recipients:
        await update.effective_message.reply_text(NO_RECIPIENTS)
        return ConversationHandler.END

    context.user_data[QUESTION_KEY] = question
    await update.effective_message.reply_text(
        f"📋 <b>{util.esc(question)}</b>\n\nSend this to everyone?",
        reply_markup=keyboards.poll_composer_keyboard(len(recipients)),
    )
    return ASKING


async def send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    question = context.user_data.get(QUESTION_KEY)
    if not question:
        await query.answer("No question yet", show_alert=True)
        return ASKING

    await query.answer()
    user = update.effective_user
    recipients = _audience()
    poll_id = db.create_poll(question, user.id)

    try:
        await query.edit_message_text(
            f"📤 Sending the poll to {len(recipients)} resident(s)…", reply_markup=None
        )
    except TelegramError as exc:
        logger.debug("Could not repurpose the poll composer: %s", exc)

    text = render_card(poll_id)
    markup = keyboards.poll_card_keyboard(poll_id)
    sent = failed = 0
    for user_id in recipients:
        try:
            message = await context.bot.send_message(user_id, text, reply_markup=markup)
        except TelegramError as exc:
            failed += 1
            logger.info("Poll %s to %s failed: %s", poll_id, user_id, exc)
            await asyncio.sleep(SEND_DELAY_S)
            continue
        db.add_poll_recipient(poll_id, user_id, message.chat_id, message.message_id)
        sent += 1
        await asyncio.sleep(SEND_DELAY_S)

    context.user_data.pop(QUESTION_KEY, None)
    report = f"📤 Poll sent to {sent}/{len(recipients)} resident(s)."
    if failed:
        report += f"\n{failed} unreachable (blocked/never started the bot)."
    chat_id = context.user_data.get(CHAT_KEY, update.effective_chat.id)
    await context.bot.send_message(chat_id, report)
    # The leader's own summary, refreshable, listing who has not answered.
    await context.bot.send_message(
        chat_id,
        render_summary(poll_id),
        reply_markup=keyboards.poll_card_keyboard(poll_id),
    )
    return ConversationHandler.END


async def abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop(QUESTION_KEY, None)
    try:
        await query.edit_message_text("❌ Poll cancelled. Nothing was sent.", reply_markup=None)
    except TelegramError as exc:
        logger.debug("Could not edit the cancelled poll composer: %s", exc)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(QUESTION_KEY, None)
    await update.effective_message.reply_text("❌ Poll cancelled. Nothing was sent.")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# answering (any resident who was sent the poll)
# --------------------------------------------------------------------------


async def cb_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    answer, poll_id = query.data.split(":")[1], int(query.data.split(":")[2])

    if db.get_poll(poll_id) is None:
        await query.answer(GONE, show_alert=True)
        return
    # False means this person was never sent this poll, so a forwarded card
    # cannot be used to vote by someone the poll never reached.
    if not db.set_poll_answer(poll_id, update.effective_user.id, answer):
        await query.answer("This poll wasn't sent to you.", show_alert=True)
        return

    await query.answer("You're in ✅" if answer == db.IN else "Noted ❌")
    await _rerender(
        context, poll_id, query.message.chat_id, query.message.message_id, answer
    )


async def cb_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    poll_id = int(query.data.split(":")[2])
    poll = db.get_poll(poll_id)
    if poll is None:
        await query.answer(GONE, show_alert=True)
        return

    user_id = update.effective_user.id
    row = db.poll_recipient(poll_id, user_id)

    # Two kinds of message carry this button: a resident's own card, and the
    # creator's summary. The creator usually holds both, so they are told
    # apart by message id rather than by who tapped.
    if row is not None and row["message_id"] == query.message.message_id:
        await query.answer("Up to date 🔄")
        await _rerender(
            context, poll_id, query.message.chat_id, query.message.message_id, row["answer"]
        )
        return

    if user_id == poll["created_by"]:
        await query.answer("Up to date 🔄")
        try:
            await query.edit_message_text(
                render_summary(poll_id),
                reply_markup=keyboards.poll_card_keyboard(poll_id),
            )
        except TelegramError as exc:
            logger.debug("Poll %s summary not refreshed: %s", poll_id, exc)
        return

    # Neither their card nor their poll: a forwarded copy.
    await query.answer("This poll wasn't sent to you.", show_alert=True)


def poll_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("poll", poll_command),
            MessageHandler(filters.TEXT & filters.Regex(keyboards.RX_POLL), poll_command),
        ],
        states={
            ASKING: [
                CallbackQueryHandler(send, pattern=keyboards.PAT_POLL_SEND),
                CallbackQueryHandler(abort, pattern=keyboards.PAT_POLL_CANCEL),
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_question),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        name="poll",
    )
