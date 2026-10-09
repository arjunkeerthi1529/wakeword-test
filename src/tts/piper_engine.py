import io
import logging
import os
import re
import subprocess
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

    def __init__(self, model_path: str, output_device=None, aplay_device: str = ""):
        """output_device: sounddevice output (index, or part of the name). None/"" = system default.
        aplay_device: play through `aplay -D <device>` instead of sounddevice — e.g. "plughw:3,0",
        or "auto" to pick the Jabra/USB card from `aplay -l`. The same ALSA route as a manual aplay."""
        self._device = self._parse_device(output_device)
        self._out_channels = 1
        self._aplay = self._resolve_aplay(aplay_device)
        if self._aplay:
            logger.info("TTS playback: aplay -D %s", self._aplay)
        else:
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
    def find_usb_playback_card(aplay_l_output: str):
        """Return 'plughw:<card>,<device>' for the first USB/Jabra playback card, else None."""
        for line in aplay_l_output.splitlines():
            m = re.match(r"card (\d+): (\S+) \[(.*?)\], device (\d+):", line)
            if m and re.search(r"jabra|usb", f"{m.group(2)} {m.group(3)}", re.IGNORECASE):
                return f"plughw:{m.group(1)},{m.group(4)}"
        return None

    def _resolve_aplay(self, aplay_device) -> str:
        spec = str(aplay_device or "").strip()
        if not spec:
            return ""
        if spec.lower() != "auto":
            return spec
        try:
            out = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("tts_aplay_device=auto: could not run `aplay -l` (%s) — using sounddevice", exc)
            return ""
        found = self.find_usb_playback_card(out)
        if not found:
            logger.warning("tts_aplay_device=auto: no USB/Jabra playback card in `aplay -l` — using sounddevice")
            return ""
        return found

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
        if self._aplay:
            if self._play_aplay(audio, samplerate):
                return
        if self._device is not None and self._out_channels >= 2 and audio.ndim == 1:
            audio = np.column_stack([audio, audio])      # direct hw devices (e.g. USB headsets) need stereo
        sd.play(audio, samplerate=samplerate, device=self._device)
        sd.wait()

    def _play_aplay(self, audio: np.ndarray, samplerate: int) -> bool:
        """Pipe raw PCM to aplay (mono -> the device's channel layout via plughw). True if played."""
        cmd = ["aplay", "-q", "-D", self._aplay, "-t", "raw", "-r", str(samplerate),
               "-f", "S16_LE", "-c", "1", "-"]
        try:
            proc = subprocess.run(cmd, input=audio.astype("<i2").tobytes(), capture_output=True)
        except OSError as exc:
            logger.error("aplay not available (%s) — falling back to sounddevice", exc)
            self._aplay = ""
            return False
        if proc.returncode != 0:
            logger.error("aplay -D %s failed (exit %d): %s", self._aplay, proc.returncode,
                         proc.stderr.decode(errors="replace").strip())
            return False
        return True

    def speak(self, text: str) -> None:
        """Convenience: synthesize then play. Used for the wake greeting."""
        audio, sr = self.synthesize(text)
        self.play(audio, sr)
