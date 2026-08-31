"""Entry point — ``python -m bot.main``."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, Defaults

from . import commands, config, db, jobs
from .handlers import broadcast, register_all

logger = logging.getLogger(__name__)


MISSING_TOKEN = (
    "❌ BOT_TOKEN is missing.\n"
    "   Copy .env.example to .env and paste the token @BotFather gave you."
)


async def post_init(application: Application) -> None:
    """Runs once before polling: schema, timer restore, command menu."""
    db.init_db()
    restored = jobs.restore_jobs(application)
    if restored:
        logger.info("Restored %s laundry timer(s) from the database", restored)
    armed, missed = await broadcast.restore_scheduled(application)
    if armed or missed:
        logger.info(
            "Scheduled announcements: %s re-armed, %s written off as too late",
            armed,
            missed,
        )
    await commands.refresh(application.bot)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.callback_query is not None:
        try:
            await update.callback_query.answer(
                "Something went wrong 😵 Please try again.", show_alert=True
            )
        except Exception:  # the query may already be answered or expired
            pass


def build_application(token: str | None = None) -> Application:
    """Build the fully wired app. Offline: no network call happens here."""
    resolved = token or config.BOT_TOKEN
    if not resolved:
        raise ValueError(MISSING_TOKEN)

    # Updates are handled concurrently. An announcement is a sequential fan-out
    # of two messages per resident, so at 120 rooms it holds the handler for
    # about a minute; on the default setting every laundry tap in that window
    # would sit in the queue behind it. The contested writes this exposes are
    # the ones bot.db already settles in a transaction (see start_session and
    # claim_nudge), which tests/test_concurrency.py covers.
    application = (
        Application.builder()
        .token(resolved)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
    register_all(application)
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s", level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not config.BOT_TOKEN:
        raise SystemExit(MISSING_TOKEN)

    application = build_application()
    logger.info("🦉 Noctua Bot is up, polling for updates")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
