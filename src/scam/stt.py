"""faster-whisper wrapper owned by the scam-guard service (its own model instance)."""
import logging
import re
import threading
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_REPEAT = re.compile(r"(\b[\w' ]{3,40}?)(?:[\s,.!?]+\1){2,}", re.IGNORECASE)


def drop_repetition_hallucination(text: str) -> str:
    """Whisper loops on noise ("put it back, put it back, ..."). If a phrase
    repeated 3+ times makes up most of the text, discard it."""
    m = _REPEAT.search(text)
    if m and (m.end() - m.start()) > 0.6 * len(text):
        return ""
    return text


class ScamSTT:
    def __init__(self, model_name: str = "small.en", cpu_threads: int = 0):
        from faster_whisper import WhisperModel

        logger.info("Loading STT model '%s' (int8, cpu)…", model_name)
        self._model = WhisperModel(
            model_name, device="cpu", compute_type="int8", cpu_threads=cpu_threads,
        )
        self._lock = threading.Lock()
        logger.info("STT ready — %s", model_name)

    def warmup(self) -> None:
        """Run one throwaway decode (VAD off, so it really decodes) to pay the
        first-call cost at startup instead of on the first live chunk."""
        t0 = time.monotonic()
        noise = (np.random.default_rng(0).standard_normal(32_000) * 0.05).astype(np.float32)
        with self._lock:
            segments, _ = self._model.transcribe(
                noise, language="en", beam_size=1, vad_filter=False,
                condition_on_previous_text=False,
            )
            list(segments)
        logger.info("STT warm-up done in %.1fs", time.monotonic() - t0)

    def transcribe(self, audio: np.ndarray, beam_size: int = 2,
                   initial_prompt: Optional[str] = None) -> str:
        f32 = audio.flatten().astype(np.float32)
        peak = np.abs(f32).max() if f32.size else 0.0
        if peak > 0:
            f32 = f32 / peak * 0.9
        with self._lock:
            segments, _ = self._model.transcribe(
                f32,
                language="en",
                beam_size=beam_size,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
                initial_prompt=initial_prompt,
                no_speech_threshold=0.4,
                condition_on_previous_text=False,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        return drop_repetition_hallucination(text)
