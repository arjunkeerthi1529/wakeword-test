"""Direct, synchronous calls to the local model server -- no shared scheduler,
matching this repo's src/work convention (see email_agent.py) rather than the
reference project's single-generation queue. Each call blocks the request
thread until the model responds; FastAPI runs these endpoints in its default
threadpool, so other endpoints (e.g. GET /health) stay responsive meanwhile.

Every function here fails *safely*: a timeout, an unreachable server, or
unparseable JSON all fall back to an honest empty/low-signal result, never a
fabricated guess. Backend re-validation (Pydantic) always re-checks the
model's output against the schema regardless of how well the server itself
constrained decoding.
"""
from __future__ import annotations

import json
import logging
from datetime import date

import requests

from .llm_tasks import (
    CATEGORIZE_BATCH_SCHEMA,
    EXTRACT_EXPENSES_SCHEMA,
    INTERPRET_QUESTION_SCHEMA,
    CategorizeBatchItem,
    CategorizeBatchResult,
    ExtractExpensesResult,
    InterpretQuestionResult,
    build_categorize_batch_messages,
    build_extract_expenses_messages,
    build_interpret_question_messages,
)

logger = logging.getLogger(__name__)


def _chat_json(
    llm_base_url: str, model: str, messages: list[dict], schema: dict, task_name: str,
    max_tokens: int, timeout: float,
) -> dict:
    """One call to the OpenAI-compatible chat-completions endpoint (both
    Ollama and llama.cpp serve this), with the reply constrained to `schema`
    via strict json_schema mode -- confirmed necessary, not just belt-and-
    braces: a loose 'json_object' response_format let a 3B model substitute
    its own field names (e.g. "category" for "category_id", "rows" for
    "results") despite the prompt spelling out the schema in words, silently
    defeating the whole task. Raises on any failure -- callers decide the
    safe fallback for their own task."""
    resp = requests.post(
        f"{llm_base_url.rstrip('/')}/v1/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": task_name, "schema": schema, "strict": True},
            },
            # Harmless no-op for a model/runtime without a "thinking" mode;
            # prevents a Qwen3-style model from burning its whole context on
            # an invisible reasoning preamble before ever emitting JSON.
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return json.loads(text)


def extract_expenses(
    text: str, *, llm_base_url: str, model: str, today: date, timeout: float = 60.0
) -> ExtractExpensesResult:
    """Splits a quick-add note into one or more structured expense drafts.
    Falls back to an empty list (nothing to save) if the model is unavailable
    or returns something unparseable -- never a fabricated expense."""
    messages = build_extract_expenses_messages(text, today)
    try:
        raw = _chat_json(llm_base_url, model, messages, EXTRACT_EXPENSES_SCHEMA, "extract_expenses", max_tokens=640, timeout=timeout)
        return ExtractExpensesResult.model_validate(raw)
    except Exception as exc:
        logger.warning("extract_expenses failed: %s", exc)
        return ExtractExpensesResult(expenses=[])


def categorize_batch(
    rows: list[dict], *, llm_base_url: str, model: str, timeout: float = 60.0
) -> CategorizeBatchResult:
    """Categorizes up to 8 previously-unseen merchant descriptions at once.
    Falls back to an empty result set on any failure -- the caller treats a
    missing row_id as 'other'+needs_review, never guesses a category."""
    messages = build_categorize_batch_messages(rows)
    try:
        raw = _chat_json(llm_base_url, model, messages, CATEGORIZE_BATCH_SCHEMA, "categorize_batch", max_tokens=512, timeout=timeout)
        return CategorizeBatchResult.model_validate(raw)
    except Exception as exc:
        logger.warning("categorize_batch failed: %s", exc)
        return CategorizeBatchResult(results=[])


def interpret_question(
    text: str, *, llm_base_url: str, model: str, today: date, timeout: float = 60.0
) -> InterpretQuestionResult:
    """Turns a free-text question into a structured intent. Falls back to
    operation='unsupported' with a clarification on any failure -- the
    service layer then gives an honest 'couldn't understand that' answer
    rather than guessing at numbers."""
    messages = build_interpret_question_messages(text, today)
    try:
        raw = _chat_json(llm_base_url, model, messages, INTERPRET_QUESTION_SCHEMA, "interpret_question", max_tokens=384, timeout=timeout)
        return InterpretQuestionResult.model_validate(raw)
    except Exception as exc:
        logger.warning("interpret_question failed: %s", exc)
        return InterpretQuestionResult(
            operation="unsupported", needs_clarification=True,
            clarification="The local model is unavailable right now -- try again shortly.",
        )
