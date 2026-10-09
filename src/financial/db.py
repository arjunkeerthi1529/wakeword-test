"""SQLite schema + repository for the financial service's own database.
One consolidated schema (no versioned migrations -- this is a fresh,
standalone service) and one synchronous repository class used directly from
plain `def` FastAPI endpoints (which FastAPI already runs in a threadpool,
so this never blocks the event loop -- see server.py).
"""
from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable

from .errors import NotFoundError, RevisionConflictError

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _encode_cursor(posted_date: str, txn_id: str) -> str:
    raw = f"{posted_date}|{txn_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    posted_date, _, txn_id = raw.partition("|")
    return posted_date, txn_id


def get_conn(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            seed_version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('bank', 'credit_card', 'cash')),
            currency TEXT NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
            last4 TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            archived_at TEXT
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            account_id TEXT REFERENCES accounts(id),
            merchant TEXT,
            merchant_key TEXT,
            description TEXT,
            amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
            currency TEXT NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
            posted_date TEXT NOT NULL,
            transaction_date TEXT,
            type TEXT NOT NULL CHECK (type IN ('expense', 'refund', 'income', 'transfer')),
            category_id TEXT REFERENCES categories(id),
            original_transaction_id TEXT REFERENCES transactions(id),
            source TEXT NOT NULL CHECK (source IN ('typed', 'voice', 'import')),
            source_import_id TEXT,
            category_origin TEXT NOT NULL CHECK (category_origin IN ('user', 'rule', 'llm', 'fallback')),
            needs_review INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            deleted_at TEXT,
            CHECK (
                (type IN ('expense', 'refund') AND category_id IS NOT NULL)
                OR (type IN ('income', 'transfer') AND category_id IS NULL)
            )
        );

        CREATE TABLE IF NOT EXISTS merchant_rules (
            id TEXT PRIMARY KEY,
            merchant_key TEXT NOT NULL,
            category_id TEXT NOT NULL REFERENCES categories(id),
            origin TEXT NOT NULL CHECK (origin IN ('user', 'seed')),
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (merchant_key)
        );

        CREATE TABLE IF NOT EXISTS categorization_cache (
            normalized_description TEXT NOT NULL,
            model_identifier TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            category_seed_version INTEGER NOT NULL,
            category_id TEXT NOT NULL,
            ambiguous INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (normalized_description, model_identifier, prompt_version, category_seed_version)
        );

        CREATE TABLE IF NOT EXISTS imports (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            original_filename TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state IN
                ('queued', 'extracting', 'categorizing', 'review_ready',
                 'committing', 'committed', 'failed', 'cancelled')),
            parser_id TEXT,
            parser_version TEXT,
            row_count INTEGER NOT NULL DEFAULT 0,
            issue_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            committed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS import_rows (
            id TEXT PRIMARY KEY,
            import_id TEXT NOT NULL REFERENCES imports(id),
            ordinal INTEGER NOT NULL,
            raw_description TEXT NOT NULL,
            posted_date TEXT,
            amount_paise INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'INR',
            type TEXT NOT NULL,
            reference TEXT,
            category_id TEXT,
            category_origin TEXT,
            needs_review INTEGER NOT NULL DEFAULT 0,
            decision TEXT NOT NULL CHECK (decision IN ('unresolved', 'include', 'exclude', 'link_existing')),
            duplicate_candidate_transaction_id TEXT,
            linked_transaction_id TEXT,
            source_locator TEXT NOT NULL,
            issue_codes TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_transactions_posted_date ON transactions(posted_date, id);
        CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id, posted_date);
        CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id, posted_date);
        CREATE INDEX IF NOT EXISTS idx_transactions_merchant ON transactions(merchant_key, posted_date);
        CREATE INDEX IF NOT EXISTS idx_import_rows_import ON import_rows(import_id, ordinal);

        INSERT OR IGNORE INTO app_metadata (key, value) VALUES ('data_revision', '0');
    """)
    conn.commit()


class FinanceRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # -- metadata / data_revision -----------------------------------------------

    def get_data_revision(self) -> int:
        row = self._conn.execute("SELECT value FROM app_metadata WHERE key = 'data_revision'").fetchone()
        return int(row["value"]) if row else 0

    def _bump_data_revision(self, cur: sqlite3.Cursor) -> int:
        cur.execute("UPDATE app_metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'data_revision'")
        return int(cur.execute("SELECT value FROM app_metadata WHERE key = 'data_revision'").fetchone()["value"])

    # -- categories ----------------------------------------------------------------

    def list_categories(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, display_name, sort_order, seed_version FROM categories ORDER BY sort_order"
        ).fetchall()
        return [dict(row) for row in rows]

    def seed_categories(self, categories: Iterable[Any], seed_version: int) -> None:
        with self._conn:
            for category in categories:
                self._conn.execute(
                    """INSERT INTO categories (id, display_name, sort_order, seed_version)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT (id) DO UPDATE SET
                           display_name = excluded.display_name,
                           sort_order = excluded.sort_order,
                           seed_version = excluded.seed_version""",
                    (category.id, category.display_name, category.sort_order, seed_version),
                )

    # -- accounts --------------------------------------------------------------

    def create_account(self, display_name: str, account_type: str, last4: str | None = None) -> dict[str, Any]:
        account_id = new_id("acct")
        with self._conn:
            self._conn.execute(
                "INSERT INTO accounts (id, display_name, type, currency, last4) VALUES (?, ?, ?, 'INR', ?)",
                (account_id, display_name, account_type, last4),
            )
        return self.get_account(account_id)  # type: ignore[return-value]

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return _row_to_dict(row)

    def require_account(self, account_id: str) -> dict[str, Any]:
        account = self.get_account(account_id)
        if account is None:
            raise NotFoundError(f"Account not found: {account_id}")
        return account

    def list_accounts(self, include_archived: bool = False) -> list[dict[str, Any]]:
        if include_archived:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM accounts WHERE archived_at IS NULL ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_account(self, account_id: str, expected_revision: int, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.get_account(account_id)
        if current is None:
            raise NotFoundError(f"Account not found: {account_id}")
        if current["revision"] != expected_revision:
            raise RevisionConflictError(f"Account {account_id} revision changed")

        allowed_fields = {"display_name", "archived_at"}
        set_clauses, params = [], []
        for field, value in changes.items():
            if field not in allowed_fields:
                continue
            set_clauses.append(f"{field} = ?")
            params.append(value)
        if not set_clauses:
            return current

        set_clauses.append("revision = revision + 1")
        set_clauses.append("updated_at = datetime('now')")
        params.append(account_id)
        with self._conn:
            self._conn.execute(f"UPDATE accounts SET {', '.join(set_clauses)} WHERE id = ?", params)
        return self.get_account(account_id)  # type: ignore[return-value]

    # -- merchant rules ----------------------------------------------------------

    def get_merchant_rule(self, merchant_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM merchant_rules WHERE merchant_key = ?", (merchant_key,)
        ).fetchone()
        return _row_to_dict(row)

    def put_merchant_rule(self, merchant_key: str, category_id: str, origin: str = "user") -> dict[str, Any]:
        existing = self.get_merchant_rule(merchant_key)
        with self._conn as conn:
            if existing is None:
                conn.execute(
                    "INSERT INTO merchant_rules (id, merchant_key, category_id, origin, version) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (new_id("rule"), merchant_key, category_id, origin),
                )
            else:
                conn.execute(
                    "UPDATE merchant_rules SET category_id = ?, version = version + 1, "
                    "updated_at = datetime('now') WHERE merchant_key = ?",
                    (category_id, merchant_key),
                )
            self._bump_data_revision(conn.cursor())
        return self.get_merchant_rule(merchant_key)  # type: ignore[return-value]

    # -- transactions --------------------------------------------------------------

    def create_transaction(self, fields: dict[str, Any]) -> dict[str, Any]:
        transaction_id = new_id("txn")
        with self._conn as conn:
            conn.execute(
                """INSERT INTO transactions (
                    id, account_id, merchant, merchant_key, description, amount_paise,
                    currency, posted_date, transaction_date, type, category_id,
                    original_transaction_id, source, category_origin, needs_review
                ) VALUES (
                    :id, :account_id, :merchant, :merchant_key, :description, :amount_paise,
                    :currency, :posted_date, :transaction_date, :type, :category_id,
                    :original_transaction_id, :source, :category_origin, :needs_review
                )""",
                {"id": transaction_id, **fields},
            )
            self._bump_data_revision(conn.cursor())
        return self.get_transaction(transaction_id)  # type: ignore[return-value]

    def get_transaction(self, transaction_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM transactions WHERE id = ? AND deleted_at IS NULL", (transaction_id,)
        ).fetchone()
        return _row_to_dict(row)

    def require_transaction(self, transaction_id: str) -> dict[str, Any]:
        txn = self.get_transaction(transaction_id)
        if txn is None:
            raise NotFoundError(f"Transaction not found: {transaction_id}")
        return txn

    def list_transactions(
        self, *, start_date: str | None = None, end_date_exclusive: str | None = None,
        category_ids: list[str] | None = None, account_ids: list[str] | None = None,
        transaction_type: str | None = None, merchant_key: str | None = None,
        min_amount_paise: int | None = None, max_amount_paise: int | None = None,
        needs_review: bool | None = None, limit: int = DEFAULT_PAGE_LIMIT, cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        limit = max(1, min(limit, MAX_PAGE_LIMIT))
        clauses = ["deleted_at IS NULL"]
        params: list[Any] = []

        if start_date is not None:
            clauses.append("posted_date >= ?"); params.append(start_date)
        if end_date_exclusive is not None:
            clauses.append("posted_date < ?"); params.append(end_date_exclusive)
        if category_ids:
            clauses.append(f"category_id IN ({','.join('?' for _ in category_ids)})"); params.extend(category_ids)
        if account_ids:
            clauses.append(f"account_id IN ({','.join('?' for _ in account_ids)})"); params.extend(account_ids)
        if transaction_type is not None:
            clauses.append("type = ?"); params.append(transaction_type)
        if merchant_key is not None:
            clauses.append("merchant_key = ?"); params.append(merchant_key)
        if min_amount_paise is not None:
            clauses.append("amount_paise >= ?"); params.append(min_amount_paise)
        if max_amount_paise is not None:
            clauses.append("amount_paise <= ?"); params.append(max_amount_paise)
        if needs_review is not None:
            clauses.append("needs_review = ?"); params.append(1 if needs_review else 0)
        if cursor is not None:
            cursor_date, cursor_id = _decode_cursor(cursor)
            clauses.append("(posted_date < ? OR (posted_date = ? AND id < ?))")
            params.extend([cursor_date, cursor_date, cursor_id])

        where_sql = " AND ".join(clauses)
        sql = f"SELECT * FROM transactions WHERE {where_sql} ORDER BY posted_date DESC, id DESC LIMIT ?"
        rows = self._conn.execute(sql, [*params, limit + 1]).fetchall()
        items = [dict(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = _encode_cursor(last["posted_date"], last["id"])
        return items, next_cursor

    def update_transaction(self, transaction_id: str, expected_revision: int, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.require_transaction(transaction_id)
        if current["revision"] != expected_revision:
            raise RevisionConflictError(f"Transaction {transaction_id} revision changed")

        allowed_fields = {
            "merchant", "merchant_key", "description", "amount_paise", "posted_date",
            "transaction_date", "category_id", "category_origin", "needs_review", "account_id",
        }
        set_clauses, params = [], []
        for field, value in changes.items():
            if field not in allowed_fields:
                continue
            set_clauses.append(f"{field} = ?")
            params.append(value)
        if not set_clauses:
            return current

        set_clauses.append("revision = revision + 1")
        set_clauses.append("updated_at = datetime('now')")
        params.append(transaction_id)
        with self._conn as conn:
            conn.execute(f"UPDATE transactions SET {', '.join(set_clauses)} WHERE id = ?", params)
            self._bump_data_revision(conn.cursor())
        return self.get_transaction(transaction_id)  # type: ignore[return-value]

    def soft_delete_transaction(self, transaction_id: str, expected_revision: int) -> None:
        current = self.require_transaction(transaction_id)
        if current["revision"] != expected_revision:
            raise RevisionConflictError(f"Transaction {transaction_id} revision changed")
        with self._conn as conn:
            conn.execute(
                "UPDATE transactions SET deleted_at = datetime('now'), revision = revision + 1 WHERE id = ?",
                (transaction_id,),
            )
            self._bump_data_revision(conn.cursor())

    def sum_amounts(
        self, *, start_date: str, end_date_exclusive: str, category_ids: list[str] | None = None,
        account_ids: list[str] | None = None, merchant_key: str | None = None,
    ) -> dict[str, int]:
        clauses = ["deleted_at IS NULL", "posted_date >= ?", "posted_date < ?"]
        params: list[Any] = [start_date, end_date_exclusive]
        if category_ids:
            clauses.append(f"category_id IN ({','.join('?' for _ in category_ids)})"); params.extend(category_ids)
        if account_ids:
            clauses.append(f"account_id IN ({','.join('?' for _ in account_ids)})"); params.extend(account_ids)
        if merchant_key is not None:
            clauses.append("merchant_key = ?"); params.append(merchant_key)
        where_sql = " AND ".join(clauses)

        row = self._conn.execute(
            f"""SELECT
                COALESCE(SUM(CASE WHEN type = 'expense' THEN amount_paise ELSE 0 END), 0) AS gross_expense_paise,
                COALESCE(SUM(CASE WHEN type = 'refund' THEN amount_paise ELSE 0 END), 0) AS refund_paise,
                COALESCE(SUM(CASE WHEN type = 'income' THEN amount_paise ELSE 0 END), 0) AS income_paise,
                COALESCE(SUM(CASE WHEN type IN ('expense', 'refund') THEN 1 ELSE 0 END), 0) AS expense_count,
                COUNT(*) AS matched_count
            FROM transactions WHERE {where_sql}""",
            params,
        ).fetchone()
        gross, refund = int(row["gross_expense_paise"]), int(row["refund_paise"])
        return {
            "gross_expense_paise": gross, "refund_paise": refund,
            "net_spending_paise": gross - refund, "income_paise": int(row["income_paise"]),
            "expense_count": int(row["expense_count"]), "matched_count": int(row["matched_count"]),
        }

    def merchant_breakdown(
        self, *, start_date: str, end_date_exclusive: str, category_ids: list[str] | None = None,
        account_ids: list[str] | None = None, limit: int = 6,
    ) -> list[dict[str, Any]]:
        clauses = ["deleted_at IS NULL", "posted_date >= ?", "posted_date < ?", "type = 'expense'", "merchant IS NOT NULL"]
        params: list[Any] = [start_date, end_date_exclusive]
        if category_ids:
            clauses.append(f"category_id IN ({','.join('?' for _ in category_ids)})"); params.extend(category_ids)
        if account_ids:
            clauses.append(f"account_id IN ({','.join('?' for _ in account_ids)})"); params.extend(account_ids)
        where_sql = " AND ".join(clauses)
        rows = self._conn.execute(
            f"""SELECT merchant, SUM(amount_paise) AS gross_expense_paise, COUNT(*) AS count
                FROM transactions WHERE {where_sql}
                GROUP BY merchant_key ORDER BY gross_expense_paise DESC LIMIT ?""",
            [*params, max(1, limit)],
        ).fetchall()
        return [dict(row) for row in rows]

    def top_expenses(
        self, *, start_date: str, end_date_exclusive: str, category_ids: list[str] | None = None,
        account_ids: list[str] | None = None, merchant_key: str | None = None, limit: int = 3,
    ) -> list[dict[str, Any]]:
        clauses = ["deleted_at IS NULL", "posted_date >= ?", "posted_date < ?", "type = 'expense'"]
        params: list[Any] = [start_date, end_date_exclusive]
        if category_ids:
            clauses.append(f"category_id IN ({','.join('?' for _ in category_ids)})"); params.extend(category_ids)
        if account_ids:
            clauses.append(f"account_id IN ({','.join('?' for _ in account_ids)})"); params.extend(account_ids)
        if merchant_key is not None:
            clauses.append("merchant_key = ?"); params.append(merchant_key)
        where_sql = " AND ".join(clauses)
        rows = self._conn.execute(
            f"""SELECT * FROM transactions WHERE {where_sql}
                ORDER BY amount_paise DESC, posted_date DESC, id DESC LIMIT ?""",
            [*params, max(1, limit)],
        ).fetchall()
        return [dict(row) for row in rows]

    def busiest_day(
        self, *, start_date: str, end_date_exclusive: str, category_ids: list[str] | None = None,
        account_ids: list[str] | None = None, merchant_key: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["deleted_at IS NULL", "posted_date >= ?", "posted_date < ?", "type = 'expense'"]
        params: list[Any] = [start_date, end_date_exclusive]
        if category_ids:
            clauses.append(f"category_id IN ({','.join('?' for _ in category_ids)})"); params.extend(category_ids)
        if account_ids:
            clauses.append(f"account_id IN ({','.join('?' for _ in account_ids)})"); params.extend(account_ids)
        if merchant_key is not None:
            clauses.append("merchant_key = ?"); params.append(merchant_key)
        where_sql = " AND ".join(clauses)
        row = self._conn.execute(
            f"""SELECT posted_date, SUM(amount_paise) AS gross_expense_paise, COUNT(*) AS count
                FROM transactions WHERE {where_sql}
                GROUP BY posted_date ORDER BY gross_expense_paise DESC, posted_date DESC LIMIT 1""",
            params,
        ).fetchone()
        return dict(row) if row is not None else None

    def category_breakdown(
        self, *, start_date: str, end_date_exclusive: str, account_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["deleted_at IS NULL", "posted_date >= ?", "posted_date < ?", "type = 'expense'"]
        params: list[Any] = [start_date, end_date_exclusive]
        if account_ids:
            clauses.append(f"account_id IN ({','.join('?' for _ in account_ids)})"); params.extend(account_ids)
        where_sql = " AND ".join(clauses)
        rows = self._conn.execute(
            f"""SELECT category_id, SUM(amount_paise) AS gross_expense_paise, COUNT(*) AS count
                FROM transactions WHERE {where_sql}
                GROUP BY category_id ORDER BY gross_expense_paise DESC""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def count_needs_review(
        self, *, start_date: str, end_date_exclusive: str, account_ids: list[str] | None = None
    ) -> int:
        clauses = ["deleted_at IS NULL", "posted_date >= ?", "posted_date < ?", "needs_review = 1"]
        params: list[Any] = [start_date, end_date_exclusive]
        if account_ids:
            clauses.append(f"account_id IN ({','.join('?' for _ in account_ids)})"); params.extend(account_ids)
        where_sql = " AND ".join(clauses)
        row = self._conn.execute(f"SELECT COUNT(*) AS count FROM transactions WHERE {where_sql}", params).fetchone()
        return int(row["count"])

    # -- imports (statement import staging) ----------------------------

    def find_committed_import_by_hash(self, account_id: str, sha256: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM imports WHERE account_id = ? AND sha256 = ? AND state = 'committed'",
            (account_id, sha256),
        ).fetchone()
        return _row_to_dict(row)

    def create_import(self, account_id: str, sha256: str, size_bytes: int, original_filename: str) -> dict[str, Any]:
        import_id = new_id("import")
        with self._conn:
            self._conn.execute(
                "INSERT INTO imports (id, account_id, sha256, size_bytes, original_filename, state) "
                "VALUES (?, ?, ?, ?, ?, 'queued')",
                (import_id, account_id, sha256, size_bytes, original_filename),
            )
        return self.get_import(import_id)  # type: ignore[return-value]

    def get_import(self, import_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
        return _row_to_dict(row)

    def require_import(self, import_id: str) -> dict[str, Any]:
        record = self.get_import(import_id)
        if record is None:
            raise NotFoundError(f"Import not found: {import_id}")
        return record

    def update_import(self, import_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        set_clauses = [f"{field} = ?" for field in changes]
        params = [*changes.values(), import_id]
        with self._conn:
            self._conn.execute(f"UPDATE imports SET {', '.join(set_clauses)} WHERE id = ?", params)
        return self.get_import(import_id)  # type: ignore[return-value]

    def insert_import_rows(self, import_id: str, rows: list[dict[str, Any]]) -> None:
        with self._conn as conn:
            for ordinal, row in enumerate(rows, start=1):
                conn.execute(
                    """INSERT INTO import_rows (
                        id, import_id, ordinal, raw_description, posted_date, amount_paise,
                        currency, type, reference, category_id, category_origin, needs_review,
                        decision, duplicate_candidate_transaction_id, source_locator, issue_codes
                    ) VALUES (
                        :id, :import_id, :ordinal, :raw_description, :posted_date, :amount_paise,
                        :currency, :type, :reference, :category_id, :category_origin, :needs_review,
                        :decision, :duplicate_candidate_transaction_id, :source_locator, :issue_codes
                    )""",
                    {
                        "id": new_id("introw"), "import_id": import_id, "ordinal": ordinal,
                        "issue_codes": "[]", "category_id": None, "category_origin": None,
                        "needs_review": 0, "decision": "unresolved",
                        "duplicate_candidate_transaction_id": None, **row,
                    },
                )

    def list_import_rows(self, import_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM import_rows WHERE import_id = ? ORDER BY ordinal", (import_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_import_row(self, row_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM import_rows WHERE id = ?", (row_id,)).fetchone()
        return _row_to_dict(row)

    def require_import_row(self, row_id: str) -> dict[str, Any]:
        row = self.get_import_row(row_id)
        if row is None:
            raise NotFoundError(f"Import row not found: {row_id}")
        return row

    def update_import_row(self, row_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        set_clauses = [f"{field} = ?" for field in changes]
        params = [*changes.values(), row_id]
        with self._conn:
            self._conn.execute(f"UPDATE import_rows SET {', '.join(set_clauses)} WHERE id = ?", params)
        return self.get_import_row(row_id)  # type: ignore[return-value]

    def find_duplicate_candidate(
        self, account_id: str, posted_date: str, amount_paise: int, txn_type: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM transactions
               WHERE deleted_at IS NULL AND (account_id = ? OR account_id IS NULL)
                 AND posted_date = ? AND amount_paise = ? AND type = ? LIMIT 1""",
            (account_id, posted_date, amount_paise, txn_type),
        ).fetchone()
        return _row_to_dict(row)

    def commit_import(self, import_id: str) -> dict[str, int]:
        rows = self.list_import_rows(import_id)
        inserted = linked = excluded = 0
        import_row = self.require_import(import_id)
        account_id = import_row["account_id"]

        with self._conn as conn:
            for row in rows:
                if row["decision"] == "exclude":
                    excluded += 1
                    continue
                if row["decision"] == "link_existing":
                    conn.execute(
                        "UPDATE transactions SET source_import_id = ? WHERE id = ?",
                        (import_id, row["linked_transaction_id"]),
                    )
                    linked += 1
                    continue
                if row["decision"] == "include":
                    conn.execute(
                        """INSERT INTO transactions (
                            id, account_id, merchant, merchant_key, description, amount_paise,
                            currency, posted_date, type, category_id, source, category_origin,
                            needs_review, source_import_id
                        ) VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, 'import', ?, ?, ?)""",
                        (
                            new_id("txn"), account_id, row["raw_description"], row["amount_paise"],
                            row["currency"], row["posted_date"], row["type"], row["category_id"],
                            row["category_origin"] or "fallback", row["needs_review"], import_id,
                        ),
                    )
                    inserted += 1
            self._bump_data_revision(conn.cursor())
            conn.execute(
                "UPDATE imports SET state = 'committed', committed_at = datetime('now') WHERE id = ?",
                (import_id,),
            )
        return {"inserted": inserted, "linked": linked, "excluded": excluded}

    def recover_interrupted_imports(self) -> int:
        """Startup recovery: 'review_ready' is a legitimate resting state
        (just awaiting user action) and is left alone. Only truly in-flight
        states are recovered -- 'committing' is safe to fail-and-retry
        because the atomic commit transaction either fully applied before a
        crash (state would already read 'committed') or didn't apply at
        all."""
        with self._conn as conn:
            cursor = conn.execute(
                "UPDATE imports SET state = 'failed', error_code = 'interrupted_by_restart' "
                "WHERE state IN ('queued', 'extracting', 'categorizing', 'committing')"
            )
            return cursor.rowcount

    # -- categorization cache ----------------------------

    def get_cached_categories(
        self, normalized_descriptions: list[str], model_identifier: str,
        prompt_version: str, category_seed_version: int,
    ) -> dict[str, dict[str, Any]]:
        if not normalized_descriptions:
            return {}
        placeholders = ",".join("?" for _ in normalized_descriptions)
        rows = self._conn.execute(
            f"""SELECT normalized_description, category_id, ambiguous FROM categorization_cache
                WHERE normalized_description IN ({placeholders})
                  AND model_identifier = ? AND prompt_version = ? AND category_seed_version = ?""",
            [*normalized_descriptions, model_identifier, prompt_version, category_seed_version],
        ).fetchall()
        return {row["normalized_description"]: dict(row) for row in rows}

    def set_cached_category(
        self, normalized_description: str, model_identifier: str, prompt_version: str,
        category_seed_version: int, category_id: str, ambiguous: bool,
    ) -> None:
        with self._conn as conn:
            conn.execute(
                """INSERT INTO categorization_cache (
                    normalized_description, model_identifier, prompt_version,
                    category_seed_version, category_id, ambiguous
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (normalized_description, model_identifier, prompt_version, category_seed_version)
                DO UPDATE SET category_id = excluded.category_id, ambiguous = excluded.ambiguous""",
                (normalized_description, model_identifier, prompt_version,
                 category_seed_version, category_id, 1 if ambiguous else 0),
            )
