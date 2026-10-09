"""Statement import workflow: upload -> parse (CSV canonical/mapped, or the
generic heuristic PDF parser) -> stage rows with duplicate candidates ->
categorize -> review -> atomic commit. Never lets the LLM extract amounts;
it only assigns categories to already-deterministically-parsed rows.

Blocking end to end (matching src/work's convention) -- POST /imports
doesn't return until the import reaches review_ready or failed. For a
typical statement this is fast (parsing is deterministic; only genuinely
unrecognized merchants reach the LLM, in batches of 8).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from . import llm_client
from .categories import (
    CATEGORY_SEED_VERSION,
    OTHER_CATEGORY_ID,
    find_alias_category_in_text,
    is_unresolved_emi_text,
    is_valid_category,
)
from .db import FinanceRepository
from .errors import DuplicateFileError, InvalidImportStateError, UnresolvedRowsError, ValidationError
from .llm_tasks import CATEGORIZE_PROMPT_VERSION
from .money import MAX_AMOUNT_PAISE, TransactionType, category_required_for_type, normalize_merchant_key
from .parsers import ParseError, ParsedRow, ParseResult, parse_statement

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ROWS = 10_000
CATEGORIZE_BATCH_SIZE = 8


class ImportService:
    def __init__(self, repo: FinanceRepository, *, llm_base_url: str, llm_model: str):
        self._repo = repo
        self._llm_base_url = llm_base_url
        self._llm_model = llm_model

    def create_import(self, account_id: str, filename: str, raw_bytes: bytes) -> dict[str, Any]:
        """Runs the whole pipeline synchronously and returns the import once
        it's review_ready (or failed) -- see module docstring."""
        if len(raw_bytes) > MAX_UPLOAD_BYTES:
            raise ParseError("file_resource_limit", "File exceeds the upload size cap")

        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        existing = self._repo.find_committed_import_by_hash(account_id, sha256)
        if existing is not None:
            raise DuplicateFileError(existing["id"])

        record = self._repo.create_import(account_id, sha256, len(raw_bytes), filename)
        self._run_extraction(record["id"], filename, raw_bytes, account_id)
        return self._repo.get_import(record["id"])  # type: ignore[return-value]

    def _run_extraction(self, import_id: str, filename: str, raw_bytes: bytes, account_id: str) -> None:
        self._repo.update_import(import_id, {"state": "extracting"})
        try:
            parse_result = parse_statement(filename, raw_bytes)
        except ParseError as exc:
            self._repo.update_import(import_id, {"state": "failed", "error_code": exc.error_code})
            return

        if len(parse_result.rows) > MAX_ROWS:
            self._repo.update_import(import_id, {"state": "failed", "error_code": "file_resource_limit"})
            return

        staged = [self._stage_row(account_id, row) for row in parse_result.rows]
        self._repo.insert_import_rows(import_id, staged)
        self._repo.update_import(
            import_id,
            {"state": "categorizing", "parser_id": parse_result.parser_id,
             "parser_version": parse_result.parser_version, "row_count": len(staged)},
        )

        self._categorize(import_id)

        rows = self._repo.list_import_rows(import_id)
        issue_count = sum(
            1 for r in rows
            if r["needs_review"] or r["duplicate_candidate_transaction_id"]
            or r["decision"] == "unresolved" or (r["issue_codes"] and r["issue_codes"] != "[]")
        )
        self._repo.update_import(import_id, {"state": "review_ready", "issue_count": issue_count})

    def _stage_row(self, account_id: str, row: ParsedRow) -> dict[str, Any]:
        duplicate = None
        if row.posted_date:
            duplicate = self._repo.find_duplicate_candidate(account_id, row.posted_date, row.amount_paise, row.type)
        fields = asdict(row)
        issue_codes = fields.pop("issue_codes")
        return {
            **fields, "issue_codes": json.dumps(issue_codes),
            "duplicate_candidate_transaction_id": duplicate["id"] if duplicate else None,
            "decision": "unresolved" if (issue_codes or duplicate) else "include",
        }

    def _categorize(self, import_id: str) -> None:
        rows = self._repo.list_import_rows(import_id)
        to_categorize = [r for r in rows if category_required_for_type(TransactionType(r["type"]))]

        # Dedupe identical descriptions within this import before ever
        # calling the LLM: a real statement often repeats the same merchant
        # many times. Categorizing each unique description once and
        # applying the result to every matching row cuts redundant model
        # calls with zero accuracy cost.
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in to_categorize:
            groups.setdefault(normalize_merchant_key(row["raw_description"]), []).append(row)

        # Layer 0: an EMI line is never confidently categorized -- always
        # other+needs_review, deterministically, with no LLM call.
        after_emi_keys: list[str] = []
        for key, rows_in_group in groups.items():
            if not is_unresolved_emi_text(key):
                after_emi_keys.append(key)
                continue
            for row in rows_in_group:
                self._repo.update_import_row(
                    row["id"], {"category_id": OTHER_CATEGORY_ID, "category_origin": "rule", "needs_review": 1}
                )

        # Layer 1: a recognized consumer brand resolves instantly -- zero
        # LLM calls, same confidence as a saved user rule.
        remaining_keys: list[str] = []
        for key in after_emi_keys:
            alias_category = find_alias_category_in_text(key)
            if alias_category is None:
                remaining_keys.append(key)
                continue
            for row in groups[key]:
                self._repo.update_import_row(
                    row["id"], {"category_id": alias_category, "category_origin": "rule", "needs_review": 0}
                )

        model_identifier = self._llm_model
        cached = self._repo.get_cached_categories(remaining_keys, model_identifier, CATEGORIZE_PROMPT_VERSION, CATEGORY_SEED_VERSION)

        # Layer 2: cache hits (persists across every future import) need no
        # LLM call either.
        uncached_keys = []
        for key in remaining_keys:
            entry = cached.get(key)
            if entry is None:
                uncached_keys.append(key)
                continue
            needs_review = bool(entry["ambiguous"]) or entry["category_id"] == OTHER_CATEGORY_ID
            for row in groups[key]:
                self._repo.update_import_row(
                    row["id"],
                    {"category_id": entry["category_id"], "category_origin": "llm",
                     "needs_review": 1 if needs_review else 0},
                )

        representatives = [groups[key][0] for key in uncached_keys]
        for start in range(0, len(representatives), CATEGORIZE_BATCH_SIZE):
            batch = representatives[start : start + CATEGORIZE_BATCH_SIZE]
            self._categorize_batch(batch, groups, model_identifier)

    def _categorize_batch(
        self, batch: list[dict[str, Any]], groups: dict[str, list[dict[str, Any]]], model_identifier: str
    ) -> None:
        payload_rows = [{"row_id": r["id"], "description": r["raw_description"]} for r in batch]
        result = llm_client.categorize_batch(payload_rows, llm_base_url=self._llm_base_url, model=self._llm_model)
        by_row_id = {item.row_id: item for item in result.results}

        for representative in batch:
            item = by_row_id.get(representative["id"])
            if item is None or not is_valid_category(item.category_id):
                category_id, origin, needs_review, ambiguous = OTHER_CATEGORY_ID, "fallback", True, True
            else:
                is_other = item.category_id == OTHER_CATEGORY_ID
                category_id, origin = item.category_id, "llm"
                needs_review = item.ambiguous or is_other
                ambiguous = item.ambiguous

            # Only cache a genuine model result -- never a "fallback" caused
            # by a missing/invalid response, which would otherwise
            # permanently stick "other" onto this description.
            key = normalize_merchant_key(representative["raw_description"])
            if origin == "llm":
                self._repo.set_cached_category(
                    key, model_identifier, CATEGORIZE_PROMPT_VERSION, CATEGORY_SEED_VERSION, category_id, ambiguous
                )
            for row in groups[key]:
                self._repo.update_import_row(
                    row["id"], {"category_id": category_id, "category_origin": origin, "needs_review": 1 if needs_review else 0}
                )

    def get_import(self, import_id: str) -> dict[str, Any]:
        return self._repo.require_import(import_id)

    def list_rows(self, import_id: str) -> list[dict[str, Any]]:
        return self._repo.list_import_rows(import_id)

    def update_row(self, row_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return self._repo.update_import_row(row_id, changes)

    def confirm(self, import_id: str) -> dict[str, int]:
        record = self._repo.require_import(import_id)
        if record["state"] != "review_ready":
            raise InvalidImportStateError(record["state"])

        rows = self._repo.list_import_rows(import_id)
        unresolved = [r for r in rows if r["decision"] == "unresolved"]
        if unresolved:
            raise UnresolvedRowsError(len(unresolved))

        for row in rows:
            if row["decision"] != "include":
                continue
            if not (0 < row["amount_paise"] <= MAX_AMOUNT_PAISE):
                raise ValidationError(f"Row {row['id']} has an invalid amount_paise for commit")
            if not row["posted_date"]:
                raise ValidationError(f"Row {row['id']} is missing posted_date")

        self._repo.update_import(import_id, {"state": "committing"})
        return self._repo.commit_import(import_id)

    def cancel(self, import_id: str) -> None:
        record = self._repo.require_import(import_id)
        if record["state"] == "committed":
            raise InvalidImportStateError(record["state"])
        self._repo.update_import(import_id, {"state": "cancelled"})
