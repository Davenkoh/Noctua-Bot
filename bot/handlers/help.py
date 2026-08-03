"""``/help`` and the stray ``/cancel`` that lands outside any conversation."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from .. import config, db, keyboards, texts


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The same card /start shows, minus the welcome line."""
    user = update.effective_user
    await update.effective_message.reply_text(
        texts.overview(db.get_user(user.id), is_leader=config.is_leader(user.username)),
        reply_markup=keyboards.main_menu(config.is_leader(user.username)),
    )


async def cancel_outside(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/cancel`` when nothing is in progress — just re-show the menu."""
    registered = db.get_user(update.effective_user.id) is not None
    await update.effective_message.reply_text(
        "Nothing to cancel 🙂",
        reply_markup=keyboards.main_menu(
            config.is_leader(update.effective_user.username) if registered else False
        ),
    )
