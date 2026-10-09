"""Money, transaction type/origin enums, and merchant-key normalization.

Exact-paise arithmetic throughout: amounts are parsed from validated decimal
strings straight to integer paise, never a binary float, and never rounded
anywhere after that first conversion.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, DecimalException, InvalidOperation
from enum import StrEnum

CURRENCY = "INR"  # this build accepts only INR; anything else is rejected visibly
MAX_AMOUNT_PAISE = 100_000_000_000

_AMOUNT_PATTERN = re.compile(r"^\d+(\.\d{1,2})?$")
_WHITESPACE_RE = re.compile(r"\s+")


class TransactionType(StrEnum):
    EXPENSE = "expense"
    REFUND = "refund"
    INCOME = "income"
    TRANSFER = "transfer"


class CategoryOrigin(StrEnum):
    USER = "user"
    RULE = "rule"
    LLM = "llm"
    FALLBACK = "fallback"


class MoneyError(ValueError):
    """Invalid amount text: unparseable, wrong precision, non-positive, or over cap."""


def parse_amount_to_paise(amount_text: str) -> int:
    """Decimal-from-validated-string -> integer paise. Never a binary float.

    Accepts at most two decimal places, requires a positive amount, and caps
    at MAX_AMOUNT_PAISE. Anything else (empty, negative, >2 decimals,
    non-numeric) raises MoneyError.
    """
    text = amount_text.strip()
    if not _AMOUNT_PATTERN.match(text):
        raise MoneyError(f"Amount must be a positive decimal with at most 2 places: {amount_text!r}")
    try:
        decimal_value = Decimal(text)
    except (InvalidOperation, DecimalException) as exc:
        raise MoneyError(f"Could not parse amount: {amount_text!r}") from exc
    if decimal_value <= 0:
        raise MoneyError("Amount must be positive")
    paise = int(decimal_value * 100)
    if paise > MAX_AMOUNT_PAISE:
        raise MoneyError(f"Amount exceeds the configured cap of {MAX_AMOUNT_PAISE} paise")
    return paise


def format_inr(paise: int) -> str:
    """Presentation-boundary formatting only -- all calculation stays on the
    integer paise; this never feeds back into arithmetic."""
    sign = "-" if paise < 0 else ""
    rupees, remainder = divmod(abs(paise), 100)
    grouped = f"{rupees:,}"
    if remainder:
        return f"{sign}₹{grouped}.{remainder:02d}"
    return f"{sign}₹{grouped}"


def normalize_merchant_key(text: str) -> str:
    """Unicode NFKC, case-fold, trim, collapse whitespace. Used for exact
    merchant-rule matching and seeded-alias lookup -- never substring/regex."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.strip().casefold()
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    return normalized


def category_required_for_type(transaction_type: TransactionType) -> bool:
    """category_id is mandatory for expense/refund, must be null for income/transfer."""
    return transaction_type in (TransactionType.EXPENSE, TransactionType.REFUND)
