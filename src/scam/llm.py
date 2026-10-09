"""Stateless, format-enforced verdict request to the local llama-server.

The reply must be exactly "none" or "<warn|watch> s<N> <caller|user|unknown>".
That is enforced by the server (a GBNF grammar) and then verified here; if the
server turns out not to apply the grammar, the client switches to a JSON-schema
constraint, which every llama-server build honours.
"""
import logging
import re
from typing import Dict, Tuple

import requests

logger = logging.getLogger(__name__)

# ~5 tokens of output instead of ~25 for JSON; generation is only a few tokens/s on a Pi.
_GRAMMAR = r'''root ::= "none" | level " s" [0-9]+ " " who
level ::= "warn" | "watch"
who ::= "caller" | "user" | "unknown"'''

_SCHEMA = {
    "type": "object",
    "properties": {
        "risk": {"enum": ["none", "watch", "warn"]},
        "evidence_id": {"type": "string"},
        "speaker": {"enum": ["user", "caller", "unknown"]},
    },
    "required": ["risk", "evidence_id", "speaker"],
    "additionalProperties": False,
}

_REPLY = re.compile(r"^(?:none|(?:warn|watch) s\d+ (?:caller|user|unknown))$")
_TIMING_KEYS = ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms")


class ScamLLM:
    def __init__(self, base_url: str, model: str = "local"):
        self.base_url = base_url.rstrip("/")
        self.model = model                    # llama-server ignores it; Ollama needs a real name
        self.mode = "grammar"                 # "grammar" (fast) or "schema" (fallback)
        self._http = requests.Session()       # keep-alive: no new connection per review

    def _payload(self, system: str, user: str, max_tokens: int) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},   # static prefix -> server reuses its KV cache
                {"role": "user", "content": user},
            ],
            "stream": False,
            "temperature": 0.0,
            "top_k": 1,
            "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if self.mode == "grammar":
            payload["grammar"] = _GRAMMAR
            payload["max_tokens"] = max_tokens
        else:
            payload["response_format"] = {
                "type": "json_schema", "json_schema": {"name": "verdict", "schema": _SCHEMA},
            }
            payload["max_tokens"] = max(max_tokens, 40)   # JSON needs more tokens than the one-liner
        return payload

    def _post(self, payload: dict, timeout: float) -> requests.Response:
        return self._http.post(f"{self.base_url}/v1/chat/completions", json=payload, timeout=timeout)

    def review(self, system: str, user: str, max_tokens: int = 12,
               timeout: float = 90.0) -> Tuple[str, Dict]:
        """Returns (reply_text, info). info has the server's prompt/decode timings
        plus "mode" so slow or unconstrained replies can be diagnosed from the log."""
        resp = self._post(self._payload(system, user, max_tokens), timeout)
        if self.mode == "grammar" and resp.status_code in (400, 422):
            logger.warning("llama-server rejected the grammar (HTTP %d) — switching to JSON-schema mode",
                           resp.status_code)
            self.mode = "schema"
            resp = self._post(self._payload(system, user, max_tokens), timeout)
        resp.raise_for_status()
        text, info = self._parse(resp)

        if self.mode == "grammar" and not _REPLY.match(text.strip()):
            logger.warning("Reply %r does not match the enforced format — the server ignored the "
                           "grammar; switching to JSON-schema mode", text[:60])
            self.mode = "schema"
            resp = self._post(self._payload(system, user, max_tokens), timeout)
            resp.raise_for_status()
            text, info = self._parse(resp)
            info["retried"] = True

        info["mode"] = self.mode
        return text, info

    @staticmethod
    def _parse(resp: requests.Response) -> Tuple[str, Dict]:
        data = resp.json()
        text = data["choices"][0]["message"].get("content", "") or ""
        timings = data.get("timings") or {}
        return text, {k: timings[k] for k in _TIMING_KEYS if k in timings}
