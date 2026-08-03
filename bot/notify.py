"""Send one message straight to named residents, through the bot.

    python -m bot.notify @sarahchauuu @jeshuatjx "You can send announcements now"
    python -m bot.notify --send @sarahchauuu "You can send announcements now"

``/announce`` goes to all 120 rooms, which is the wrong shape for telling two
people something that concerns only them. This takes handles instead.

A bot cannot open a chat with someone who has never pressed Start, so a handle
that isn't registered yet is reported and skipped rather than failing the run.
That is also the honest answer to "did they get it": an unregistered resident
has to be told some other way.

The message is sent as HTML, the same as everything else the bot says, so
``<b>bold</b>`` works and a literal ``<`` does not.

Nothing is sent without ``--send``, and the recipients are printed either way.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from . import config, db

SEND_DELAY_S = 0.05  # same courtesy to the rate limit as a broadcast
USAGE = __doc__

Recipient = tuple[str, sqlite3.Row | None]


def clean_handle(raw: str) -> str:
    return raw.strip().lstrip("@").strip().lower()


def parse_args(args: list[str]) -> tuple[list[str], str, bool] | None:
    """``[--send] @handle... message`` into handles, message, write flag.

    Handles are taken only from the front and only with their ``@``, so a
    message that mentions someone by handle stays part of the message.
    """
    send = "--send" in args
    rest = [arg for arg in args if arg != "--send"]

    handles: list[str] = []
    while rest and rest[0].startswith("@"):
        handle = clean_handle(rest.pop(0))
        if handle and handle not in handles:
            handles.append(handle)

    message = " ".join(rest).strip()
    if not handles or not message:
        return None
    return handles, message, send


def plan(handles: list[str]) -> list[Recipient]:
    """Pair each handle with the registered resident it reaches, or None."""
    return [(handle, db.user_by_handle(handle)) for handle in handles]


def report(rows: list[Recipient], message: str, *, sending: bool) -> None:
    """Print the message and who it is about to reach, before anything is sent."""
    print("─" * 46)
    print(message)
    print("─" * 46)
    print("Sending:" if sending else "Plan (nothing sent yet):")

    width = max(len(handle) for handle, _ in rows) + 1
    for handle, row in rows:
        tag = f"@{handle}"
        if row is None:
            print(f"  ❌ {tag:<{width}} not registered, so the bot cannot open a chat")
            continue
        mark = "→" if sending else "·"
        print(f"  {mark} {tag:<{width}} {row['name']} ({row['room']})")


async def deliver(rows: list[Recipient], message: str) -> list[str]:
    """Send to everyone reachable. Returns the handles that failed."""
    reachable = [(handle, row) for handle, row in rows if row is not None]
    if not reachable:
        return []

    failed: list[str] = []
    async with Bot(config.BOT_TOKEN) as bot:
        for handle, row in reachable:
            try:
                await bot.send_message(row["user_id"], message, parse_mode=ParseMode.HTML)
                print(f"  ✅ @{handle}")
            except TelegramError as exc:
                # Blocked the bot, deleted the chat, or a bad HTML tag. Print
                # which, since the fix differs for each.
                failed.append(handle)
                print(f"  ❌ @{handle} was not reached: {exc}")
            await asyncio.sleep(SEND_DELAY_S)
    return failed


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(USAGE)
        return 0 if args else 2

    parsed = parse_args(args)
    if parsed is None:
        print(USAGE)
        print("❌ Need at least one @handle followed by the message.")
        return 2
    handles, message, send = parsed

    if send and not config.BOT_TOKEN:
        print("❌ BOT_TOKEN is missing, so nothing can be sent.")
        return 1

    db.init_db()
    rows = plan(handles)
    report(rows, message, sending=send)
    failed = asyncio.run(deliver(rows, message)) if send else []

    missing = [handle for handle, row in rows if row is None]
    skipped = len(missing) + len(failed)
    reached = len(rows) - skipped

    print()
    if send:
        print(f"{reached} resident(s) got it, {skipped} did not.")
    else:
        print(f"{reached} resident(s) would get it, {skipped} would be skipped.")
        print("Re-run with --send to send it.")
    if missing:
        print("Not registered yet: " + ", ".join(f"@{handle}" for handle in missing))
    return 1 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
