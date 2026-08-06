"""Application configuration for Noctua Bot.

Values are loaded from environment variables (and an optional ``.env`` file
at the project root) at import time. Importing this module must never raise
— even when required settings like ``BOT_TOKEN`` are missing — only
``bot.main.main()`` validates that and exits with a friendly message.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Machine:
    id: str
    label: str                  # "Washer 1"
    kind: str                   # "washer" | "dryer"
    emoji: str                  # "🫧" | "💨"
    durations: tuple[int, ...]  # minutes


PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# Load .env (if present) into the environment. Never overrides real env vars
# that are already set, so a deployment's real environment always wins.
load_dotenv(PROJECT_ROOT / ".env")

MACHINES: dict[str, Machine] = {
    "w1": Machine(id="w1", label="Washer 1", kind="washer", emoji="🫧", durations=(30,)),
    "w2": Machine(id="w2", label="Washer 2", kind="washer", emoji="🫧", durations=(30,)),
    "d1": Machine(id="d1", label="Dryer 1", kind="dryer", emoji="💨", durations=(30, 45, 60)),
    "d2": Machine(id="d2", label="Dryer 2", kind="dryer", emoji="💨", durations=(30, 45, 60)),
}

# Normalize "" (unset in .env) to None so callers can just check truthiness.
BOT_TOKEN: str | None = os.environ.get("BOT_TOKEN") or None


def _parse_leader_usernames(raw: str) -> set[str]:
    """Parse a comma-separated LEADER_USERNAMES value into lowercase handles."""
    usernames: set[str] = set()
    for part in raw.split(","):
        name = part.strip().lstrip("@").strip().lower()
        if name:
            usernames.add(name)
    return usernames


# Two tiers. Leaders can send announcements; admins can also reset the
# machines. Every admin is implicitly a leader, so nobody is listed twice.
ADMIN_USERNAMES: set[str] = _parse_leader_usernames(os.environ.get("ADMIN_USERNAMES", ""))
LEADER_USERNAMES: set[str] = (
    _parse_leader_usernames(os.environ.get("LEADER_USERNAMES", "")) | ADMIN_USERNAMES
)
# Who residents should contact about wrong name/room details.
CONTACT_HANDLE: str = os.environ.get("CONTACT_HANDLE", "daven_koh").strip().lstrip("@")
# Dry-run audience for /poll: when set, a poll reaches only these handles
# instead of every registered resident. Leave empty in normal operation.
POLL_TEST_HANDLES: set[str] = _parse_leader_usernames(
    os.environ.get("POLL_TEST_HANDLES", "")
)
TIMEZONE: ZoneInfo = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Singapore"))
DB_PATH: str = os.environ.get("DB_PATH", str(PROJECT_ROOT / "noctua.db"))
PING_COOLDOWN_MIN: int = 3      # min minutes between "nudge last user" nudges per machine


def _normalize(username: str | None) -> str:
    return (username or "").strip().lstrip("@").strip().lower()


def is_leader(username: str | None) -> bool:
    """Can send announcements. Admins count as leaders too."""
    handle = _normalize(username)
    return bool(handle) and handle in LEADER_USERNAMES


def is_admin(username: str | None) -> bool:
    """Can also reset every machine back to free."""
    handle = _normalize(username)
    return bool(handle) and handle in ADMIN_USERNAMES
