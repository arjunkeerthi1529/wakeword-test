"""Request/response contracts for the Scam Guard HTTP and WebSocket API.

These models are the single source of truth for the API: FastAPI builds the
interactive docs (/docs) and the OpenAPI spec (/openapi.json) from them, and
GET /events/schema publishes the JSON Schema of every live WebSocket event.
"""
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field, StringConstraints, model_validator

Risk = Literal["none", "watch", "warn"]
AlertRisk = Literal["watch", "warn"]
Speaker = Literal["caller", "user", "unknown"]
AlertSource = Literal["rule", "llm", "rule+llm"]
Outcome = Literal["none", "watch", "warn", "advice", "invalid"]
Line = Annotated[str, StringConstraints(min_length=1, max_length=500)]

API_VERSION = "1"


# ── Shared objects ───────────────────────────────────────────────────────────

class Segment(BaseModel):
    segment_id: str = Field(description="Stable id within one call: s1, s2, ...")
    start_ms: int = Field(description="Milliseconds from the start of the call")
    end_ms: int
    text: str = Field(description="Committed transcript text (a sentence or two)")


class Alert(BaseModel):
    segment_id: str = Field(description="The transcript segment that triggered the alert")
    risk: AlertRisk = Field(description="'watch' = be careful (pressure/impersonation). "
                                        "'warn' = a request for a code, PIN, money or remote access.")
    speaker: Speaker = Field(description="Who most likely said it. A suggestion only: one microphone hears both people.")
    evidence: str = Field(description="The words that triggered it (rules: the matched phrase; LLM: the whole line)")
    message: str = Field(description="Plain-language advice to show the user")
    source: AlertSource = Field(description="'rule' = instant pattern match, 'llm' = on-device model, "
                                            "'rule+llm' = raised by a rule and confirmed by the model")
    revision: int = Field(description="Starts at 1; increases when the same alert is updated (never a second vibration)")


class LastCheck(BaseModel):
    segment_id: str = Field(description="Last segment the LLM has analyzed")
    outcome: Outcome = Field(description="none = no warning raised; watch/warn = alert raised; "
                                         "advice = model flagged a refusal/safety-advice line and it was ignored; "
                                         "invalid = model reply could not be used")
    took_s: float


class Snapshot(BaseModel):
    """Full state of the current call. Returned by GET /status and sent first on every WebSocket connection."""
    active: bool = Field(description="True while a call is being monitored")
    call_id: str = Field(description="Empty string when not monitoring")
    segments: List[Segment]
    alerts: List[Alert]
    audio_gap: bool = Field(description="True if the microphone has stopped delivering audio (monitoring may be incomplete)")
    llm_busy: bool = Field(description="True while the model is analyzing")
    last_check: Optional[LastCheck] = None


# ── REST: control ────────────────────────────────────────────────────────────

class StartResponse(BaseModel):
    ok: bool = True
    call_id: str


class StopResponse(BaseModel):
    ok: bool = True
    was_active: bool = Field(description="False if no call was being monitored (stop is idempotent)")


class ErrorResponse(BaseModel):
    detail: str = Field(description="Human-readable explanation")
    code: str = Field(description="Machine-readable: already_monitoring | mic_unavailable | invalid_request")


class LLMHealth(BaseModel):
    url: str
    model: str
    reachable: bool
    mode: Optional[str] = Field(None, description="'grammar' (fast, format enforced by server) or 'schema' (JSON fallback)")
    detail: str = ""


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = Field(description="'degraded' when the language model is not reachable")
    api_version: str
    monitoring: bool
    stt_model: str
    rules_enabled: bool = Field(description="False = the language model alone raises alerts")
    analysis_log: bool = Field(description="True if transcripts are being written to the analysis log")
    llm: LLMHealth


# ── REST: stateless analysis ─────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    segments: Optional[List[Line]] = Field(
        None, max_length=40, description="Lines of a conversation, in order. Provide this OR text.")
    text: Optional[str] = Field(
        None, max_length=4000, description="Free text; it is split into sentences. Provide this OR segments.")
    context: List[Line] = Field(
        default_factory=list, max_length=20,
        description="Earlier lines to read alongside the new ones. They are not judged themselves.")
    use_llm: bool = Field(True, description="False = pattern rules only (fast, no model call)")

    @model_validator(mode="after")
    def _exactly_one_input(self):
        has_segments = bool(self.segments)
        has_text = bool(self.text and self.text.strip())
        if has_segments == has_text:
            raise ValueError("provide exactly one of 'segments' or 'text'")
        return self


class RuleFinding(BaseModel):
    level: Risk
    label: str = Field("", description="secret | remote | payment | qr | giftcard | pressure, or empty")
    evidence: str = ""
    shadow: bool = Field(description="True when rules are switched off: reported for comparison but they raise no alert")


class AnalyzeSegment(BaseModel):
    segment_id: str = Field(description="s1, s2, ... in the order given (context lines are c1, c2, ...)")
    text: str
    skipped_by_llm: bool = Field(description="True for one- or two-word lines ('Hello?') that are never sent to the model")
    rule: RuleFinding


class LLMFinding(BaseModel):
    used: bool = Field(description="True if the model was called and gave a usable reply")
    skipped_reason: Optional[str] = None
    risk: Risk = "none"
    segment_id: str = ""
    speaker: Speaker = "unknown"
    vetoed: bool = Field(False, description="True if the model flagged a refusal/safety-advice line and it was ignored")
    reply: Optional[str] = Field(None, description="Raw model reply")
    mode: Optional[str] = None
    duration_s: Optional[float] = None
    prompt_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    error: Optional[str] = None


class AnalyzeResponse(BaseModel):
    risk: Risk = Field(description="Highest risk among the alerts. 'none' is NOT an all-clear when complete is false.")
    complete: bool = Field(description="False if the model was requested but failed, so the result may be missing findings")
    alerts: List[Alert]
    segments: List[AnalyzeSegment]
    llm: LLMFinding
    rules_enabled: bool
    took_s: float


# ── WebSocket: live events (GET /events) ─────────────────────────────────────

class _EventBase(BaseModel):
    call_id: str
    event_id: int = Field(description="Increases by 1 per event; use it to drop duplicates after a reconnect")
    at_ms: int = Field(description="Milliseconds since the call started")


class SnapshotEvent(Snapshot):
    """First message on every connection: the full current state, so a late or reconnecting client catches up."""
    type: Literal["snapshot"] = "snapshot"
    event_id: int = 0


class ListeningEvent(_EventBase):
    type: Literal["listening"]
    message: str


class TranscriptEvent(_EventBase):
    type: Literal["transcript"]
    segment_id: str
    start_ms: int
    end_ms: int
    text: str


class ProcessingEvent(_EventBase):
    type: Literal["processing"]
    active: bool = Field(description="True while the model analyzes, false when it finishes or fails")
    message: str = Field(description="e.g. 'Analysis delayed — rule-based warnings still active' on failure")


class WarningEvent(_EventBase, Alert):
    type: Literal["warning"]
    vibrate: bool = Field(description="True only for a new 'warn', or a 'watch' upgraded to 'warn'. Vibrate once.")


class CheckedEvent(_EventBase, LastCheck):
    type: Literal["checked"]


class InfoEvent(_EventBase):
    type: Literal["info"]
    message: str


class ErrorEvent(_EventBase):
    type: Literal["error"]
    code: str = Field(description="stt_failed | no_audio")
    message: str


class StoppedEvent(_EventBase):
    type: Literal["stopped"]
    reason: str = Field(description="user | max_session | connection_lost | service_exit")
    message: str


Event = Annotated[
    Union[SnapshotEvent, ListeningEvent, TranscriptEvent, ProcessingEvent, WarningEvent,
          CheckedEvent, InfoEvent, ErrorEvent, StoppedEvent],
    Field(discriminator="type"),
]
