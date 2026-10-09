# Financial Service API — Responses

Base URL: `http://<pi-ip>:8002`. All amounts are integer **paise**. All examples below are real responses captured live from the running service with seeded demo data.

## GET /health
```json
{ "status": "ok", "data_revision": 59 }
```

## GET /categories
```json
[
  { "id": "food_dining", "display_name": "Food & Dining", "sort_order": 1, "seed_version": 1 },
  { "id": "groceries", "display_name": "Groceries", "sort_order": 2, "seed_version": 1 },
  { "id": "transport", "display_name": "Transport", "sort_order": 3, "seed_version": 1 },
  { "id": "rent_utilities", "display_name": "Rent & Utilities", "sort_order": 4, "seed_version": 1 },
  { "id": "shopping", "display_name": "Shopping", "sort_order": 5, "seed_version": 1 },
  { "id": "health_fitness", "display_name": "Health & Fitness", "sort_order": 6, "seed_version": 1 },
  { "id": "education", "display_name": "Education", "sort_order": 7, "seed_version": 1 },
  { "id": "entertainment", "display_name": "Entertainment", "sort_order": 8, "seed_version": 1 },
  { "id": "subscriptions", "display_name": "Digital Subscriptions", "sort_order": 9, "seed_version": 1 },
  { "id": "travel", "display_name": "Travel", "sort_order": 10, "seed_version": 1 },
  { "id": "fees_charges", "display_name": "Fees & Charges", "sort_order": 11, "seed_version": 1 },
  { "id": "other", "display_name": "Other", "sort_order": 12, "seed_version": 1 }
]
```

## GET /accounts
```json
[
  { "id": "acct-3c60...", "display_name": "HDFC Salary Account", "type": "bank", "currency": "INR", "last4": "4821", "revision": 1, "archived_at": null },
  { "id": "acct-7411...", "display_name": "HDFC Regalia Credit Card", "type": "credit_card", "currency": "INR", "last4": "9034", "revision": 1, "archived_at": null }
]
```
`POST /accounts`, `GET /accounts/{id}`, `PATCH /accounts/{id}` all return this same object shape.

## POST/GET /transactions
```json
{
  "items": [
    {
      "id": "txn-532e...", "account_id": "acct-7411...", "merchant": "Cult.fit", "merchant_key": "cult.fit",
      "description": null, "amount_paise": 99900, "currency": "INR", "posted_date": "2026-10-10",
      "transaction_date": null, "type": "expense", "category_id": "health_fitness",
      "original_transaction_id": null, "source": "typed", "source_import_id": null,
      "category_origin": "user", "needs_review": 0, "revision": 1, "deleted_at": null
    }
  ],
  "next_cursor": "MjAyNi0xMC0wOHx0eG4tYzY2ZjA3YzgtZDg0OC00OTVlLWE4ZDktZTljNDU1MWQ2MDU2",
  "data_revision": 59
}
```
`GET /transactions/{id}`, `PATCH /transactions/{id}` return one item object (no envelope). `DELETE` returns `204`.

## POST /entries/parse
```json
{
  "drafts": [
    {
      "draft_id": "draft-0", "status": "ready", "source": "voice", "merchant": "Swiggy",
      "amount_paise": 22000, "currency": "INR", "posted_date": "2026-10-09", "type": "expense",
      "category_id": "food_dining", "category_origin": "rule", "account_id": null,
      "needs_review": false, "missing_fields": [], "warnings": []
    },
    {
      "draft_id": "draft-1", "status": "ready", "source": "voice", "merchant": "Ola",
      "amount_paise": 18000, "currency": "INR", "posted_date": "2026-10-09", "type": "expense",
      "category_id": "transport", "category_origin": "rule", "account_id": null,
      "needs_review": false, "missing_fields": [], "warnings": []
    }
  ]
}
```

## GET /budgets
```json
[ { "id": "budget-fa94...", "category_id": "food_dining", "amount_paise": 800000, "revision": 1 } ]
```

## GET /budgets/status
```json
[
  { "category_id": "food_dining", "amount_paise": 800000, "spent_paise": 699999, "left_paise": 100001, "usage_percent": 87.5, "per_day_paise": 4545, "days_left": 22, "expense_count": 7 },
  { "category_id": "subscriptions", "amount_paise": 100000, "spent_paise": 76800, "left_paise": 23200, "usage_percent": 76.8, "per_day_paise": 1054, "days_left": 22, "expense_count": 2 }
]
```

## GET /goals
```json
[
  {
    "id": "goal-55f8...", "name": "Emergency Fund", "emoji": "🛟", "target_paise": 10000000,
    "start_date": "2026-04-10", "due_date": "2027-04-10", "archived": 0, "revision": 1,
    "saved_paise": 1500000, "progress_percent": 15.0, "expected_paise": 5013698,
    "on_track": false, "need_per_month_paise": 1416666, "months_left": 6
  }
]
```

## GET /loans
```json
[
  {
    "id": "loan-0cf1...", "name": "iPhone 15", "emoji": "📱", "lender": "HDFC", "kind": "no_cost_emi",
    "emi_paise": 833300, "start_date": "2026-06-10", "months": 12, "due_day": 5, "source": "card",
    "archived": 0, "revision": 1, "current_installment": 5, "paid_installments": 4,
    "done": false, "remaining_paise": 6666400, "end_date": "2027-05-05"
  }
]
```

## GET /recurring
```json
[
  { "merchant": "Spotify", "merchant_key": "spotify", "category_id": "subscriptions", "amount_paise": 11900, "typical_day": 3, "months_present": 3 },
  { "merchant": "Netflix", "merchant_key": "netflix", "category_id": "subscriptions", "amount_paise": 64900, "typical_day": 6, "months_present": 3 }
]
```
Field is `months_present` (how many of the last 3 full months this merchant appeared in) — **not** `occurrences`.

## GET /forecast
```json
{
  "ok": true, "category_id": null, "actual_paise": 1461700, "projected_paise": 4531270,
  "elapsed_days": 10, "days_in_month": 31, "budget_paise": null, "over_budget": false,
  "expense_count": 19,
  "daily_spending": [ { "posted_date": "2026-10-01", "total_paise": 460000, "count": 6 } ],
  "calculation": "₹14,617 ÷ 10 days × 31 days = ₹45,312.70"
}
```
Real fields are `days_in_month` / `daily_spending[].posted_date` / `daily_spending[].total_paise` / `calculation` — not `total_days` / `date` / `amount_paise` / `daily_average_paise`.

## GET /advice
```json
[
  { "id": "budget-food_dining", "tone": "warn", "title": "food_dining: 88% of budget used", "body": "₹6,999.99 of ₹8,000 spent, with 22 days left.", "category_id": "food_dining", "kind": "budget_usage" },
  { "id": "forecast-shopping", "tone": "warn", "title": "shopping may go over budget", "body": "At this pace you'd reach about ₹8,060 by end of month, around ₹2,060 above your ₹6,000 limit.", "category_id": "shopping", "kind": "forecast_over" },
  { "id": "compare-food", "tone": "warn", "title": "Food spending is ₹5,500 higher than this time last month", "body": "₹9,000 so far vs ₹3,500 for the same days of last month.", "category_ids": ["food_dining","groceries"], "kind": "month_comparison" },
  { "id": "goal-goal-55f8...", "tone": "warn", "title": "Emergency Fund is a little behind", "body": "₹15,000 saved so far. About ₹14,166.66/month would reach ₹100,000 by the due date.", "goal_id": "goal-55f8...", "kind": "goal_behind" },
  { "id": "goal-ok-goal-5d91...", "tone": "good", "title": "New Laptop is on track", "body": "₹22,000 of ₹60,000 saved.", "goal_id": "goal-5d91...", "kind": "goal_on_track" }
]
```
Real fields are `id` / `tone` (`good`|`warn`|`bad`) / `title` / `body` / `kind` / `category_id` or `category_ids` or `goal_id` — **not** `type` / `severity` / `message` / `detail`.

## GET /summary
```json
{
  "start_date": "2026-10-01", "end_date_exclusive": "2026-10-11",
  "gross_expense_paise": 1461700, "refund_paise": 0, "net_spending_paise": 1461700,
  "income_paise": 6500000, "expense_count": 19, "matched_count": 20,
  "category_breakdown": [ { "category_id": "food_dining", "gross_expense_paise": 699999, "count": 7 } ],
  "review_count": 0, "data_revision": 59
}
```

## GET /comparisons
```json
{
  "current_period": { "label": "this month to date", "net_spending_paise": 1461700, "expense_count": 19 },
  "previous_period": { "label": "same elapsed days last month", "net_spending_paise": 547700, "expense_count": 6 },
  "delta_net_paise": 914000, "change_percent": "166.9",
  "category_comparison": [
    { "category_id": "food_dining", "current_paise": 699999, "previous_paise": 220000, "delta_paise": 479999, "change_percent": "218.2" },
    { "category_id": "shopping", "current_paise": 260000, "previous_paise": 0, "delta_paise": 260000, "change_percent": null }
  ]
}
```
`change_percent` is `null` when the previous value was 0.

## POST /questions
Envelope is always `{status, operation, answer, ...operation-specific fields}`.

```json
// total_spending
{ "status": "completed", "operation": "total_spending", "category_ids": ["food_dining"],
  "gross_expense_paise": 699999, "net_spending_paise": 699999, "expense_count": 7,
  "answer": "Your recorded food_dining spending for this month to date was ₹6,999.99." }

// category_breakdown
{ "status": "completed", "operation": "category_breakdown",
  "breakdown": [ { "category_id": "food_dining", "gross_expense_paise": 699999, "count": 7 } ],
  "answer": "Your top spending category for this month to date was food_dining at ₹6,999.99." }

// merchant_breakdown
{ "status": "completed", "operation": "merchant_breakdown",
  "breakdown": [ { "merchant": "Swiggy", "gross_expense_paise": 420000, "count": 4 } ],
  "answer": "Your top merchant for this month to date was Swiggy at ₹4,200." }

// biggest_expenses
{ "status": "completed", "operation": "biggest_expenses",
  "items": [ { "id": "txn-...", "merchant": "Myntra", "amount_paise": 130000, "posted_date": "2026-10-06", "...": "full Transaction object" } ],
  "answer": "Your biggest expense for this month to date was ₹1,300 at Myntra on 2026-10-06." }

// busiest_day
{ "status": "completed", "operation": "busiest_day",
  "day": { "posted_date": "2026-10-01", "gross_expense_paise": 460000, "count": 6 },
  "answer": "Your highest-spending day in this month to date was 2026-10-01 at ₹4,600 across 6 expenses." }

// emi_info
{ "status": "completed", "operation": "emi_info",
  "loans": [ { "name": "iPhone 15", "lender": "HDFC", "kind": "no_cost_emi", "emi_paise": 833300, "paid": 4, "total": 12, "remaining_paise": 6666400 } ],
  "total_emi_paise": 1283300, "total_remaining_paise": 18366400,
  "answer": "₹12,833 a month across 2 EMIs. About ₹183,664 still left to pay in total." }
// NOTE: loan fields here are "paid"/"total" (not paid_installments/months as in GET /loans)

// recurring_info
{ "status": "completed", "operation": "recurring_info",
  "recurring": [ { "merchant": "Spotify", "category_id": "subscriptions", "amount_paise": 11900, "typical_day": 3 } ],
  "recurring_total_paise": 475600, "emi_total_paise": 1283300, "emi_count": 2, "grand_total_paise": 1758900,
  "answer": "₹17,589 committed every month: ₹4,756 in 7 recurring payments plus ₹12,833 in 2 EMIs." }

// transaction_count / average_spending / compare_periods / budget_remaining /
// hypothetical_spend / list_transactions / unsupported — same {status, operation, answer, ...}
// envelope; see docs/financial_api.md in the repo for their exact extra fields.
```

## Errors (all endpoints)
```json
{ "detail": "not found" }
```
```json
{ "detail": "revision mismatch" }
```
```json
{ "detail": { "code": "duplicate_file", "existing_import_id": "import-..." } }
```
