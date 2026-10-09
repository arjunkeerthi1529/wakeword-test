"""Meeting / conversation recorder + summariser.

Click "Start" -> continuously records from the default microphone until
"Stop" is clicked. Unlike the voice assistant's AudioCapture (wake-word
gated, VAD-segmented into separate utterances), this keeps recording straight
through pauses and multiple speakers -- a meeting's silence is part of the
recording, not a turn boundary.

On stop: the whole recording is transcribed once, locally, with
faster-whisper (same engine the voice assistant uses for STT), then
summarised by the local LLM. Both the transcript and summary are handed back
to the caller (server.py) to persist.
"""
import logging
import threading
import time
from typing import Optional

import numpy as np
import requests

logger = logging.getLogger(__name__)

_SUMMARIZE_SYSTEM = (
    "You summarise a recorded conversation or meeting from its transcript. "
    "Write a short paragraph (3-5 sentences) covering what was discussed, "
    "then a bulleted list of key points or action items if any were "
    "mentioned -- omit the list if there are none. Base this only on what is "
    "actually in the transcript; never invent names, dates, or decisions "
    "that are not there. If the transcript is too short or unclear to "
    "summarise, say so plainly instead of guessing."
)


class RecorderStateError(RuntimeError):
    """Raised when start()/stop() is called in the wrong state."""


class MeetingRecorder:
    """Continuous mic capture, start/stop controlled. One recording at a
    time -- start() raises if already recording, stop() raises if not."""

    def __init__(self, samplerate: int = 16000):
        self.samplerate = samplerate
        self._stream = None
        self._blocks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._recording = False
        self._started_at_monotonic: Optional[float] = None
        self.started_at_iso: Optional[str] = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        if self._recording:
            raise RecorderStateError("Already recording")
        import sounddevice as sd
        from datetime import datetime

        self._blocks = []
        self._started_at_monotonic = time.monotonic()
        self.started_at_iso = datetime.now().isoformat(timespec="seconds")

        def _callback(indata, frames, time_info, status):
            with self._lock:
                self._blocks.append(indata.copy())

        self._stream = sd.InputStream(samplerate=self.samplerate, channels=1, callback=_callback)
        self._stream.start()
        self._recording = True
        logger.info("Meeting recording started")

    def stop(self) -> dict:
        """Stops capture. Returns {audio, duration_s, started_at, ended_at}."""
        if not self._recording:
            raise RecorderStateError("Not recording")
        from datetime import datetime

        self._stream.stop()
        self._stream.close()
        self._stream = None
        self._recording = False
        duration_s = time.monotonic() - (self._started_at_monotonic or time.monotonic())
        ended_at_iso = datetime.now().isoformat(timespec="seconds")

        with self._lock:
            blocks, self._blocks = self._blocks, []

        audio = (
            np.concatenate(blocks, axis=0).flatten().astype(np.float32)
            if blocks else np.zeros(0, dtype=np.float32)
        )
        logger.info("Meeting recording stopped — %.1fs captured", duration_s)
        return {
            "audio": audio,
            "duration_s": duration_s,
            "started_at": self.started_at_iso,
            "ended_at": ended_at_iso,
        }


# ── Transcription (local, offline — faster-whisper) ───────────────────────────

_whisper_model = None
_whisper_lock = threading.Lock()


def preload_whisper(model_name: str) -> None:
    """Load the STT model eagerly at service startup, so the first "Stop"
    click isn't stuck waiting on a multi-second (or first-run download) load.
    Raises if faster-whisper isn't installed — caller decides how to degrade.
    """
    _get_whisper(model_name)


def _get_whisper(model_name: str):
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            logger.info("Loading STT model '%s' for meeting transcription…", model_name)
            _whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
            logger.info("Meeting STT model ready")
        return _whisper_model


def transcribe(audio: np.ndarray, model_name: str = "small.en") -> str:
    """Local, offline transcription of a full recording. Empty string for
    silence/empty input — never a fabricated guess at what was said."""
    if audio.size == 0:
        return ""
    peak = np.abs(audio).max()
    if peak > 0:
        audio = audio / peak * 0.9
    model = _get_whisper(model_name)
    segments, _ = model.transcribe(
        audio, language="en", beam_size=5, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    return " ".join(s.text.strip() for s in segments).strip()


# ── Summarisation (local LLM) ──────────────────────────────────────────────────

def summarize_transcript(llm_base_url: str, model: str, transcript: str) -> str:
    """One plain-text-in, plain-text-out LLM call. Falls back to an honest
    'could not summarise' message rather than fabricating a summary."""
    if not transcript.strip():
        return "No speech was detected in this recording."
    try:
        resp = requests.post(
            f"{llm_base_url.rstrip('/')}/v1/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _SUMMARIZE_SYSTEM},
                    {"role": "user", "content": transcript},
                ],
                "stream": False,
                "temperature": 0.3,
                "max_tokens": 350,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("LLM summarise failed for meeting: %s", exc)
        return "Could not summarise — the local model was unavailable. The transcript is still saved below."
