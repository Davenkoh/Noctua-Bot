"""The live status board for all four machines — 🟢 free or 🔴 in use."""

from __future__ import annotations

from telegram import InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import config, db, keyboards, util

INDENT = "    "


def render_status() -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🧺 <b>Noctua Laundry Status</b>", ""]
    nudgeable: list[config.Machine] = []

    for machine in config.MACHINES.values():
        head = f"{machine.emoji} {machine.label}"
        active = db.get_active(machine.id)
        if active is not None:
            ends_at = util.parse_iso(active["ends_at"])
            lines.append(f"{head}: 🔴 In use by {util.who_html(active)}")
            lines.append(
                f"{INDENT}{util.fmt_remaining(ends_at)} left · est. done {util.fmt_clock(ends_at)}"
            )
            lines.append("")
            continue

        # Free. The last session — done, cancelled or a legacy 'collected' one —
        # is just "who used it last", and the handle is there so you can PM them.
        latest = db.get_latest(machine.id)
        if latest is None:
            lines.append(f"{head}: 🟢 Free · not used yet")
            lines.append("")
            continue

        lines.append(f"{head}: 🟢 Free")
        lines.append(f"{INDENT}Last: {util.who_html(latest)} · {util.finished_line(latest)}")
        lines.append("")
        nudgeable.append(machine)

    lines.append(f"Updated {util.fmt_clock(util.now_utc())} · times are estimates")
    return "\n".join(lines), keyboards.status_keyboard(nudgeable)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = render_status()
    await update.effective_message.reply_text(text, reply_markup=markup)


async def cb_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text, markup = render_status()
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            raise
