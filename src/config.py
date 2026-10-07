import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]

_config: Optional["Config"] = None


@dataclass
class Config:
    stt_binary:          str
    stt_model:           str
    stt_threads:         int
    ollama_base_url:     str
    ollama_model:        str
    piper_voice:         str
    wake_model:          str
    wake_threshold:      float
    wake_backend:        str
    gpio_mute_pin:       int
    gpio_listen_led_pin: int
    gpio_online_led_pin: int
    log_latency_csv:     bool


def get_config() -> Config:
    global _config
    if _config is not None:
        return _config

    yaml_path = ROOT_DIR / "config" / "raspberrypi.yaml"
    with open(yaml_path) as f:
        raw = yaml.safe_load(f)

    wake_cfg = raw.get("wake_word", {})
    gpio_cfg = raw.get("gpio", {})

    def _resolve(raw_path: str) -> str:
        p = Path(raw_path)
        return str(p) if p.is_absolute() else str(ROOT_DIR / p)

    _config = Config(
        stt_binary=os.getenv("STT_BINARY", raw["stt_binary"]),
        stt_model=_resolve(os.getenv("STT_MODEL", raw["stt_model"])),
        stt_threads=int(os.getenv("STT_THREADS", raw.get("stt_threads", 3))),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL",
                                  raw.get("ollama_base_url", "http://localhost:11434")),
        ollama_model=os.getenv("OLLAMA_MODEL", raw.get("ollama_model", "llama3.2:1b")),
        piper_voice=_resolve(os.getenv("PIPER_VOICE", raw["piper_voice"])),
        wake_model=os.getenv("WAKE_MODEL", wake_cfg.get("model", "hey_jarvis")),
        wake_threshold=float(os.getenv("WAKE_THRESHOLD",
                                       wake_cfg.get("threshold", 0.5))),
        wake_backend=os.getenv("WAKE_BACKEND", wake_cfg.get("backend", "tflite")),
        gpio_mute_pin=int(gpio_cfg.get("mute_button_pin", 17)),
        gpio_listen_led_pin=int(gpio_cfg.get("listening_led_pin", 27)),
        gpio_online_led_pin=int(gpio_cfg.get("online_led_pin", 22)),
        log_latency_csv=raw.get("log_latency_csv", True),
    )
    return _config
