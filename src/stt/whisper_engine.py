import logging

import numpy as np

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000


class WhisperEngine:
    """Local STT via faster-whisper (CTranslate2 int8).

    Significantly more accurate than whisper.cpp at the same model size.
    Model auto-downloads on first use (~250MB for small.en).

    model_name — faster-whisper model name: tiny.en, base.en, small.en, medium.en
    """

    def __init__(self, model_name: str = "small.en", **kwargs):
        from faster_whisper import WhisperModel
        logger.info("Loading faster-whisper model '%s' (int8, cpu)…", model_name)
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8")
        logger.info("faster-whisper ready")

    def transcribe(self, audio: np.ndarray) -> str:
        audio_f32 = audio.flatten().astype(np.float32)
        segments, _ = self.model.transcribe(
            audio_f32,
            language="en",
            beam_size=5,
            vad_filter=True,        # skip silent chunks automatically
            vad_parameters={"min_silence_duration_ms": 300},
        )
        text = " ".join(s.text.strip() for s in segments)
        return text.strip()
