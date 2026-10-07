import json
import logging
from typing import Callable, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a concise voice assistant running fully on a Raspberry Pi. "
    "Keep your reply to two or three short sentences. "
    "No markdown, no bullet points, no formatting — plain conversational speech only."
)


class OllamaStreamClient:
    """HTTP streaming client for Ollama /api/generate.

    Fires on_first_token() callback the instant the first non-empty token
    arrives, enabling accurate TTFT (time-to-first-token) measurement from
    the caller's LatencyTracker.

    Returns (reply_text, stats) where stats contains:
        eval_count, eval_duration,
        prompt_eval_count, prompt_eval_duration
    All durations are in nanoseconds (Ollama's native unit).
    """

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def chat(
        self,
        user_text: str,
        on_first_token: Optional[Callable[[], None]] = None,
    ) -> Tuple[str, Dict[str, int]]:
        """Send user_text to Ollama, stream the response.

        on_first_token — called exactly once when the first non-empty token
                         arrives; use to record tracker.mark("llm_first_token").
        Returns (full_reply, stats_dict).
        """
        payload = {
            "model": self.model,
            "prompt": f"{_SYSTEM_PROMPT}\n\nUser: {user_text}\nAssistant:",
            "stream": True,
            "options": {
                "temperature": 0.7,
                "num_predict": 150,
                "stop": ["\nUser:", "\n\n"],
            },
        }

        logger.debug("POST %s/api/generate model=%s", self.base_url, self.model)
        resp = requests.post(
            f"{self.base_url}/api/generate",
            json=payload,
            stream=True,
            timeout=60,
        )
        resp.raise_for_status()

        tokens = []
        first_fired = False
        stats: Dict[str, int] = {}

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            data = json.loads(raw_line)

            token_text = data.get("response", "")

            # Fire TTFT callback exactly once on the first non-empty token
            if token_text and not first_fired:
                if on_first_token is not None:
                    on_first_token()
                first_fired = True

            if token_text:
                tokens.append(token_text)

            if data.get("done"):
                stats = {
                    "eval_count":              data.get("eval_count", 0),
                    "eval_duration":           data.get("eval_duration", 0),
                    "prompt_eval_count":       data.get("prompt_eval_count", 0),
                    "prompt_eval_duration":    data.get("prompt_eval_duration", 0),
                }
                logger.info(
                    "Ollama done: %d tokens in %.2f s (%.1f tok/s)",
                    stats["eval_count"],
                    stats["eval_duration"] / 1e9,
                    stats["eval_count"] / max(stats["eval_duration"] / 1e9, 1e-9),
                )
                break

        reply = "".join(tokens).strip()
        return reply, stats
