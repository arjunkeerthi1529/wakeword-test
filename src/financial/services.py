"""Account, transaction, category-resolution and analytics business logic.
Plain synchronous functions/classes over FinanceRepository -- no asyncio
anywhere in this service, since FastAPI runs plain `def` endpoints in its
own threadpool already (see server.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .categories import OTHER_CATEGORY_ID, SEEDED_MERCHANT_ALIASES, is_valid_category
from .db import FinanceRepository
from .errors import ValidationError
from .money import (
    CategoryOrigin,
    MoneyError,
    TransactionType,
    category_required_for_type,
    normalize_merchant_key,
    parse_amount_to_paise,
)
from .periods import Period

ACCOUNT_TYPES = {"bank", "credit_card", "cash"}
VALID_SOURCES = {"typed", "voice"}  # "import" is set only by the import service, never direct writes


# -- category resolution ---------------------------------------------------------

@dataclass
class CategoryResolution:
    category_id: str
    origin: CategoryOrigin
    needs_review: bool
    review_reason: str | None


def resolve_category(
    repo: FinanceRepository, merchant: str | None, proposed_category: str | None,
    model_flagged_ambiguous: bool,
) -> CategoryResolution:
    """Precedence: user explicit selection (caller's concern, not this
    function) > saved exact merchant rule > narrowly-scoped seeded alias >
    model suggestion (if a valid enum id) > otherwise `other`, flagged."""
    if merchant:
        merchant_key = normalize_merchant_key(merchant)
        rule = repo.get_merchant_rule(merchant_key)
        if rule is not None:
            return CategoryResolution(rule["category_id"], CategoryOrigin.RULE, False, None)
        alias_category = SEEDED_MERCHANT_ALIASES.get(merchant_key)
        if alias_category is not None:
            return CategoryResolution(alias_category, CategoryOrigin.RULE, False, None)

    if proposed_category and is_valid_category(proposed_category):
        # `other` is inherently an unresolved classification -- it always
        # needs review, independent of whether the model itself flagged
        # ambiguity. A confident rule/alias match never reaches this branch.
        is_other = proposed_category == OTHER_CATEGORY_ID
        needs_review = model_flagged_ambiguous or is_other
        review_reason = (
            "model_ambiguous" if model_flagged_ambiguous else "other_uncertain" if is_other else None
        )
        return CategoryResolution(proposed_category, CategoryOrigin.LLM, needs_review, review_reason)

    return CategoryResolution(OTHER_CATEGORY_ID, CategoryOrigin.FALLBACK, True, "invalid_model_category")


# -- accounts ---------------------------------------------------------------------

class AccountService:
    def __init__(self, repo: FinanceRepository):
        self._repo = repo

    def create_account(self, display_name: str, account_type: str, last4: str | None = None) -> dict[str, Any]:
        if account_type not in ACCOUNT_TYPES:
            raise ValidationError(f"Unknown account type: {account_type}")
        if not display_name or not display_name.strip():
            raise ValidationError("display_name is required")
        return self._repo.create_account(display_name.strip(), account_type, last4)

    def get_account(self, account_id: str) -> dict[str, Any]:
        return self._repo.require_account(account_id)

    def list_accounts(self, include_archived: bool = False) -> list[dict[str, Any]]:
        return self._repo.list_accounts(include_archived)

    def update_account(self, account_id: str, expected_revision: int, changes: dict[str, Any]) -> dict[str, Any]:
        return self._repo.update_account(account_id, expected_revision, changes)


# -- transactions ------------------------------------------------------------------

class TransactionService:
    """Exact paise arithmetic, mandatory category for expense/refund only,
    immutable provenance, revision-checked mutation."""

    def __init__(self, repo: FinanceRepository):
        self._repo = repo

    def create_transaction(self, draft: dict[str, Any]) -> dict[str, Any]:
        fields = self._build_create_fields(draft)
        return self._repo.create_transaction(fields)

    def _resolve_amount_paise(self, draft: dict[str, Any]) -> int:
        amount_paise = draft.get("amount_paise")
        if amount_paise is not None:
            if not isinstance(amount_paise, int) or amount_paise <= 0:
                raise ValidationError("amount_paise must be a positive integer")
            return amount_paise
        amount_text = draft.get("amount_text")
        if amount_text is None:
            raise ValidationError("amount_paise or amount_text is required")
        try:
            return parse_amount_to_paise(str(amount_text))
        except MoneyError as exc:
            raise ValidationError(str(exc)) from exc

    def _build_create_fields(self, draft: dict[str, Any]) -> dict[str, Any]:
        try:
            txn_type = TransactionType(draft["type"])
        except (KeyError, ValueError) as exc:
            raise ValidationError(f"Invalid transaction type: {draft.get('type')!r}") from exc

        amount_paise = self._resolve_amount_paise(draft)

        currency = draft.get("currency", "INR")
        if currency != "INR":
            raise ValidationError(f"Unsupported currency: {currency}")

        category_id = draft.get("category_id")
        if category_required_for_type(txn_type):
            if not category_id:
                raise ValidationError("category_id is required for expense/refund")
            if not is_valid_category(category_id):
                raise ValidationError(f"Unknown category_id: {category_id}")
        elif category_id is not None:
            raise ValidationError(f"category_id must be null for type={txn_type.value}")

        posted_date = draft.get("posted_date")
        if not posted_date:
            raise ValidationError("posted_date is required")

        original_transaction_id = draft.get("original_transaction_id")
        if original_transaction_id is not None:
            original = self._repo.get_transaction(original_transaction_id)
            if original is None:
                raise ValidationError(f"original_transaction_id not found: {original_transaction_id}")
            if original["currency"] != currency:
                raise ValidationError("Linked refund currency must match the original expense")

        source = draft.get("source", "typed")
        if source not in VALID_SOURCES:
            raise ValidationError(f"Unsupported source for a direct write: {source}")

        merchant = draft.get("merchant")
        merchant_key = normalize_merchant_key(merchant) if merchant else None

        return {
            "account_id": draft.get("account_id"), "merchant": merchant, "merchant_key": merchant_key,
            "description": draft.get("description"), "amount_paise": amount_paise, "currency": currency,
            "posted_date": posted_date, "transaction_date": draft.get("transaction_date"),
            "type": txn_type.value, "category_id": category_id,
            "original_transaction_id": original_transaction_id, "source": source,
            "category_origin": draft.get("category_origin", CategoryOrigin.USER.value),
            "needs_review": 1 if draft.get("needs_review") else 0,
        }

    def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        return self._repo.require_transaction(transaction_id)

    def list_transactions(self, **filters: Any) -> tuple[list[dict[str, Any]], str | None]:
        return self._repo.list_transactions(**filters)

    def _validate_partial_update(self, changes: dict[str, Any]) -> dict[str, Any]:
        cleaned = dict(changes)
        if "category_id" in cleaned and cleaned["category_id"] is not None:
            if not is_valid_category(cleaned["category_id"]):
                raise ValidationError(f"Unknown category_id: {cleaned['category_id']}")
            cleaned.setdefault("category_origin", CategoryOrigin.USER.value)
        if "amount_paise" in cleaned:
            amount = cleaned["amount_paise"]
            if not isinstance(amount, int) or amount <= 0:
                raise ValidationError("amount_paise must be a positive integer")
        if "merchant" in cleaned and cleaned["merchant"]:
            cleaned["merchant_key"] = normalize_merchant_key(cleaned["merchant"])
        return cleaned

    def update_transaction(self, transaction_id: str, expected_revision: int, changes: dict[str, Any]) -> dict[str, Any]:
        cleaned = self._validate_partial_update(changes)
        return self._repo.update_transaction(transaction_id, expected_revision, cleaned)

    def delete_transaction(self, transaction_id: str, expected_revision: int) -> None:
        self._repo.soft_delete_transaction(transaction_id, expected_revision)

    def remember_merchant_rule(self, merchant: str, category_id: str) -> dict[str, Any]:
        if not is_valid_category(category_id):
            raise ValidationError(f"Unknown category_id: {category_id}")
        return self._repo.put_merchant_rule(normalize_merchant_key(merchant), category_id, "user")


# -- analytics (deterministic aggregates only -- no LLM involvement) --------------

def compute_change_percent(delta_paise: int, previous_net_paise: int) -> str | None:
    """Null (never a fabricated 0% or a divide-by-zero crash) whenever the
    previous period's net wasn't positive -- a negative/zero baseline makes
    a percentage change meaningless, so only the absolute delta is reportable."""
    if previous_net_paise <= 0:
        return None
    percent = (Decimal(delta_paise) * 100 / Decimal(previous_net_paise)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return str(percent)


class AnalyticsService:
    def __init__(self, repo: FinanceRepository):
        self._repo = repo

    def summary(self, start_date: str, end_date_exclusive: str, account_ids: list[str] | None = None) -> dict[str, Any]:
        totals = self._repo.sum_amounts(start_date=start_date, end_date_exclusive=end_date_exclusive, account_ids=account_ids)
        breakdown = self._repo.category_breakdown(start_date=start_date, end_date_exclusive=end_date_exclusive, account_ids=account_ids)
        review_count = self._repo.count_needs_review(start_date=start_date, end_date_exclusive=end_date_exclusive, account_ids=account_ids)
        return {
            "start_date": start_date, "end_date_exclusive": end_date_exclusive, **totals,
            "category_breakdown": breakdown, "review_count": review_count,
            "data_revision": self._repo.get_data_revision(),
        }

    def compare(
        self, current: Period, previous: Period, category_ids: list[str] | None = None,
        account_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        current_totals = self._repo.sum_amounts(
            start_date=current.start_date, end_date_exclusive=current.end_date_exclusive,
            category_ids=category_ids, account_ids=account_ids,
        )
        previous_totals = self._repo.sum_amounts(
            start_date=previous.start_date, end_date_exclusive=previous.end_date_exclusive,
            category_ids=category_ids, account_ids=account_ids,
        )
        delta = current_totals["net_spending_paise"] - previous_totals["net_spending_paise"]
        change_percent = compute_change_percent(delta, previous_totals["net_spending_paise"])
        return {
            "current_period": {
                "start_date": current.start_date, "end_date_exclusive": current.end_date_exclusive,
                "as_of_date": current.as_of_date, "label": current.label, **current_totals,
            },
            "previous_period": {
                "start_date": previous.start_date, "end_date_exclusive": previous.end_date_exclusive,
                "as_of_date": previous.as_of_date, "label": previous.label, **previous_totals,
            },
            "delta_net_paise": delta, "change_percent": change_percent,
        }
