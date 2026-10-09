"""POST /questions: one bounded model call translates free text into a
structured intent; the backend validates it and always calls a predefined,
parameterized query function -- never model-generated SQL, never a raw date
the model invented for anything but an explicit_range the user actually
stated. Blocking (matching src/work's convention) -- returns the answer
directly, no job/polling layer.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import llm_client
from .categories import is_valid_category
from .db import FinanceRepository
from .llm_tasks import InterpretQuestionResult
from .money import format_inr, normalize_merchant_key
from .periods import Period, UnsupportedPeriodError, resolve_month_to_date_comparison, resolve_period, today_in

SUPPORTED_OPERATIONS = {
    "total_spending", "category_breakdown", "merchant_breakdown",
    "biggest_expenses", "busiest_day", "transaction_count",
    "average_spending", "compare_periods",
}


def _period_payload(period: Period) -> dict[str, Any]:
    return {
        "start_date": period.start_date, "end_date_exclusive": period.end_date_exclusive,
        "as_of_date": period.as_of_date, "label": period.label,
    }


def answer_question(repo: FinanceRepository, text: str, *, llm_base_url: str, llm_model: str, timezone: str) -> dict[str, Any]:
    today = today_in(timezone)
    intent = llm_client.interpret_question(text, llm_base_url=llm_base_url, model=llm_model, today=today)
    return _execute_intent(repo, intent, today)


def _execute_intent(repo: FinanceRepository, intent: InterpretQuestionResult, today: date) -> dict[str, Any]:
    if intent.needs_clarification or intent.operation == "unsupported":
        return {
            "status": "needs_clarification", "operation": "unsupported",
            "clarification": intent.clarification or "Please rephrase as a spending question about a "
            "specific period -- for example a total, a breakdown, your biggest expenses, a count, an "
            "average, or a comparison with last month.",
        }

    if intent.operation not in SUPPORTED_OPERATIONS:
        return {
            "status": "needs_clarification", "operation": intent.operation,
            "clarification": f"'{intent.operation}' isn't supported yet in this build. Try a total, "
            "breakdown, biggest-expenses, count, average or comparison question.",
        }

    category_ids = [c for c in intent.category_ids if is_valid_category(c)]
    merchant_key = normalize_merchant_key(intent.merchant_text) or None
    merchant_label = intent.merchant_text.strip() or None

    if intent.operation == "compare_periods":
        return _answer_compare_periods(repo, intent, today, category_ids)

    try:
        period = resolve_period(
            intent.period_kind, today, start_date=intent.period_start_date or None,
            end_date_exclusive=intent.period_end_date_exclusive or None, days=intent.period_days or None,
        )
    except UnsupportedPeriodError as exc:
        return {"status": "needs_clarification", "operation": intent.operation,
                "clarification": f"Could not resolve the requested period: {exc}"}

    if intent.operation == "total_spending":
        return _answer_total_spending(repo, period, category_ids, merchant_key, merchant_label)
    if intent.operation == "category_breakdown":
        return _answer_category_breakdown(repo, period)
    if intent.operation == "merchant_breakdown":
        return _answer_merchant_breakdown(repo, period, category_ids)
    if intent.operation == "biggest_expenses":
        return _answer_biggest_expenses(repo, period, category_ids, merchant_key, merchant_label, intent.limit)
    if intent.operation == "busiest_day":
        return _answer_busiest_day(repo, period, category_ids, merchant_key)
    if intent.operation == "transaction_count":
        return _answer_transaction_count(repo, period, category_ids, merchant_key, merchant_label)
    return _answer_average_spending(repo, period, category_ids, merchant_key, merchant_label, intent.average_unit)


def _filter_label(category_ids: list[str], merchant_label: str | None) -> str:
    if merchant_label:
        return merchant_label
    if category_ids:
        return " and ".join(category_ids)
    return "total"


def _answer_total_spending(repo, period, category_ids, merchant_key, merchant_label) -> dict[str, Any]:
    totals = repo.sum_amounts(
        start_date=period.start_date, end_date_exclusive=period.end_date_exclusive,
        category_ids=category_ids or None, merchant_key=merchant_key,
    )
    if totals["matched_count"] == 0:
        answer = "No recorded transactions for this period."
    else:
        if merchant_label:
            scope = f"spending at {merchant_label}"
        elif category_ids:
            scope = f"{' and '.join(category_ids)} spending"
        else:
            scope = "total spending"
        answer = f"Your recorded {scope} for {period.label} was {format_inr(totals['net_spending_paise'])}"
        answer += f", after {format_inr(totals['refund_paise'])} in refunds." if totals["refund_paise"] > 0 else "."
    return {
        "status": "completed", "operation": "total_spending", "period": _period_payload(period),
        "category_ids": category_ids, "merchant_text": merchant_label or "", **totals, "answer": answer,
    }


def _answer_category_breakdown(repo, period) -> dict[str, Any]:
    breakdown = repo.category_breakdown(start_date=period.start_date, end_date_exclusive=period.end_date_exclusive)
    if not breakdown:
        answer = "No recorded transactions for this period."
    else:
        top = breakdown[0]
        answer = f"Your top spending category for {period.label} was {top['category_id']} at {format_inr(top['gross_expense_paise'])}."
    return {"status": "completed", "operation": "category_breakdown", "period": _period_payload(period),
            "breakdown": breakdown, "answer": answer}


def _answer_merchant_breakdown(repo, period, category_ids) -> dict[str, Any]:
    breakdown = repo.merchant_breakdown(start_date=period.start_date, end_date_exclusive=period.end_date_exclusive, category_ids=category_ids or None)
    scope = (" and ".join(category_ids) + " ") if category_ids else ""
    if not breakdown:
        answer = "No recorded transactions for this period."
    else:
        top = breakdown[0]
        answer = f"Your top {scope}merchant for {period.label} was {top['merchant']} at {format_inr(top['gross_expense_paise'])}."
    return {"status": "completed", "operation": "merchant_breakdown", "period": _period_payload(period),
            "category_ids": category_ids, "breakdown": breakdown, "answer": answer}


def _answer_biggest_expenses(repo, period, category_ids, merchant_key, merchant_label, limit) -> dict[str, Any]:
    limit = min(max(limit, 1), 10) if limit else 3
    rows = repo.top_expenses(
        start_date=period.start_date, end_date_exclusive=period.end_date_exclusive,
        category_ids=category_ids or None, merchant_key=merchant_key, limit=limit,
    )
    scope = _filter_label(category_ids, merchant_label)
    scope_text = "" if scope == "total" else f"{scope} "
    if not rows:
        answer = "No recorded transactions for this period."
    else:
        top = rows[0]
        answer = (f"Your biggest {scope_text}expense for {period.label} was {format_inr(top['amount_paise'])} "
                  f"at {top['merchant'] or 'an unnamed merchant'} on {top['posted_date']}.")
    return {"status": "completed", "operation": "biggest_expenses", "period": _period_payload(period),
            "category_ids": category_ids, "merchant_text": merchant_label or "", "items": rows, "answer": answer}


def _answer_busiest_day(repo, period, category_ids, merchant_key) -> dict[str, Any]:
    day = repo.busiest_day(start_date=period.start_date, end_date_exclusive=period.end_date_exclusive, category_ids=category_ids or None, merchant_key=merchant_key)
    if day is None:
        answer = "No recorded transactions for this period."
    else:
        answer = (f"Your highest-spending day in {period.label} was {day['posted_date']} at "
                  f"{format_inr(day['gross_expense_paise'])} across {day['count']} expense{'s' if day['count'] != 1 else ''}.")
    return {"status": "completed", "operation": "busiest_day", "period": _period_payload(period),
            "category_ids": category_ids, "day": day, "answer": answer}


def _answer_transaction_count(repo, period, category_ids, merchant_key, merchant_label) -> dict[str, Any]:
    totals = repo.sum_amounts(start_date=period.start_date, end_date_exclusive=period.end_date_exclusive, category_ids=category_ids or None, merchant_key=merchant_key)
    count = totals["expense_count"]
    scope = _filter_label(category_ids, merchant_label)
    scope_text = "" if scope == "total" else f"{scope} "
    if count == 0:
        answer = "No recorded transactions for this period."
    else:
        answer = f"You recorded {count} {scope_text}expense{'s' if count != 1 else ''} for {period.label}, totalling {format_inr(totals['net_spending_paise'])}."
    return {"status": "completed", "operation": "transaction_count", "period": _period_payload(period),
            "category_ids": category_ids, "merchant_text": merchant_label or "", "count": count, **totals, "answer": answer}


def _answer_average_spending(repo, period, category_ids, merchant_key, merchant_label, unit) -> dict[str, Any]:
    totals = repo.sum_amounts(start_date=period.start_date, end_date_exclusive=period.end_date_exclusive, category_ids=category_ids or None, merchant_key=merchant_key)
    start = date.fromisoformat(period.start_date)
    end = date.fromisoformat(period.end_date_exclusive)
    day_count = max((end - start).days, 1)
    unit = "monthly" if unit == "monthly" else "daily"
    if unit == "monthly":
        divisor, unit_label = max(round(day_count / 30), 1), "month"
    else:
        divisor, unit_label = day_count, "day"
    average_paise = totals["net_spending_paise"] // divisor
    scope = _filter_label(category_ids, merchant_label)
    scope_text = "" if scope == "total" else f"{scope} "
    if totals["matched_count"] == 0:
        answer = "No recorded transactions for this period."
    else:
        answer = f"Your average {scope_text}spending in {period.label} was about {format_inr(average_paise)} per {unit_label}."
    return {"status": "completed", "operation": "average_spending", "period": _period_payload(period),
            "category_ids": category_ids, "merchant_text": merchant_label or "", "average_paise": average_paise,
            "average_unit": unit, **totals, "answer": answer}


def _answer_compare_periods(repo, intent: InterpretQuestionResult, today: date, category_ids: list[str]) -> dict[str, Any]:
    try:
        current, previous = _compare_windows(intent, today)
    except UnsupportedPeriodError as exc:
        return {"status": "needs_clarification", "operation": "compare_periods",
                "clarification": f"Could not resolve the periods to compare: {exc}"}
    current_totals = repo.sum_amounts(start_date=current.start_date, end_date_exclusive=current.end_date_exclusive, category_ids=category_ids or None)
    previous_totals = repo.sum_amounts(start_date=previous.start_date, end_date_exclusive=previous.end_date_exclusive, category_ids=category_ids or None)
    delta = current_totals["net_spending_paise"] - previous_totals["net_spending_paise"]
    scope = (" and ".join(category_ids) + " ") if category_ids else ""
    direction = "more" if delta > 0 else "less" if delta < 0 else "the same"
    if delta == 0:
        answer = f"You spent {scope}about the same in {current.label} as in {previous.label} ({format_inr(current_totals['net_spending_paise'])})."
    else:
        answer = (f"You spent {format_inr(abs(delta))} {direction} on {scope or 'everything '}in {current.label} "
                  f"({format_inr(current_totals['net_spending_paise'])}) than in {previous.label} ({format_inr(previous_totals['net_spending_paise'])}).")
    return {
        "status": "completed", "operation": "compare_periods", "category_ids": category_ids,
        "current_period": {**_period_payload(current), **current_totals},
        "previous_period": {**_period_payload(previous), **previous_totals},
        "delta_net_paise": delta, "answer": answer,
    }


def _compare_windows(intent: InterpretQuestionResult, today: date) -> tuple[Period, Period]:
    """"Compare X with last month" is the dominant phrasing and means
    this-month-to-date vs the same elapsed days of last month -- so a plain
    monthly comparison uses that regardless of which period_kind the model
    tagged. Only an explicit range or last-N-days compares the equal window
    immediately before it."""
    if intent.period_kind in ("this_month_to_date", "last_month", "unknown"):
        return resolve_month_to_date_comparison(today)

    current = resolve_period(
        intent.period_kind, today, start_date=intent.period_start_date or None,
        end_date_exclusive=intent.period_end_date_exclusive or None, days=intent.period_days or None,
    )
    cur_start = date.fromisoformat(current.start_date)
    cur_end = date.fromisoformat(current.end_date_exclusive)
    span = (cur_end - cur_start).days
    prev_start = cur_start - timedelta(days=span)
    previous = Period(prev_start.isoformat(), cur_start.isoformat(), label="the period before")
    return current, previous
