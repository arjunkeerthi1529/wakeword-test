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

            CREATE TABLE IF NOT EXISTS meetings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at  TEXT NOT NULL,
                ended_at    TEXT NOT NULL,
                duration_s  REAL NOT NULL,
                transcript  TEXT,
                summary     TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS outbound_drafts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                source_email_uid TEXT,
                action           TEXT CHECK(action IN ('reply', 'notify')) NOT NULL,
                to_addr          TEXT NOT NULL,
                subject          TEXT NOT NULL,
                body             TEXT NOT NULL,
                reason           TEXT,
                needs_approval   INTEGER NOT NULL,
                status           TEXT CHECK(status IN ('pending', 'approved', 'rejected', 'sent'))
                                      NOT NULL DEFAULT 'pending',
                created_at       TEXT DEFAULT (datetime('now')),
                decided_at       TEXT
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


# ── Meeting helpers ───────────────────────────────────────────────────────────

def save_meeting(
    conn: sqlite3.Connection,
    *,
    started_at: str,
    ended_at: str,
    duration_s: float,
    transcript: str,
    summary: str,
) -> int:
    with _lock:
        cur = conn.execute(
            """INSERT INTO meetings (started_at, ended_at, duration_s, transcript, summary)
               VALUES (?, ?, ?, ?, ?)""",
            (started_at, ended_at, duration_s, transcript, summary),
        )
        conn.commit()
        return cur.lastrowid


def list_meetings(conn: sqlite3.Connection) -> list:
    """Summary fields only (no transcript) -- keeps the list payload light;
    use get_meeting() for the full transcript of one entry."""
    cur = conn.execute(
        """SELECT id, started_at, ended_at, duration_s, summary, created_at
           FROM meetings ORDER BY created_at DESC"""
    )
    return cur.fetchall()


def get_meeting(conn: sqlite3.Connection, meeting_id: int):
    cur = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    return cur.fetchone()


# ── Outbound draft helpers ─────────────────────────────────────────────────────
# A draft is only ever created with a to_addr the backend itself decided
# (original sender for 'reply', the one configured notify address for
# 'notify') -- never an address the LLM proposed, since the email body being
# summarised is untrusted external text.

def save_outbound_draft(
    conn: sqlite3.Connection,
    *,
    source_email_uid: str,
    action: str,
    to_addr: str,
    subject: str,
    body: str,
    reason: str,
    needs_approval: bool,
) -> int:
    with _lock:
        cur = conn.execute(
            """INSERT INTO outbound_drafts
               (source_email_uid, action, to_addr, subject, body, reason, needs_approval)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source_email_uid, action, to_addr, subject, body, reason, 1 if needs_approval else 0),
        )
        conn.commit()
        return cur.lastrowid


def get_draft(conn: sqlite3.Connection, draft_id: int):
    cur = conn.execute("SELECT * FROM outbound_drafts WHERE id = ?", (draft_id,))
    return cur.fetchone()


def list_drafts(conn: sqlite3.Connection, status: Optional[str] = None) -> list:
    if status:
        cur = conn.execute(
            "SELECT * FROM outbound_drafts WHERE status = ? ORDER BY created_at DESC", (status,)
        )
    else:
        cur = conn.execute("SELECT * FROM outbound_drafts ORDER BY created_at DESC")
    return cur.fetchall()


def set_draft_status(conn: sqlite3.Connection, draft_id: int, status: str) -> None:
    with _lock:
        conn.execute(
            "UPDATE outbound_drafts SET status = ?, decided_at = datetime('now') WHERE id = ?",
            (status, draft_id),
        )
        conn.commit()
