"""Handler wiring — one module per feature, all registered from here.

Order matters: the unregistered gate sits in group -1, conversations claim
their entry points first, then the stateless button/command handlers.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from .. import keyboards
from . import (
    broadcast,
    help as help_module,
    laundry,
    poll,
    recall,
    registration,
    status,
)

__all__ = ["register_all"]


def register_all(application: Application) -> None:
    """Attach every Noctua handler to ``application``."""
    # Group -1: refresh handles, and stop unregistered users before anything else.
    application.add_handler(TypeHandler(Update, registration.gate), group=-1)

    # Conversations own /start and /broadcast (plus their menu buttons).
    application.add_handler(registration.onboarding_handler())
    application.add_handler(broadcast.broadcast_handler())
    application.add_handler(poll.poll_handler())

    # A scheduled announcement's receipt outlives the composer that produced
    # it, and its ❌ Cancel has to still work when the leader scrolls back to
    # it days later, so that button is a stateless handler too.
    # /waiting, not /scheduled: /schedule already opens the composer, and two
    # commands one letter apart that do different things is a trap. The old
    # spelling stays as an alias because it is what a leader will guess.
    application.add_handler(
        CommandHandler(["waiting", "scheduled"], broadcast.scheduled_command)
    )
    application.add_handler(
        CallbackQueryHandler(broadcast.cb_drop, pattern=keyboards.PAT_BC_DROP)
    )

    # Recall is stateless for the same reason, and for one more: the card
    # recomputes what is still in reach on every tap, so an old one scrolled
    # back to acts on what is true now instead of what it was drawn with.
    application.add_handler(CommandHandler("recall", recall.recall_command))
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(keyboards.RX_RECALL), recall.recall_command)
    )
    application.add_handler(
        CallbackQueryHandler(recall.cb_all, pattern=keyboards.PAT_RC_ALL)
    )
    application.add_handler(
        CallbackQueryHandler(recall.cb_one, pattern=keyboards.PAT_RC_ONE)
    )
    application.add_handler(
        CallbackQueryHandler(recall.cb_cancel, pattern=keyboards.PAT_RC_CANCEL)
    )

    # Poll cards live in every resident's DM long after the conversation that
    # created them ended, so their buttons are stateless handlers, not states.
    application.add_handler(
        CallbackQueryHandler(poll.cb_answer, pattern=keyboards.PAT_POLL_ANSWER)
    )
    application.add_handler(
        CallbackQueryHandler(poll.cb_refresh, pattern=keyboards.PAT_POLL_REFRESH)
    )

    # Profile is a read-only card; corrections go through a dorm leader.
    application.add_handler(CommandHandler("profile", registration.profile))
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(keyboards.RX_PROFILE), registration.profile)
    )

    # Laundry — every menu button also has a slash command
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(keyboards.RX_LAUNDRY), laundry.open_hub)
    )
    application.add_handler(CommandHandler("laundry", laundry.open_hub))
    application.add_handler(CommandHandler("use", laundry.use_command))
    application.add_handler(CommandHandler("ping", laundry.nudge_command))
    application.add_handler(CallbackQueryHandler(laundry.cb_hub, pattern=keyboards.PAT_HUB))
    application.add_handler(CallbackQueryHandler(laundry.cb_menu, pattern=keyboards.PAT_MENU))
    application.add_handler(CallbackQueryHandler(laundry.cb_machine, pattern=keyboards.PAT_MACHINE))
    application.add_handler(
        CallbackQueryHandler(laundry.cb_duration, pattern=keyboards.PAT_DURATION)
    )
    application.add_handler(
        CallbackQueryHandler(laundry.cb_extend_confirm, pattern=keyboards.PAT_EXTEND_OK)
    )
    application.add_handler(CallbackQueryHandler(laundry.cb_stop, pattern=keyboards.PAT_STOP))
    application.add_handler(
        CallbackQueryHandler(laundry.cb_stop_confirm, pattern=keyboards.PAT_STOP_OK)
    )
    application.add_handler(CallbackQueryHandler(laundry.cb_nudge, pattern=keyboards.PAT_PING))
    application.add_handler(
        CallbackQueryHandler(laundry.cb_nudge_picker, pattern=keyboards.PAT_PING_PICK)
    )

    # Leaders' "everything back to green" escape hatch (kept out of the menu).
    application.add_handler(CommandHandler("resetmachines", laundry.reset_command))
    application.add_handler(CallbackQueryHandler(laundry.cb_reset, pattern=keyboards.PAT_RESET))
    application.add_handler(
        CallbackQueryHandler(laundry.cb_reset_confirm, pattern=keyboards.PAT_RESET_OK)
    )

    # Status — the reply button is gone, but /status and stale keyboards stay
    application.add_handler(CommandHandler("status", status.status_command))
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(keyboards.RX_STATUS), status.status_command)
    )
    application.add_handler(CallbackQueryHandler(status.cb_status, pattern=keyboards.PAT_STATUS))

    # Help + a /cancel that lands outside any conversation
    application.add_handler(CommandHandler("help", help_module.help_command))
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(keyboards.RX_HELP), help_module.help_command)
    )
    application.add_handler(CommandHandler("cancel", help_module.cancel_outside))

    # Leader-only testing tool; deliberately absent from the command menu.
    application.add_handler(CommandHandler("resetme", registration.reset_me))
