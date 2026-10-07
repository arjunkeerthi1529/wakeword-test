import logging
import time

import numpy as np
from openwakeword.model import Model

logger = logging.getLogger(__name__)

_LOG_INTERVAL = 3.0  # log max score every N seconds so you can see it's running


class WakeWordDetector:
    """Thin wrapper around openWakeWord for offline, on-device wake-word detection.

    Accepts float32 audio chunks (sounddevice's native format) and converts
    them to int16 PCM internally — openWakeWord requires int16 input.

    model_name can be a pre-trained model name (e.g. "hey_jarvis") or a
    full path to a custom .onnx model file for a trained custom wake word.
    """

    def __init__(
        self,
        model_name: str = "hey_jarvis",
        threshold: float = 0.5,
        inference_framework: str = "onnx",
    ):
        self.threshold = threshold
        self._last_log = 0.0
        self._max_since_log = 0.0
        logger.info("Loading wake word model '%s' (framework=%s)", model_name, inference_framework)
        self.model = Model(
            wakeword_models=[model_name],
            inference_framework=inference_framework,
        )
        logger.info("Wake word model ready — threshold=%.2f", threshold)

    def detect(self, audio_f32: np.ndarray) -> bool:
        """Feed a float32 [-1, 1] chunk; returns True if wake word fires."""
        audio_i16 = (np.clip(audio_f32.flatten(), -1.0, 1.0) * 32767).astype(np.int16)
        predictions = self.model.predict(audio_i16)
        # values() returns numpy arrays (shape (1,)) or scalars — normalise to float
        score = max(
            (float(np.max(v)) for v in predictions.values()),
            default=0.0,
        )

        # Periodic score logging so you can verify the detector is running
        self._max_since_log = max(self._max_since_log, score)
        now = time.monotonic()
        if now - self._last_log >= _LOG_INTERVAL:
            logger.info("[wake] max score in last %.0fs = %.3f  (threshold=%.2f)",
                        _LOG_INTERVAL, self._max_since_log, self.threshold)
            self._max_since_log = 0.0
            self._last_log = now

        if score >= self.threshold:
            logger.info(">>> WAKE WORD FIRED — score=%.3f", score)
        return score >= self.threshold

    def reset(self):
        """Clear internal mel-feature buffers — call after mute or post-utterance."""
        try:
            self.model.reset()
        except AttributeError:
            # Fallback: clear the prediction deques manually if reset() absent
            for buf in getattr(self.model, "prediction_buffer", {}).values():
                buf.clear()
