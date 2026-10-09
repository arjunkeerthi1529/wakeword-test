import json
import logging
import time
from typing import Callable, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a concise voice assistant. "
    "Keep your reply to two or three short sentences. "
    "No markdown, no bullet points, no formatting — plain conversational speech only."
)


class LlamaCppClient:
    """HTTP streaming client for llama.cpp server /v1/chat/completions endpoint.

    Uses the OpenAI-compatible endpoint so llama.cpp automatically applies
    the model's chat template (required for Qwen, Llama-3, etc.).

    llama.cpp server must be running:
        ~/llm/llama.cpp/build/bin/llama-server \\
            -m ~/models/Qwen3.5-0.8B-Q4_0.gguf \\
            --host 0.0.0.0 --port 8080 -c 2048 -t 4 --parallel 1

    Fires on_first_token() the instant the first non-empty token arrives,
    enabling accurate TTFT measurement from the caller's LatencyTracker.

    Returns (reply_text, stats) where stats contains:
        tok_per_s    — generated tokens/s (computed from wall clock)
        tok_count    — completion tokens (from usage field)
        prompt_tok   — prompt tokens (from usage field)
        prompt_per_s — prompt tokens/s (computed from wall clock)
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._history: list = []   # conversation turns: [{role, content}, ...]

    def reset_history(self) -> None:
        """Clear conversation history — call when starting a new conversation."""
        self._history = []
        logger.info("Conversation history cleared")

    def chat(
        self,
        user_text: str,
        on_first_token: Optional[Callable[[], None]] = None,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> Tuple[str, Dict]:
        # Append user turn to history
        self._history.append({"role": "user", "content": user_text})

        payload = {
            "model": "local",           # llama.cpp ignores this, uses loaded model
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                *self._history,         # full conversation history
            ],
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 200,
            "stop": ["\nUser:", "<|im_end|>"],
            # Disable Qwen3 chain-of-thought thinking mode via llama.cpp's
            # Jinja template parameter — prevents all tokens going to reasoning_content
            "chat_template_kwargs": {"enable_thinking": False},
        }

        logger.debug("POST %s/v1/chat/completions", self.base_url)
        resp = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            stream=True,
            timeout=60,
        )
        resp.raise_for_status()

        tokens = []
        first_fired = False
        t_first: Optional[float] = None
        t_start = time.monotonic()
        usage: Dict = {}

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line

            # SSE format: "data: {...}" or "data: [DONE]"
            if not line.startswith("data:"):
                continue
            line = line[len("data:"):].strip()
            if line == "[DONE]":
                break

            data = json.loads(line)

            # Extract token text from delta
            # Qwen3 puts reasoning in reasoning_content and reply in content
            choices = data.get("choices", [])
            token_text = ""
            finish_reason = None
            if choices:
                delta = choices[0].get("delta", {})
                token_text = delta.get("content", "") or ""
                finish_reason = choices[0].get("finish_reason")

            # Fire TTFT callback exactly once on first non-empty token
            if token_text and not first_fired:
                t_first = time.monotonic()
                if on_first_token is not None:
                    on_first_token()
                first_fired = True

            if token_text:
                tokens.append(token_text)
                if on_token is not None:
                    on_token(token_text)

            # usage is in the final chunk (finish_reason == "stop")
            if finish_reason == "stop":
                usage = data.get("usage", {})
                break

        t_end = time.monotonic()
        tok_count = usage.get("completion_tokens", len(tokens))
        prompt_tok = usage.get("prompt_tokens", 0)

        # Compute tok/s from wall clock (llama.cpp doesn't return timings
        # on the /v1/chat/completions endpoint)
        gen_elapsed = t_end - (t_first or t_start)
        tok_per_s = tok_count / gen_elapsed if gen_elapsed > 0 else 0.0

        prompt_elapsed = (t_first or t_end) - t_start
        prompt_per_s = prompt_tok / prompt_elapsed if prompt_elapsed > 0 else 0.0

        stats = {
            "tok_per_s":    tok_per_s,
            "tok_count":    tok_count,
            "prompt_tok":   prompt_tok,
            "prompt_per_s": prompt_per_s,
        }
        logger.info(
            "llama.cpp done: %d tokens @ %.1f tok/s  (prompt %d tok @ %.1f tok/s)",
            tok_count, tok_per_s, prompt_tok, prompt_per_s,
        )

        reply = "".join(tokens).strip()

        # Save assistant reply to history for context in follow-up turns
        if reply:
            self._history.append({"role": "assistant", "content": reply})

        # Trim history to last 10 turns to stay within context window
        if len(self._history) > 20:
            self._history = self._history[-20:]

        return reply, stats
