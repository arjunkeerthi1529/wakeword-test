"""SQLite persistence for email summaries and reminders.

Single shared connection with a write lock — SQLite WAL mode keeps reads
non-blocking even when a write lock is held.
"""
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


def get_conn(db_path: str) -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    logger.info("SQLite opened: %s", path.resolve())
    return _conn


def init_schema(conn: sqlite3.Connection) -> None:
    with _lock:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS email_summaries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                fetched_at  TEXT NOT NULL,
                sender      TEXT,
                subject     TEXT,
                summary     TEXT,
                importance  TEXT CHECK(importance IN ('low', 'normal', 'urgent')),
                raw_uid     TEXT UNIQUE
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                message     TEXT NOT NULL,
                fires_at    TEXT NOT NULL,
                fired       INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now'))
            );
        """)
        conn.commit()
    logger.info("DB schema ready")


# ── Email helpers ─────────────────────────────────────────────────────────────

def save_email_summary(
    conn: sqlite3.Connection,
    *,
    uid: str,
    sender: str,
    subject: str,
    summary: str,
    importance: str,
    fetched_at: str,
) -> bool:
    """Insert one email summary. Returns True if inserted, False if already present."""
    with _lock:
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO email_summaries
                   (raw_uid, sender, subject, summary, importance, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (uid, sender, subject, summary, importance, fetched_at),
            )
            conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error as exc:
            logger.error("save_email_summary failed: %s", exc)
            return False


def get_all_email_summaries(conn: sqlite3.Connection) -> list:
    cur = conn.execute(
        "SELECT * FROM email_summaries ORDER BY fetched_at DESC"
    )
    return cur.fetchall()


# ── Reminder helpers ──────────────────────────────────────────────────────────

def save_reminder(conn: sqlite3.Connection, message: str, fires_at: str) -> int:
    """Persist a reminder. Returns the new row id."""
    with _lock:
        cur = conn.execute(
            "INSERT INTO reminders (message, fires_at) VALUES (?, ?)",
            (message, fires_at),
        )
        conn.commit()
        return cur.lastrowid


def mark_reminder_fired(conn: sqlite3.Connection, rid: int) -> None:
    with _lock:
        conn.execute("UPDATE reminders SET fired=1 WHERE id=?", (rid,))
        conn.commit()


def get_pending_reminders(conn: sqlite3.Connection) -> list:
    """Return reminders that haven't fired yet and whose time is still in the future."""
    cur = conn.execute(
        "SELECT * FROM reminders WHERE fired=0 AND fires_at > datetime('now')"
    )
    return cur.fetchall()


def get_missed_reminders(conn: sqlite3.Connection) -> list:
    """Return reminders that were due during downtime (fired=0, time has passed)."""
    cur = conn.execute(
        "SELECT * FROM reminders WHERE fired=0 AND fires_at <= datetime('now')"
    )
    return cur.fetchall()
