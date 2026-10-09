"""Config for the standalone scam-guard service (config/scam_guard.yaml)."""
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parents[2]


@dataclass
class SpamGuardConfig:
    host:            str   = "0.0.0.0"
    port:            int   = 8000
    stt_model:       str   = "base.en"
    stt_threads:     int   = 4           # 0 = library default
    stt_beam_size:   int   = 1
    llm_base_url:    str   = "http://localhost:8080"
    led_pin:         int   = 22          # 0 disables the LED
    mic_device:      str   = ""          # "" = system default input
    analysis_log:    str   = "logs/scam_events.jsonl"    # "" disables; transcripts only, never audio
    chunk_target_s:  float = 5.0
    chunk_max_s:     float = 10.0
    overlap_s:       float = 0.3
    silence_thresh:  float = 0.012
    pause_s:         float = 0.6
    llm_cadence_s:   float = 10.0
    llm_max_tokens:  int   = 40
    llm_timeout_s:   float = 90.0
    max_session_s:   float = 1800.0


def load_config(path: Path = None) -> SpamGuardConfig:
    path = path or ROOT_DIR / "config" / "scam_guard.yaml"
    raw = {}
    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    defaults = SpamGuardConfig()
    kwargs = {}
    for name, default in vars(defaults).items():
        if name in raw:
            kwargs[name] = type(default)(raw[name])
    if os.getenv("LLM_BASE_URL"):
        kwargs["llm_base_url"] = os.environ["LLM_BASE_URL"]
    return SpamGuardConfig(**kwargs)
