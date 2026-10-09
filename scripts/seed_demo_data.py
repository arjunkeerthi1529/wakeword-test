"""Wipe the financial service's SQLite database and reseed it with a
realistic, demo-friendly dataset -- 3 months of transaction history, budgets
tuned to actually trigger GET /advice, two savings goals (one behind pace,
one on track), and two EMIs.

Dates are computed relative to *today*, not hardcoded, so the demo always
looks current regardless of which day it's run on. Writes go through the
same service layer src/financial/server.py uses (not raw SQL), so every
value is validated exactly as the API would validate it.

Run from the project root (after `pip install -r requirements-agents.txt`):
    python -m scripts.seed_demo_data
or, with the venv:
    .venv/Scripts/python.exe -m scripts.seed_demo_data

The financial service must NOT be running while this executes (it holds its
own connection to the same SQLite file) -- stop `python -m src.financial`
first, run this, then start the service again.
"""
from __future__ import annotations

import calendar
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.financial.categories import CATEGORIES, CATEGORY_SEED_VERSION
from src.financial.config import get_financial_config
from src.financial.db import FinanceRepository, get_conn, init_schema
from src.financial.services import (
    AccountService, AdviceService, BudgetService, ForecastService,
    GoalService, LoanService, TransactionService,
)


def add_months(d: date, delta: int) -> date:
    m = d.month - 1 + delta
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def P(rupees: float) -> int:
    """Rupees -> integer paise."""
    return round(rupees * 100)


def main() -> None:
    cfg = get_financial_config()
    db_path = cfg.db_path

    if os.path.exists(db_path):
        os.remove(db_path)
        print(f"Deleted existing database: {db_path}")
    for ext in ("-wal", "-shm"):
        side = db_path + ext
        if os.path.exists(side):
            os.remove(side)

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = get_conn(db_path)
    init_schema(conn)
    repo = FinanceRepository(conn)
    repo.seed_categories(CATEGORIES, CATEGORY_SEED_VERSION)
    print(f"Fresh schema created at: {db_path}")

    accounts = AccountService(repo)
    transactions = TransactionService(repo)
    budgets = BudgetService(repo)
    goals = GoalService(repo)
    loans = LoanService(repo)

    today = date.today()
    this_month_start = today.replace(day=1)
    m1_start = add_months(this_month_start, -1)   # last full month
    m2_start = add_months(this_month_start, -2)   # 2 months ago
    m3_start = add_months(this_month_start, -3)   # 3 months ago

    # -- accounts -----------------------------------------------------------
    bank = accounts.create_account("HDFC Salary Account", "bank", "4821")
    card = accounts.create_account("HDFC Regalia Credit Card", "credit_card", "9034")
    bank_id, card_id = bank["id"], card["id"]
    print(f"Accounts: bank={bank_id} card={card_id}")

    txn_count = 0

    def spend(dt: date, merchant: str, rupees: float, category: str, account_id: str = None, card_: bool = False) -> None:
        nonlocal txn_count
        transactions.create_transaction({
            "merchant": merchant, "amount_paise": P(rupees), "posted_date": dt.isoformat(),
            "type": "expense", "category_id": category,
            "account_id": account_id or (card_id if card_ else bank_id), "source": "typed",
        })
        txn_count += 1

    def income(dt: date, merchant: str, rupees: float) -> None:
        nonlocal txn_count
        transactions.create_transaction({
            "merchant": merchant, "amount_paise": P(rupees), "posted_date": dt.isoformat(),
            "type": "income", "category_id": None, "account_id": bank_id, "source": "typed",
        })
        txn_count += 1

    # -- recurring subscriptions: one hit per month in M1, M2, M3, same day/amount,
    #    so GET /recurring (needs >=2 of last 3 full months) always fires -------
    for m_start in (m1_start, m2_start, m3_start):
        spend(m_start.replace(day=6), "Netflix", 649, "subscriptions", card_=True)
        spend(m_start.replace(day=3), "Spotify", 119, "subscriptions", card_=True)
        spend(m_start.replace(day=10), "Cult.fit", 999, "health_fitness", card_=True)

    # -- salary, every month including this one ------------------------------
    for m_start in (m3_start, m2_start, m1_start, this_month_start):
        income(m_start.replace(day=1), "Salary", 65000)

    # -- M3 / M2: light variety, categories kept OUT of the Food comparison
    #    group (food_dining/groceries) so they never skew the spending_spike
    #    comparison below --------------------------------------------------
    for m_start in (m3_start, m2_start):
        spend(m_start.replace(day=4), "Ola", 180, "transport")
        spend(m_start.replace(day=12), "Uber", 240, "transport")
        spend(m_start.replace(day=18), "Myntra", 1400, "shopping")
        spend(m_start.replace(day=22), "PVR Cinemas", 450, "entertainment")
        spend(m_start.replace(day=27), "Decathlon", 899, "shopping")

    # -- M1 (last full month): the comparison baseline. ALL of last month's
    #    food_dining + groceries spend is anchored on day 1, so it always
    #    falls inside "same elapsed days as this month" no matter what day
    #    of the month this script is run on (even day 1) -------------------
    spend(m1_start.replace(day=1), "Swiggy", 2200, "food_dining")
    spend(m1_start.replace(day=1), "Big Bazaar", 1300, "groceries")
    # total: food_dining 2200 + groceries 1300 = 3500 -> "then_val"
    spend(m1_start.replace(day=8), "Ola", 210, "transport")
    spend(m1_start.replace(day=14), "Amazon", 1600, "shopping")
    spend(m1_start.replace(day=19), "BookMyShow", 380, "entertainment")
    spend(m1_start.replace(day=25), "Rapido", 150, "transport")

    # -- this month, up to today: food+groceries deliberately ~2.6x last
    #    month's baseline (>=25% and >=Rs500 jump) to trigger spending_spike;
    #    plus budgeted categories tuned against the limits set below --------
    cur_days = max(1, (today - this_month_start).days + 1)

    def spread(total_rupees: float, merchant: str, category: str, n: int, card_: bool = False) -> None:
        """Split a target monthly total across n transactions dated between
        the 1st and today, so the total lands exactly on the target
        regardless of how many days have elapsed."""
        per = total_rupees / n
        for i in range(n):
            day_offset = min(cur_days - 1, int(i * cur_days / n))
            spend(this_month_start + timedelta(days=day_offset), merchant, per, category, card_=card_)

    spread(4200, "Swiggy", "food_dining", 4)
    spread(2800, "Zomato", "food_dining", 3)
    spread(2000, "Zepto", "groceries", 3)
    # food_dining 4200+2800=7000 + groceries 2000 = 9000 vs last month's 3500
    # ratio 2.57x, delta Rs5,500 -> spending_spike on "Food"

    spread(2600, "Myntra", "shopping", 2)
    spread(900, "Ola", "transport", 3)
    spread(350, "PVR Cinemas", "entertainment", 1)

    # this month's own recurring charges (for realism; /recurring only looks
    # at the 3 PRIOR full months, so these don't affect that detection)
    if today.day >= 6:
        spend(this_month_start.replace(day=6), "Netflix", 649, "subscriptions", card_=True)
    if today.day >= 3:
        spend(this_month_start.replace(day=3), "Spotify", 119, "subscriptions", card_=True)
    if today.day >= 10:
        spend(this_month_start.replace(day=10), "Cult.fit", 999, "health_fitness", card_=True)
    # subscriptions so far this month: Netflix 649 + Spotify 119 (+ Cult.fit
    # is health_fitness, not subscriptions) = up to 768, intentionally over
    # the Rs1,000 budget once both have posted this month

    print(f"Seeded {txn_count} transactions across {m3_start.isoformat()} .. {today.isoformat()}")

    # -- budgets: tuned to produce one bad, one warn, one forecast-only, and
    #    one healthy category on GET /advice -------------------------------
    budgets.upsert("food_dining", P(8000))       # ~87% used -> budget_warning (warn)
    budgets.upsert("subscriptions", P(1000))      # ~150%+ used -> budget_warning (bad)
    budgets.upsert("shopping", P(6000))           # ~43% used, but pace projects over -> forecast_over_budget
    budgets.upsert("transport", P(3000))          # ~30% used, healthy, no nudge
    print("Budgets set: food_dining=8000 subscriptions=1000 shopping=6000 transport=3000")

    # -- goals: one behind pace, one on track --------------------------------
    ef_start = add_months(today, -6)
    ef_due = add_months(today, 6)
    emergency_fund = goals.create("Emergency Fund", "🛟", P(100000), ef_start.isoformat(), ef_due.isoformat())
    goals.add_contribution(emergency_fund["id"], P(15000), add_months(today, -4).isoformat(), "initial deposit")
    # ~6 months in of a 12-month plan (50% expected) but only 15% saved -> goal_behind

    laptop_start = add_months(today, -2)
    laptop_due = add_months(today, 4)
    laptop_fund = goals.create("New Laptop", "💻", P(60000), laptop_start.isoformat(), laptop_due.isoformat())
    goals.add_contribution(laptop_fund["id"], P(14000), add_months(today, -1).isoformat(), "month 1")
    goals.add_contribution(laptop_fund["id"], P(8000), today.isoformat(), "month 2")
    # ~2 months in of a 6-month plan (33% expected), 22000/60000 = 36.7% saved -> on_track
    print("Goals: Emergency Fund (behind pace), New Laptop (on track)")

    # -- loans / EMIs ---------------------------------------------------------
    loans.create({
        "name": "iPhone 15", "emoji": "📱", "lender": "HDFC", "kind": "no_cost_emi",
        "emi_paise": P(8333), "start_date": add_months(today, -4).isoformat(),
        "months": 12, "due_day": 5, "source": "card",
    })
    loans.create({
        "name": "Bike Loan", "emoji": "🏍️", "lender": "Bajaj Finance", "kind": "loan_emi",
        "emi_paise": P(4500), "start_date": add_months(today, -10).isoformat(),
        "months": 36, "due_day": 10, "source": "bank",
    })
    print("Loans: iPhone 15 EMI, Bike Loan EMI")

    # -- self-check: print what GET /advice will actually return ------------
    forecast_svc = ForecastService(repo)
    advice_svc = AdviceService(repo, budgets, goals, forecast_svc)
    print("\n--- GET /advice preview ---")
    for a in advice_svc.generate(today):
        print(f"[{a['tone']:>4}] {a['title']} -- {a['body']}")

    conn.close()
    print("\nDone. Start the service normally: python -m src.financial")


if __name__ == "__main__":
    main()
