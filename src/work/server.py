"""Agent Service — FastAPI REST server.

Exposes the email digest and reminder agent over HTTP so they run as a
fully independent process from the voice assistant.

Endpoints
---------
GET  /health              service health + counts
POST /remind              schedule a reminder  (called by voice assistant)
GET  /reminders           list pending reminders
GET  /emails              list stored email summaries
POST /emails/fetch        trigger an immediate email fetch + summarise + a prioritized digest
GET  /digest              the latest prioritized briefing (what needs attention, ranked)
GET  /drafts              list outbound drafts (replies/notifications), optional ?status=
POST /drafts/{id}/approve approve a pending draft and send it (mock)
POST /drafts/{id}/reject  discard a pending draft
POST /meetings/start      start continuously recording from the mic
POST /meetings/stop       stop recording, transcribe + summarise, persist, return it
GET  /meetings            list past meeting notes (summary only, no transcript)
GET  /meetings/{id}       one meeting note with its full transcript
"""
import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .meeting_agent import MeetingRecorder, RecorderStateError

logger = logging.getLogger(__name__)

app = FastAPI(title="Jarvis Agent Service", version="1.0.0")

# Allow the local test HTML file (file://) and any localhost origin to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Module-level state — set by __main__.py before uvicorn starts
_conn = None
_tts = None
_reminder_agent = None
_agent_cfg = None
_main_cfg = None
_recorder = MeetingRecorder()
_stt_available = False


def set_state(*, conn, tts, reminder_agent, agent_cfg, main_cfg, stt_available=False) -> None:
    """Called once from __main__.py before uvicorn.run() so all heavy
    initialisation (model loading, DB open) happens outside the event loop."""
    global _conn, _tts, _reminder_agent, _agent_cfg, _main_cfg, _stt_available
    _conn = conn
    _tts = tts
    _reminder_agent = reminder_agent
    _agent_cfg = agent_cfg
    _main_cfg = main_cfg
    _stt_available = stt_available


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
    """Trigger an immediate email fetch + LLM summarise + a prioritized
    digest. Useful for testing without waiting for the daily schedule."""
    from .email_agent import run as email_run
    tts_arg = _tts if (_agent_cfg and _agent_cfg.agent_read_digest) else None
    digest_text = email_run(
        llm_base_url=_main_cfg.llm_base_url,
        conn=_conn,
        cfg=_agent_cfg,
        tts=tts_arg,
    )
    from .db import get_all_email_summaries
    return {"status": "done", "total_stored": len(get_all_email_summaries(_conn)), "digest": digest_text}


@app.get("/digest")
def get_digest():
    """The latest prioritized briefing (what needs your attention, ranked),
    separate from the raw per-email list in GET /emails."""
    from .db import get_latest_digest
    row = get_latest_digest(_conn)
    if row is None:
        return {"digest_text": None, "email_count": 0, "created_at": None}
    return dict(row)


@app.get("/drafts")
def list_drafts_endpoint(status: str | None = None):
    """List outbound drafts (replies to a sender, or notifications to the
    configured address). Pass ?status=pending to see only what's awaiting
    approval."""
    from .db import list_drafts
    rows = list_drafts(_conn, status)
    return [dict(r) for r in rows]


@app.post("/drafts/{draft_id}/approve")
def approve_draft(draft_id: int):
    """Approve a pending draft and send it (mock send in this build)."""
    from .db import get_draft, set_draft_status
    from .email_agent import _mock_send

    row = get_draft(_conn, draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Draft is already {row['status']}")

    _mock_send(row["to_addr"], row["subject"], row["body"])
    set_draft_status(_conn, draft_id, "sent")
    logger.info("Draft #%d approved and sent to %s", draft_id, row["to_addr"])
    return {"status": "sent", "id": draft_id}


@app.post("/drafts/{draft_id}/reject")
def reject_draft(draft_id: int):
    """Discard a pending draft without sending it."""
    from .db import get_draft, set_draft_status

    row = get_draft(_conn, draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Draft is already {row['status']}")

    set_draft_status(_conn, draft_id, "rejected")
    logger.info("Draft #%d rejected", draft_id)
    return {"status": "rejected", "id": draft_id}


@app.post("/meetings/start")
def start_meeting():
    """Begin continuously recording from the default microphone. Returns
    immediately -- recording runs in the background until /meetings/stop."""
    if not _stt_available:
        raise HTTPException(
            status_code=503,
            detail="Speech-to-text not available (faster-whisper not installed)",
        )
    try:
        _recorder.start()
    except RecorderStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Could not start recording: %s", exc)
        raise HTTPException(status_code=500, detail=f"Could not start recording: {exc}") from exc
    return {"status": "recording", "started_at": _recorder.started_at_iso}


@app.post("/meetings/stop")
def stop_meeting():
    """Stop recording, transcribe locally, summarise via the local LLM, and
    persist. Blocks until both finish -- for a long meeting this can take a
    while, same tradeoff as POST /emails/fetch."""
    from .meeting_agent import transcribe, summarize_transcript
    from .db import save_meeting

    try:
        result = _recorder.stop()
    except RecorderStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    stt_model = _main_cfg.stt_model_name if _main_cfg else "small.en"
    transcript = transcribe(result["audio"], model_name=stt_model)
    summary = summarize_transcript(
        _main_cfg.llm_base_url, _agent_cfg.llm_model, transcript
    )
    meeting_id = save_meeting(
        _conn,
        started_at=result["started_at"],
        ended_at=result["ended_at"],
        duration_s=result["duration_s"],
        transcript=transcript,
        summary=summary,
    )
    logger.info("Meeting #%d saved — %.1fs, %d transcript chars", meeting_id, result["duration_s"], len(transcript))
    return {
        "id": meeting_id,
        "started_at": result["started_at"],
        "ended_at": result["ended_at"],
        "duration_s": result["duration_s"],
        "transcript": transcript,
        "summary": summary,
    }


@app.get("/meetings")
def list_meetings_endpoint():
    from .db import list_meetings
    rows = list_meetings(_conn)
    return [dict(r) for r in rows]


@app.get("/meetings/{meeting_id}")
def get_meeting_endpoint(meeting_id: int):
    from .db import get_meeting
    row = get_meeting(_conn, meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return dict(row)
