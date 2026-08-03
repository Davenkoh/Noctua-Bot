"""``/help`` and the stray ``/cancel`` that lands outside any conversation."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from .. import config, db, keyboards

HELP_TEXT = (
    "🦉 <b>Noctua Bot</b> is your dorm's sidekick.\n\n"
    "Tap 🧺 <b>Laundry menu</b> for everything laundry. It lists all four "
    "machines with their live state, and 📊 <b>Machine status</b> · 🔔 <b>Nudge "
    "last user</b> sit underneath.\n\n"
    "<b>▶️ Start a machine</b>\n"
    "1. Put your stuff in and pay.\n"
    "2. Tap 🧺 <b>Laundry menu</b> and pick your machine.\n"
    "3. Tap the cycle length. I'll DM you when it should be done.\n"
    "💸 Paid twice? Tap a duration again and confirm to add the extra time.\n"
    "✅ Taking your load out early? Open the machine and tap <b>Collecting "
    "now</b> so the next person can use it.\n\n"
    "<b>📊 Machine status</b>\n"
    "All four machines live: 🟢 free or 🔴 running, nothing in between. Free "
    "machines show who used them last, when, and their handle. (/status works "
    "too.)\n\n"
    "<b>🔔 Nudge last user</b>\n"
    "Nudge whoever used a 🟢 free machine last, in case their load is still "
    "sitting in it (once every few minutes).\n\n"
    "<b>Etiquette</b>\n"
    "Machines are just 🟢 free or 🔴 running. When your timer ends the machine "
    "shows free again, so clear your load quickly 🙏\n"
    "Found a load sitting in a free machine? Check 📊 <b>Machine status</b> to "
    "see whose it is and 🔔 nudge them (or just move it aside).\n"
    "Timers are estimates: washers aren't precise.\n\n"
    "<b>Announcements</b>\n"
    "📢 <b>Announce</b> (or /announce) lets dorm leaders send an announcement "
    "to everyone. The dorm admin also gets 🔄 <b>Reset all machines</b> in "
    "the 🧺 <b>Laundry menu</b>, which forces all four back to 🟢 free.\n\n"
    "<b>Fix your details</b>\n"
    "👤 <b>Profile</b> (/profile) shows the name and room saved for you. "
    f"Something wrong? Message @{config.CONTACT_HANDLE} to get it fixed. "
    "/cancel backs out of anything."
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        HELP_TEXT,
        reply_markup=keyboards.main_menu(config.is_leader(update.effective_user.username)),
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
