"""Leaders-only announcements: a multi-message draft composer.

The draft is just a list of the leader's own message ids. Nothing is copied
until ✅ Send, so anything the leader edits in this chat beforehand goes out
in its edited form.
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

from .. import config, db, keyboards

logger = logging.getLogger(__name__)

COMPOSING = 20

DRAFT_KEY = "bc_draft"      # list[int]: message ids, in send order
STATUS_KEY = "bc_status"    # message id of the composer status message
CHAT_KEY = "bc_chat"        # the leader's DM chat id

SEND_DELAY_S = 0.05  # gentle on Telegram's per-bot rate limit
HEADER = "📢 <b>Noctua Announcement</b>"

INTRO = (
    "📢 <b>New announcement</b>\n\n"
    "SEND ALL YOUR MULTIPLE MESSAGES BEFORE PRESSING ✅ <b>Send</b>.\n\n"
    "Text, photos, videos, files are supported.\n\n"
    "/cancel to drop the draft."
)

# Only fresh messages: an edit must not append the same id twice.
COMPOSE_FILTER = filters.UpdateType.MESSAGE & ~filters.COMMAND & ~filters.StatusUpdate.ALL


# --------------------------------------------------------------------------
# draft + composer status message
# --------------------------------------------------------------------------


def _draft(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    return context.user_data.setdefault(DRAFT_KEY, [])


def _clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (DRAFT_KEY, STATUS_KEY, CHAT_KEY):
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


async def _show_status(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-post the composer status so its buttons stay at the bottom."""
    await _delete_status(context)
    chat_id = context.user_data.get(CHAT_KEY)
    if chat_id is None:
        return

    count = len(_draft(context))
    if count:
        text = (
            f"📝 Draft: <b>{count}</b> message(s)\n"
            "Send more, edit them above, or ✅ Send when you're ready."
        )
    else:
        text = "📝 Draft is empty. Send me something, or tap ❌ Cancel."

    message = await context.bot.send_message(
        chat_id, text, reply_markup=keyboards.composer_keyboard(db.count_users())
    )
    context.user_data[STATUS_KEY] = message.message_id


# --------------------------------------------------------------------------
# composing
# --------------------------------------------------------------------------


async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not config.is_leader(update.effective_user.username):
        await update.effective_message.reply_text("🔒 Leaders only.")
        return ConversationHandler.END

    await _delete_status(context)  # a composer left over from a previous run
    _clear(context)
    context.user_data[CHAT_KEY] = update.effective_chat.id
    context.user_data[DRAFT_KEY] = []
    await update.effective_message.reply_text(INTRO)
    return COMPOSING


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
    await context.bot.send_message(chat_id, HEADER)
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


async def send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    draft = list(_draft(context))
    if not draft:
        await query.answer("Draft is empty", show_alert=True)
        return COMPOSING

    await query.answer()
    chat_id = context.user_data.get(CHAT_KEY, update.effective_chat.id)
    user_ids = db.all_user_ids()
    total = len(user_ids)

    # The status message turns into the progress line, so drop its buttons
    # (and stop tracking it — it must survive as the record of the send).
    context.user_data.pop(STATUS_KEY, None)
    try:
        await query.edit_message_text(
            f"📤 Sending {len(draft)} message(s) to {total} residents…", reply_markup=None
        )
    except TelegramError as exc:
        logger.debug("Could not repurpose the composer status: %s", exc)

    sent = failed = 0
    broken: set[int] = set()  # draft ids Telegram refuses to copy — skip for all
    for user_id in user_ids:
        try:
            await context.bot.send_message(user_id, HEADER)
        except TelegramError as exc:
            failed += 1
            logger.info("Announcement header to %s failed: %s", user_id, exc)
            await asyncio.sleep(SEND_DELAY_S)
            continue
        await asyncio.sleep(SEND_DELAY_S)

        for message_id in draft:
            if message_id in broken:
                continue
            try:
                await context.bot.copy_message(
                    chat_id=user_id, from_chat_id=chat_id, message_id=message_id
                )
            except TelegramError as exc:
                broken.add(message_id)
                logger.warning(
                    "Draft message %s can't be copied, skipping it for everyone: %s",
                    message_id,
                    exc,
                )
                continue
            await asyncio.sleep(SEND_DELAY_S)
        sent += 1

    report = f"📤 Sent to {sent}/{total} residents."
    if failed:
        report += f"\n{failed} unreachable (blocked/never started the bot)."
    if broken:
        report += f"\n{len(broken)} draft message(s) couldn't be copied and were skipped."

    _clear(context)
    await context.bot.send_message(chat_id, report)
    return ConversationHandler.END


async def abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _clear(context)  # the tapped message IS the status one — edit, don't delete
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


def broadcast_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            # /announce matches the button label; /broadcast kept for muscle memory.
            CommandHandler(["announce", "broadcast"], announce),
            MessageHandler(filters.TEXT & filters.Regex(keyboards.RX_ANNOUNCE), announce),
        ],
        states={
            COMPOSING: [
                CallbackQueryHandler(send, pattern=keyboards.PAT_BC_SEND),
                CallbackQueryHandler(preview, pattern=keyboards.PAT_BC_PREVIEW),
                CallbackQueryHandler(undo, pattern=keyboards.PAT_BC_UNDO),
                CallbackQueryHandler(abort, pattern=keyboards.PAT_BC_CANCEL),
                MessageHandler(COMPOSE_FILTER, collect),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        name="announce",
    )
