import logging

import numpy as np

logger = logging.getLogger(__name__)


class WhisperEngine:
    """Two-model STT:
    - tiny.en  for live word-by-word display (~0.5s per chunk)
    - small.en for accurate final transcription sent to LLM
    """

    def __init__(self, model_name: str = "small.en", live_model_name: str = "tiny.en", **kwargs):
        from faster_whisper import WhisperModel
        logger.info("Loading live STT model '%s' (int8, cpu)…", live_model_name)
        self._live = WhisperModel(live_model_name, device="cpu", compute_type="int8")
        logger.info("Loading accurate STT model '%s' (int8, cpu)…", model_name)
        self._model = WhisperModel(model_name, device="cpu", compute_type="int8")
        logger.info("faster-whisper ready — live=%s  final=%s", live_model_name, model_name)

    def transcribe_chunk(self, audio: np.ndarray) -> str:
        """Fast partial transcription for live display (tiny.en, beam_size=1)."""
        f32 = audio.flatten().astype(np.float32)
        segments, _ = self._live.transcribe(f32, language="en", beam_size=1, vad_filter=False)
        return " ".join(s.text.strip() for s in segments).strip()

    def transcribe(self, audio: np.ndarray) -> str:
        """Accurate final transcription (small.en, beam_size=5)."""
        f32 = audio.flatten().astype(np.float32)
        # Normalize audio to prevent low-volume distortion
        peak = np.abs(f32).max()
        if peak > 0:
            f32 = f32 / peak * 0.9
        segments, _ = self._model.transcribe(
            f32,
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            # Bias toward common tech/AI vocabulary to reduce misrecognitions
            initial_prompt=(
                "The user is talking to an AI assistant about technology, "
                "machine learning, algorithms, sorting, programming, and daily tasks."
            ),
            no_speech_threshold=0.4,
        )
        return " ".join(s.text.strip() for s in segments).strip()
