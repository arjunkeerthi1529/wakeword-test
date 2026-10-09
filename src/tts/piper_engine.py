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

    def __init__(self, model_path: str, output_device=None):
        """output_device: sounddevice output (index, or part of the name). None/"" = system default."""
        self._device = self._parse_device(output_device)
        self._out_channels = 1
        self._log_output_device()
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

    @staticmethod
    def _parse_device(device):
        if device is None or str(device).strip() == "":
            return None
        text = str(device).strip()
        return int(text) if text.isdigit() else text

    def _log_output_device(self) -> None:
        """Say where speech will come out, so a silent TTS is diagnosable from the log."""
        try:
            info = sd.query_devices(self._device, "output")
            self._out_channels = int(info["max_output_channels"])
            logger.info("TTS output device: [%s] %s (%d ch)%s", info.get("index", "?"), info["name"],
                        self._out_channels, "" if self._device is not None else "  <- system default")
        except Exception as exc:
            logger.warning("Could not query TTS output device %r (%s) — using sounddevice's default",
                           self._device, exc)
            self._device = None

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
        if self._device is not None and self._out_channels >= 2 and audio.ndim == 1:
            audio = np.column_stack([audio, audio])      # direct hw devices (e.g. USB headsets) need stereo
        sd.play(audio, samplerate=samplerate, device=self._device)
        sd.wait()

    def speak(self, text: str) -> None:
        """Convenience: synthesize then play. Used for the wake greeting."""
        audio, sr = self.synthesize(text)
        self.play(audio, sr)
