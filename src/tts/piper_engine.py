import io
import logging
import os
import wave
from typing import Dict, Tuple

import numpy as np
import sounddevice as sd
from piper.voice import PiperVoice

logger = logging.getLogger(__name__)

# Phrases pre-synthesized at startup so first speak() is instant (no ONNX inference wait)
_WARMUP_PHRASES = [
    "Hey, what's up?",
]


class PiperEngine:
    """Offline neural TTS via piper-tts (ONNX inference, no cloud).

    Pre-synthesizes common phrases at __init__ time so the wake-word
    greeting plays with zero synthesis delay — just instant audio playback.

    Public interface for latency measurement:
        synthesize(text) -> (audio, samplerate)  # synthesis only, no playback
        play(audio, samplerate)                  # playback only, blocks until done
        speak(text)                              # convenience: synthesize + play
    """

    def __init__(self, model_path: str):
        json_path = model_path + ".json"
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Piper model not found: {model_path}\n"
                "Download from https://huggingface.co/rhasspy/piper-voices"
            )
        if not os.path.exists(json_path):
            raise FileNotFoundError(
                f"Piper sidecar config missing: {json_path}\n"
                "Download the .onnx.json alongside the .onnx file."
            )
        logger.info("Loading Piper model: %s", model_path)
        self.voice = PiperVoice.load(model_path)
        self._cache: Dict[str, Tuple[np.ndarray, int]] = {}

        logger.info("Pre-synthesizing %d warmup phrases…", len(_WARMUP_PHRASES))
        for phrase in _WARMUP_PHRASES:
            self._cache[phrase] = self._do_synthesize(phrase)
        logger.info("Piper ready — greeting will play instantly on wake")

    def _do_synthesize(self, text: str) -> Tuple[np.ndarray, int]:
        buf = io.BytesIO()
        wav_out = wave.open(buf, "wb")
        try:
            self.voice.synthesize_wav(text, wav_out)
        finally:
            wav_out.close()
        buf.seek(0)
        with wave.open(buf, "rb") as wf:
            audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            sr = wf.getframerate()
        return audio, sr

    def synthesize(self, text: str) -> Tuple[np.ndarray, int]:
        """Return (audio_array, samplerate) without playing. Cached after first call."""
        if text not in self._cache:
            self._cache[text] = self._do_synthesize(text)
        return self._cache[text]

    def play(self, audio: np.ndarray, samplerate: int) -> None:
        """Play pre-synthesized audio and block until playback completes."""
        sd.play(audio, samplerate=samplerate)
        sd.wait()

    def speak(self, text: str) -> None:
        """Convenience: synthesize then play. Used for the wake greeting."""
        audio, sr = self.synthesize(text)
        self.play(audio, sr)
