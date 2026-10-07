import csv
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class LatencyTracker:
    """Records monotonic timestamps at named pipeline stages and prints a
    formatted latency table at end-of-turn.

    Pipeline stages (mark in this order each turn):
        wake_fire         — WakeGate on_wake callback fires
        speech_end        — VAD silence timeout, utterance yielded
        stt_done          — whisper.cpp subprocess returns transcription
        llm_first_token   — first non-empty streaming token from Ollama
        llm_done          — Ollama stream done=true
        tts_synth_done    — Piper finishes synthesizing bytes
        tts_play_done     — sounddevice.wait() returns after playback

    Usage:
        tracker = LatencyTracker()
        tracker.mark("wake_fire")
        ...
        tracker.mark("tts_play_done")
        tracker.report(eval_count=N, eval_duration_ns=D, log_csv=Path("latency_log.csv"))
        tracker.reset()
    """

    _marks: Dict[str, float] = field(default_factory=dict)

    def mark(self, stage: str) -> float:
        """Record time.monotonic() for stage. Returns the recorded timestamp."""
        t = time.monotonic()
        self._marks[stage] = t
        return t

    def elapsed(self, start_stage: str, end_stage: str) -> Optional[float]:
        """Seconds between two marks, or None if either mark is missing."""
        s = self._marks.get(start_stage)
        e = self._marks.get(end_stage)
        return (e - s) if (s is not None and e is not None) else None

    def report(
        self,
        ollama_eval_count: int = 0,
        ollama_eval_duration_ns: int = 0,
        ollama_prompt_eval_count: int = 0,
        ollama_prompt_eval_duration_ns: int = 0,
        log_csv: Optional[Path] = None,
    ) -> None:
        stt_lat   = self.elapsed("speech_end",     "stt_done")
        ttft      = self.elapsed("stt_done",        "llm_first_token")
        llm_total = self.elapsed("stt_done",        "llm_done")
        tts_synth = self.elapsed("llm_done",        "tts_synth_done")
        tts_play  = self.elapsed("tts_synth_done",  "tts_play_done")
        e2e       = self.elapsed("wake_fire",       "tts_play_done")

        tps = 0.0
        if ollama_eval_duration_ns > 0 and ollama_eval_count > 0:
            tps = ollama_eval_count / (ollama_eval_duration_ns / 1_000_000_000)

        def _fmt(v: Optional[float]) -> str:
            return f"{v:>6.2f} s" if v is not None else "   N/A  "

        SEP = "─" * 50
        print(f"\n{SEP}")
        print("  LATENCY REPORT")
        print(SEP)
        print(f"  STT               {_fmt(stt_lat)}")
        print(f"  LLM TTFT          {_fmt(ttft)}")
        print(f"  LLM total         {_fmt(llm_total)}")
        print(f"  TTS synthesize    {_fmt(tts_synth)}")
        print(f"  TTS play          {_fmt(tts_play)}")
        print(f"  End-to-end        {_fmt(e2e)}")
        if tps > 0:
            print(f"  LLM speed        {tps:>5.1f} tok/s  ({ollama_eval_count} tok generated)")
        if ollama_prompt_eval_count > 0 and ollama_prompt_eval_duration_ns > 0:
            prompt_tps = ollama_prompt_eval_count / (ollama_prompt_eval_duration_ns / 1_000_000_000)
            print(f"  Prompt eval      {prompt_tps:>5.1f} tok/s  ({ollama_prompt_eval_count} prompt tok)")
        print(SEP + "\n")

        if log_csv is not None:
            _append_csv(log_csv, {
                "wall_ts":      time.time(),
                "stt_s":        stt_lat,
                "llm_ttft_s":   ttft,
                "llm_total_s":  llm_total,
                "tts_synth_s":  tts_synth,
                "tts_play_s":   tts_play,
                "e2e_s":        e2e,
                "tok_per_s":    round(tps, 2),
                "tok_count":    ollama_eval_count,
                "prompt_tok":   ollama_prompt_eval_count,
            })

    def reset(self) -> None:
        self._marks.clear()


def _append_csv(path: Path, row: dict) -> None:
    write_header = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)
