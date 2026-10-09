"""Account, transaction, category-resolution and analytics business logic.
Plain synchronous functions/classes over FinanceRepository -- no asyncio
anywhere in this service, since FastAPI runs plain `def` endpoints in its
own threadpool already (see server.py).
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .categories import OTHER_CATEGORY_ID, SEEDED_MERCHANT_ALIASES, is_valid_category
from .db import FinanceRepository
from .errors import NotFoundError, ValidationError
from .money import (
    CategoryOrigin,
    MoneyError,
    TransactionType,
    category_required_for_type,
    format_inr,
    normalize_merchant_key,
    parse_amount_to_paise,
)
from .periods import Period, resolve_month_to_date_comparison

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

        category_comparison = None
        if not category_ids:
            cur_bd = self._repo.category_breakdown(
                start_date=current.start_date, end_date_exclusive=current.end_date_exclusive,
                account_ids=account_ids,
            )
            prev_bd = self._repo.category_breakdown(
                start_date=previous.start_date, end_date_exclusive=previous.end_date_exclusive,
                account_ids=account_ids,
            )
            prev_map = {r["category_id"]: r["gross_expense_paise"] for r in prev_bd}
            category_comparison = []
            for row in cur_bd:
                cid = row["category_id"]
                cur_val = row["gross_expense_paise"]
                prev_val = prev_map.pop(cid, 0)
                d = cur_val - prev_val
                category_comparison.append({
                    "category_id": cid,
                    "current_paise": cur_val, "previous_paise": prev_val,
                    "delta_paise": d,
                    "change_percent": compute_change_percent(d, prev_val),
                })
            for cid, prev_val in prev_map.items():
                category_comparison.append({
                    "category_id": cid,
                    "current_paise": 0, "previous_paise": prev_val,
                    "delta_paise": -prev_val, "change_percent": "-100.0",
                })

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
            "category_comparison": category_comparison,
        }


# -- budgets -----------------------------------------------------------------------

class BudgetService:
    def __init__(self, repo: FinanceRepository):
        self._repo = repo

    def upsert(self, category_id: str, amount_paise: int) -> dict[str, Any]:
        if not is_valid_category(category_id):
            raise ValidationError(f"Unknown category_id: {category_id}")
        if not isinstance(amount_paise, int) or amount_paise <= 0:
            raise ValidationError("amount_paise must be a positive integer")
        return self._repo.upsert_budget(category_id, amount_paise)

    def list_budgets(self) -> list[dict[str, Any]]:
        return self._repo.list_budgets()

    def get_by_category(self, category_id: str) -> dict[str, Any] | None:
        return self._repo.get_budget_by_category(category_id)

    def delete(self, category_id: str) -> bool:
        return self._repo.delete_budget(category_id)

    def status(self, today: date) -> list[dict[str, Any]]:
        budgets = self._repo.list_budgets()
        if not budgets:
            return []
        start = today.replace(day=1)
        end_exclusive = (today + timedelta(days=1)).isoformat()
        dim = calendar.monthrange(today.year, today.month)[1]
        days_left = dim - today.day + 1
        out = []
        for b in budgets:
            totals = self._repo.sum_amounts(
                start_date=start.isoformat(), end_date_exclusive=end_exclusive,
                category_ids=[b["category_id"]],
            )
            spent = totals["net_spending_paise"]
            limit = b["amount_paise"]
            left = limit - spent
            pct = spent / limit if limit else 0
            per_day = max(0, left) // days_left if days_left else 0
            out.append({
                **b,
                "spent_paise": spent, "left_paise": left,
                "usage_percent": round(pct * 100, 1),
                "per_day_paise": per_day, "days_left": days_left,
                "expense_count": totals["expense_count"],
            })
        return out


# -- goals -------------------------------------------------------------------------

class GoalService:
    def __init__(self, repo: FinanceRepository):
        self._repo = repo

    def create(self, name: str, emoji: str, target_paise: int, start_date: str, due_date: str) -> dict[str, Any]:
        if not name or not name.strip():
            raise ValidationError("name is required")
        if not isinstance(target_paise, int) or target_paise <= 0:
            raise ValidationError("target_paise must be a positive integer")
        goal = self._repo.create_goal(name.strip(), emoji or "⭐", target_paise, start_date, due_date)
        return self._enrich(goal)

    def get(self, goal_id: str, today: date | None = None) -> dict[str, Any]:
        return self._enrich(self._repo.require_goal(goal_id), today)

    def list_goals(self, include_archived: bool = False, today: date | None = None) -> list[dict[str, Any]]:
        return [self._enrich(g, today) for g in self._repo.list_goals(include_archived)]

    def update(self, goal_id: str, expected_revision: int, changes: dict[str, Any], today: date | None = None) -> dict[str, Any]:
        return self._enrich(self._repo.update_goal(goal_id, expected_revision, changes), today)

    def delete(self, goal_id: str) -> None:
        self._repo.delete_goal(goal_id)

    def add_contribution(self, goal_id: str, amount_paise: int, contributed_date: str, note: str | None = None) -> dict[str, Any]:
        if not isinstance(amount_paise, int) or amount_paise <= 0:
            raise ValidationError("amount_paise must be a positive integer")
        return self._repo.add_goal_contribution(goal_id, amount_paise, contributed_date, note)

    def list_contributions(self, goal_id: str) -> list[dict[str, Any]]:
        return self._repo.list_goal_contributions(goal_id)

    def _enrich(self, goal: dict[str, Any], today: date | None = None) -> dict[str, Any]:
        saved = self._repo.goal_saved_paise(goal["id"])
        target = goal["target_paise"]
        pct = saved / target if target else 0
        result = {**goal, "saved_paise": saved, "progress_percent": round(pct * 100, 1)}
        if today:
            try:
                start_d = date.fromisoformat(goal["start_date"])
                due_d = date.fromisoformat(goal["due_date"])
                total_span = max((due_d - start_d).days, 1)
                elapsed = max(0, (today - start_d).days)
                expected_paise = int(target * min(1.0, elapsed / total_span))
                months_left = max(1, (due_d.year - today.year) * 12 + due_d.month - today.month)
                need_per_month = max(0, target - saved) // months_left
                result["expected_paise"] = expected_paise
                result["on_track"] = saved >= expected_paise * 0.95
                result["need_per_month_paise"] = need_per_month
                result["months_left"] = months_left
            except (ValueError, ZeroDivisionError):
                pass
        return result


# -- loans -------------------------------------------------------------------------

LOAN_KINDS = {"no_cost_emi", "loan_emi", "card_emi"}
LOAN_SOURCES = {"bank", "card"}


class LoanService:
    def __init__(self, repo: FinanceRepository):
        self._repo = repo

    def create(self, fields: dict[str, Any]) -> dict[str, Any]:
        if fields.get("kind") not in LOAN_KINDS:
            raise ValidationError(f"Invalid loan kind: {fields.get('kind')}")
        if fields.get("source") not in LOAN_SOURCES:
            raise ValidationError(f"Invalid loan source: {fields.get('source')}")
        if not isinstance(fields.get("emi_paise"), int) or fields["emi_paise"] <= 0:
            raise ValidationError("emi_paise must be a positive integer")
        if not isinstance(fields.get("months"), int) or fields["months"] <= 0:
            raise ValidationError("months must be a positive integer")
        day = fields.get("due_day")
        if not isinstance(day, int) or not (1 <= day <= 28):
            raise ValidationError("due_day must be 1–28")
        return self._repo.create_loan(fields)

    def get(self, loan_id: str, today: date | None = None) -> dict[str, Any]:
        loan = self._repo.require_loan(loan_id)
        return self._enrich(loan, today) if today else loan

    def list_loans(self, include_archived: bool = False, today: date | None = None) -> list[dict[str, Any]]:
        loans = self._repo.list_loans(include_archived)
        if today:
            return [self._enrich(ln, today) for ln in loans]
        return loans

    def update(self, loan_id: str, expected_revision: int, changes: dict[str, Any], today: date | None = None) -> dict[str, Any]:
        loan = self._repo.update_loan(loan_id, expected_revision, changes)
        return self._enrich(loan, today) if today else loan

    def delete(self, loan_id: str) -> None:
        self._repo.delete_loan(loan_id)

    def _enrich(self, loan: dict[str, Any], today: date) -> dict[str, Any]:
        start = date.fromisoformat(loan["start_date"])
        cur_inst = (today.year - start.year) * 12 + (today.month - start.month) + 1
        total = loan["months"]
        done = cur_inst > total
        paid = min(total, max(0, cur_inst - 1)) if not done else total
        remaining_paise = (total - paid) * loan["emi_paise"]
        end_m = start.month + total - 1
        end_y = start.year + (end_m - 1) // 12
        end_m = ((end_m - 1) % 12) + 1
        end_date = date(end_y, end_m, loan["due_day"])
        return {
            **loan,
            "current_installment": cur_inst, "paid_installments": paid,
            "done": done, "remaining_paise": remaining_paise,
            "end_date": end_date.isoformat(),
        }


# -- recurring detection -----------------------------------------------------------

@dataclass
class RecurringPayment:
    merchant: str
    merchant_key: str
    category_id: str
    amount_paise: int
    typical_day: int
    months_present: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "merchant": self.merchant, "merchant_key": self.merchant_key,
            "category_id": self.category_id, "amount_paise": self.amount_paise,
            "typical_day": self.typical_day, "months_present": self.months_present,
        }


class RecurringService:
    def __init__(self, repo: FinanceRepository):
        self._repo = repo

    def detect(self, today: date) -> list[dict[str, Any]]:
        months = []
        for offset in range(1, 4):
            m = today.month - offset
            y = today.year
            while m < 1:
                m += 12
                y -= 1
            start = date(y, m, 1)
            dim = calendar.monthrange(y, m)[1]
            end = date(y, m, dim) + timedelta(days=1)
            months.append((f"{y}-{m:02d}", start.isoformat(), end.isoformat()))

        if not months:
            return []

        overall_start = months[-1][1]
        overall_end = months[0][2]
        txns = self._repo.transactions_for_recurring_detection(overall_start, overall_end)

        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in txns:
            groups[t["merchant_key"]].append(t)

        results = []
        month_prefixes = [mp[0] for mp in months]

        for mkey, txs in groups.items():
            per_month = {mp: [] for mp in month_prefixes}
            for t in txs:
                prefix = t["posted_date"][:7]
                if prefix in per_month:
                    per_month[prefix].append(t)

            if any(len(v) > 1 for v in per_month.values()):
                continue
            hits = [v[0] for v in per_month.values() if len(v) == 1]
            if len(hits) < 2:
                continue

            amounts = [h["amount_paise"] for h in hits]
            avg = sum(amounts) / len(amounts)
            if avg == 0:
                continue
            if (max(amounts) - min(amounts)) / avg > 0.15:
                continue

            days = sorted(date.fromisoformat(h["posted_date"]).day for h in hits)
            typical_day = days[len(days) // 2]
            latest = max(txs, key=lambda t: t["posted_date"])

            results.append(RecurringPayment(
                merchant=latest["merchant"],
                merchant_key=mkey,
                category_id=latest["category_id"] or OTHER_CATEGORY_ID,
                amount_paise=latest["amount_paise"],
                typical_day=typical_day,
                months_present=len(hits),
            ).to_dict())

        results.sort(key=lambda r: r["typical_day"])
        return results


# -- forecast ----------------------------------------------------------------------

class ForecastService:
    def __init__(self, repo: FinanceRepository):
        self._repo = repo

    def project(self, today: date, category_id: str | None = None) -> dict[str, Any]:
        start = today.replace(day=1)
        end_exclusive = (today + timedelta(days=1)).isoformat()
        dim = calendar.monthrange(today.year, today.month)[1]
        elapsed = today.day

        cats = [category_id] if category_id else None
        totals = self._repo.sum_amounts(
            start_date=start.isoformat(), end_date_exclusive=end_exclusive,
            category_ids=cats,
        )
        actual = totals["net_spending_paise"]

        if elapsed < 1:
            return {"ok": False, "reason": "No days elapsed this month yet."}

        projected = int(actual / elapsed * dim)

        budget_paise = None
        if category_id:
            b = self._repo.get_budget_by_category(category_id)
            if b:
                budget_paise = b["amount_paise"]

        daily = self._repo.daily_spending(
            start.isoformat(), end_exclusive, category_ids=cats,
        )

        return {
            "ok": True,
            "category_id": category_id,
            "actual_paise": actual,
            "projected_paise": projected,
            "elapsed_days": elapsed,
            "days_in_month": dim,
            "budget_paise": budget_paise,
            "over_budget": budget_paise is not None and projected > budget_paise,
            "expense_count": totals["expense_count"],
            "daily_spending": daily,
            "calculation": f"{format_inr(actual)} ÷ {elapsed} days × {dim} days = {format_inr(projected)}",
        }


# -- advice ------------------------------------------------------------------------

class AdviceService:
    def __init__(self, repo: FinanceRepository, budget_svc: BudgetService,
                 goal_svc: GoalService, forecast_svc: ForecastService):
        self._repo = repo
        self._budgets = budget_svc
        self._goals = goal_svc
        self._forecast = forecast_svc

    def generate(self, today: date) -> list[dict[str, Any]]:
        advice: list[dict[str, Any]] = []
        self._budget_advice(today, advice)
        self._forecast_advice(today, advice)
        self._comparison_advice(today, advice)
        self._goal_advice(today, advice)
        return advice

    def _budget_advice(self, today: date, out: list[dict[str, Any]]) -> None:
        for b in self._budgets.status(today):
            pct = b["usage_percent"]
            if pct >= 75:
                tone = "bad" if pct >= 100 else "warn"
                out.append({
                    "id": f"budget-{b['category_id']}",
                    "tone": tone,
                    "title": f"{b['category_id']}: {pct:.0f}% of budget used",
                    "body": f"{format_inr(b['spent_paise'])} of {format_inr(b['amount_paise'])} spent, "
                            f"with {b['days_left']} days left.",
                    "category_id": b["category_id"],
                    "kind": "budget_usage",
                })

    def _forecast_advice(self, today: date, out: list[dict[str, Any]]) -> None:
        for b in self._repo.list_budgets():
            cat = b["category_id"]
            already = any(a["id"] == f"budget-{cat}" for a in out)
            if already:
                continue
            f = self._forecast.project(today, cat)
            if f.get("ok") and f.get("over_budget"):
                out.append({
                    "id": f"forecast-{cat}",
                    "tone": "warn",
                    "title": f"{cat} may go over budget",
                    "body": f"At this pace you'd reach about {format_inr(f['projected_paise'])} by end of month, "
                            f"around {format_inr(f['projected_paise'] - f['budget_paise'])} above your "
                            f"{format_inr(f['budget_paise'])} limit.",
                    "category_id": cat,
                    "kind": "forecast_over",
                })

    def _comparison_advice(self, today: date, out: list[dict[str, Any]]) -> None:
        current, previous = resolve_month_to_date_comparison(today)
        check_groups = [
            ("Food", ["food_dining", "groceries"]),
            ("Transport", ["transport"]),
            ("Entertainment", ["entertainment"]),
        ]
        for name, cats in check_groups:
            now_t = self._repo.sum_amounts(
                start_date=current.start_date, end_date_exclusive=current.end_date_exclusive,
                category_ids=cats,
            )
            then_t = self._repo.sum_amounts(
                start_date=previous.start_date, end_date_exclusive=previous.end_date_exclusive,
                category_ids=cats,
            )
            now_val = now_t["net_spending_paise"]
            then_val = then_t["net_spending_paise"]
            if then_val > 0 and now_val - then_val >= 50000 and now_val / then_val >= 1.25:
                out.append({
                    "id": f"compare-{name.lower()}",
                    "tone": "warn",
                    "title": f"{name} spending is {format_inr(now_val - then_val)} higher than this time last month",
                    "body": f"{format_inr(now_val)} so far vs {format_inr(then_val)} for the same days of last month.",
                    "category_ids": cats,
                    "kind": "month_comparison",
                })

    def _goal_advice(self, today: date, out: list[dict[str, Any]]) -> None:
        for g in self._goals.list_goals(today=today):
            if g.get("on_track") is False:
                out.append({
                    "id": f"goal-{g['id']}",
                    "tone": "warn",
                    "title": f"{g['name']} is a little behind",
                    "body": f"{format_inr(g['saved_paise'])} saved so far. "
                            f"About {format_inr(g.get('need_per_month_paise', 0))}/month would reach "
                            f"{format_inr(g['target_paise'])} by the due date.",
                    "goal_id": g["id"],
                    "kind": "goal_behind",
                })
            elif g.get("on_track") is True and g.get("progress_percent", 0) > 0:
                out.append({
                    "id": f"goal-ok-{g['id']}",
                    "tone": "good",
                    "title": f"{g['name']} is on track",
                    "body": f"{format_inr(g['saved_paise'])} of {format_inr(g['target_paise'])} saved.",
                    "goal_id": g["id"],
                    "kind": "goal_on_track",
                })
