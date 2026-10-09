"""Email Digest Agent — mocked data edition.

Runs at EMAIL_FETCH_TIME each day. Summarises each email via the local LLM,
stores results in SQLite, and optionally reads the digest aloud via TTS.

Mocked emails are inserted with INSERT OR IGNORE so they only appear once
in the DB even if the agent runs multiple times.  When real IMAP is wired
in later, swap _load_mock_emails() for an IMAP fetch and keep everything else.
"""
import logging
import sqlite3
from datetime import datetime
from typing import Optional

import requests

from .db import save_email_summary, get_all_email_summaries
from .config import AgentConfig

logger = logging.getLogger(__name__)

# ── Mock email data ───────────────────────────────────────────────────────────

MOCK_EMAILS = [
    {
        "uid": "mock_001",
        "sender": "manager@company.com",
        "subject": "Q4 Budget Review — Action Required",
        "body": (
            "Hi, please review the Q4 budget spreadsheet I shared and send me "
            "your department's projections by Friday. We need to finalise the "
            "numbers before the board meeting next Monday morning."
        ),
        "received_at": "2026-10-09 07:15:00",
    },
    {
        "uid": "mock_002",
        "sender": "calendar-noreply@google.com",
        "subject": "Invitation: Weekly Team Standup — Tomorrow 10:00 AM",
        "body": (
            "You have been invited to Weekly Team Standup on Friday 10 October "
            "at 10:00 AM IST. Attendees: Pavan, Priya, Rahul, Deepa. "
            "Google Meet link: meet.google.com/abc-defg-hij"
        ),
        "received_at": "2026-10-09 08:00:00",
    },
    {
        "uid": "mock_003",
        "sender": "github@github.com",
        "subject": "[wakeword-test] PR #12 merged: feat: Scam Guard API",
        "body": (
            "Pull request #12 'feat: documented, typed Scam Guard API with "
            "/analyze and /health' was merged into main by Mittapalli Pavan."
        ),
        "received_at": "2026-10-09 08:45:00",
    },
    {
        "uid": "mock_004",
        "sender": "noreply@amazon.in",
        "subject": "Your order #402-8837261 has been shipped",
        "body": (
            "Great news! Your order of Raspberry Pi 4 Model B 8GB has been "
            "shipped and is expected to arrive by October 11. Track your "
            "package with: IN123456789."
        ),
        "received_at": "2026-10-09 09:10:00",
    },
    {
        "uid": "mock_005",
        "sender": "mum@gmail.com",
        "subject": "Dinner this Sunday?",
        "body": (
            "Hi beta, are you coming home for dinner on Sunday? I am making "
            "your favourite biryani. Let me know by Saturday evening so I can "
            "buy the groceries. Love, Mum."
        ),
        "received_at": "2026-10-09 09:30:00",
    },
    {
        "uid": "mock_006",
        "sender": "doctor@healthclinic.in",
        "subject": "Appointment Reminder — Tomorrow 2:00 PM",
        "body": (
            "This is a reminder that you have an appointment with Dr. Sharma "
            "tomorrow, Friday 10 October at 2:00 PM at Healthpoint Clinic, "
            "Koramangala. Please arrive 10 minutes early and bring your "
            "previous test reports."
        ),
        "received_at": "2026-10-09 10:00:00",
    },
    {
        "uid": "mock_007",
        "sender": "newsletter@tldr.tech",
        "subject": "TLDR Newsletter — AI edition 09 Oct 2026",
        "body": (
            "Today's highlights: OpenAI releases o4 reasoning model. "
            "Google DeepMind publishes AlphaFold 4. Raspberry Pi 5 now "
            "ships with 16 GB RAM. Meta open-sources Llama 4 weights."
        ),
        "received_at": "2026-10-09 06:00:00",
    },
    {
        "uid": "mock_008",
        "sender": "hr@company.com",
        "subject": "Leave Balance Update — October 2026",
        "body": (
            "Dear Pavan, your current leave balance is: Earned Leave 12 days, "
            "Casual Leave 3 days, Sick Leave 5 days. Please plan and apply "
            "for any upcoming leaves via the HR portal before month-end."
        ),
        "received_at": "2026-10-09 10:30:00",
    },
]


# ── LLM summarisation (one-shot, no conversation history) ────────────────────

_SUMMARISE_SYSTEM = (
    "You are an email summariser. "
    "Given an email's sender, subject, and body, reply with exactly two things "
    "on separate lines — nothing else:\n"
    "Line 1: A single sentence summary (max 20 words).\n"
    "Line 2: One word importance tag — low, normal, or urgent.\n"
    "No markdown, no labels, no extra text."
)


def _summarise_email(llm_base_url: str, email: dict) -> tuple[str, str]:
    """Call the local LLM to summarise one email.

    Returns (summary_text, importance) where importance is low/normal/urgent.
    Falls back to a plain subject line if the LLM is unavailable.
    """
    prompt = (
        f"From: {email['sender']}\n"
        f"Subject: {email['subject']}\n"
        f"Body: {email['body']}"
    )
    try:
        resp = requests.post(
            f"{llm_base_url.rstrip('/')}/v1/chat/completions",
            json={
                "model": "local",
                "messages": [
                    {"role": "system", "content": _SUMMARISE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "temperature": 0.3,
                "max_tokens": 60,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        summary = lines[0] if lines else email["subject"]
        raw_imp = lines[1].lower() if len(lines) > 1 else "normal"
        importance = raw_imp if raw_imp in ("low", "normal", "urgent") else "normal"
        return summary, importance
    except Exception as exc:
        logger.warning("LLM summarise failed for %s: %s", email["uid"], exc)
        return email["subject"], "normal"


# ── Main entry point ──────────────────────────────────────────────────────────

def run(
    llm_base_url: str,
    conn: sqlite3.Connection,
    cfg: AgentConfig,
    tts=None,
) -> None:
    """Fetch (mocked) emails, summarise, store, optionally speak.

    Called by the scheduler at EMAIL_FETCH_TIME each day.
    Safe to call manually for testing.
    """
    logger.info("Email agent running — processing %d mock emails", len(MOCK_EMAILS))
    fetched_at = datetime.now().isoformat(timespec="seconds")

    new_count = 0
    urgent_subjects: list[str] = []

    for email in MOCK_EMAILS:
        summary, importance = _summarise_email(llm_base_url, email)
        inserted = save_email_summary(
            conn,
            uid=email["uid"],
            sender=email["sender"],
            subject=email["subject"],
            summary=summary,
            importance=importance,
            fetched_at=fetched_at,
        )
        if inserted:
            new_count += 1
            logger.info("[%s] %s | %s", importance.upper(), email["sender"], summary)
            if importance == "urgent":
                urgent_subjects.append(summary)

    logger.info("Email agent done — %d new email(s) stored", new_count)

    if tts and cfg.agent_read_digest and new_count > 0:
        _speak_digest(tts, conn, urgent_subjects, new_count)


def _speak_digest(tts, conn: sqlite3.Connection, urgent: list[str], new_count: int) -> None:
    """Read a short email digest aloud."""
    rows = get_all_email_summaries(conn)
    total = len(rows)

    lines = [f"You have {total} emails, {new_count} new."]
    if urgent:
        lines.append(f"Urgent: {'. '.join(urgent)}.")
    else:
        lines.append("No urgent emails.")

    digest = " ".join(lines)
    logger.info("Speaking digest: %s", digest)
    tts.speak(digest)


# ── CLI helper: print stored summaries ───────────────────────────────────────

def print_stored(conn: sqlite3.Connection) -> None:
    rows = get_all_email_summaries(conn)
    if not rows:
        print("No email summaries stored yet.")
        return
    print(f"\n{'─'*60}")
    print(f"{'ID':<4} {'IMP':<8} {'SENDER':<25} {'SUMMARY'}")
    print(f"{'─'*60}")
    for r in rows:
        print(f"{r['id']:<4} {r['importance']:<8} {(r['sender'] or '')[:24]:<25} {r['summary']}")
    print(f"{'─'*60}\n")
