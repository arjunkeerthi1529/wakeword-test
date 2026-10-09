"""Agent Service — FastAPI REST server.

Exposes the email digest and reminder agent over HTTP so they run as a
fully independent process from the voice assistant.

Endpoints
---------
GET  /health          service health + counts
POST /remind          schedule a reminder  (called by voice assistant)
GET  /reminders       list pending reminders
GET  /emails          list stored email summaries
POST /emails/fetch    trigger an immediate email fetch + summarise
"""
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(title="Jarvis Agent Service", version="1.0.0")

# Module-level state — set by __main__.py before uvicorn starts
_conn = None
_tts = None
_reminder_agent = None
_agent_cfg = None
_main_cfg = None


def set_state(*, conn, tts, reminder_agent, agent_cfg, main_cfg) -> None:
    """Called once from __main__.py before uvicorn.run() so all heavy
    initialisation (model loading, DB open) happens outside the event loop."""
    global _conn, _tts, _reminder_agent, _agent_cfg, _main_cfg
    _conn = conn
    _tts = tts
    _reminder_agent = reminder_agent
    _agent_cfg = agent_cfg
    _main_cfg = main_cfg


# ── Request / Response models ─────────────────────────────────────────────────

class RemindRequest(BaseModel):
    message: str
    delay_s: int | None = None    # relative — seconds from now
    fires_at: str | None = None   # absolute — ISO-8601 datetime string


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    from .db import get_pending_reminders, get_all_email_summaries
    pending = len(get_pending_reminders(_conn)) if _conn else -1
    emails  = len(get_all_email_summaries(_conn)) if _conn else -1
    return {
        "status": "ok",
        "reminders_pending": pending,
        "emails_stored": emails,
    }


@app.post("/remind")
def create_reminder(req: RemindRequest):
    """Schedule a reminder. Called by the voice assistant when it detects
    a [REMINDER ...] tag in the LLM reply."""
    if _reminder_agent is None:
        raise HTTPException(status_code=503, detail="Agent service not initialised")

    spec: dict = {"message": req.message}

    if req.delay_s is not None:
        spec["delay_s"] = req.delay_s
    elif req.fires_at is not None:
        from datetime import datetime
        try:
            spec["fires_at"] = datetime.fromisoformat(req.fires_at)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid fires_at: {req.fires_at!r}")
    else:
        raise HTTPException(status_code=400, detail="Provide delay_s or fires_at")

    _reminder_agent.schedule_from_spec(spec)
    logger.info("Reminder scheduled via API: %r", req.message)
    return {"status": "scheduled", "message": req.message}


@app.get("/reminders")
def list_reminders():
    from .db import get_pending_reminders
    rows = get_pending_reminders(_conn)
    return [dict(r) for r in rows]


@app.get("/emails")
def list_emails():
    from .db import get_all_email_summaries
    rows = get_all_email_summaries(_conn)
    return [dict(r) for r in rows]


@app.post("/emails/fetch")
def fetch_emails():
    """Trigger an immediate email fetch + LLM summarise. Useful for testing
    without waiting for the daily schedule."""
    from .email_agent import run as email_run
    tts_arg = _tts if (_agent_cfg and _agent_cfg.agent_read_digest) else None
    email_run(
        llm_base_url=_main_cfg.llm_base_url,
        conn=_conn,
        cfg=_agent_cfg,
        tts=tts_arg,
    )
    from .db import get_all_email_summaries
    return {"status": "done", "total_stored": len(get_all_email_summaries(_conn))}
