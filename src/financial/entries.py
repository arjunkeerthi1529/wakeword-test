"""Quick-add expense parsing: free text -> one or more editable drafts.
Blocking (no job/polling layer, matching src/work's convention) -- this
never writes a transaction itself; only POST /transactions does."""
from __future__ import annotations

from typing import Any

from . import llm_client
from .db import FinanceRepository
from .money import MoneyError, TransactionType, category_required_for_type, parse_amount_to_paise
from .periods import AmbiguousDateError, resolve_date_expression, today_in
from .services import resolve_category


def parse_entry_text(
    repo: FinanceRepository, text: str, source: str, account_id: str | None,
    *, llm_base_url: str, llm_model: str, timezone: str,
) -> dict[str, Any]:
    """Splits `text` into one or more expenses via the LLM, resolves each
    deterministically (amount/date/category), and returns {"drafts": [...]}.
    A note with nothing the model could extract returns an empty list, not
    an error -- the manual entry form still works regardless."""
    today = today_in(timezone)
    result = llm_client.extract_expenses(text, llm_base_url=llm_base_url, model=llm_model, today=today)
    return {
        "drafts": [
            _build_draft(repo, f"draft-{index}", source, account_id, item, timezone)
            for index, item in enumerate(result.expenses)
        ]
    }


def _build_draft(
    repo: FinanceRepository, draft_id: str, source: str, account_id: str | None,
    extracted, timezone: str,
) -> dict[str, Any]:
    missing_fields: list[str] = []
    warnings: list[str] = []

    amount_paise: int | None = None
    if extracted.amount_text.strip():
        try:
            amount_paise = parse_amount_to_paise(extracted.amount_text)
        except MoneyError:
            missing_fields.append("amount")
    else:
        missing_fields.append("amount")

    posted_date: str | None = None
    try:
        today = today_in(timezone)
        posted_date = resolve_date_expression(extracted.date_expression, today).isoformat()
    except AmbiguousDateError:
        missing_fields.append("posted_date")

    if "amount" in extracted.ambiguous_fields and "amount" not in missing_fields:
        missing_fields.append("amount")
    if "date" in extracted.ambiguous_fields and "posted_date" not in missing_fields:
        missing_fields.append("posted_date")

    merchant = extracted.merchant.strip() or None

    try:
        txn_type = TransactionType(extracted.proposed_type)
    except ValueError:
        txn_type = TransactionType.EXPENSE

    if category_required_for_type(txn_type):
        resolution = resolve_category(
            repo, merchant, extracted.proposed_category,
            model_flagged_ambiguous=bool(extracted.ambiguous_fields),
        )
        category_id, category_origin = resolution.category_id, resolution.origin.value
        category_needs_review = resolution.needs_review
        if resolution.review_reason:
            warnings.append(resolution.review_reason)
    else:
        category_id = category_origin = None
        category_needs_review = False

    needs_review = category_needs_review or bool(missing_fields)
    status = "needs_input" if missing_fields else "ready"

    return {
        "draft_id": draft_id, "status": status, "source": source, "merchant": merchant,
        "amount_paise": amount_paise, "currency": extracted.currency or "INR",
        "posted_date": posted_date, "type": txn_type.value, "category_id": category_id,
        "category_origin": category_origin, "account_id": account_id, "needs_review": needs_review,
        "missing_fields": missing_fields, "warnings": warnings,
    }
