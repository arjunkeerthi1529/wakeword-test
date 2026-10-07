import json
import logging
from typing import Callable, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a concise voice assistant. "
    "Keep your reply to two or three short sentences. "
    "No markdown, no bullet points, no formatting — plain conversational speech only."
)


class LlamaCppClient:
    """HTTP streaming client for llama.cpp server /completion endpoint.

    llama.cpp server must be running:
        ./llama-server -m <model.gguf> --port 8080

    Fires on_first_token() the instant the first non-empty token arrives,
    enabling accurate TTFT measurement from the caller's LatencyTracker.

    Returns (reply_text, stats) where stats contains:
        tok_per_s       — generated tokens per second (from llama.cpp timings)
        tok_count       — number of tokens generated
        prompt_tok      — number of prompt tokens processed
        prompt_per_s    — prompt evaluation tokens per second
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def chat(
        self,
        user_text: str,
        on_first_token: Optional[Callable[[], None]] = None,
    ) -> Tuple[str, Dict]:
        """Send user_text to llama.cpp server, stream the response.

        on_first_token — called exactly once when the first non-empty token
                         arrives; use to record tracker.mark("llm_first_token").
        Returns (full_reply, stats_dict).
        """
        prompt = f"{_SYSTEM_PROMPT}\n\nUser: {user_text}\nAssistant:"

        payload = {
            "prompt": prompt,
            "stream": True,
            "temperature": 0.7,
            "n_predict": 150,
            "stop": ["\nUser:", "\n\n"],
        }

        logger.debug("POST %s/completion", self.base_url)
        resp = requests.post(
            f"{self.base_url}/completion",
            json=payload,
            stream=True,
            timeout=60,
        )
        resp.raise_for_status()

        tokens = []
        first_fired = False
        stats: Dict = {}

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            # llama.cpp streams Server-Sent Events: "data: {...}"
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if line.startswith("data:"):
                line = line[len("data:"):].strip()
            if not line:
                continue

            data = json.loads(line)
            token_text = data.get("content", "")

            # Fire TTFT callback exactly once on the first non-empty token
            if token_text and not first_fired:
                if on_first_token is not None:
                    on_first_token()
                first_fired = True

            if token_text:
                tokens.append(token_text)

            if data.get("stop"):
                timings = data.get("timings", {})
                tok_per_s = timings.get("predicted_per_second", 0.0)
                tok_count = timings.get("predicted_n", len(tokens))
                prompt_per_s = timings.get("prompt_per_second", 0.0)
                prompt_tok = timings.get("prompt_n", 0)
                stats = {
                    "tok_per_s":    tok_per_s,
                    "tok_count":    tok_count,
                    "prompt_per_s": prompt_per_s,
                    "prompt_tok":   prompt_tok,
                }
                logger.info(
                    "llama.cpp done: %d tokens @ %.1f tok/s",
                    tok_count, tok_per_s,
                )
                break

        reply = "".join(tokens).strip()
        return reply, stats
