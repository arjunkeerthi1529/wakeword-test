"""Reminder Agent — voice-triggered personal reminders.

The main pipeline detects a [REMINDER delay=<t> message=<text>] tag in the
LLM reply, strips it before TTS plays it, then calls ReminderAgent.schedule().
A threading.Timer fires at the right moment and speaks the reminder via TTS.

Reminder tag formats emitted by the LLM:
    [REMINDER delay=5m message=drink water]      # relative: s/m/h
    [REMINDER at=15:00 message=call Mum]         # absolute HH:MM today

On restart, any reminders whose fires_at is still in the future are re-armed
from SQLite. Reminders that were missed during downtime are announced
immediately with a "sorry, missed" prefix.
"""
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional

from .db import save_reminder, mark_reminder_fired, get_pending_reminders, get_missed_reminders

logger = logging.getLogger(__name__)

# Matches the tag the LLM emits, case-insensitive
_TAG_RE = re.compile(
    r'\[REMINDER\s+'
    r'(?:delay=(\S+)|at=([\d]{1,2}:[\d]{2}))'
    r'\s+message=([^\]]+)\]',
    re.IGNORECASE,
)

# Parses delay strings: 5m, 30s, 2h
_DELAY_RE = re.compile(r'^(\d+)([smh])$', re.IGNORECASE)


def parse_tag(reply: str) -> tuple[str, Optional[dict]]:
    """Strip [REMINDER ...] tag from reply text and parse it.

    Returns (clean_reply, spec_or_None) where spec is:
        {"message": str, "delay_s": int}  for delay-based reminders
        {"message": str, "fires_at": datetime} for absolute-time reminders
    """
    m = _TAG_RE.search(reply)
    if not m:
        return reply, None

    clean = (reply[:m.start()] + reply[m.end():]).strip()
    delay_str, at_str, message = m.group(1), m.group(2), m.group(3).strip()

    if delay_str:
        dm = _DELAY_RE.match(delay_str)
        if not dm:
            logger.warning("Unrecognised delay format: %r", delay_str)
            return clean, None
        n, unit = int(dm.group(1)), dm.group(2).lower()
        delay_s = n * {"s": 1, "m": 60, "h": 3600}[unit]
        return clean, {"message": message, "delay_s": delay_s}

    if at_str:
        try:
            h, minute = map(int, at_str.split(":"))
            now = datetime.now()
            fires_at = now.replace(hour=h, minute=minute, second=0, microsecond=0)
            if fires_at <= now:
                fires_at += timedelta(days=1)   # assume tomorrow if time has passed
            return clean, {"message": message, "fires_at": fires_at}
        except ValueError:
            logger.warning("Unrecognised at= time: %r", at_str)
            return clean, None

    return clean, None


class ReminderAgent:
    """Manages voice reminders: schedule, persist, fire via TTS."""

    def __init__(self, tts, conn: Optional[sqlite3.Connection] = None):
        self._tts = tts
        self._conn = conn
        self._timers: dict[int, threading.Timer] = {}   # rid -> Timer
        self._lock = threading.Lock()
        self._counter = 0   # local id for in-memory-only reminders

    # ── Public API ────────────────────────────────────────────────────────────

    def schedule_from_spec(self, spec: dict) -> None:
        """Schedule a reminder from a parsed spec dict (output of parse_tag)."""
        if "delay_s" in spec:
            delay_s = spec["delay_s"]
            fires_at = datetime.now() + timedelta(seconds=delay_s)
        else:
            fires_at = spec["fires_at"]
            delay_s = (fires_at - datetime.now()).total_seconds()

        message = spec["message"]

        if delay_s <= 0:
            logger.warning("Reminder delay is in the past — firing immediately")
            delay_s = 1

        # Persist reminders longer than 30 s so they survive a restart
        rid = None
        if self._conn and delay_s > 30:
            rid = save_reminder(self._conn, message, fires_at.isoformat(timespec="seconds"))
            logger.info("Reminder saved (id=%d) fires_at=%s: %r", rid, fires_at.isoformat(), message)
        else:
            with self._lock:
                self._counter -= 1
                rid = self._counter   # negative ids = in-memory only

        self._arm(rid, message, delay_s)
        logger.info("Reminder set — %s in %.0f s: %r", fires_at.strftime("%H:%M:%S"), delay_s, message)

    def recover_from_db(self) -> None:
        """Re-arm pending reminders from SQLite after a restart."""
        if not self._conn:
            return

        pending = get_pending_reminders(self._conn)
        for row in pending:
            fires_at = datetime.fromisoformat(row["fires_at"])
            delay_s = max(1.0, (fires_at - datetime.now()).total_seconds())
            self._arm(row["id"], row["message"], delay_s)
            logger.info("Re-armed reminder id=%d in %.0f s: %r", row["id"], delay_s, row["message"])

        missed = get_missed_reminders(self._conn)
        for row in missed:
            logger.info("Missed reminder id=%d: %r — announcing now", row["id"], row["message"])
            self._fire(row["id"], f"Sorry, I missed this earlier: {row['message']}")

    def cancel_all(self) -> None:
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
        logger.info("All reminders cancelled")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _arm(self, rid: int, message: str, delay_s: float) -> None:
        t = threading.Timer(delay_s, self._fire, args=(rid, message))
        t.daemon = True
        t.name = f"reminder-{rid}"
        with self._lock:
            self._timers[rid] = t
        t.start()

    def _fire(self, rid: int, message: str) -> None:
        logger.info("Reminder firing (id=%d): %r", rid, message)
        with self._lock:
            self._timers.pop(rid, None)

        # Mark fired in DB (only for real persisted reminders)
        if self._conn and rid > 0:
            mark_reminder_fired(self._conn, rid)

        try:
            self._tts.speak(f"Reminder: {message}")
        except Exception as exc:
            logger.error("TTS failed for reminder id=%d: %s", rid, exc)
