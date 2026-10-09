"""Per-call state: committed transcript segments, review progress, and alerts."""
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_LEVEL_RANK = {"none": 0, "watch": 1, "warn": 2}
_WORD = re.compile(r"[a-z0-9']+")
_MAX_OVERLAP_WORDS = 5
_LABEL_TEXT = {
    "secret": "a request for a code, PIN or password",
    "remote": "a request to install remote-access software",
    "payment": "an urgent payment request",
    "qr": "a QR-code or payment-approval request",
    "giftcard": "a gift-card payment request",
    "pressure": "pressure or impersonation",
    "llm": "suspicious content",
}


def rank(level: str) -> int:
    return _LEVEL_RANK.get(level, 0)


def _words(text: str) -> List[str]:
    return _WORD.findall(text.lower())


def trim_overlap(prev_text: str, text: str) -> str:
    """Drop leading words of `text` that repeat the tail of `prev_text` (audio
    overlap between chunks makes the same words appear twice)."""
    prev_words = _words(prev_text)
    words = text.split()
    norm = [(_words(w) or [""])[0] for w in words]
    max_n = min(_MAX_OVERLAP_WORDS, len(prev_words), len(norm))
    for n in range(max_n, 0, -1):
        if prev_words[-n:] == norm[:n]:
            return " ".join(words[n:]).strip()
    return text.strip()


@dataclass
class Segment:
    id: str
    start_ms: int
    end_ms: int
    text: str


@dataclass
class Alert:
    segment_id: str
    level: str
    label: str
    evidence: str
    message: str
    source: str
    speaker: str = "unknown"
    reason: str = ""
    advice: str = ""
    revision: int = 1


@dataclass
class Session:
    call_id: str = field(default_factory=lambda: "c" + uuid.uuid4().hex[:6])
    started_at: float = field(default_factory=time.monotonic)
    segments: List[Segment] = field(default_factory=list)
    reviewed_upto: int = 0                      # index into segments; [0, reviewed_upto) are reviewed
    llm_busy: bool = False
    last_dispatch: float = 0.0
    alerts: Dict[str, Alert] = field(default_factory=dict)
    next_segment_no: int = 1

    def add_segment(self, text: str, start_ms: int, end_ms: int) -> Optional[Segment]:
        """Commit stable text. Strips words repeated from the previous segment's
        tail (chunk overlap) and drops exact duplicates."""
        text = text.strip()
        if not text:
            return None
        if self.segments:
            prev_words = _words(self.segments[-1].text)
            text = trim_overlap(self.segments[-1].text, text)
            if not text or _words(text) == prev_words:
                return None
        seg = Segment(f"s{self.next_segment_no}", start_ms, end_ms, text)
        self.next_segment_no += 1
        self.segments.append(seg)
        return seg

    def previous_text(self) -> str:
        return self.segments[-2].text if len(self.segments) >= 2 else ""

    def unreviewed(self) -> List[Segment]:
        return self.segments[self.reviewed_upto:]

    def max_level(self) -> str:
        best = "none"
        for a in self.alerts.values():
            if rank(a.level) > rank(best):
                best = a.level
        return best

    def older_summary(self, recent_ids: set, limit: int = 3) -> str:
        """Brief evidence-based summary of concerns from segments that fell out
        of the recent-context window."""
        parts = []
        for a in self.alerts.values():
            if a.segment_id in recent_ids:
                continue
            parts.append(f"{_LABEL_TEXT.get(a.label, 'a concern')} ({a.segment_id})")
        return "; ".join(parts[-limit:])
