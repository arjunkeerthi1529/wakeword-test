import logging
import os
import re
import subprocess
import tempfile
import wave

import numpy as np

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
# Strips "[00:00:00.000 --> 00:00:05.000]  " prefixes if -nt flag is ignored
_TIMESTAMP_RE = re.compile(r"^\[[\d:\.]+\s*-->\s*[\d:\.]+\]\s*")


class WhisperEngine:
    """Local STT via whisper.cpp subprocess.

    Audio is written to a temp WAV, whisper-cli is invoked, and the temp
    file is deleted immediately. No model loaded in-process — the binary
    handles all memory, so it does not compete with the LLM's RAM budget.
    binary  — full path to whisper-cli (or whisper-cli.exe on Windows)
    model   — full path to ggml-*.bin model file
    """

    def __init__(self, binary: str, model: str, n_threads: int = 4):
        self.binary = binary
        self.model = model
        self.n_threads = n_threads

    def transcribe(self, audio: np.ndarray) -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            _write_wav(tmp.name, audio)
            result = subprocess.run(
                [
                    self.binary,
                    "-m", self.model,
                    "-f", tmp.name,
                    "-l", "en",
                    "-t", str(self.n_threads),
                    "--beam-size", "1",
                    "--no-fallback",
                    "-nt",  # no timestamps
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning("whisper-cli exited %d: %s", result.returncode, result.stderr[:300])
            return _parse(result.stdout)
        finally:
            os.unlink(tmp.name)


def _write_wav(path: str, audio: np.ndarray) -> None:
    pcm = (np.clip(audio.flatten(), -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


def _parse(stdout: str) -> str:
    lines = []
    for line in stdout.splitlines():
        line = _TIMESTAMP_RE.sub("", line).strip()
        # Skip whisper.cpp internal log lines that occasionally leak to stdout
        if line and not line.startswith(("whisper_", "system_info", "main:")):
            lines.append(line)
    return " ".join(lines).strip()
