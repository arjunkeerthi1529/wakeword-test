# Financial Service API (v1)

Expense tracker with quick-add, bank statement import, and a natural-language Ask engine. Runs as its own
process, independent of the voice assistant, Scam Guard, and the Work service — all data stays in a local
SQLite file on the Raspberry Pi.

- **Base URL:** `http://<pi-ip>:8002` (port is `FINANCIAL_PORT` in `.env`, default `8002`)
- **Interactive docs:** `GET /docs` · OpenAPI spec: `GET /openapi.json`
- **Auth:** none. Use only on a private Wi-Fi or hotspot. CORS is wide open (`*`).
- **Format:** JSON, UTF-8.
- **Currency:** INR only (enforced). All amounts are **integer paise** (1 INR = 100 paise) — never floats.
  Display formatting (e.g. `₹1,250.50`) is the UI's responsibility.

Start the service: `python -m src.financial`

---

## Design principles the UI must respect

1. **The LLM never does arithmetic.** Every number the UI shows must come from the API's deterministic
   response, not from the model's text. The model only ever extracts/interprets — it never computes a total,
   invents a date, or writes SQL.
2. **Amounts are integer paise.** `amount_paise: 22000` means ₹220.00. The API never returns floats for money.
   The UI must format paise → display string (e.g. `(paise / 100).toFixed(2)`).
3. **Categories are locked.** There are exactly 12 categories (see below). No create/rename/delete endpoint
   exists. If the UI shows a category picker, it must use `GET /categories` as the source of truth.
4. **Revision-checked mutations.** Every `PATCH` requires `expected_revision` (the `revision` field from the
   last `GET`). If someone else changed the record, the API returns `409` — the UI should re-fetch and retry
   or show a conflict message.
5. **Blocking endpoints.** `POST /entries/parse`, `POST /imports`, and `POST /questions` each block until their
   LLM work finishes (typically 2–15 seconds). Show a spinner; don't fire multiple in parallel — they compete
   for the same local model.

---

## Categories

12 locked categories, returned by `GET /categories`:

| `id` | `display_name` | `sort_order` |
|---|---|---:|
| `food_dining` | Food & Dining | 1 |
| `groceries` | Groceries | 2 |
| `transport` | Transport | 3 |
| `rent_utilities` | Rent & Utilities | 4 |
| `shopping` | Shopping | 5 |
| `health_fitness` | Health & Fitness | 6 |
| `education` | Education | 7 |
| `entertainment` | Entertainment | 8 |
| `subscriptions` | Digital Subscriptions | 9 |
| `travel` | Travel | 10 |
| `fees_charges` | Fees & Charges | 11 |
| `other` | Other | 12 |

A category of `other` almost always means "needs human review" — the system couldn't confidently classify it.

---

## Objects

### Account

| Field | Type | Notes |
|---|---|---|
| `id` | string | `acct-<uuid>` |
| `display_name` | string | user-chosen name |
| `type` | `"bank"` \| `"credit_card"` \| `"cash"` | |
| `currency` | `"INR"` | always INR |
| `last4` | string \| null | last 4 digits of card/account, optional |
| `revision` | int | for optimistic concurrency — pass in `expected_revision` on PATCH |
| `created_at` | string | ISO-8601 |
| `updated_at` | string | ISO-8601 |
| `archived_at` | string \| null | non-null means archived |

### Transaction

| Field | Type | Notes |
|---|---|---|
| `id` | string | `txn-<uuid>` |
| `account_id` | string \| null | linked account (optional) |
| `merchant` | string \| null | display name of payee |
| `merchant_key` | string \| null | normalized (lowercase, NFKC) — used for rule matching, not display |
| `description` | string \| null | |
| `amount_paise` | int | always positive, always > 0 |
| `currency` | `"INR"` | |
| `posted_date` | string | ISO `YYYY-MM-DD` |
| `transaction_date` | string \| null | optional, if different from posted |
| `type` | `"expense"` \| `"refund"` \| `"income"` \| `"transfer"` | |
| `category_id` | string \| null | one of the 12 IDs above; **required** for expense/refund, **null** for income/transfer |
| `original_transaction_id` | string \| null | for refunds: links to the original expense |
| `source` | `"typed"` \| `"voice"` \| `"import"` | how this transaction was created |
| `source_import_id` | string \| null | which import created it, if any |
| `category_origin` | `"user"` \| `"rule"` \| `"llm"` \| `"fallback"` | who assigned the category |
| `needs_review` | int | `0` or `1` — `1` means the category assignment is uncertain |
| `revision` | int | for PATCH |
| `created_at` | string | |
| `updated_at` | string | |
| `deleted_at` | string \| null | soft-deleted if non-null; excluded from all listings |

### Import

| Field | Type | Notes |
|---|---|---|
| `id` | string | `import-<uuid>` |
| `account_id` | string | |
| `original_filename` | string | |
| `sha256` | string | file hash, used for duplicate detection |
| `size_bytes` | int | |
| `state` | string | see Import States below |
| `parser_id` | string \| null | which parser was used |
| `parser_version` | string \| null | |
| `row_count` | int | total rows extracted |
| `issue_count` | int | rows needing attention |
| `error_code` | string \| null | set when `state == "failed"` |
| `created_at` | string | |
| `committed_at` | string \| null | when the import was committed to transactions |

**Import states:** `queued` → `extracting` → `categorizing` → `review_ready` → `committing` → `committed`.
Can also reach `failed` or `cancelled` from most states.

The UI only ever sees `review_ready` (returned by `POST /imports`), `committed` (after confirm), `failed`,
or `cancelled`. The intermediate states happen inside the blocking `POST /imports` call.

### Import Row

| Field | Type | Notes |
|---|---|---|
| `id` | string | `introw-<uuid>` |
| `import_id` | string | |
| `ordinal` | int | position in the original file (1-based) |
| `raw_description` | string | the description text from the statement |
| `posted_date` | string \| null | |
| `amount_paise` | int | |
| `currency` | `"INR"` | |
| `type` | `"expense"` \| `"refund"` \| `"income"` \| `"transfer"` | |
| `reference` | string \| null | |
| `category_id` | string \| null | |
| `category_origin` | string \| null | |
| `needs_review` | int | `0` or `1` |
| `decision` | `"unresolved"` \| `"include"` \| `"exclude"` \| `"link_existing"` | |
| `duplicate_candidate_transaction_id` | string \| null | if the system found a possible duplicate |
| `linked_transaction_id` | string \| null | set when decision is `link_existing` |
| `source_locator` | string | e.g. `"row 5"` or `"page 2 line 14"` |
| `issue_codes` | string[] | row-level problems (see Issue Codes below) |

### Question Answer

| Field | Type | Notes |
|---|---|---|
| `status` | `"completed"` \| `"needs_clarification"` | |
| `operation` | string | the resolved operation (see Ask Operations below) |
| `answer` | string | human-readable sentence the UI can display directly |
| `clarification` | string | present when `status == "needs_clarification"` |
| *(varies)* | | each operation returns additional structured data (see below) |

---

## Endpoints

### Health & Categories

#### `GET /health`

```json
{ "status": "ok", "data_revision": 42 }
```

`data_revision` increments on every write — the UI can poll this cheaply to know when to refresh.

#### `GET /categories`

Returns `Category[]` sorted by `sort_order`.

```json
[
  { "id": "food_dining", "display_name": "Food & Dining", "sort_order": 1, "seed_version": 1 },
  ...
]
```

---

### Accounts

#### `POST /accounts`

Create a new account.

| Field | Type | Required | Notes |
|---|---|---|---|
| `display_name` | string | yes | |
| `type` | `"bank"` \| `"credit_card"` \| `"cash"` | yes | |
| `last4` | string | no | last 4 digits |

```bash
curl -X POST http://<pi-ip>:8002/accounts \
  -H 'Content-Type: application/json' \
  -d '{"display_name": "HDFC Savings", "type": "bank", "last4": "4321"}'
```

`201` — returns the created `Account`.

`422` — invalid type or empty display_name.

#### `GET /accounts`

List all accounts. Optional: `?include_archived=true` to include archived ones.

Returns `Account[]`.

#### `GET /accounts/{account_id}`

Returns one `Account`. `404` if not found.

#### `PATCH /accounts/{account_id}`

| Field | Type | Required | Notes |
|---|---|---|---|
| `expected_revision` | int | yes | must match current `revision` |
| `display_name` | string | no | |
| `archived` | bool | no | `true` to archive, `false` to un-archive |

`200` — returns the updated `Account`.

`404` — not found. `409` — revision mismatch (someone else changed it).

---

### Transactions

#### `POST /transactions`

Create a transaction manually. This is the only endpoint that writes a transaction — `POST /entries/parse`
only returns drafts, never writes.

| Field | Type | Required | Notes |
|---|---|---|---|
| `merchant` | string | no | |
| `description` | string | no | |
| `amount_paise` | int | * | positive integer; provide this OR `amount_text` |
| `amount_text` | string | * | e.g. `"220.50"` — parsed to paise; ignored if `amount_paise` is set |
| `currency` | `"INR"` | no | defaults to `"INR"` |
| `posted_date` | string | yes | ISO `YYYY-MM-DD` |
| `transaction_date` | string | no | |
| `type` | `"expense"` \| `"refund"` \| `"income"` \| `"transfer"` | yes | |
| `category_id` | string | * | **required** for expense/refund, **must be null** for income/transfer |
| `account_id` | string | no | |
| `original_transaction_id` | string | no | for refunds: the original expense's id |
| `source` | `"typed"` \| `"voice"` | no | defaults to `"typed"` |
| `remember_category` | bool | no | if `true` and merchant+category are set, saves a merchant→category rule for future auto-categorization |

```bash
curl -X POST http://<pi-ip>:8002/transactions \
  -H 'Content-Type: application/json' \
  -d '{
    "merchant": "Swiggy",
    "amount_paise": 22000,
    "posted_date": "2026-10-10",
    "type": "expense",
    "category_id": "food_dining",
    "remember_category": true
  }'
```

`201` — returns the created `Transaction`.

`422` — validation errors (missing required fields, invalid category, amount <= 0, etc.).

#### `GET /transactions`

Paginated, newest first.

| Param | Type | Notes |
|---|---|---|
| `start_date` | string | inclusive, ISO `YYYY-MM-DD` |
| `end_date_exclusive` | string | exclusive |
| `category_ids` | string | comma-separated: `"food_dining,transport"` |
| `account_ids` | string | comma-separated |
| `type` | string | `"expense"`, `"refund"`, `"income"`, or `"transfer"` |
| `needs_review` | bool | `true` to show only items needing review |
| `limit` | int | default 50, max 200 |
| `cursor` | string | opaque cursor from a previous response's `next_cursor` |

```json
{
  "items": [ /* Transaction[] */ ],
  "next_cursor": "MjAyNi0xMC0xMHx0eG4tYWJjZGVm",
  "data_revision": 42
}
```

`next_cursor` is `null` when there are no more pages. Pass it as `?cursor=...` to get the next page.

#### `GET /transactions/{transaction_id}`

Returns one `Transaction`. `404` if not found (or soft-deleted).

#### `PATCH /transactions/{transaction_id}`

| Field | Type | Required | Notes |
|---|---|---|---|
| `expected_revision` | int | yes | |
| `merchant` | string | no | |
| `description` | string | no | |
| `amount_paise` | int | no | must be > 0 |
| `posted_date` | string | no | |
| `transaction_date` | string | no | |
| `category_id` | string | no | must be a valid category ID |
| `account_id` | string | no | |
| `remember_category` | bool | no | save merchant→category rule |

`200` — returns the updated `Transaction`.

`404` — not found. `409` — revision mismatch. `422` — invalid data.

#### `DELETE /transactions/{transaction_id}?expected_revision={n}`

Soft-deletes a transaction. `expected_revision` is a **query parameter**.

`204` — no content. `404` — not found. `409` — revision mismatch.

---

### Quick-Add (Entry Parsing)

#### `POST /entries/parse`

Send natural-language text (typed or from voice transcription), get back structured expense drafts. **Blocks
on one LLM call** (2–10 seconds). **Never writes a transaction** — the UI must let the user review and
confirm, then call `POST /transactions` for each accepted draft.

| Field | Type | Required | Notes |
|---|---|---|---|
| `text` | string | yes | e.g. `"Swiggy 220 and Ola 180 yesterday"` |
| `source` | `"typed"` \| `"voice"` | no | defaults to `"typed"` |
| `account_id` | string | no | pre-fill for all drafts |

```bash
curl -X POST http://<pi-ip>:8002/entries/parse \
  -H 'Content-Type: application/json' \
  -d '{"text": "Swiggy 220 and Ola 180 yesterday", "source": "voice"}'
```

`200`
```json
{
  "drafts": [
    {
      "draft_id": "draft-0",
      "status": "ready",
      "source": "voice",
      "merchant": "Swiggy",
      "amount_paise": 22000,
      "currency": "INR",
      "posted_date": "2026-10-09",
      "type": "expense",
      "category_id": "food_dining",
      "category_origin": "rule",
      "account_id": null,
      "needs_review": false,
      "missing_fields": [],
      "warnings": []
    },
    {
      "draft_id": "draft-1",
      "status": "ready",
      "source": "voice",
      "merchant": "Ola",
      "amount_paise": 18000,
      "currency": "INR",
      "posted_date": "2026-10-09",
      "type": "expense",
      "category_id": "transport",
      "category_origin": "rule",
      "account_id": null,
      "needs_review": false,
      "missing_fields": [],
      "warnings": []
    }
  ]
}
```

**Draft fields the UI should handle:**

| Field | Meaning |
|---|---|
| `status` | `"ready"` = can be submitted as-is; `"needs_input"` = `missing_fields` lists what's still needed |
| `missing_fields` | `string[]` — e.g. `["amount", "posted_date"]` — fields the user must fill in |
| `needs_review` | `true` if the category was uncertain — show a category picker |
| `warnings` | `string[]` — e.g. `["model_ambiguous", "other_uncertain"]` — informational |
| `category_origin` | `"rule"` = confident auto-match; `"llm"` = model's suggestion; `"fallback"` = couldn't classify |

**UI flow:**
1. Show each draft as an editable card.
2. For `status: "needs_input"`, highlight the `missing_fields` and require the user to fill them.
3. For `needs_review: true`, show a category dropdown.
4. On confirm, `POST /transactions` with the draft's fields (plus any user edits).
5. Optionally pass `remember_category: true` to save the merchant→category rule for future auto-categorization.

---

### Statement Import

A three-step flow: upload → review → confirm (or cancel).

#### Supported file formats

| Format | Parser | Notes |
|---|---|---|
| `.csv` (canonical) | `csv_canonical` | Columns: `posted_date, description, amount, currency, type, reference` |
| `.csv` (bank-style) | `csv_mapped_debit_credit` | Columns: `Date, Description, Debit, Credit` (+ optional `Reference`) |
| `.pdf` (text-based) | `pdf_generic_single_line` or `pdf_generic_block` | Indian bank/credit-card statements with text-extractable tables |

Max file size: **25 MB**. Max rows: **10,000**.

Scanned/image-only PDFs and encrypted PDFs are rejected with an honest error code.

#### Step 1: `POST /imports`

Upload a statement file. **Multipart form data**, not JSON.

| Field | Type | Notes |
|---|---|---|
| `account_id` | string (form field) | which account this statement belongs to |
| `file` | file upload | `.csv` or `.pdf` |

```bash
curl -X POST http://<pi-ip>:8002/imports \
  -F "account_id=acct-abc123" \
  -F "file=@statement_oct.csv"
```

**Blocks until the import reaches `review_ready` or `failed`** — parsing is deterministic and fast; only
genuinely unrecognized merchants reach the LLM (in batches of 8).

`201` — returns the `Import` object with `state: "review_ready"`.

`409` — duplicate file (same account + same SHA-256 as an already-committed import):
```json
{ "detail": { "code": "duplicate_file", "existing_import_id": "import-xyz789" } }
```

`413` — parsing failed. `error_code` values:
- `file_resource_limit` — file too large or too many rows
- `csv_empty_file` — no header row
- `csv_unsupported_encoding` — not UTF-8
- `csv_unrecognized_columns` — columns don't match either supported layout
- `encrypted_pdf_unsupported` — password-protected PDF
- `scanned_pdf_unsupported` — image-only PDF (no extractable text)
- `unsupported_pdf_layout` — text extracted but no recognizable transaction pattern
- `unsupported_file_type` — not `.csv` or `.pdf`

#### Step 2: Review rows

##### `GET /imports/{import_id}`

Returns the `Import` object. Use to check `state`, `row_count`, `issue_count`.

##### `GET /imports/{import_id}/rows`

Returns `ImportRow[]` sorted by `ordinal`. The UI should render these as a review table.

**Row-level decision logic the UI must implement:**

| `decision` | Meaning | UI action |
|---|---|---|
| `"include"` | Will be committed as a new transaction | Default for clean rows; no action needed unless user disagrees |
| `"unresolved"` | Needs a human decision before commit | Show prominently; user must choose include/exclude/link |
| `"exclude"` | Will be skipped | User chose to skip this row |
| `"link_existing"` | Will be linked to an existing transaction (duplicate) | User must provide `linked_transaction_id` |

**When to flag a row for the user:**

| Condition | What to show |
|---|---|
| `decision == "unresolved"` | Decision buttons (include / exclude / link) |
| `needs_review == 1` | Category might be wrong — show a category picker |
| `duplicate_candidate_transaction_id != null` | "Possible duplicate of txn-xxx" — let user decide include/exclude/link |
| `issue_codes` contains items | Show issue badges (see Issue Codes below) |

##### `PATCH /imports/{import_id}/rows/{row_id}`

Update a single import row (resolve it, change category, fix date/amount, etc.).

| Field | Type | Notes |
|---|---|---|
| `decision` | `"include"` \| `"exclude"` \| `"link_existing"` | |
| `linked_transaction_id` | string | **required** when decision is `"link_existing"` |
| `category_id` | string | override the auto-assigned category |
| `raw_description` | string | edit the description |
| `posted_date` | string | fix the date |
| `amount_paise` | int | fix the amount |
| `type` | `"expense"` \| `"refund"` \| `"income"` \| `"transfer"` | |
| `reference` | string | |

Only include fields you're changing. `200` — returns the updated `ImportRow`.

#### Step 3: Confirm or cancel

##### `POST /imports/{import_id}/confirm`

Commits all `"include"` rows as new transactions and links all `"link_existing"` rows. **All `"unresolved"`
rows must be resolved first** — if any remain, returns `422`.

`200`
```json
{ "inserted": 45, "linked": 3, "excluded": 2 }
```

`409` — import is not in `review_ready` state.

`422` — unresolved rows remain:
```json
{ "detail": "3 row(s) still need a decision before this import can commit" }
```

##### `DELETE /imports/{import_id}`

Cancel an import (before or instead of confirming). No transactions are created.

`204` — no content. `409` — already committed (can't cancel).

#### Issue codes reference

Issue codes appear in `ImportRow.issue_codes[]`. They're row-level problems flagged during parsing — never
fatal to the file, but the UI should show them:

| Code | Meaning |
|---|---|
| `invalid_type` | The `type` column had an unrecognized value; defaulted to `"expense"` |
| `unsupported_currency` | Non-INR currency; forced to INR |
| `invalid_amount` | Amount couldn't be parsed; set to 0 |
| `missing_posted_date` | No date found for this row |
| `direction_assumed` | Debit/Credit CSV: the type was inferred from the column, not stated explicitly |
| `debit_and_credit_both_set` | Both Debit and Credit had values; amount set to 0 |
| `missing_amount` | Neither Debit nor Credit had a value |
| `credit_direction_assumed` | PDF: a credit was assumed to be refund or transfer based on keywords |

---

### Questions (Ask Engine)

#### `POST /questions`

Ask a free-text spending question. **Blocks on one LLM call** to interpret the question, then always answers
via a fixed parameterized SQL query — the model never computes a total or picks its own date range.

| Field | Type | Required |
|---|---|---|
| `text` | string | yes |

```bash
curl -X POST http://<pi-ip>:8002/questions \
  -H 'Content-Type: application/json' \
  -d '{"text": "How much did I spend on food this month?"}'
```

**Completed response:**
```json
{
  "status": "completed",
  "operation": "total_spending",
  "period": {
    "start_date": "2026-10-01",
    "end_date_exclusive": "2026-10-11",
    "as_of_date": "2026-10-10",
    "label": "this month to date"
  },
  "category_ids": ["food_dining"],
  "merchant_text": "",
  "gross_expense_paise": 450000,
  "refund_paise": 0,
  "net_spending_paise": 450000,
  "income_paise": 0,
  "expense_count": 12,
  "matched_count": 12,
  "answer": "Your recorded food_dining spending for this month to date was ₹4,500."
}
```

**Needs-clarification response:**
```json
{
  "status": "needs_clarification",
  "operation": "unsupported",
  "clarification": "EMIs aren't tracked in this build yet. I can answer spending totals, biggest expenses, breakdowns, counts, averages and month comparisons."
}
```

The `answer` field is a pre-formatted human-readable sentence — the UI can display it directly. The
structured fields (`gross_expense_paise`, `breakdown`, `items`, etc.) are also available for richer rendering
(charts, tables).

#### Supported operations

| Operation | Triggered by | Extra response fields |
|---|---|---|
| `total_spending` | "how much did I spend on X" | `gross_expense_paise`, `refund_paise`, `net_spending_paise`, `income_paise`, `expense_count` |
| `category_breakdown` | "where did my money go", "breakdown" | `breakdown[]` — `{category_id, gross_expense_paise, count}` |
| `merchant_breakdown` | "top merchants", "which shops" | `breakdown[]` — `{merchant, gross_expense_paise, count}` |
| `biggest_expenses` | "biggest expenses", "top 5 purchases" | `items[]` — full `Transaction` objects |
| `busiest_day` | "which day did I spend the most" | `day` — `{posted_date, gross_expense_paise, count}` |
| `transaction_count` | "how many times", "how often" | `count`, plus spending totals |
| `average_spending` | "average daily/monthly spend" | `average_paise`, `average_unit` (`"daily"` or `"monthly"`) |
| `compare_periods` | "compare with last month", "more or less" | `current_period`, `previous_period` (each with spending totals), `delta_net_paise` |

#### Period resolution

The model classifies the user's time reference into one of these `period_kind` values, and the backend
resolves it deterministically:

| `period_kind` | Resolved to |
|---|---|
| `today` | today only |
| `yesterday` | yesterday only |
| `this_month_to_date` | 1st of current month through today (inclusive) |
| `last_month` | full previous calendar month |
| `last_n_days` | today minus N days through today |
| `explicit_range` | user-stated start/end dates |

For `compare_periods`, the comparison is **this month to date vs. the same elapsed days of last month** (not
full-month-vs-full-month), handling month-length and year-boundary differences automatically.

---

### Analytics

#### `GET /summary`

Period summary with category breakdown.

| Param | Type | Required | Notes |
|---|---|---|---|
| `start_date` | string | yes | inclusive, ISO `YYYY-MM-DD` |
| `end_date_exclusive` | string | yes | exclusive |
| `account_ids` | string | no | comma-separated |

```json
{
  "start_date": "2026-10-01",
  "end_date_exclusive": "2026-10-11",
  "gross_expense_paise": 850000,
  "refund_paise": 15000,
  "net_spending_paise": 835000,
  "income_paise": 5000000,
  "expense_count": 28,
  "matched_count": 30,
  "category_breakdown": [
    { "category_id": "food_dining", "gross_expense_paise": 320000, "count": 12 },
    { "category_id": "transport", "gross_expense_paise": 180000, "count": 8 },
    ...
  ],
  "review_count": 3,
  "data_revision": 42
}
```

`review_count` is the number of transactions in this period that still have `needs_review: 1`.

#### `GET /comparisons`

This month to date vs. the same elapsed days of last month.

| Param | Type | Required | Notes |
|---|---|---|---|
| `category_ids` | string | no | comma-separated |
| `account_ids` | string | no | comma-separated |

```json
{
  "current_period": {
    "start_date": "2026-10-01",
    "end_date_exclusive": "2026-10-11",
    "as_of_date": "2026-10-10",
    "label": "this month to date",
    "gross_expense_paise": 850000,
    "refund_paise": 15000,
    "net_spending_paise": 835000,
    "income_paise": 0,
    "expense_count": 28,
    "matched_count": 28
  },
  "previous_period": {
    "start_date": "2026-09-01",
    "end_date_exclusive": "2026-09-11",
    "as_of_date": "2026-09-10",
    "label": "same elapsed days last month",
    "gross_expense_paise": 920000,
    "refund_paise": 0,
    "net_spending_paise": 920000,
    "income_paise": 0,
    "expense_count": 31,
    "matched_count": 31
  },
  "delta_net_paise": -85000,
  "change_percent": "-9.2"
}
```

`change_percent` is `null` when the previous period's net spending was zero or negative (a percentage is
meaningless in that case).

---

## Errors

| HTTP | When |
|---|---|
| 404 | Account, transaction, import, or import row not found |
| 409 | Revision mismatch (`PATCH` with stale `expected_revision`); duplicate import file; import not in valid state for action |
| 413 | Statement parsing failed (see error codes under POST /imports) |
| 422 | Validation error (missing required fields, invalid category, amount <= 0, non-INR currency, unresolved rows on confirm, etc.) |

All error responses are `{"detail": "<message>"}` except duplicate-file 409 which includes the existing import ID.

---

## Typical client flows

### Quick-add expense (voice or typed)

```text
1. POST /entries/parse  {"text": "Swiggy 220", "source": "voice"}
   → 200 {"drafts": [{status: "ready", merchant: "Swiggy", amount_paise: 22000, ...}]}
   (blocks 2-10s — show a spinner)

2. Show each draft as an editable card
   - "ready" drafts: show a Confirm button
   - "needs_input" drafts: highlight missing_fields, require user input
   - needs_review: show a category picker

3. User confirms → POST /transactions  {merchant, amount_paise, posted_date, type, category_id, ...}
   → 201 Transaction
   (optionally pass remember_category: true)
```

### Statement import

```text
1. POST /imports  (multipart: account_id + file)
   → 201 Import {state: "review_ready", row_count: 47, issue_count: 5}
   (blocks until parsing + categorization finish — show "Processing statement…")

2. GET /imports/{id}/rows
   → ImportRow[] — render as a review table

3. For each row:
   - Clean rows (decision: "include", needs_review: 0): auto-checked, no action needed
   - Flagged rows: show issue badges, category picker, include/exclude/link buttons
   - Duplicate candidates: "This looks like txn-xyz — link, include as new, or skip?"

4. User resolves all "unresolved" rows → PATCH /imports/{id}/rows/{row_id} for each change

5. POST /imports/{id}/confirm
   → 200 {inserted: 42, linked: 3, excluded: 2}

   OR: DELETE /imports/{id} to cancel
```

### Ask a question

```text
1. POST /questions  {"text": "How much did I spend on food this month?"}
   → 200 {status: "completed", answer: "Your recorded food_dining spending...", ...}
   (blocks 2-10s — show a spinner)

2. If status == "completed":
   - Display the `answer` string directly
   - Optionally render richer UI from the structured fields (breakdown chart, expense list, etc.)

3. If status == "needs_clarification":
   - Display the `clarification` string
   - Optionally suggest example questions the user can try
```

### Dashboard / summary view

```text
1. GET /summary?start_date=2026-10-01&end_date_exclusive=2026-10-11
   → spending totals + category breakdown for this month to date

2. GET /comparisons
   → this month vs last month comparison

3. GET /transactions?needs_review=true&limit=10
   → items needing the user's attention

4. Poll GET /health for data_revision changes to know when to refresh
```

---

## Limits and behaviour to know

- **INR only.** Any non-INR currency is rejected on create/import. The system never converts currencies.
- **Amounts are always positive integers** (paise). The `type` field determines direction (expense vs income vs
  refund), not the sign of the amount.
- **Soft deletes.** `DELETE /transactions` sets `deleted_at`, doesn't remove the row. Deleted transactions are
  excluded from all listings, summaries, and analytics.
- **Categorization precedence** (cheapest first): saved user merchant rule → seeded alias (~50 known Indian
  brands) → deterministic EMI detection (always `other` + needs_review) → per-import LLM cache → LLM batch
  (≤8 rows at a time). The UI should indicate the `category_origin` so users know how confident the assignment is.
- **One LLM at a time.** All three blocking endpoints (`/entries/parse`, `/imports`, `/questions`) share the
  same local model. Firing multiple in parallel makes each slower. The UI should queue or disable concurrent
  requests.
- **No auth.** Designed for a private network only. Don't expose port 8002 to the internet.
