"""``/status`` and the ``st`` callback, kept as aliases of the laundry home.

The status board and the machine menu merged into one screen in v1.5. This
module survives because older messages in residents' chats still carry a
📊 Machine status button, and a dead end there would be worse than a redirect.
"""

from __future__ import annotations

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import config
from . import laundry


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = laundry.render_home(config.is_admin(update.effective_user.username))
    await update.effective_message.reply_text(text, reply_markup=markup)


async def cb_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text, markup = laundry.render_home(config.is_admin(update.effective_user.username))
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            raise
