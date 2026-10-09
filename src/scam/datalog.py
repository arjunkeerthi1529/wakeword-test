"""Append-only JSON-lines record of what the scam monitor heard and decided.

For testing and analysis: transcripts, rule results, LLM prompts/replies and
timings. Never raw audio. Disable by setting analysis_log: "" in the config.
"""
import json
import threading
from datetime import datetime
from pathlib import Path


class DataLog:
    def __init__(self, path: str = ""):
        self._lock = threading.Lock()
        self._file = None
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._file = open(path, "a", encoding="utf-8")

    def write(self, kind: str, call_id: str = "", **fields) -> None:
        if self._file is None:
            return
        record = {"ts": datetime.now().isoformat(timespec="milliseconds"),
                  "kind": kind, "call_id": call_id, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self._file.write(line + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None
