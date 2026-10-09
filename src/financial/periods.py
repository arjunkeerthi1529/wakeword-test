"""Deterministic date/period math. The LLM supplies a period *kind* (or an
explicit range the user actually stated) or a date *expression* like
"yesterday" -- it never supplies a raw computed date, and nothing here ever
asks the model to do arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 fallback, unused on 3.11+
    ZoneInfo = None  # type: ignore[assignment]


class AmbiguousDateError(ValueError):
    """date_expression couldn't be resolved deterministically; caller should
    flag the field as missing/needs-review rather than guess."""


class UnsupportedPeriodError(ValueError):
    """The requested period can't be resolved deterministically; the caller
    should ask for clarification rather than guess."""


def today_in(timezone_name: str) -> date:
    return datetime.now(ZoneInfo(timezone_name)).date()


def resolve_date_expression(date_expression: str, today: date) -> date:
    """omitted/"today" -> today; "yesterday" -> today - 1 day; an explicit
    ISO "YYYY-MM-DD" is parsed as-is. Anything else (relative weekday names,
    ambiguous phrasing) raises AmbiguousDateError so the caller asks the user
    instead of guessing. A resolved future date is also rejected."""
    text = date_expression.strip().lower()
    if text in ("", "today"):
        resolved = today
    elif text == "yesterday":
        resolved = today - timedelta(days=1)
    else:
        try:
            resolved = date.fromisoformat(date_expression.strip())
        except ValueError as exc:
            raise AmbiguousDateError(f"Could not resolve date expression: {date_expression!r}") from exc

    if resolved > today:
        raise AmbiguousDateError(f"Resolved date is in the future: {resolved.isoformat()}")
    return resolved


@dataclass
class Period:
    start_date: str  # ISO, inclusive
    end_date_exclusive: str  # ISO, exclusive
    as_of_date: str | None = None  # set for a partial/month-to-date period
    label: str = ""


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _subtract_one_month(d: date) -> date:
    if d.month == 1:
        return date(d.year - 1, 12, 1)
    return date(d.year, d.month - 1, 1)


def resolve_period(
    kind: str,
    today: date,
    *,
    start_date: str | None = None,
    end_date_exclusive: str | None = None,
    days: int | None = None,
) -> Period:
    """Backend-only date math: the model supplies a period *kind*, never a
    raw date it invented, except for an explicit_range the user actually
    stated. Clamping a requested future period or an invalid range raises
    UnsupportedPeriodError rather than inventing future transactions."""
    if kind == "today":
        return Period(today.isoformat(), (today + timedelta(days=1)).isoformat(), label="today")

    if kind == "yesterday":
        y = today - timedelta(days=1)
        return Period(y.isoformat(), today.isoformat(), label="yesterday")

    if kind == "this_month_to_date":
        start = _month_start(today)
        return Period(
            start.isoformat(), (today + timedelta(days=1)).isoformat(),
            as_of_date=today.isoformat(), label="this month to date",
        )

    if kind == "last_month":
        this_month_start = _month_start(today)
        last_month_start = _subtract_one_month(this_month_start)
        return Period(last_month_start.isoformat(), this_month_start.isoformat(), label="last month")

    if kind == "last_n_days":
        if days is None or not (1 <= days <= 366):
            raise UnsupportedPeriodError(f"last_n_days requires 1-366 days, got {days!r}")
        start = today - timedelta(days=days)
        return Period(
            start.isoformat(), (today + timedelta(days=1)).isoformat(), label=f"last {days} days"
        )

    if kind == "explicit_range":
        if not start_date or not end_date_exclusive:
            raise UnsupportedPeriodError("explicit_range requires both dates")
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date_exclusive)
        except ValueError as exc:
            raise UnsupportedPeriodError(f"Invalid explicit range: {exc}") from exc
        if end <= start:
            raise UnsupportedPeriodError("end_date_exclusive must be after start_date")
        if start > today:
            raise UnsupportedPeriodError("Requested period is entirely in the future")
        return Period(start.isoformat(), end.isoformat(), label=f"{start_date} to {end_date_exclusive}")

    raise UnsupportedPeriodError(f"Unsupported period kind: {kind!r}")


def resolve_month_to_date_comparison(today: date) -> tuple[Period, Period]:
    """Compare the current partial month to the *same elapsed-day window* of
    the previous month, capped to that month's actual length (handles
    February/leap-year and year-boundary cases by construction, never a
    hardcoded day count)."""
    current_start = _month_start(today)
    current_end_exclusive = today + timedelta(days=1)
    elapsed_days = (today - current_start).days + 1

    previous_start = _subtract_one_month(current_start)
    days_in_previous_month = (current_start - previous_start).days
    capped_elapsed = min(elapsed_days, days_in_previous_month)
    previous_end_exclusive = previous_start + timedelta(days=capped_elapsed)

    current = Period(
        current_start.isoformat(), current_end_exclusive.isoformat(),
        as_of_date=today.isoformat(), label="this month to date",
    )
    previous = Period(
        previous_start.isoformat(), previous_end_exclusive.isoformat(),
        as_of_date=(previous_end_exclusive - timedelta(days=1)).isoformat(),
        label="same elapsed days last month",
    )
    return current, previous
