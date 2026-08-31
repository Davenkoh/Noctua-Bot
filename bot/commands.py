"""The slash-command menu Telegram shows when someone types "/".

Every menu button has a matching command here, so residents can drive the bot
by typing as well as tapping. Leader-only commands are pushed per chat, since
Telegram has no "only these users" scope.
"""

from __future__ import annotations

import logging

from telegram import Bot, BotCommand, BotCommandScopeChat
from telegram.error import TelegramError

from . import config, db

logger = logging.getLogger(__name__)

# One command per menu button, in the order residents meet them.
COMMANDS = [
    BotCommand("start", "Register or open the menu"),
    BotCommand("laundry", "Laundry menu: machines, status, nudges"),
    BotCommand("use", "Start a washer or dryer"),
    BotCommand("status", "See which machines are free"),
    BotCommand("ping", "Nudge whoever left their load inside"),
    BotCommand("profile", "Your saved name and room"),
    BotCommand("help", "How this bot works"),
    BotCommand("cancel", "Back out of what you're doing"),
]

ANNOUNCE = BotCommand("announce", "Send an announcement to everyone")
SCHEDULE = BotCommand("schedule", "Write an announcement to go out later")
WAITING = BotCommand("waiting", "Announcements waiting to go out")
POLL = BotCommand("poll", "Ask everyone who's in")
RECALL = BotCommand("recall", "Undo an announcement you sent")
RESET = BotCommand("resetmachines", "Set all machines back to free")

# Leaders can announce and poll; admins can also reset the machines.
LEADER_COMMANDS = COMMANDS + [ANNOUNCE, SCHEDULE, WAITING, POLL, RECALL]
ADMIN_COMMANDS = COMMANDS + [ANNOUNCE, SCHEDULE, WAITING, POLL, RECALL, RESET]


def menu_for(username: str | None) -> list[BotCommand]:
    """The command list this person should see."""
    if config.is_admin(username):
        return ADMIN_COMMANDS
    if config.is_leader(username):
        return LEADER_COMMANDS
    return COMMANDS


async def set_for(bot: Bot, chat_id: int, username: str | None) -> bool:
    """Give one chat the menu matching that person's tier."""
    try:
        await bot.set_my_commands(
            menu_for(username), scope=BotCommandScopeChat(chat_id=chat_id)
        )
        return True
    except TelegramError as exc:
        logger.warning("Could not set commands for %s: %s", chat_id, exc)
        return False


async def refresh(bot: Bot) -> int:
    """Set the default menu, then top up every leader's and admin's chat."""
    await bot.set_my_commands(COMMANDS)
    pushed = 0
    for row in db.leader_rows(config.LEADER_USERNAMES):
        if await set_for(bot, row["user_id"], row["username"]):
            pushed += 1
    if pushed:
        logger.info("Extended command menu pushed to %s chat(s)", pushed)
    return pushed
