import logging
import queue
import threading
import time
from typing import Optional

import numpy as np

from .wake_word import WakeWordDetector

logger = logging.getLogger(__name__)

# openWakeWord processes audio in 80 ms frames at 16 kHz
_CHUNK_SAMPLES = 1280
# How long to stay awake waiting for speech before auto-resetting
_LISTEN_TIMEOUT = 30.0


class WakeGate:
    """Background thread that gates audio between the mic callback and the VAD.

    Reads raw float32 blocks from AudioCapture._q, reframes them into 80 ms
    chunks for the wake-word detector, and only forwards audio downstream once
    the wake phrase fires. After the orchestrator signals that one utterance has
    been fully handled it calls sleep(), which closes the gate and waits for the
    next wake event.

    Gate mode (current default):
        Mic audio is suppressed until wake word -> one utterance passes through
        -> gate sleeps again. Switching to hybrid mode later only requires
        changing the forwarding logic here; AudioCapture and Orchestrator are
        unaffected.

    Mute invariant: AudioCapture._callback drops blocks before they reach _q
    when muted, so this thread never sees muted audio — the compliance
    guarantee is preserved upstream.
    """

    def __init__(self, raw_queue: "queue.Queue[np.ndarray]", detector: WakeWordDetector, on_wake=None):
        self._raw_q = raw_queue
        self._detector = detector
        self._out_q: "queue.Queue[Optional[np.ndarray]]" = queue.Queue()
        self._awake = False
        self._stop = threading.Event()
        self._on_wake = on_wake  # called on orchestrator when wake fires
        self.suppressed = False  # set True during TTS to block echo re-trigger
        self._awake_since: Optional[float] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        t = threading.Thread(target=self._run, daemon=True, name="wake-gate")
        t.start()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    # State control (called from orchestrator thread)
    # ------------------------------------------------------------------

    def reset(self):
        """Clear detector state — call after mute toggle to avoid spurious fires."""
        self._awake = False
        self._detector.reset()

    def sleep(self):
        """Return to waiting-for-wake state after an utterance is processed."""
        self._awake = False
        self._awake_since = None
        self._detector.reset()
        logger.info("Wake gate sleeping — waiting for next wake word")

    def extend_timeout(self):
        """Reset the listen timeout — call after each reply so follow-up gets full window."""
        self._awake_since = time.time()

    def reopen(self):
        """Force gate open again — call after STT/LLM pipeline to allow follow-up."""
        self._awake = True
        self._awake_since = time.time()
        self._detector.reset()

    # ------------------------------------------------------------------
    # Consumer interface (called from stream_utterances)
    # ------------------------------------------------------------------

    def get_block(self, timeout: float = 1.0) -> Optional[np.ndarray]:
        """Return the next audio block for VAD, or None on timeout."""
        try:
            return self._out_q.get(timeout=timeout)
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            try:
                self._run_loop()
            except Exception:
                logger.exception("Wake gate thread crashed — restarting in 1s")
                time.sleep(1)

    def _run_loop(self):
        leftover = np.array([], dtype=np.float32)

        while not self._stop.is_set():
            try:
                block = self._raw_q.get(timeout=0.05)
            except queue.Empty:
                # While awake, check listen timeout even when no new blocks arrive.
                # Skip while suppressed — STT/LLM/TTS processing can easily run
                # longer than the timeout and shouldn't close the gate mid-turn.
                if (self._awake and self._awake_since and not self.suppressed
                        and time.time() - self._awake_since > _LISTEN_TIMEOUT):
                    logger.info("Listen timeout — no speech detected, resetting gate")
                    self.sleep()
                continue

            flat = block.flatten().astype(np.float32)

            if self._awake:
                # Gate is open — check listen timeout (skip while suppressed;
                # STT/LLM/TTS processing can run longer than the timeout and
                # shouldn't close the gate mid-turn)
                if self._awake_since is None:
                    self._awake_since = time.time()
                if not self.suppressed and time.time() - self._awake_since > _LISTEN_TIMEOUT:
                    logger.info("Listen timeout — no speech detected, resetting gate")
                    self.sleep()
                    continue
                # Pass block straight through to VAD
                self._out_q.put(flat.reshape(-1, 1))
                continue

            # Gate is closed — reset timer
            self._awake_since = None

            # Skip detection while TTS is playing to prevent speaker echo re-trigger
            if self.suppressed:
                leftover = np.array([], dtype=np.float32)
                continue

            # Scan the block in 80 ms chunks for the wake word
            audio = np.concatenate([leftover, flat]) if len(leftover) else flat
            offset = 0
            woke = False

            while offset + _CHUNK_SAMPLES <= len(audio):
                chunk = audio[offset: offset + _CHUNK_SAMPLES]
                if self._detector.detect(chunk):
                    self._awake = True
                    awake_since = time.time()
                    logger.info("Wake word detected — listening for query")
                    if self._on_wake:
                        try:
                            self._on_wake()
                        except Exception:
                            logger.exception("on_wake callback failed")
                        # Flush audio captured while greeting was playing
                        # so we don't transcribe the speaker output
                        while not self._raw_q.empty():
                            try:
                                self._raw_q.get_nowait()
                            except queue.Empty:
                                break
                        while not self._out_q.empty():
                            try:
                                self._out_q.get_nowait()
                            except queue.Empty:
                                break
                        self._awake_since = time.time()  # reset timeout after greeting
                    # Forward the audio tail AFTER the wake chunk so the
                    # query that follows is captured from the start
                    tail = audio[offset + _CHUNK_SAMPLES:]
                    if len(tail) > 0:
                        self._out_q.put(tail.reshape(-1, 1))
                    woke = True
                    leftover = np.array([], dtype=np.float32)
                    break
                offset += _CHUNK_SAMPLES

            if not woke:
                # Keep the last partial chunk for the next block
                leftover = audio[offset:]
