import logging
import queue
import time
from typing import TYPE_CHECKING, Optional

import numpy as np
import sounddevice as sd

if TYPE_CHECKING:
    from .wake_gate import WakeGate

logger = logging.getLogger(__name__)

_ENERGY_LOG_INTERVAL = 2.0  # log peak mic energy every N seconds while listening
SILENCE_TIMEOUT = "silence_timeout"  # yielded when follow-up window expires


class AudioCapture:
    """Mic -> simple energy-based voice-activity segmentation -> utterance
    generator. While hardware.muted is True, incoming blocks are dropped in
    the audio callback itself — no audio is buffered or written anywhere,
    which is what makes the mute switch a real guarantee and not just a UI
    state.

    When a WakeGate is attached via set_wake_gate(), stream_utterances()
    reads gated blocks from the gate instead of the raw mic queue. The gate
    runs in its own thread and only passes audio through after the wake phrase
    fires. AudioCapture._callback is unchanged — mute is still enforced there
    before anything else sees the audio.
    """

    def __init__(
        self,
        hardware,
        samplerate: int = 16000,
        block_duration: float = 0.5,
        silence_threshold: float = 0.008,
        silence_duration_s: float = 1.0,
    ):
        self.hardware = hardware
        self.samplerate = samplerate
        self.block_duration = block_duration
        self.block_size = int(samplerate * block_duration)
        self.silence_threshold = silence_threshold
        self.silence_blocks = max(1, int(silence_duration_s / block_duration))
        self._q: "queue.Queue[np.ndarray]" = queue.Queue()
        self._wake_gate: Optional["WakeGate"] = None
        self._on_energy = None

    def set_wake_gate(self, gate: "WakeGate"):
        self._wake_gate = gate

    def set_energy_callback(self, cb):
        self._on_energy = cb

    def _callback(self, indata, frames, time_info, status):
        if self.hardware.muted:
            return
        self._q.put(indata.copy())

    def _get_block(self, timeout: float = 1.0) -> Optional[np.ndarray]:
        if self._wake_gate is not None:
            return self._wake_gate.get_block(timeout=timeout)
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def stream_utterances(self, follow_up_window_s: float = 0.0):
        """Yields audio segments (np.ndarray) after VAD detects speech.
        When a WakeGate is attached, yields None first the moment the gate
        opens — the caller should play a greeting and flush queues, then
        resume iteration to capture the actual query.
        After each segment, gate stays open for follow_up_window_s seconds.
        If no speech in that window, yields SILENCE_TIMEOUT so the caller
        can close the gate and return to wake-word detection."""
        buffer = []
        silence_count = 0
        speaking = False
        gate_was_open = False

        # For periodic energy logging while the gate is open
        _last_energy_log = 0.0
        _peak_energy = 0.0
        _no_speech_since: float = 0.0

        with sd.InputStream(
            samplerate=self.samplerate,
            channels=1,
            blocksize=self.block_size,
            callback=self._callback,
        ):
            while True:
                block = self._get_block(timeout=1.0)

                if block is None:
                    gate_was_open = False
                    _peak_energy = 0.0
                    continue

                # Gate just opened
                if self._wake_gate is not None and not gate_was_open:
                    gate_was_open = True
                    buffer = []
                    silence_count = 0
                    speaking = False
                    _last_energy_log = time.monotonic()
                    _peak_energy = 0.0
                    yield None  # sentinel — caller plays greeting & flushes
                    continue

                gate_was_open = True
                energy = float(np.abs(block).mean())
                _peak_energy = max(_peak_energy, energy)

                if self._on_energy:
                    self._on_energy(energy)

                # Follow-up timeout: gate still open but user hasn't spoken
                if follow_up_window_s > 0 and not speaking and _no_speech_since > 0:
                    if time.monotonic() - _no_speech_since >= follow_up_window_s:
                        gate_was_open = False
                        _no_speech_since = 0.0
                        yield SILENCE_TIMEOUT
                        continue

                # Periodic energy log so you can see if your mic is loud enough
                now = time.monotonic()
                if now - _last_energy_log >= _ENERGY_LOG_INTERVAL:
                    logger.info(
                        "[VAD] peak energy=%.4f  threshold=%.4f  %s",
                        _peak_energy,
                        self.silence_threshold,
                        "SPEECH DETECTED" if speaking else "(silence — speak louder if stuck)",
                    )
                    _peak_energy = 0.0
                    _last_energy_log = now

                if energy > self.silence_threshold:
                    buffer.append(block)
                    speaking = True
                    silence_count = 0
                    _no_speech_since = 0.0  # user is speaking, cancel timeout
                elif speaking:
                    silence_count += 1
                    buffer.append(block)
                    if silence_count >= self.silence_blocks:
                        segment = np.concatenate(buffer, axis=0).flatten()
                        buffer = []
                        speaking = False
                        silence_count = 0
                        # Keep gate_was_open = True so the next block doesn't
                        # trigger the "gate just opened" sentinel again.
                        # The caller decides when to close the gate.
                        _peak_energy = 0.0
                        _no_speech_since = time.monotonic()  # start follow-up timer
                        yield segment
