"""Stateless single-line verdict request to the local llama-server."""
import logging
from typing import Dict, Tuple

import requests

logger = logging.getLogger(__name__)

# Constrains the reply to e.g. "none" or "warn s4 caller": ~5 tokens instead of
# the ~25 a JSON object needs, and generation is only a few tokens/s on a Pi.
_GRAMMAR = r'''root ::= "none" | level " s" [0-9]+ " " who
level ::= "warn" | "watch"
who ::= "caller" | "user" | "unknown"'''


class ScamLLM:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def review(self, system: str, user: str, max_tokens: int = 12,
               timeout: float = 90.0) -> Tuple[str, Dict]:
        """Returns (reply_text, server_timings). Timings split prompt processing
        from generation so slow reviews can be diagnosed."""
        payload = {
            "model": "local",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "cache_prompt": True,
            "grammar": _GRAMMAR,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        url = f"{self.base_url}/v1/chat/completions"
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code in (400, 422):
            logger.warning("llama-server rejected the grammar (HTTP %d) — retrying without it",
                           resp.status_code)
            payload.pop("grammar")
            resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"].get("content", "") or ""
        raw_timings = data.get("timings") or {}
        timings = {k: raw_timings[k] for k in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms")
                   if k in raw_timings}
        return text, timings
