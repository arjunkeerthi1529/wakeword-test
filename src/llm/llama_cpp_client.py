import json
import logging
import time
from typing import Callable, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a concise voice assistant called Jarvis. "
    "Keep replies to two or three short spoken sentences. "
    "No markdown, no bullet points — plain conversational speech only.\n\n"
    "You have tools: use log_expense to record purchases, set_reminder for reminders. "
    "After a tool succeeds, confirm naturally in your spoken reply.\n\n"
    "REMINDERS (fallback): If tool calling is unavailable you may also emit a "
    "machine-readable tag on a new line at the end:\n"
    "  [REMINDER delay=<n><s|m|h> message=<text>]\n"
    "  [REMINDER at=HH:MM message=<text>]\n"
    "Prefer tool calling when available."
)


class LlamaCppClient:
    """HTTP streaming client for llama.cpp with agent-loop tool calling.

    Flow:
      1. Send user message (+ tools if provided)
      2. If LLM returns tool_calls → execute via tool_executor, append results,
         re-call LLM (up to MAX_ROUNDS)
      3. When LLM returns text only → stream tokens via on_token callback → done
    """

    MAX_ROUNDS = 3

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._history: list = []

    def reset_history(self) -> None:
        self._history = []
        logger.info("Conversation history cleared")

    def chat(
        self,
        user_text: str,
        on_first_token: Optional[Callable[[], None]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        tools: Optional[List[dict]] = None,
        tool_executor: Optional[Callable[[str, dict], str]] = None,
    ) -> Tuple[str, Dict]:
        self._history.append({"role": "user", "content": user_text})

        all_stats: Dict = {}
        first_token_fired = False

        for _round in range(self.MAX_ROUNDS):
            payload = {
                "model": "local",
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    *self._history,
                ],
                "stream": True,
                "temperature": 0.7,
                "max_tokens": 200,
                "stop": ["\nUser:", "<|im_end|>"],
                "chat_template_kwargs": {"enable_thinking": False},
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            tokens, tool_calls, usage, t_first, t_start, t_end = self._stream_once(
                payload,
                on_first_token=on_first_token if not first_token_fired else None,
                on_token=on_token,
            )

            if t_first is not None:
                first_token_fired = True

            tok_count = usage.get("completion_tokens", len(tokens))
            prompt_tok = usage.get("prompt_tokens", 0)
            gen_elapsed = t_end - (t_first or t_start)
            prompt_elapsed = (t_first or t_end) - t_start
            all_stats = {
                "tok_per_s": tok_count / gen_elapsed if gen_elapsed > 0 else 0.0,
                "tok_count": tok_count,
                "prompt_tok": prompt_tok,
                "prompt_per_s": prompt_tok / prompt_elapsed if prompt_elapsed > 0 else 0.0,
            }

            if not tool_calls:
                reply = "".join(tokens).strip()
                if reply:
                    self._history.append({"role": "assistant", "content": reply})
                if len(self._history) > 20:
                    self._history = self._history[-20:]

                logger.info(
                    "llama.cpp done (round %d): %d tok @ %.1f tok/s",
                    _round + 1, tok_count, all_stats["tok_per_s"],
                )
                all_stats["tool_calls"] = []
                return reply, all_stats

            # --- tool calls: execute and loop ---
            assistant_msg: dict = {"role": "assistant", "content": None, "tool_calls": []}
            executed_calls = []
            for tc in tool_calls:
                tc_entry = {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                }
                assistant_msg["tool_calls"].append(tc_entry)
                executed_calls.append(tc)

            self._history.append(assistant_msg)

            for tc in executed_calls:
                result = "{}"
                if tool_executor:
                    try:
                        result = tool_executor(tc["name"], tc["arguments"])
                    except Exception as exc:
                        logger.warning("Tool executor error for %s: %s", tc["name"], exc)
                        result = json.dumps({"status": "error", "detail": str(exc)})
                self._history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

            logger.info(
                "Round %d: %d tool call(s) executed, looping for verbal reply",
                _round + 1, len(executed_calls),
            )
            all_stats["tool_calls"] = executed_calls

        reply = "".join(tokens).strip()
        if reply:
            self._history.append({"role": "assistant", "content": reply})
        if len(self._history) > 20:
            self._history = self._history[-20:]
        return reply, all_stats

    def _stream_once(
        self,
        payload: dict,
        on_first_token: Optional[Callable[[], None]],
        on_token: Optional[Callable[[str], None]],
    ) -> Tuple[list, list, dict, Optional[float], float, float]:
        """Single streaming request. Returns (tokens, tool_calls, usage, t_first, t_start, t_end)."""
        logger.debug("POST %s/v1/chat/completions", self.base_url)
        resp = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            stream=True,
            timeout=60,
        )
        resp.raise_for_status()

        tokens: list[str] = []
        tool_call_chunks: dict[int, dict] = {}
        first_fired = False
        t_first: Optional[float] = None
        t_start = time.monotonic()
        usage: Dict = {}

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line

            if not line.startswith("data:"):
                continue
            line = line[len("data:"):].strip()
            if line == "[DONE]":
                break

            data = json.loads(line)
            choices = data.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta", {})
            finish_reason = choices[0].get("finish_reason")

            # --- text content ---
            token_text = delta.get("content", "") or ""
            if token_text and not first_fired:
                t_first = time.monotonic()
                if on_first_token is not None:
                    on_first_token()
                first_fired = True

            if token_text:
                tokens.append(token_text)
                if on_token is not None and not tool_call_chunks:
                    on_token(token_text)

            # --- tool call fragments ---
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in tool_call_chunks:
                    tool_call_chunks[idx] = {"id": tc.get("id", f"call_{idx}"), "name": "", "args_buf": ""}
                fn = tc.get("function", {})
                if fn.get("name"):
                    tool_call_chunks[idx]["name"] = fn["name"]
                tool_call_chunks[idx]["args_buf"] += fn.get("arguments", "") or ""
                if tc.get("id"):
                    tool_call_chunks[idx]["id"] = tc["id"]

            if finish_reason == "stop" or finish_reason == "tool_calls":
                usage = data.get("usage", {})
                break

        t_end = time.monotonic()

        parsed_calls = []
        for chunk in tool_call_chunks.values():
            try:
                args = json.loads(chunk["args_buf"]) if chunk["args_buf"] else {}
            except json.JSONDecodeError:
                logger.warning("Bad tool call JSON: %s", chunk["args_buf"])
                continue
            parsed_calls.append({
                "id": chunk["id"],
                "name": chunk["name"],
                "arguments": args,
            })

        return tokens, parsed_calls, usage, t_first, t_start, t_end
