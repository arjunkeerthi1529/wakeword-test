"""Microphone input for the scam-guard service.

The mic is opened only while a call is being monitored and closed on Stop, so
this service never holds the device while idle.
"""
import logging
import queue
from typing import Optional

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BLOCK_SIZE = 1280      # 80 ms


class MicSource:
    def __init__(self, device: str = ""):
        self.device = device or None
        self._stream = None

    def start(self) -> "queue.Queue":
        import sounddevice as sd

        q: "queue.Queue" = queue.Queue()

        def callback(indata, frames, time_info, status):
            q.put(indata.copy())

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, blocksize=BLOCK_SIZE,
                device=self.device, callback=callback,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise RuntimeError(
                f"Could not open the microphone ({exc}). If the voice assistant is "
                "running, both programs must share the mic through an ALSA dsnoop "
                "device (set it as the default input in ~/.asoundrc)."
            ) from exc
        logger.info("Microphone opened")
        return q

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                logger.exception("Error closing microphone")
            logger.info("Microphone closed")
