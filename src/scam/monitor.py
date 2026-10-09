"""ScamMonitor: mic listener -> VAD segmenter -> STT -> rules + LLM review -> alerts.

Capture, STT and LLM review run in separate threads, so a busy LLM never stops
recording and a slow STT chunk never blocks the instant rule path for
segments that are already committed.
"""
import collections
import logging
import math
import queue
import re
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

from . import rules
from .datalog import DataLog
from .llm_review import SYSTEM_PROMPT, ReviewResult, Snapshot, build_snapshot, validate
from .session import Alert, Session, rank, trim_overlap

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
MIN_SPEECH_S = 0.4          # shorter bursts (coughs, clicks) are discarded
PREROLL_BLOCKS = 2          # audio kept before speech starts so first words aren't clipped
NO_AUDIO_WARN_S = 5.0
FRAGMENT_FLUSH_S = 4.0      # a held-back unfinished sentence is committed alone after this long
_SENT_END = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> List[str]:
    return [part for part in _SENT_END.split(text.strip()) if part]

SCAM_STT_PROMPT = (
    "A phone call about a bank account: OTP, one time password, verification code, "
    "UPI PIN, CVV, KYC, Aadhaar, PAN card, debit card, credit card, AnyDesk, "
    "TeamViewer, refund, transfer, customer care."
)
_DEFAULT_PRESSURE_MESSAGE = (
    "Possible pressure or impersonation was heard. Don't share codes or send money."
)
_LLM_MESSAGES = {
    "warn": "The on-device assistant flagged this as a likely scam request. "
            "Don't share codes or send money; verify through the official app or a number you looked up yourself.",
    "watch": "The on-device assistant noticed possible pressure or impersonation. Stay cautious.",
}


class ScamMonitor:
    def __init__(self, cfg, mic, stt, llm, led, on_event: Callable[[dict], None],
                 datalog: Optional[DataLog] = None):
        self.cfg = cfg                      # SpamGuardConfig
        self.mic = mic                      # MicSource: start() -> Queue, stop()
        self.stt = stt                      # ScamSTT
        self.llm = llm                      # ScamLLM
        self.led = led                      # WarningLED
        self.on_event = on_event
        self.datalog = datalog or DataLog("")
        self._seg_cut: Dict[str, float] = {}    # segment id -> monotonic time its chunk was cut

        self._lock = threading.RLock()
        self._session: Optional[Session] = None
        self._stop_evt = threading.Event()
        self._listener: Optional[queue.Queue] = None
        self._event_id = 0
        self._review_soon = False
        self._llm_failing = False
        self._audio_gap = False
        self._last_block_t = 0.0
        self._stt_busy = False
        self._fragment = ""                # unfinished last sentence of a forced chunk cut
        self._fragment_start_ms = 0
        self._fragment_t = 0.0

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._session is not None

    def start(self) -> str:
        with self._lock:
            if self._session is not None:
                raise RuntimeError("Already monitoring a call")

            listener = self.mic.start()         # raises RuntimeError if the mic is busy/unavailable
            self._listener = listener

            session = Session()
            stop_evt = threading.Event()
            chunks: "queue.Queue" = queue.Queue()

            self._session = session
            self._stop_evt = stop_evt
            self._review_soon = False
            self._llm_failing = False
            self._audio_gap = False
            self._last_block_t = time.monotonic()
            self._seg_cut = {}
            self._fragment = ""
            self.datalog.write(
                "call_start", session.call_id,
                stt_model=getattr(self.cfg, "stt_model", ""), stt_threads=self.cfg.stt_threads,
                stt_beam_size=self.cfg.stt_beam_size, chunk_target_s=self.cfg.chunk_target_s,
                chunk_max_s=self.cfg.chunk_max_s, pause_s=self.cfg.pause_s,
                silence_thresh=self.cfg.silence_thresh, llm_cadence_s=self.cfg.llm_cadence_s,
                llm_max_tokens=self.cfg.llm_max_tokens, llm_url=getattr(self.cfg, "llm_base_url", ""),
            )

            for name, target, args in (
                ("scam-segmenter", self._segment_loop, (session, stop_evt, listener, chunks)),
                ("scam-stt", self._stt_loop, (session, stop_evt, chunks)),
                ("scam-watchdog", self._watchdog_loop, (session, stop_evt)),
            ):
                threading.Thread(target=target, args=args, daemon=True, name=name).start()

            self._emit("listening", session=session, message="Monitoring started")
        logger.info("Scam monitoring started — call %s", session.call_id)
        return session.call_id

    def stop(self, reason: str = "user") -> None:
        with self._lock:
            session = self._session
            if session is None:
                return
            self._session = None
            self._stop_evt.set()
            self._listener = None
            self.mic.stop()
            alerts = len(session.alerts)
            segments = len(session.segments)
            self._emit("stopped", session=session, reason=reason, message="Monitoring stopped")
            self.datalog.write("call_end", session.call_id, reason=reason, segments=segments,
                               alerts=alerts, duration_s=round(time.monotonic() - session.started_at, 1))
        self.led.set("off")
        logger.info("Scam monitoring stopped (%s) — %d segments, %d alerts", reason, segments, alerts)

    def snapshot(self) -> dict:
        with self._lock:
            s = self._session
            if s is None:
                return {"active": False, "call_id": "", "segments": [], "alerts": []}
            return {
                "active": True,
                "call_id": s.call_id,
                "segments": [
                    {"segment_id": g.id, "start_ms": g.start_ms, "end_ms": g.end_ms, "text": g.text}
                    for g in s.segments
                ],
                "alerts": [self._alert_fields(a) for a in s.alerts.values()],
                "audio_gap": self._audio_gap,
                "llm_busy": s.llm_busy,
            }

    # ── Events ────────────────────────────────────────────────────────────

    def _emit(self, type_: str, session: Optional[Session] = None, **fields) -> None:
        with self._lock:
            s = session or self._session
            if type_ == "error":
                self.datalog.write("error", s.call_id if s else "", **fields)
            self._event_id += 1
            event = {
                "call_id": s.call_id if s else "",
                "event_id": self._event_id,
                "type": type_,
                "at_ms": int((time.monotonic() - s.started_at) * 1000) if s else 0,
                **fields,
            }
            try:
                self.on_event(event)
            except Exception:
                logger.exception("on_event callback failed")

    @staticmethod
    def _alert_fields(a: Alert) -> dict:
        return {
            "segment_id": a.segment_id,
            "risk": a.level,
            "speaker": a.speaker,
            "evidence": a.evidence,
            "message": a.message,
            "reason": a.reason,
            "advice": a.advice,
            "source": a.source,
            "revision": a.revision,
        }

    # ── Capture → chunks ──────────────────────────────────────────────────

    def _segment_loop(self, session, stop_evt, listener, chunks) -> None:
        cfg = self.cfg
        preroll: collections.deque = collections.deque(maxlen=PREROLL_BLOCKS)
        buf: List[np.ndarray] = []
        speaking = False
        silence_s = 0.0
        dur_s = 0.0
        t_s = 0.0
        chunk_start_s = 0.0

        def emit_chunk(end_s: float, hard: bool = False) -> None:
            audio = np.concatenate(buf).astype(np.float32)
            chunks.put((audio, int(chunk_start_s * 1000), int(end_s * 1000), time.monotonic(), hard))

        while not stop_evt.is_set():
            try:
                block = listener.get(timeout=0.5)
            except queue.Empty:
                continue
            self._last_block_t = time.monotonic()

            flat = block.flatten().astype(np.float32)
            block_s = len(flat) / SAMPLE_RATE
            block_start = t_s
            t_s += block_s
            energy = float(np.abs(flat).mean())

            if energy > cfg.silence_thresh:
                if not speaking:
                    speaking = True
                    buf = list(preroll)
                    preroll.clear()
                    dur_s = len(buf) * block_s
                    chunk_start_s = block_start - dur_s
                buf.append(flat)
                dur_s += block_s
                silence_s = 0.0
            elif speaking:
                buf.append(flat)
                dur_s += block_s
                silence_s += block_s
                needed = cfg.pause_s if dur_s < cfg.chunk_target_s else cfg.short_pause_s
                if silence_s >= needed:
                    has_speech = dur_s - silence_s >= MIN_SPEECH_S
                    if has_speech and (self._stt_busy or not chunks.empty()):
                        # Every chunk costs a full fixed-size decode, so while STT is
                        # still busy keep batching speech instead of queuing short chunks.
                        pass
                    else:
                        if has_speech:
                            emit_chunk(t_s)
                        buf, speaking, silence_s, dur_s = [], False, 0.0, 0.0
            else:
                preroll.append(flat)

            if speaking and dur_s >= cfg.chunk_max_s:
                emit_chunk(t_s, hard=True)      # cut mid-speech: the last sentence may be unfinished
                keep = max(1, math.ceil(cfg.overlap_s / block_s))
                buf = buf[-keep:]
                dur_s = len(buf) * block_s
                chunk_start_s = t_s - dur_s
                silence_s = 0.0

    # ── Chunks → committed segments ───────────────────────────────────────

    def _stt_loop(self, session, stop_evt, chunks) -> None:
        while not stop_evt.is_set():
            try:
                audio, start_ms, end_ms, cut_mono, hard = chunks.get(timeout=1.0)
            except queue.Empty:
                self._flush_stale_fragment(session)
                self._maybe_dispatch_llm(session)    # lets the routine cadence fire during quiet spells
                continue

            backlog = chunks.qsize()
            t0 = time.monotonic()
            self._stt_busy = True
            try:
                text = self.stt.transcribe(
                    audio, beam_size=self.cfg.stt_beam_size, initial_prompt=SCAM_STT_PROMPT,
                )
            except Exception as exc:
                logger.exception("STT failed on a chunk")
                self.datalog.write("stt_error", session.call_id, error=str(exc))
                self._emit("error", session=session, code="stt_failed",
                           message="May have missed speech.")
                continue
            finally:
                self._stt_busy = False
            elapsed = time.monotonic() - t0
            audio_s = len(audio) / SAMPLE_RATE
            logger.info("STT rtf=%.2f (%.1fs audio in %.1fs, backlog=%d)",
                        elapsed / audio_s, audio_s, elapsed, backlog)
            self.datalog.write(
                "stt", session.call_id, start_ms=start_ms, end_ms=end_ms,
                audio_s=round(audio_s, 2), stt_s=round(elapsed, 2), rtf=round(elapsed / audio_s, 2),
                queue_wait_s=round(t0 - cut_mono, 2), backlog=backlog, text=text,
            )

            if stop_evt.is_set():
                return
            self._commit(session, text, start_ms, end_ms, cut_mono, hard)

    def _commit(self, session: Session, text: str, start_ms: int, end_ms: int,
                cut_mono: float, hard: bool) -> None:
        """Join any held-back unfinished sentence with this chunk's text. When the chunk
        was cut mid-speech, hold back its last sentence (it may be cut in half) and
        commit only the complete ones, so a request split across chunks reaches the
        rules and the LLM as one sentence."""
        text = text.strip()
        if self._fragment:
            start_ms = self._fragment_start_ms
            text = f"{self._fragment} {trim_overlap(self._fragment, text)}".strip()
            self._fragment = ""
        if hard and text:
            sentences = split_sentences(text)
            self._fragment = sentences[-1]
            self._fragment_t = time.monotonic()
            if len(sentences) == 1:
                self._fragment_start_ms = start_ms
                text = ""
            else:
                text = " ".join(sentences[:-1])
                self._fragment_start_ms = end_ms     # approximate: the tail begins near the end
        if text:
            self._commit_segment(session, text, start_ms, end_ms, cut_mono)

    def _flush_stale_fragment(self, session: Session) -> None:
        if self._fragment and time.monotonic() - self._fragment_t > FRAGMENT_FLUSH_S:
            text, start_ms = self._fragment, self._fragment_start_ms
            self._fragment = ""
            self._commit_segment(session, text, start_ms, start_ms, self._fragment_t)

    def _commit_segment(self, session: Session, text: str, start_ms: int, end_ms: int,
                        cut_mono: float) -> None:
        with self._lock:
            if self._session is not session:
                return
            seg = session.add_segment(text, start_ms, end_ms)
            if seg is None:
                if text.strip():
                    self.datalog.write("segment_dropped", session.call_id, text=text,
                                       reason="duplicate of previous segment (chunk overlap)")
                return
            prev = session.previous_text()
            self._seg_cut[seg.id] = cut_mono
            logger.info("SEGMENT %s: %s", seg.id, seg.text)
            self.datalog.write(
                "segment", session.call_id, segment_id=seg.id, start_ms=seg.start_ms,
                end_ms=seg.end_ms, text=seg.text,
                since_cut_s=round(time.monotonic() - cut_mono, 2),
            )
            self._emit("transcript", session=session, segment_id=seg.id,
                       start_ms=seg.start_ms, end_ms=seg.end_ms, text=seg.text)

        result = rules.evaluate(seg.text, seg.id, prev)
        shadow = not self.cfg.rules_enabled
        self.datalog.write("rule", session.call_id, segment_id=seg.id, level=result.level,
                           label=result.label, evidence=result.evidence, shadow=shadow)
        if result.level != "none" and shadow:
            logger.info("RULE (shadow, no alert) %s: %s/%s — %r", seg.id, result.level,
                        result.label, result.evidence)
        elif result.level != "none":
            logger.info("RULE %s: %s/%s — %r", seg.id, result.level, result.label, result.evidence)
            self._raise(
                session, level=result.level, label=result.label, evidence=result.evidence,
                segment_id=seg.id, message=result.message or _DEFAULT_PRESSURE_MESSAGE,
                source="rule",
            )
            self._review_soon = True
        self._maybe_dispatch_llm(session)

    # ── Alerts ────────────────────────────────────────────────────────────

    def _raise(self, session: Session, level: str, label: str, evidence: str, segment_id: str,
               message: str, source: str, speaker: str = "unknown",
               reason: str = "", advice: str = "") -> None:
        with self._lock:
            if self._session is not session:
                return
            existing = session.alerts.get(segment_id)
            vibrate = False
            if existing is None:
                alert = Alert(segment_id, level, label, evidence, message, source,
                              speaker, reason, advice, 1)
                session.alerts[segment_id] = alert
                vibrate = level == "warn"
            else:
                before = (existing.level, existing.reason, existing.advice, existing.speaker)
                if rank(level) > rank(existing.level):
                    vibrate = level == "warn"
                    existing.level = level
                if source == "llm":
                    existing.reason = reason or existing.reason
                    existing.advice = advice or existing.advice
                    if speaker != "unknown":
                        existing.speaker = speaker
                if before == (existing.level, existing.reason, existing.advice, existing.speaker):
                    return
                if source == "llm":
                    existing.source = "llm"
                existing.revision += 1
                alert = existing

            overall = session.max_level()
            self._emit("warning", session=session, vibrate=vibrate, **self._alert_fields(alert))
            cut = self._seg_cut.get(segment_id)
            since_cut = round(time.monotonic() - cut, 2) if cut else None
            logger.info("ALERT %s %s via %s rev%d%s", segment_id, alert.level, alert.source,
                        alert.revision, f" ({since_cut:.1f}s after chunk cut)" if since_cut is not None else "")
            self.datalog.write("alert", session.call_id, vibrate=vibrate, since_cut_s=since_cut,
                               **self._alert_fields(alert))
        self.led.set(overall)

    # ── LLM review scheduling (one request in flight) ─────────────────────

    def _maybe_dispatch_llm(self, session: Session) -> None:
        with self._lock:
            if self._session is not session or session.llm_busy:
                return
            if not session.unreviewed():
                return
            now = time.monotonic()
            if not self._review_soon and now - session.last_dispatch < self.cfg.llm_cadence_s:
                return
            snap = build_snapshot(session)
            if snap is None:
                # Everything pending is trivial ("Hello?", "Okay."): nothing to review.
                session.reviewed_upto = len(session.segments)
                return
            session.llm_busy = True
            session.last_dispatch = now
            self._review_soon = False
            self._emit("processing", session=session, active=True, message="Analyzing on Raspberry Pi")
        threading.Thread(target=self._run_review, args=(session, snap),
                         daemon=True, name="scam-llm").start()

    def _run_review(self, session: Session, snap: Snapshot) -> None:
        result: Optional[ReviewResult] = None
        transport_error: Optional[Exception] = None
        raw: Optional[str] = None
        timings: dict = {}
        t0 = time.monotonic()
        try:
            raw, timings = self.llm.review(
                SYSTEM_PROMPT, snap.prompt,
                max_tokens=self.cfg.llm_max_tokens, timeout=self.cfg.llm_timeout_s,
            )
            result = validate(raw, snap)
            logger.info(
                "LLM review took %.1fs (prompt %s tok in %.1fs, decode %s tok in %.1fs, %s) → %s",
                time.monotonic() - t0,
                timings.get("prompt_n"), timings.get("prompt_ms", 0) / 1000,
                timings.get("predicted_n"), timings.get("predicted_ms", 0) / 1000,
                timings.get("mode", "?"),
                (f"{result.risk} {result.segment_id} {result.speaker}".strip()
                 if result else "invalid reply"),
            )
        except Exception as exc:
            transport_error = exc
        self.datalog.write(
            "llm", snap.call_id, duration_s=round(time.monotonic() - t0, 2), timings=timings,
            segments=[s.id for s in snap.new_segments], prompt=snap.prompt, raw=raw,
            verdict=({"risk": result.risk, "segment_id": result.segment_id, "speaker": result.speaker}
                     if result else None),
            error=str(transport_error) if transport_error else None,
        )

        with self._lock:
            if self._session is not session or snap.call_id != session.call_id:
                return
            session.llm_busy = False
            if transport_error is not None:
                logger.warning("LLM review failed: %s", transport_error)
                self._llm_failing = True
                session.last_dispatch = time.monotonic() + 20.0   # retry in ~cadence+20s, not every tick
                self._review_soon = False
                self._emit("processing", session=session, active=False,
                           message="Analysis delayed — rule-based warnings still active")
            else:
                self._llm_failing = False
                # Valid or unusable reply: advance either way so one bad reply
                # can't wedge the queue. Rules already covered these segments.
                session.reviewed_upto = max(session.reviewed_upto, snap.upto_index)
                self._emit("processing", session=session, active=False, message="")
                if result is not None and result.risk != "none":
                    advice_line = rules.looks_like_safety_advice(result.evidence)
                    if advice_line and self.cfg.llm_advice_veto:
                        logger.info("LLM %s on %s ignored: the line is a refusal or safety advice",
                                    result.risk, result.segment_id)
                        self.datalog.write("llm_veto", snap.call_id, segment_id=result.segment_id,
                                           risk=result.risk, text=result.evidence)
                    else:
                        self._raise(
                            session, level=result.risk, label="llm", evidence=result.evidence,
                            segment_id=result.segment_id, message=_LLM_MESSAGES[result.risk],
                            source="llm", speaker=result.speaker,
                        )
        self._maybe_dispatch_llm(session)

    # ── Watchdog: max session length and audio gaps ───────────────────────

    def _watchdog_loop(self, session: Session, stop_evt: threading.Event) -> None:
        while not stop_evt.wait(1.0):
            if time.monotonic() - session.started_at > self.cfg.max_session_s:
                logger.info("Max session length reached")
                self.stop("max_session")
                return
            gap = time.monotonic() - self._last_block_t > NO_AUDIO_WARN_S
            if gap and not self._audio_gap:
                self._audio_gap = True
                self._emit("error", session=session, code="no_audio",
                           message="Microphone is not delivering audio. Monitoring may be incomplete.")
            elif not gap and self._audio_gap:
                self._audio_gap = False
                self._emit("status", session=session, message="Audio resumed")
