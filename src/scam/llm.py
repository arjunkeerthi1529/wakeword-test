"""Stateless JSON-schema request to the local llama-server."""
import requests


class ScamLLM:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def complete_json(self, system: str, user: str, schema: dict,
                      max_tokens: int = 120, timeout: float = 60.0) -> str:
        payload = {
            "model": "local",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "cache_prompt": True,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema},
            },
            "chat_template_kwargs": {"enable_thinking": False},
        }
        resp = requests.post(f"{self.base_url}/v1/chat/completions", json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"].get("content", "") or ""
