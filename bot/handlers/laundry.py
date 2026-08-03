"""Laundry menu, machine list and view, start/extend/early-finish, nudges, reset.

Since v1.2 a machine has exactly two states: 🟢 free (no active session) or
🔴 running (one active session). Nobody confirms collection after a timer ends —
the machine frees itself and the "last used by" line says whose load is still
sitting in it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from telegram import InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import config, db, jobs, keyboards, texts, util

logger = logging.getLogger(__name__)

LIST_TEXT = (
    "🧺 <b>Laundry menu</b>\n"
    "Tap your machine once your stuff is in and paid."
)
PICK_TEXT = (
    "🔔 <b>Nudge last user</b>\nThese machines are free but may still hold "
    "someone's load. Tap one to nudge them:"
)
PICK_EMPTY_TEXT = (
    "🔔 <b>Nudge last user</b>\nNothing to nudge, every machine is free or running 🎉"
)
CYCLE_PROMPT = "Choose your cycle. Tap once your stuff is in and paid:"
NUDGE_HINT = "Someone's load still inside? Tap 🔔 below to nudge them."

# Two residents can tap the same free machine in the same second. The loser
# gets this plus a re-render, so the alert never has to explain the new state.
TAKEN_TEXT = "⚡ Too late, {label} was just taken. Here's what it's doing now."

RESET_ASK_TEXT = (
    "🔄 <b>Reset all machines?</b>\n"
    "This ends every running timer and sets all four machines to 🟢 free. "
    'Residents keep their registrations; the "last used" history stays.'
)
RESET_DONE_TEXT = "✅ All machines reset. Everything shows 🟢 free."
ADMINS_ONLY = "🔒 Admin only."


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _suffix(machine: config.Machine) -> str:
    """Live state for a machine-list button — free or running, nothing else."""
    active = db.get_active(machine.id)
    if active is not None:
        return f"· 🔴 {util.fmt_remaining(util.parse_iso(active['ends_at']))} left"
    return "· 🟢 free"


def render_machine_list(is_admin: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """The laundry home screen: machines first, then status and nudge."""
    labels = {
        machine.id: f"{machine.emoji} {machine.label} {_suffix(machine)}"
        for machine in config.MACHINES.values()
    }
    return LIST_TEXT, keyboards.machine_list_keyboard(labels, is_admin)


# "Hub" is now the machine list itself; both callbacks land in the same place
# so keyboards sent before this change keep working.
render_hub = render_machine_list


def render_nudge_picker(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """One button per free machine whose last load belongs to someone else."""
    entries: list[tuple[str, str]] = []
    for machine in config.MACHINES.values():
        if db.get_active(machine.id) is not None:
            continue
        latest = db.get_latest(machine.id)
        if latest is None or latest["user_id"] == user_id:
            continue
        entries.append(
            (
                machine.id,
                f"🔔 {machine.label} · {util.shorten(latest['name'], 12)} · "
                f"{util.fmt_ago(util.finished_at(latest))}",
            )
        )
    text = PICK_TEXT if entries else PICK_EMPTY_TEXT
    return text, keyboards.nudge_picker_keyboard(entries)


def render_machine_view(machine: config.Machine, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    head = f"{machine.emoji} <b>{machine.label}</b>"
    active = db.get_active(machine.id)

    if active is not None:
        ends_at = util.parse_iso(active["ends_at"])
        left = util.fmt_remaining(ends_at)
        clock = util.fmt_clock(ends_at)
        if active["user_id"] == user_id:
            text = (
                f"{head}\n"
                f"🌀 Your cycle: {left} left, est. done {clock}.\n"
                "💸 Paid twice? Tap a duration to add time."
            )
            markup = keyboards.machine_view_keyboard(
                machine, durations=True, extend=True, stop_sid=active["id"]
            )
        else:
            text = (
                f"{head}\n"
                f"🔴 In use by {util.who_html(active)}. "
                f"{left} left, est. done {clock}."
            )
            markup = keyboards.machine_view_keyboard(machine)
        return text, markup

    # Free. Whatever the last session's status was (done, cancelled, or a
    # legacy 'collected'), it is simply "who used this last".
    latest = db.get_latest(machine.id)
    if latest is None:
        return (
            f"{head}: 🟢 Free · not used yet\n\n{CYCLE_PROMPT}",
            keyboards.machine_view_keyboard(machine, durations=True),
        )

    theirs = latest["user_id"] != user_id
    lines = [
        f"{head}: 🟢 Free",
        f"Last used by {util.who_html(latest)} · {util.finished_line(latest)}",
    ]
    if theirs:
        lines.append(NUDGE_HINT)
    lines += ["", CYCLE_PROMPT]
    markup = keyboards.machine_view_keyboard(
        machine, durations=True, ping_name=latest["name"] if theirs else None
    )
    return "\n".join(lines), markup


async def _safe_edit(update: Update, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Edit in place, tolerating Telegram's "message is not modified"."""
    try:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            raise


async def _show_machine(update: Update, machine: config.Machine, user_id: int) -> None:
    text, markup = render_machine_view(machine, user_id)
    await _safe_edit(update, text, markup)


def _machine_from(data: str, index: int = 1) -> config.Machine | None:
    return config.MACHINES.get(data.split(":")[index])


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------


async def open_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The 🧺 Laundry reply button — opens the hub as a fresh message."""
    text, markup = render_hub(config.is_admin(update.effective_user.username))
    await update.effective_message.reply_text(text, reply_markup=markup)


async def use_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/use`` — the machine picker, same as the hub's Start a machine."""
    text, markup = render_machine_list(config.is_admin(update.effective_user.username))
    await update.effective_message.reply_text(text, reply_markup=markup)


async def nudge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/ping`` — same as the menu's Nudge last user."""
    text, markup = render_nudge_picker(update.effective_user.id)
    await update.effective_message.reply_text(text, reply_markup=markup)


async def cb_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    text, markup = render_hub(config.is_admin(update.effective_user.username))
    await _safe_edit(update, text, markup)


async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    text, markup = render_machine_list(config.is_admin(update.effective_user.username))
    await _safe_edit(update, text, markup)


async def cb_nudge_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    text, markup = render_nudge_picker(update.effective_user.id)
    await _safe_edit(update, text, markup)


async def cb_machine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    machine = _machine_from(query.data)
    if machine is None:
        await query.answer("That machine is gone 🤔", show_alert=True)
        return
    await query.answer()
    await _show_machine(update, machine, update.effective_user.id)


# --------------------------------------------------------------------------
# start / extend
# --------------------------------------------------------------------------


async def cb_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, machine_id, raw_minutes = query.data.split(":")
    machine = config.MACHINES.get(machine_id)
    minutes = int(raw_minutes)
    if machine is None or minutes not in machine.durations:
        await query.answer("That option isn't available anymore.", show_alert=True)
        return

    user_id = update.effective_user.id
    active = db.get_active(machine.id)

    if active is not None and active["user_id"] == user_id:
        await query.answer()
        ends_at = util.parse_iso(active["ends_at"])
        new_end = ends_at + timedelta(minutes=minutes)
        await _safe_edit(
            update,
            f"💸 You already have a timer on <b>{machine.label}</b> "
            f"(ends {util.fmt_clock(ends_at)}).\n"
            f"Add {minutes} min → new end <b>{util.fmt_clock(new_end)}</b>?",
            keyboards.extend_confirm_keyboard(machine.id, active["id"], minutes),
        )
        return

    # Somebody else is on it. The keyboard this tap came from was rendered when
    # the machine was still free, so say so and re-render rather than trusting it.
    if active is not None:
        await query.answer(TAKEN_TEXT.format(label=machine.label), show_alert=True)
        await _show_machine(update, machine, user_id)
        return

    # The real referee: `start_session` re-checks inside its own write
    # transaction, so of two taps that arrive together exactly one gets a row
    # back and the other lands here.
    session = db.start_session(machine.id, user_id, minutes)
    if session is None:
        await query.answer(TAKEN_TEXT.format(label=machine.label), show_alert=True)
        await _show_machine(update, machine, user_id)
        return

    jobs.schedule_done(context.job_queue, session["id"], util.parse_iso(session["ends_at"]))

    await query.answer("Timer started ⏱️")
    ends_at = util.parse_iso(session["ends_at"])
    await _safe_edit(
        update,
        f"{machine.emoji} <b>{machine.label} started</b> · {minutes} min\n"
        f"Est. done <b>{util.fmt_clock(ends_at)}</b>. I'll message you "
        "(times are estimates).\n\n"
        "💸 Paid twice? Tap a duration below to add time.\n"
        "✅ Out early? Tap <b>Collecting now</b> to free it.",
        keyboards.started_keyboard(machine, session["id"]),
    )


async def cb_extend_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, raw_sid, raw_minutes = query.data.split(":")
    session_id, minutes = int(raw_sid), int(raw_minutes)

    row = db.get_session(session_id)
    if row is None or row["status"] != "active" or row["user_id"] != update.effective_user.id:
        await query.answer("That cycle already finished.", show_alert=True)
        machine = config.MACHINES.get(row["machine"]) if row is not None else None
        if machine is not None:
            await _show_machine(update, machine, update.effective_user.id)
        return

    updated = db.extend_session(session_id, minutes)
    if updated is None:
        await query.answer("That cycle already finished.", show_alert=True)
        return

    jobs.schedule_done(context.job_queue, session_id, util.parse_iso(updated["ends_at"]))

    machine = config.MACHINES[updated["machine"]]
    await query.answer("Time added ⏱️")
    await _safe_edit(
        update,
        f"✅ <b>Extended!</b> {machine.label} · total {updated['duration_min']} min · "
        f"est. done <b>{util.fmt_clock(util.parse_iso(updated['ends_at']))}</b>.",
        keyboards.started_keyboard(machine, session_id),
    )


# --------------------------------------------------------------------------
# early finish ("collected" before the timer runs out)
# --------------------------------------------------------------------------


async def cb_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    session_id = int(query.data.split(":")[1])
    row = db.get_session(session_id)
    if row is None or row["status"] != "active" or row["user_id"] != update.effective_user.id:
        await query.answer("That cycle already finished.", show_alert=True)
        return

    await query.answer()
    machine = config.MACHINES[row["machine"]]
    await _safe_edit(
        update,
        f"✅ <b>Collecting {machine.label} now?</b>\n"
        "It'll show 🟢 free straight away so someone else can use it.",
        keyboards.stop_confirm_keyboard(machine.id, session_id),
    )


async def cb_stop_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    session_id = int(query.data.split(":")[1])
    row = db.get_session(session_id)
    if row is None or row["user_id"] != update.effective_user.id:
        await query.answer("That isn't your cycle.", show_alert=True)
        return

    freed = db.finish_early(session_id)
    jobs.cancel_session_jobs(context.job_queue, session_id)
    await query.answer("Thanks! ✅" if freed else "That cycle already finished.")

    machine = config.MACHINES[row["machine"]]
    await _safe_edit(
        update,
        f"✅ <b>{machine.label}</b> is free again. Thanks for clearing it! 🙏",
        keyboards.machine_view_keyboard(machine),
    )


# --------------------------------------------------------------------------
# nudge (callback data and /ping keep their v1 names; residents never see them)
# --------------------------------------------------------------------------


async def cb_nudge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    machine = _machine_from(query.data)
    if machine is None:
        await query.answer("That machine is gone 🤔", show_alert=True)
        return

    if db.get_active(machine.id) is not None:
        await query.answer("That machine is still running 🔴", show_alert=True)
        return

    latest = db.get_latest(machine.id)
    if latest is None:
        await query.answer(f"Nobody has used {machine.label} yet.", show_alert=True)
        return
    if latest["user_id"] == update.effective_user.id:
        await query.answer("That's your own load 🙂 nothing to nudge.", show_alert=True)
        return

    handle = util.handle_plain(latest["username"], latest["name"])

    # Take the cooldown slot BEFORE sending. Two residents tapping 🔔 in the
    # same second would otherwise both pass a read-only check and both DM the
    # owner; here exactly one wins the claim and the other is told to wait.
    claimed, previous = db.claim_nudge(latest["id"], config.PING_COOLDOWN_MIN)
    if not claimed:
        since = util.elapsed_min(util.parse_iso(previous)) if previous else 0
        when = "just now" if since <= 0 else f"{since} min ago"
        await query.answer(
            f"Already nudged {when}. You can PM them: {handle}", show_alert=True
        )
        return

    ago = util.elapsed_min(util.finished_at(latest))
    nudger = util.nudger_html(db.get_user(update.effective_user.id))
    try:
        await context.bot.send_message(
            latest["user_id"],
            f"🔔 <b>Nudge!</b> {nudger} needs <b>{machine.label}</b>. Your laundry "
            f"finished {ago} min ago, please clear it when you can 🙏\n"
            f"{texts.COLLECT_RULE_OWNER}",
        )
    except TelegramError as exc:
        logger.warning("Nudge to %s failed: %s", latest["user_id"], exc)
        # The DM never went out, so hand the slot back instead of blocking the
        # next person for the whole cooldown.
        db.release_nudge(latest["id"], previous)
        await query.answer(
            f"Couldn't reach {latest['name']} (they may have blocked the bot). "
            f"Try a PM: {handle}",
            show_alert=True,
        )
        return

    await query.answer(
        f"✅ Nudged {latest['name']} ({latest['room']}). You can also PM them: {handle}",
        show_alert=True,
    )


# --------------------------------------------------------------------------
# leader-only reset
# --------------------------------------------------------------------------


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/resetmachines`` — deliberately absent from the command menu."""
    if not config.is_admin(update.effective_user.username):
        await update.effective_message.reply_text(ADMINS_ONLY)
        return
    await update.effective_message.reply_text(
        RESET_ASK_TEXT, reply_markup=keyboards.reset_confirm_keyboard()
    )


async def cb_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    # Re-checked here too: a hub message rendered for a leader stays tappable.
    if not config.is_admin(update.effective_user.username):
        await query.answer(ADMINS_ONLY, show_alert=True)
        return
    await query.answer()
    await _safe_edit(update, RESET_ASK_TEXT, keyboards.reset_confirm_keyboard())


async def cb_reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not config.is_admin(user.username):
        await query.answer(ADMINS_ONLY, show_alert=True)
        return

    session_ids = db.reset_machines()
    for session_id in session_ids:
        jobs.cancel_session_jobs(context.job_queue, session_id)
    logger.info(
        "Machines reset by @%s (%s): %s running session(s) cancelled",
        user.username,
        user.id,
        len(session_ids),
    )

    await query.answer("All machines reset 🔄")
    await _safe_edit(update, RESET_DONE_TEXT, keyboards.hub_only_keyboard())
