"""SQLite persistence for Noctua Bot.

One module-level connection (WAL, ``sqlite3.Row``, ``check_same_thread=False``)
and plain synchronous calls — at ~120 residents on a single event loop the
queries are far too cheap to be worth threading.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from typing import Iterator

from . import config, util

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  username TEXT,
  name TEXT NOT NULL,
  room TEXT NOT NULL,
  registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine TEXT NOT NULL,
  user_id INTEGER NOT NULL REFERENCES users(user_id),
  started_at TEXT NOT NULL,
  duration_min INTEGER NOT NULL,
  ends_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  done_notified INTEGER NOT NULL DEFAULT 0,
  reminder_sent INTEGER NOT NULL DEFAULT 0,
  last_ping_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_machine ON sessions(machine, id DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE TABLE IF NOT EXISTS roster (
  handle TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  room TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS polls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question TEXT NOT NULL,
  created_by INTEGER NOT NULL REFERENCES users(user_id),
  created_at TEXT NOT NULL,
  closed_at TEXT
);
-- One row per resident the poll was delivered to, so a tap can re-render
-- that person's own card. answer is NULL until they choose.
CREATE TABLE IF NOT EXISTS poll_recipients (
  poll_id INTEGER NOT NULL REFERENCES polls(id),
  user_id INTEGER NOT NULL REFERENCES users(user_id),
  chat_id INTEGER NOT NULL,
  message_id INTEGER,
  answer TEXT,
  answered_at TEXT,
  PRIMARY KEY (poll_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_poll_recipients_poll ON poll_recipients(poll_id);
"""

# Every session read is joined with its owner so handlers can render
# "Name (#room) @handle" without a second query.
_SESSION_SELECT = """
SELECT s.*, u.name AS name, u.room AS room, u.username AS username
FROM sessions s
JOIN users u ON u.user_id = s.user_id
"""

_conn: sqlite3.Connection | None = None


def connection() -> sqlite3.Connection:
    """Lazily open the single shared connection."""
    global _conn
    if _conn is None:
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # isolation_level=None: we drive BEGIN/COMMIT ourselves in _tx().
        conn.isolation_level = None
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # A second process on the same file (a roster import run while the bot
        # is live, or sqlite3 on the shell) holds the write lock for a few ms.
        # Without this, BEGIN IMMEDIATE raises "database is locked" instantly
        # instead of simply waiting its turn.
        conn.execute("PRAGMA busy_timeout=5000")
        _conn = conn
    return _conn


def init_db() -> None:
    """Create the schema if needed. Safe to call on every startup."""
    connection().executescript(SCHEMA)


@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    """A write transaction — and the bot's only mutual-exclusion primitive.

    ``BEGIN IMMEDIATE`` takes the database's write lock up front, so a
    read-then-write pair placed inside one block ("is this machine free? then
    claim it") can never interleave with another one. Every check that decides
    who wins a contested action belongs in here, not in the calling handler.
    """
    conn = connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------


def upsert_user(user_id: int, username: str | None, name: str, room: str) -> None:
    """Register a resident, or overwrite name/room if they re-register."""
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, name, room, registered_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                name = excluded.name,
                room = excluded.room
            """,
            (user_id, username, name, room, util.to_iso(util.now_utc())),
        )


def get_user(user_id: int) -> sqlite3.Row | None:
    return connection().execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()


def update_user_fields(
    user_id: int, *, name: str | None = None, room: str | None = None
) -> None:
    fields: list[str] = []
    values: list[object] = []
    if name is not None:
        fields.append("name = ?")
        values.append(name)
    if room is not None:
        fields.append("room = ?")
        values.append(room)
    if not fields:
        return
    values.append(user_id)
    with _tx() as conn:
        conn.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?", tuple(values)
        )


def touch_username(user_id: int, username: str | None) -> None:
    """Keep the stored handle in sync — residents rename themselves often."""
    with _tx() as conn:
        conn.execute(
            "UPDATE users SET username = ? WHERE user_id = ?", (username, user_id)
        )


def purge_user(user_id: int) -> list[int]:
    """Delete a user and ALL their sessions (testing/removal tool).

    Returns the ids of their sessions that were still active so the caller
    can cancel the matching timer jobs.
    """
    with _tx() as conn:
        active_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM sessions WHERE user_id = ? AND status = 'active'",
                (user_id,),
            )
        ]
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    return active_ids


def user_by_handle(handle: str | None) -> sqlite3.Row | None:
    """A registered resident by Telegram handle, compared lowercase.

    Handles are not unique in ``users`` (a resident who changes their username
    frees it up), so the newest registration wins.
    """
    key = (handle or "").strip().lstrip("@").strip().lower()
    if not key:
        return None
    return connection().execute(
        "SELECT * FROM users WHERE lower(username) = ? ORDER BY registered_at DESC LIMIT 1",
        (key,),
    ).fetchone()


def leader_rows(handles: set[str]) -> list[sqlite3.Row]:
    """Registered users whose handle is in ``handles`` (compared lowercase)."""
    if not handles:
        return []
    placeholders = ",".join("?" for _ in handles)
    return connection().execute(
        f"SELECT * FROM users WHERE lower(username) IN ({placeholders})",
        tuple(sorted(handles)),
    ).fetchall()


def all_user_ids() -> list[int]:
    rows = connection().execute("SELECT user_id FROM users ORDER BY user_id").fetchall()
    return [row["user_id"] for row in rows]


def count_users() -> int:
    return int(connection().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


# --------------------------------------------------------------------------
# roster (the leader's resident whitelist, imported from Excel)
# --------------------------------------------------------------------------


def roster_count() -> int:
    """0 means "no sheet imported yet" — registration stays open."""
    return int(connection().execute("SELECT COUNT(*) AS n FROM roster").fetchone()["n"])


def roster_lookup(handle: str | None) -> sqlite3.Row | None:
    """Find a resident by Telegram handle (stored lowercase, no ``@``)."""
    if not handle:
        return None
    key = handle.strip().lstrip("@").strip().lower()
    if not key:
        return None
    return connection().execute(
        "SELECT * FROM roster WHERE handle = ?", (key,)
    ).fetchone()


def roster_replace(rows: list[tuple[str, str, str]]) -> int:
    """Wipe and reload the whole roster in one transaction. Returns row count."""
    with _tx() as conn:
        conn.execute("DELETE FROM roster")
        conn.executemany(
            "INSERT INTO roster (handle, name, room) VALUES (?, ?, ?)", rows
        )
    return len(rows)


def roster_all() -> list[sqlite3.Row]:
    return connection().execute("SELECT * FROM roster ORDER BY room, handle").fetchall()


def roster_rows_for_room(room: str) -> list[sqlite3.Row]:
    """Everyone currently listed at one room. A room is not unique in the
    schema, so a mistagged room can legitimately hold two rows until it's fixed.
    """
    return connection().execute(
        "SELECT * FROM roster WHERE room = ? ORDER BY handle", (room,)
    ).fetchall()


def roster_apply(removals: list[str], upserts: list[tuple[str, str, str]]) -> None:
    """Apply a batch of roster edits atomically.

    One transaction for the whole batch so a correction that retags a room
    (drop the wrong handle, add the right one) can never leave both handles
    whitelisted for that room, not even for the instant between two writes.
    Removals run first: that is what frees a room for its new occupant.
    """
    with _tx() as conn:
        for handle in removals:
            conn.execute("DELETE FROM roster WHERE handle = ?", (handle,))
        for row in upserts:
            conn.execute(
                "INSERT INTO roster (handle, name, room) VALUES (?, ?, ?) "
                "ON CONFLICT(handle) DO UPDATE SET name = excluded.name, "
                "room = excluded.room",
                row,
            )


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


def get_session(session_id: int) -> sqlite3.Row | None:
    return connection().execute(
        f"{_SESSION_SELECT} WHERE s.id = ?", (session_id,)
    ).fetchone()


def get_active(machine: str) -> sqlite3.Row | None:
    return connection().execute(
        f"{_SESSION_SELECT} WHERE s.machine = ? AND s.status = 'active' "
        "ORDER BY s.id DESC LIMIT 1",
        (machine,),
    ).fetchone()


def get_latest(machine: str) -> sqlite3.Row | None:
    """Newest session on a machine, whatever its status."""
    return connection().execute(
        f"{_SESSION_SELECT} WHERE s.machine = ? ORDER BY s.id DESC LIMIT 1",
        (machine,),
    ).fetchone()


def active_sessions() -> list[sqlite3.Row]:
    return connection().execute(
        f"{_SESSION_SELECT} WHERE s.status = 'active' ORDER BY s.id"
    ).fetchall()


def start_session(machine: str, user_id: int, minutes: int) -> sqlite3.Row | None:
    """Claim a machine.

    Atomic: returns ``None`` if somebody else grabbed it first. The free check
    and the INSERT share one ``_tx()``, so two residents tapping the same free
    machine in the same second produce exactly one session — the loser gets
    ``None`` and the handler re-renders the machine as taken. Only an
    ``active`` session blocks a start — a machine whose last load simply
    finished is 🟢 free and anyone may take it.
    """
    started = util.now_utc()
    ends = started + timedelta(minutes=minutes)
    with _tx() as conn:
        taken = conn.execute(
            "SELECT 1 FROM sessions WHERE machine = ? AND status = 'active' LIMIT 1",
            (machine,),
        ).fetchone()
        if taken is not None:
            return None

        cursor = conn.execute(
            """
            INSERT INTO sessions
                (machine, user_id, started_at, duration_min, ends_at, status)
            VALUES (?, ?, ?, ?, ?, 'active')
            """,
            (machine, user_id, util.to_iso(started), minutes, util.to_iso(ends)),
        )
        session_id = int(cursor.lastrowid)

    return get_session(session_id)


def extend_session(session_id: int, minutes: int) -> sqlite3.Row | None:
    """"Paid twice": push ``ends_at`` out and grow the recorded duration."""
    with _tx() as conn:
        row = conn.execute(
            "SELECT status, ends_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None or row["status"] != "active":
            return None
        new_ends = util.parse_iso(row["ends_at"]) + timedelta(minutes=minutes)
        conn.execute(
            """
            UPDATE sessions
            SET ends_at = ?, duration_min = duration_min + ?
            WHERE id = ?
            """,
            (util.to_iso(new_ends), minutes, session_id),
        )
    return get_session(session_id)


def mark_done(session_id: int) -> bool:
    """End a cycle whose timer is genuinely up.

    The ``ends_at`` check is the point: a "paid twice" extension that commits
    in the same instant the timer job fires must win, or the machine would go
    🟢 free with the load still spinning. Returns False in that case and the
    job re-arms itself for the new finish time.
    """
    with _tx() as conn:
        row = conn.execute(
            "SELECT status, ends_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None or row["status"] != "active":
            return False
        if util.parse_iso(row["ends_at"]) > util.now_utc():
            return False
        conn.execute(
            "UPDATE sessions SET status = 'done', done_notified = 1 WHERE id = ?",
            (session_id,),
        )
        return True


def finish_early(session_id: int) -> bool:
    """Owner took their load out before the timer ended.

    The session becomes a perfectly ordinary ``done`` one and ``ends_at`` moves
    to now, so every "finished {clock} ({X} min ago)" renderer keeps working off
    ``ends_at`` without a special case for early finishes.
    """
    with _tx() as conn:
        cursor = conn.execute(
            "UPDATE sessions SET status = 'done', ends_at = ? "
            "WHERE id = ? AND status = 'active'",
            (util.to_iso(util.now_utc()), session_id),
        )
        return cursor.rowcount > 0


def reset_machines() -> list[int]:
    """Leaders' escape hatch: force every machine back to 🟢 free.

    Returns the ids of the sessions that were running so the caller can drop
    their pending done jobs. History (the "last used by" line) is untouched.
    """
    with _tx() as conn:
        ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM sessions WHERE status = 'active' ORDER BY id"
            )
        ]
        conn.execute("UPDATE sessions SET status = 'cancelled' WHERE status = 'active'")
    return ids


def claim_nudge(session_id: int, cooldown_min: int) -> tuple[bool, str | None]:
    """Take the nudge slot for a session, if the cooldown has run out.

    Returns ``(claimed, previous_stamp)``. The cooldown is read and stamped in
    one transaction so two residents tapping 🔔 at the same moment can't both
    get through and DM the owner twice; the loser gets ``(False, stamp)`` and
    can say how long ago the real nudge went out. Callers must claim *before*
    sending and hand the slot back with :func:`release_nudge` if the send fails.
    """
    now = util.now_utc()
    with _tx() as conn:
        row = conn.execute(
            "SELECT last_ping_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return False, None
        previous = row["last_ping_at"]
        if previous and util.elapsed_min(util.parse_iso(previous), now) < cooldown_min:
            return False, previous
        conn.execute(
            "UPDATE sessions SET last_ping_at = ? WHERE id = ?",
            (util.to_iso(now), session_id),
        )
    return True, previous


def release_nudge(session_id: int, previous: str | None) -> None:
    """Undo a claim whose DM never made it out, so the next person can retry."""
    with _tx() as conn:
        conn.execute(
            "UPDATE sessions SET last_ping_at = ? WHERE id = ?", (previous, session_id)
        )


# --------------------------------------------------------------------------
# polls ("count me in")
# --------------------------------------------------------------------------

IN, OUT = "in", "out"


def create_poll(question: str, created_by: int) -> int:
    with _tx() as conn:
        cursor = conn.execute(
            "INSERT INTO polls (question, created_by, created_at) VALUES (?, ?, ?)",
            (question, created_by, util.to_iso(util.now_utc())),
        )
        return int(cursor.lastrowid)


def get_poll(poll_id: int) -> sqlite3.Row | None:
    return connection().execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()


def latest_poll() -> sqlite3.Row | None:
    return connection().execute(
        "SELECT * FROM polls ORDER BY id DESC LIMIT 1"
    ).fetchone()


def add_poll_recipient(poll_id: int, user_id: int, chat_id: int, message_id: int) -> None:
    """Record where one resident's copy of the card lives, so taps can edit it."""
    with _tx() as conn:
        conn.execute(
            "INSERT INTO poll_recipients (poll_id, user_id, chat_id, message_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(poll_id, user_id) DO UPDATE SET "
            "chat_id = excluded.chat_id, message_id = excluded.message_id",
            (poll_id, user_id, chat_id, message_id),
        )


def set_poll_answer(poll_id: int, user_id: int, answer: str | None) -> bool:
    """Record one person's answer. False when they were never sent this poll.

    Only ever touches the caller's own row, and re-tapping overwrites rather
    than appending, so a resident can change their mind any number of times.
    """
    with _tx() as conn:
        cursor = conn.execute(
            "UPDATE poll_recipients SET answer = ?, answered_at = ? "
            "WHERE poll_id = ? AND user_id = ?",
            (answer, util.to_iso(util.now_utc()) if answer else None, poll_id, user_id),
        )
        return cursor.rowcount > 0


def poll_answers(poll_id: int) -> dict[str, list[str]]:
    """``{"in": [names...], "out": [names...]}`` in the order people answered."""
    rows = connection().execute(
        "SELECT p.answer AS answer, u.name AS name FROM poll_recipients p "
        "JOIN users u ON u.user_id = p.user_id "
        "WHERE p.poll_id = ? AND p.answer IS NOT NULL "
        "ORDER BY p.answered_at, u.name",
        (poll_id,),
    ).fetchall()
    tally: dict[str, list[str]] = {IN: [], OUT: []}
    for row in rows:
        tally.setdefault(row["answer"], []).append(row["name"])
    return tally


def poll_no_reply(poll_id: int) -> list[sqlite3.Row]:
    """Everyone the poll reached who has not answered. Leader's chase list."""
    return connection().execute(
        "SELECT u.name AS name, u.room AS room, u.username AS username "
        "FROM poll_recipients p JOIN users u ON u.user_id = p.user_id "
        "WHERE p.poll_id = ? AND p.answer IS NULL ORDER BY u.room",
        (poll_id,),
    ).fetchall()


def poll_cards(poll_id: int, *, exclude_user: int | None = None) -> list[sqlite3.Row]:
    """Every delivered copy of a poll, for a sweep re-render."""
    sql = (
        "SELECT user_id, chat_id, message_id FROM poll_recipients "
        "WHERE poll_id = ? AND message_id IS NOT NULL"
    )
    params: list[object] = [poll_id]
    if exclude_user is not None:
        sql += " AND user_id != ?"
        params.append(exclude_user)
    return connection().execute(sql, params).fetchall()


def poll_recipient(poll_id: int, user_id: int) -> sqlite3.Row | None:
    """One person's row for a poll: where their card is, and their answer."""
    return connection().execute(
        "SELECT * FROM poll_recipients WHERE poll_id = ? AND user_id = ?",
        (poll_id, user_id),
    ).fetchone()
