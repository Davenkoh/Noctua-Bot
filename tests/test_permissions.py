"""Tests for the two permission tiers: leader (announce) and admin (reset).

Run with: ./.venv/bin/python -m tests.test_permissions

Sets DB_PATH, BOT_TOKEN and the two username lists *before* anything imports
bot.config, which reads them at import time. So this never touches the real
noctua.db or the real leader list, even while the live bot is running.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

# --- must happen before any `bot.*` import below ---------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="noctua_test_perms_")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "test_perms.db")
os.environ["BOT_TOKEN"] = "test-dummy-token"
os.environ["ADMIN_USERNAMES"] = "boss"
os.environ["LEADER_USERNAMES"] = "lead_one, @Lead_Two"

from bot import commands, config, db, keyboards, texts  # noqa: E402
from bot.handlers import registration  # noqa: E402


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class FakeUser:
    def __init__(self, username: str | None, user_id: int = 1) -> None:
        self.username = username
        self.id = user_id


class FakeUpdate:
    def __init__(self, username: str | None, user_id: int = 1) -> None:
        self.effective_user = FakeUser(username, user_id)
        self.effective_message = FakeMessage()


class FakeContext:
    job_queue = None


# --------------------------------------------------------------------------
# config tiers
# --------------------------------------------------------------------------


def test_admin_is_implicitly_a_leader() -> int:
    assert config.is_admin("boss"), "the admin list must grant admin"
    assert config.is_leader("boss"), "an admin must count as a leader too"
    assert config.is_leader("lead_one"), "a listed leader is a leader"
    assert not config.is_admin("lead_one"), "a leader must NOT be an admin"
    return 4


def test_tiers_normalise_at_and_case_and_reject_nobody() -> int:
    assert config.is_admin("@BOSS"), "'@' and case must not matter"
    assert config.is_leader("@lead_two"), "list entries are normalised too"
    for nobody in (None, "", "  ", "stranger"):
        assert not config.is_leader(nobody), f"{nobody!r} must not be a leader"
        assert not config.is_admin(nobody), f"{nobody!r} must not be an admin"
    return 6


def test_command_menu_matches_the_tier() -> int:
    resident = [c.command for c in commands.menu_for("stranger")]
    leader = [c.command for c in commands.menu_for("lead_one")]
    admin = [c.command for c in commands.menu_for("boss")]

    assert "announce" not in resident and "resetmachines" not in resident
    assert "poll" not in resident, "residents answer polls, they don't create them"
    assert "schedule" not in resident and "waiting" not in resident
    assert "announce" in leader and "poll" in leader, "leaders get announce + poll"
    assert "schedule" in leader and "waiting" in leader, "and scheduling, and its list"
    assert "resetmachines" not in leader, "leaders must NOT see /resetmachines"
    assert "announce" in admin and "poll" in admin and "resetmachines" in admin
    assert "schedule" in admin and "waiting" in admin
    # /resetme is hidden from every tier, admins included.
    for menu in (resident, leader, admin):
        assert "resetme" not in menu, "/resetme must stay out of the menu"
    return 9


def test_the_poll_button_is_leaders_only() -> int:
    resident = keyboards.main_menu(is_leader=False).keyboard
    leader = keyboards.main_menu(is_leader=True).keyboard

    # ReplyKeyboardMarkup turns the caption strings into KeyboardButtons.
    flat_resident = [b.text for row in resident for b in row]
    flat_leader = [b.text for row in leader for b in row]
    assert keyboards.MENU_POLL not in flat_resident, "residents must not see Poll"
    assert keyboards.MENU_ANNOUNCE not in flat_resident
    assert keyboards.MENU_RECALL not in flat_resident, "residents cannot recall"
    assert keyboards.MENU_POLL in flat_leader, "leaders get the Poll button"
    assert keyboards.MENU_ANNOUNCE in flat_leader
    assert keyboards.MENU_RECALL in flat_leader, "and the recall door"
    # A button caption that no regex matches is a button that does nothing.
    import re

    assert re.match(keyboards.RX_POLL, keyboards.MENU_POLL), "RX_POLL must match its caption"
    assert re.match(keyboards.RX_RECALL, keyboards.MENU_RECALL), "RX_RECALL too"
    assert re.match(keyboards.RX_ANNOUNCE, keyboards.MENU_ANNOUNCE), "RX_ANNOUNCE too"
    # Retired captions still have to route, or a leader whose keyboard predates
    # the merge taps a dead button forever.
    assert re.match(keyboards.RX_ANNOUNCE, keyboards.LEGACY_ANNOUNCE), "old Announce"
    assert re.match(keyboards.RX_SCHEDULE, keyboards.LEGACY_SCHEDULE), "old Scheduled"
    # And they must not both open the same door: the retired one led to the
    # timed step, which is the whole reason it is kept separate.
    assert not re.match(keyboards.RX_ANNOUNCE, keyboards.LEGACY_SCHEDULE)
    assert not re.match(keyboards.RX_SCHEDULE, keyboards.MENU_ANNOUNCE)
    return 12


# --------------------------------------------------------------------------
# /resetme is admin-only
# --------------------------------------------------------------------------


def _run_reset_me(username: str | None, user_id: int = 1) -> FakeUpdate:
    update = FakeUpdate(username, user_id)
    asyncio.run(registration.reset_me(update, FakeContext()))
    return update


def test_resetme_refuses_a_leader_and_a_plain_resident() -> int:
    db.init_db()
    for username in ("lead_one", "stranger", None):
        update = _run_reset_me(username)
        assert update.effective_message.replies == [texts.ADMIN_ONLY], (
            f"{username!r} must be refused, got {update.effective_message.replies}"
        )
    return 3


def test_resetme_lets_the_admin_wipe_their_own_registration() -> int:
    db.init_db()
    db.upsert_user(42, "boss", "Boss", "#08-27")
    assert db.get_user(42) is not None, "sanity: the admin is registered"

    update = _run_reset_me("boss", user_id=42)
    assert update.effective_message.replies, "the admin should get a reply"
    assert texts.ADMIN_ONLY not in update.effective_message.replies[0]
    assert db.get_user(42) is None, "the admin's own row should be gone"
    return 3


def test_resetme_only_touches_the_caller() -> int:
    db.init_db()
    db.upsert_user(42, "boss", "Boss", "#08-27")
    db.upsert_user(43, "lead_one", "Lead", "#06-01A")

    _run_reset_me("boss", user_id=42)
    assert db.get_user(42) is None, "caller wiped"
    assert db.get_user(43) is not None, "everyone else must be untouched"
    return 2


def main() -> None:
    tests = (
        test_admin_is_implicitly_a_leader,
        test_tiers_normalise_at_and_case_and_reject_nobody,
        test_command_menu_matches_the_tier,
        test_the_poll_button_is_leaders_only,
        test_resetme_refuses_a_leader_and_a_plain_resident,
        test_resetme_lets_the_admin_wipe_their_own_registration,
        test_resetme_only_touches_the_caller,
    )
    total_cases = 0
    try:
        for test in tests:
            total_cases += test()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)  # best-effort cleanup

    summary = f"{len(tests)}/{len(tests)} test functions, {total_cases} cases"
    print(f"PASS: {summary} — tests/test_permissions.py")


if __name__ == "__main__":
    main()
