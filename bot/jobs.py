"""Laundry timers: the single "your load is done" ping when a cycle ends.

Job names are ``done:<session_id>`` so any handler can cancel or reschedule a
session's timer knowing only the session id. There is no follow-up reminder:
since v1.2 nobody confirms collection, so the bot has no way to tell whether a
nag would be warranted — and nagging everybody is worse than not nagging.
"""

from __future__ import annotations

import logging
from datetime import datetime

from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes, JobQueue

from . import config, db, util

logger = logging.getLogger(__name__)

DONE_PREFIX = "done:"


def done_job_name(session_id: int) -> str:
    return f"{DONE_PREFIX}{session_id}"


def _remove(job_queue: JobQueue | None, name: str) -> None:
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()


def cancel_done(job_queue: JobQueue | None, session_id: int) -> None:
    _remove(job_queue, done_job_name(session_id))


def cancel_session_jobs(job_queue: JobQueue | None, session_id: int) -> None:
    """Drop every timer belonging to a session (only the done ping, today)."""
    cancel_done(job_queue, session_id)


def schedule_done(job_queue: JobQueue | None, session_id: int, ends_at: datetime) -> None:
    """(Re)schedule the done ping. Overdue sessions fire almost immediately."""
    if job_queue is None:
        return
    cancel_done(job_queue, session_id)
    delay = max(1.0, (ends_at - util.now_utc()).total_seconds())
    job_queue.run_once(done_job, when=delay, name=done_job_name(session_id), data=session_id)


async def _dm(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> bool:
    try:
        await context.bot.send_message(user_id, text)
        return True
    except TelegramError as exc:
        logger.warning("Could not DM %s: %s", user_id, exc)
        return False


async def done_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cycle over: the machine goes 🟢 free by itself and the owner hears about it."""
    session_id = int(context.job.data)
    row = db.get_session(session_id)
    if row is None or row["status"] != "active":
        return

    # An extension that lands in the same instant this job fires can't cancel
    # it any more — ``schedule_removal`` doesn't stop a job already handed to
    # the loop — so the finish line is re-read here and the timer re-armed.
    ends_at = util.parse_iso(row["ends_at"])
    if ends_at > util.now_utc():
        schedule_done(context.job_queue, session_id, ends_at)
        return

    if not db.mark_done(session_id):
        return

    machine = config.MACHINES.get(row["machine"])
    label = machine.label if machine else row["machine"]
    await _dm(
        context,
        row["user_id"],
        f"⏰ <b>{label} is done!</b> ({row['duration_min']} min cycle, "
        f"{util.fmt_clock(ends_at)}). Grab your laundry when you can 🙏\n\n"
        "The machine now shows as free, so someone else may move your load if "
        "you leave it.",
    )


def restore_jobs(application: Application) -> int:
    """Re-arm timers after a restart. Returns how many jobs were scheduled."""
    job_queue = application.job_queue
    if job_queue is None:
        logger.warning("No JobQueue available, laundry timers will not fire.")
        return 0

    scheduled = 0
    for row in db.active_sessions():
        schedule_done(job_queue, row["id"], util.parse_iso(row["ends_at"]))
        scheduled += 1
    return scheduled
