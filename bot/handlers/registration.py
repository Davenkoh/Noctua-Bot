"""Onboarding (``/start``), the unregistered gate, and ``/profile`` edits."""

from __future__ import annotations

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .. import commands, config, db, jobs, keyboards, rooms, texts, util

# Onboarding states
ASK_NAME, ASK_ROOM, ASK_LETTER, CONFIRM = range(4)

ONBOARDING_KEY = "onboarding"
DRAFT_KEY = "registration_draft"
BASE_KEY = "suite_base"

GATE_TEXT = "🦉 Please register first. Tap /start"
ROOM_PROMPT = "What's your <b>room</b>? (e.g. #06-27)"

NO_USERNAME_TEXT = (
    "This bot matches residents by Telegram username.\n"
    "Add a username in Telegram <b>Settings → Username</b>, then tap /start again."
)
NOT_ON_ROSTER_TEXT = (
    "🚫 This bot is for Noctua residents.\n"
    "If you live here, ask a dorm leader to add your handle ({handle}) to the "
    "resident list."
)


def _menu(update: Update) -> ReplyKeyboardMarkup:
    return keyboards.main_menu(config.is_leader(update.effective_user.username))


def _overview(row, *, is_leader: bool, greeting: str) -> str:
    """The /start card, which is the same card ❓ Help shows."""
    return texts.overview(row, is_leader=is_leader, greeting=greeting)


# --------------------------------------------------------------------------
# gate + username refresh (registered in group -1)
# --------------------------------------------------------------------------


async def _refresh_stale_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Replace a reply keyboard left over from an older version of the bot.

    Telegram keeps a reply keyboard in the client until the bot sends a new
    one, and only a handful of handlers ever do — so after a button is renamed
    residents keep tapping the old caption indefinitely. That caption arriving
    is the signal, and swapping the keyboard here means it happens exactly once
    per resident, on whatever they tap next.
    """
    message = update.message
    if message is None or message.text not in keyboards.LEGACY_CAPTIONS:
        return
    await message.reply_text(
        "🔄 Menu updated, check the buttons below.",
        reply_markup=_menu(update),
    )


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh handles and stale menus for residents; stop everyone else."""
    user = update.effective_user
    if user is None or user.is_bot:
        return

    row = db.get_user(user.id)
    if row is not None:
        if (row["username"] or None) != (user.username or None):
            db.touch_username(user.id, user.username)
        await _refresh_stale_menu(update, context)
        return

    if context.user_data.get(ONBOARDING_KEY):
        return
    # Only a real incoming message can be /start (never a button's caption).
    text = (update.message.text or "") if update.message is not None else ""
    if text.startswith("/start"):
        return

    if update.callback_query is not None:
        await update.callback_query.answer(GATE_TEXT, show_alert=True)
    chat = update.effective_chat
    if chat is not None:
        await context.bot.send_message(chat.id, GATE_TEXT, reply_markup=ReplyKeyboardRemove())
    raise ApplicationHandlerStop


# --------------------------------------------------------------------------
# onboarding
# --------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if config.is_leader(user.username):
        # Cheap, and means a new leader gets /announce without a restart.
        await commands.set_for(context.bot, update.effective_chat.id, user.username)

    row = db.get_user(user.id)
    if row is not None:
        # Residents who registered before a roster import keep their access.
        context.user_data.pop(ONBOARDING_KEY, None)
        await update.effective_message.reply_text(
            _overview(
                row,
                is_leader=config.is_leader(user.username),
                greeting=texts.greeting(row["name"]),
            ),
            reply_markup=_menu(update),
        )
        return ConversationHandler.END

    # A roster only gates registration once the leader has imported one.
    if db.roster_count() > 0:
        return await _start_from_roster(update, context)

    return await _ask_details(update, context)


async def _ask_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open onboarding: ask for a name, then a room."""
    context.user_data[ONBOARDING_KEY] = True
    context.user_data[DRAFT_KEY] = {}
    await update.effective_message.reply_text(
        "🦉 Welcome to <b>Noctua Bot</b>! Let's get you set up, it takes 20 seconds. "
        "What's your <b>name</b>?",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_NAME


async def _start_from_roster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Whitelisted onboarding: the tele tag maps straight to name + room."""
    user = update.effective_user
    context.user_data.pop(ONBOARDING_KEY, None)
    context.user_data.pop(DRAFT_KEY, None)

    if not user.username:
        await update.effective_message.reply_text(
            NO_USERNAME_TEXT, reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    entry = db.roster_lookup(user.username)
    if entry is None:
        await update.effective_message.reply_text(
            NOT_ON_ROSTER_TEXT.format(handle=f"@{util.esc(user.username)}"),
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    if not entry["room"]:
        # Whitelisted, but the list has no room for them (test accounts, guests,
        # a resident whose room isn't settled yet). Access is the part the
        # roster decides; the details they can answer themselves.
        return await _ask_details(update, context)

    # Zero questions: the resident list is the source of truth.
    db.upsert_user(user.id, user.username, entry["name"], entry["room"])
    await update.effective_message.reply_text(
        _overview(
            db.get_user(user.id),
            is_leader=config.is_leader(user.username),
            greeting=texts.greeting(entry["name"]),
        ),
        reply_markup=_menu(update),
    )
    return ConversationHandler.END


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = util.clean_name(update.effective_message.text)
    if name is None:
        await update.effective_message.reply_text(
            "I need something between 1 and 40 characters. What should I call you?"
        )
        return ASK_NAME

    draft = context.user_data.setdefault(DRAFT_KEY, {})
    draft["name"] = name
    if draft.get("room"):  # roster prefilled the room — nothing left to ask
        return await _ask_confirm(update, context)

    await update.effective_message.reply_text(f"Hi {util.esc(name)}! {ROOM_PROMPT}")
    return ASK_ROOM


async def got_room(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    result = rooms.validate_room(update.effective_message.text or "")
    if result.ok:
        context.user_data.setdefault(DRAFT_KEY, {})["room"] = result.room
        return await _ask_confirm(update, context)

    if result.needs_letter:
        context.user_data[BASE_KEY] = result.base
        await update.effective_message.reply_text(
            f"Room #{util.esc(result.base)} has units A-F, tap yours:",
            reply_markup=keyboards.suite_letters_keyboard(rooms.suite_letters(result.base)),
        )
        return ASK_LETTER

    await update.effective_message.reply_text(f"{util.esc(result.error)}\n\n{ROOM_PROMPT}")
    return ASK_ROOM


async def got_letter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    letter = query.data.split(":")[1]
    base = context.user_data.get(BASE_KEY)
    if not base:
        await query.edit_message_text(f"Let's try that again. {ROOM_PROMPT}")
        return ASK_ROOM

    result = rooms.validate_room(f"{base}{letter}")
    if not result.ok:
        await query.edit_message_text(f"{util.esc(result.error or 'Hmm.')}\n\n{ROOM_PROMPT}")
        return ASK_ROOM

    context.user_data.setdefault(DRAFT_KEY, {})["room"] = result.room
    context.user_data.pop(BASE_KEY, None)
    await query.edit_message_text(f"Room: <b>{util.esc(result.room)}</b>")
    return await _ask_confirm(update, context)


async def _ask_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data.get(DRAFT_KEY, {})
    await context.bot.send_message(
        update.effective_chat.id,
        f"Name: <b>{util.esc(draft.get('name'))}</b>\n"
        f"Room: <b>{util.esc(draft.get('room'))}</b>\n"
        "All correct?",
        reply_markup=keyboards.registration_confirm_keyboard(roster=bool(draft.get("roster"))),
    )
    return CONFIRM


async def confirm_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = context.user_data.get(DRAFT_KEY, {})
    name, room = draft.get("name"), draft.get("room")
    if not name or not room:
        await query.edit_message_text("Something got lost. Tap /start to try again.")
        context.user_data.pop(ONBOARDING_KEY, None)
        return ConversationHandler.END

    user = update.effective_user
    db.upsert_user(user.id, user.username, name, room)
    context.user_data.pop(ONBOARDING_KEY, None)
    context.user_data.pop(DRAFT_KEY, None)
    context.user_data.pop(BASE_KEY, None)

    await query.edit_message_text(
        f"✅ Name: <b>{util.esc(name)}</b>\n✅ Room: <b>{util.esc(room)}</b>"
    )
    await context.bot.send_message(
        update.effective_chat.id,
        _overview(
            db.get_user(user.id),
            is_leader=config.is_leader(user.username),
            greeting=texts.greeting(name),
        ),
        reply_markup=_menu(update),
    )
    return ConversationHandler.END


async def confirm_redo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = context.user_data.get(DRAFT_KEY, {})
    context.user_data.pop(BASE_KEY, None)

    if draft.get("roster"):
        # Only the name is theirs to fix; the room comes from the resident list.
        context.user_data[DRAFT_KEY] = {"room": draft.get("room"), "roster": True}
        await query.edit_message_text("No problem! What should I call you?")
        return ASK_NAME

    context.user_data[DRAFT_KEY] = {}
    await query.edit_message_text("No problem, starting over.")
    await context.bot.send_message(update.effective_chat.id, "What's your <b>name</b>?")
    return ASK_NAME


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    registered = db.get_user(update.effective_user.id) is not None
    context.user_data.pop(ONBOARDING_KEY, None)
    context.user_data.pop(DRAFT_KEY, None)
    context.user_data.pop(BASE_KEY, None)
    if registered:
        await update.effective_message.reply_text("Okay, cancelled 👍", reply_markup=_menu(update))
    else:
        await update.effective_message.reply_text(
            "Okay, cancelled. Tap /start whenever you're ready to register.",
            reply_markup=ReplyKeyboardRemove(),
        )
    return ConversationHandler.END


async def reset_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Leader-only ``/resetme``: wipe own registration to re-test onboarding."""
    user = update.effective_user
    if not config.is_leader(user.username):
        await update.effective_message.reply_text("🔒 Leaders only.")
        return

    active_ids = db.purge_user(user.id)
    for session_id in active_ids:
        jobs.cancel_session_jobs(context.job_queue, session_id)
    await update.effective_message.reply_text(
        "🧹 You've been reset. Tap /start to register again.",
        reply_markup=ReplyKeyboardRemove(),
    )


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only details card — corrections go through a dorm leader."""
    row = db.get_user(update.effective_user.id)
    if row is None:
        await update.effective_message.reply_text(GATE_TEXT)
        return

    handle = f"@{util.esc(row['username'])}" if row["username"] else "not set"
    await update.effective_message.reply_text(
        f"👤 <b>{util.esc(row['name'])}</b>\n"
        f"🏠 {util.esc(row['room'])}\n"
        f"💬 {handle}\n\n"
        f"Wrong details, or anything else? Text @{util.esc(config.CONTACT_HANDLE)}.",
        reply_markup=_menu(update),
    )


# --------------------------------------------------------------------------
# handler factories
# --------------------------------------------------------------------------


def onboarding_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            ASK_ROOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_room)],
            ASK_LETTER: [
                CallbackQueryHandler(got_letter, pattern=keyboards.PAT_SUITE_LETTER),
                # Typing the full room instead of tapping a letter also works.
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_room),
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_ok, pattern=keyboards.PAT_REG_OK),
                CallbackQueryHandler(confirm_redo, pattern=keyboards.PAT_REG_REDO),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        name="onboarding",
    )


