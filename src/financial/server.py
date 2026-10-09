"""Financial Service -- FastAPI REST server.

Expense tracker + statement import, as an independent process. Exact
integer-paise arithmetic; 12 locked categories; the LLM only ever
interprets/extracts/categorizes/drafts -- every number shown anywhere comes
from deterministic SQL, never the model.

Endpoints
---------
GET    /health
GET    /categories
POST   /accounts                      GET /accounts              GET /accounts/{id}      PATCH /accounts/{id}
POST   /transactions                  GET /transactions          GET /transactions/{id}  PATCH /transactions/{id}  DELETE /transactions/{id}
POST   /entries/parse                 quick-add text -> {"drafts": [...]} (blocks on one LLM call)
POST   /imports                       multipart upload -> staged ImportOut (blocks until review_ready/failed)
GET    /imports/{id}                  GET /imports/{id}/rows     PATCH /imports/{id}/rows/{row_id}
POST   /imports/{id}/confirm          DELETE /imports/{id}
POST   /questions                     free-text question -> answer (blocks on one LLM call)
GET    /summary                       GET /comparisons
"""
from __future__ import annotations

import json
import logging
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import entries, imports_service, questions
from .categories import CATEGORIES, CATEGORY_SEED_VERSION
from .config import get_financial_config
from .db import FinanceRepository, get_conn, init_schema
from .errors import (
    DuplicateFileError,
    InvalidImportStateError,
    NotFoundError,
    RevisionConflictError,
    UnresolvedRowsError,
    ValidationError,
)
from .parsers import ParseError
from .periods import resolve_month_to_date_comparison, today_in
from .services import AccountService, AnalyticsService, TransactionService

logger = logging.getLogger(__name__)

app = FastAPI(title="Financial Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_repo: FinanceRepository | None = None
_accounts: AccountService | None = None
_transactions: TransactionService | None = None
_analytics: AnalyticsService | None = None
_imports: imports_service.ImportService | None = None
_cfg = None


def set_state(*, repo: FinanceRepository, cfg) -> None:
    global _repo, _accounts, _transactions, _analytics, _imports, _cfg
    _repo = repo
    _cfg = cfg
    _accounts = AccountService(repo)
    _transactions = TransactionService(repo)
    _analytics = AnalyticsService(repo)
    _imports = imports_service.ImportService(repo, llm_base_url=cfg.llm_base_url, llm_model=cfg.llm_model)


def create_app_state() -> None:
    """Convenience for __main__.py: build config, open the DB, seed
    categories, and wire everything into this module's state."""
    cfg = get_financial_config()
    conn = get_conn(cfg.db_path)
    init_schema(conn)
    repo = FinanceRepository(conn)
    repo.seed_categories(CATEGORIES, CATEGORY_SEED_VERSION)
    recovered = repo.recover_interrupted_imports()
    if recovered:
        logger.info("Recovered %d interrupted import(s) to failed state", recovered)
    set_state(repo=repo, cfg=cfg)


# -- domain error -> HTTP mapping (centralized, not per-route) ------------------

@app.exception_handler(NotFoundError)
def _not_found(_, exc: NotFoundError):
    return _json_error(404, str(exc))


@app.exception_handler(RevisionConflictError)
def _revision_conflict(_, exc: RevisionConflictError):
    return _json_error(409, str(exc))


@app.exception_handler(ValidationError)
def _validation_error(_, exc: ValidationError):
    return _json_error(422, str(exc))


def _json_error(status_code: int, detail: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=status_code, content={"detail": detail})


# -- request/response models -----------------------------------------------------

class AccountCreate(BaseModel):
    display_name: str
    type: Literal["bank", "credit_card", "cash"]
    last4: str | None = None


class AccountPatch(BaseModel):
    expected_revision: int
    display_name: str | None = None
    archived: bool | None = None


class TransactionCreate(BaseModel):
    merchant: str | None = None
    description: str | None = None
    amount_paise: int | None = None
    amount_text: str | None = None
    currency: str = "INR"
    posted_date: str
    transaction_date: str | None = None
    type: Literal["expense", "refund", "income", "transfer"]
    category_id: str | None = None
    account_id: str | None = None
    original_transaction_id: str | None = None
    source: Literal["typed", "voice"] = "typed"
    remember_category: bool = False


class TransactionPatch(BaseModel):
    expected_revision: int
    merchant: str | None = None
    description: str | None = None
    amount_paise: int | None = None
    posted_date: str | None = None
    transaction_date: str | None = None
    category_id: str | None = None
    account_id: str | None = None
    remember_category: bool = False


class EntryParseRequest(BaseModel):
    text: str
    source: Literal["typed", "voice"] = "typed"
    account_id: str | None = None


class ImportRowPatch(BaseModel):
    decision: Literal["include", "exclude", "link_existing"] | None = None
    linked_transaction_id: str | None = None
    category_id: str | None = None
    raw_description: str | None = None
    posted_date: str | None = None
    amount_paise: int | None = None
    type: Literal["expense", "refund", "income", "transfer"] | None = None
    reference: str | None = None


class QuestionRequest(BaseModel):
    text: str


def _maybe_remember(merchant: str | None, category_id: str | None, remember: bool) -> None:
    if remember and merchant and category_id:
        _transactions.remember_merchant_rule(merchant, category_id)


# -- health / categories ----------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "data_revision": _repo.get_data_revision()}


@app.get("/categories")
def list_categories():
    return _repo.list_categories()


# -- accounts -----------------------------------------------------------------------

@app.post("/accounts", status_code=201)
def create_account(body: AccountCreate):
    return _accounts.create_account(body.display_name, body.type, body.last4)


@app.get("/accounts")
def list_accounts(include_archived: bool = False):
    return _accounts.list_accounts(include_archived)


@app.get("/accounts/{account_id}")
def get_account(account_id: str):
    return _accounts.get_account(account_id)


@app.patch("/accounts/{account_id}")
def update_account(account_id: str, body: AccountPatch):
    changes = body.model_dump(exclude={"expected_revision"}, exclude_none=True)
    if "archived" in changes:
        from datetime import datetime, timezone
        changes["archived_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds") if changes.pop("archived") else None
    return _accounts.update_account(account_id, body.expected_revision, changes)


# -- transactions -------------------------------------------------------------------

@app.post("/transactions", status_code=201)
def create_transaction(body: TransactionCreate):
    draft = body.model_dump(exclude={"remember_category"})
    created = _transactions.create_transaction(draft)
    _maybe_remember(body.merchant, body.category_id, body.remember_category)
    return created


@app.get("/transactions")
def list_transactions(
    start_date: str | None = None, end_date_exclusive: str | None = None,
    category_ids: str | None = None, account_ids: str | None = None,
    type: str | None = None, needs_review: bool | None = None,
    limit: int = 50, cursor: str | None = None,
):
    items, next_cursor = _transactions.list_transactions(
        start_date=start_date, end_date_exclusive=end_date_exclusive,
        category_ids=category_ids.split(",") if category_ids else None,
        account_ids=account_ids.split(",") if account_ids else None,
        transaction_type=type, needs_review=needs_review, limit=limit, cursor=cursor,
    )
    return {"items": items, "next_cursor": next_cursor, "data_revision": _repo.get_data_revision()}


@app.get("/transactions/{transaction_id}")
def get_transaction(transaction_id: str):
    return _transactions.get_transaction(transaction_id)


@app.patch("/transactions/{transaction_id}")
def update_transaction(transaction_id: str, body: TransactionPatch):
    changes = body.model_dump(exclude={"expected_revision", "remember_category"}, exclude_none=True)
    updated = _transactions.update_transaction(transaction_id, body.expected_revision, changes)
    _maybe_remember(body.merchant, body.category_id, body.remember_category)
    return updated


@app.delete("/transactions/{transaction_id}", status_code=204, response_model=None)
def delete_transaction(transaction_id: str, expected_revision: int):
    _transactions.delete_transaction(transaction_id, expected_revision)
    return Response(status_code=204)


# -- quick-add entry parsing --------------------------------------------------------

@app.post("/entries/parse")
def parse_entry(body: EntryParseRequest):
    """Blocks on one LLM call, then returns {"drafts": [...]} -- one per
    extracted expense. Never writes a transaction; only POST /transactions
    does."""
    return entries.parse_entry_text(
        _repo, body.text, body.source, body.account_id,
        llm_base_url=_cfg.llm_base_url, llm_model=_cfg.llm_model, timezone=_cfg.timezone,
    )


# -- statement import -----------------------------------------------------------------

@app.post("/imports", status_code=201)
async def create_import(account_id: str = Form(...), file: UploadFile = File(...)):
    """Blocks until the import reaches review_ready or failed -- parsing is
    deterministic and fast; only genuinely unrecognized merchants reach the
    LLM, in batches of 8."""
    raw_bytes = await file.read()
    try:
        return _imports.create_import(account_id, file.filename or "upload", raw_bytes)
    except ParseError as exc:
        raise HTTPException(status_code=413, detail=exc.error_code) from exc
    except DuplicateFileError as exc:
        raise HTTPException(status_code=409, detail={"code": "duplicate_file", "existing_import_id": exc.existing_import_id}) from exc


@app.get("/imports/{import_id}")
def get_import(import_id: str):
    return _imports.get_import(import_id)


@app.get("/imports/{import_id}/rows")
def list_import_rows(import_id: str):
    rows = _imports.list_rows(import_id)
    for row in rows:
        row["issue_codes"] = json.loads(row["issue_codes"]) if row.get("issue_codes") else []
        row["needs_review"] = bool(row["needs_review"])
    return rows


@app.patch("/imports/{import_id}/rows/{row_id}")
def update_import_row(import_id: str, row_id: str, body: ImportRowPatch):
    if body.decision == "link_existing" and not body.linked_transaction_id:
        raise HTTPException(status_code=422, detail="linked_transaction_id is required for decision=link_existing")
    changes = body.model_dump(exclude_none=True)
    return _imports.update_row(row_id, changes)


@app.post("/imports/{import_id}/confirm")
def confirm_import(import_id: str):
    try:
        return _imports.confirm(import_id)
    except InvalidImportStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnresolvedRowsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/imports/{import_id}", status_code=204, response_model=None)
def cancel_import(import_id: str):
    try:
        _imports.cancel(import_id)
    except InvalidImportStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=204)


# -- questions (Ask) -----------------------------------------------------------------

@app.post("/questions")
def ask_question(body: QuestionRequest):
    """Blocks on one LLM call (interpret the question), then always answers
    via a fixed, parameterized query -- the model never computes a total or
    picks its own date range."""
    return questions.answer_question(
        _repo, body.text, llm_base_url=_cfg.llm_base_url, llm_model=_cfg.llm_model, timezone=_cfg.timezone
    )


# -- analytics ------------------------------------------------------------------------

@app.get("/summary")
def summary(start_date: str, end_date_exclusive: str, account_ids: str | None = None):
    return _analytics.summary(start_date, end_date_exclusive, account_ids.split(",") if account_ids else None)


@app.get("/comparisons")
def comparisons(category_ids: str | None = None, account_ids: str | None = None):
    """This month to date vs. the same elapsed days of last month -- the one
    comparison with an unambiguous, fair definition (handles month-length
    and year-boundary differences by construction)."""
    today = today_in(_cfg.timezone)
    current, previous = resolve_month_to_date_comparison(today)
    return _analytics.compare(
        current, previous,
        category_ids=category_ids.split(",") if category_ids else None,
        account_ids=account_ids.split(",") if account_ids else None,
    )
