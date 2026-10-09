"""Statement parsers: CSV (canonical or mapped debit/credit columns) and a
generic heuristic PDF parser (two text layouts). Deterministic only -- the
LLM is never used to extract dates/amounts from a file, only to categorize
an already-parsed row's description later on.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import fitz  # PyMuPDF

from .money import MAX_AMOUNT_PAISE, MoneyError, parse_amount_to_paise


class ParseError(Exception):
    """Raised with a specific, honest error_code: e.g. csv_unrecognized_columns,
    encrypted_pdf_unsupported, scanned_pdf_unsupported, unsupported_pdf_layout.
    Never a silent guess."""

    def __init__(self, error_code: str, message: str | None = None):
        super().__init__(message or error_code)
        self.error_code = error_code


@dataclass
class ParsedRow:
    """One normalized statement row before any categorization or duplicate
    check runs. `type` is already resolved to one of the four transaction
    types by the parser (from an explicit column or a sign convention) --
    never inferred by a model."""

    raw_description: str
    posted_date: str  # ISO YYYY-MM-DD
    amount_paise: int
    currency: str
    type: str  # expense | refund | income | transfer
    reference: str | None
    source_locator: str  # e.g. "row 5" or "page 2 line 14"
    issue_codes: list[str] = field(default_factory=list)  # row-level problems; never fatal to the file


@dataclass
class ParseResult:
    rows: list[ParsedRow]
    parser_id: str
    parser_version: str


# ── CSV ─────────────────────────────────────────────────────────────────────────

PARSER_ID_CSV_CANONICAL = "csv_canonical"
PARSER_ID_CSV_MAPPED = "csv_mapped_debit_credit"
CSV_PARSER_VERSION = "1.0"

CANONICAL_COLUMNS = {"posted_date", "description", "amount", "currency", "type", "reference"}
VALID_TYPES = {"expense", "refund", "income", "transfer"}


def _normalize_header(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _decode_text(raw_bytes: bytes) -> str:
    try:
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("csv_unsupported_encoding", "CSV must be UTF-8 encoded") from exc


def parse_csv(raw_bytes: bytes) -> ParseResult:
    text = _decode_text(raw_bytes)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ParseError("csv_empty_file", "CSV file has no header row")

    headers = {_normalize_header(h) for h in reader.fieldnames if h}

    if CANONICAL_COLUMNS.issubset(headers):
        return _parse_csv_canonical(reader)
    if {"date", "description", "debit", "credit"}.issubset(headers):
        return _parse_csv_mapped_debit_credit(reader)

    raise ParseError(
        "csv_unrecognized_columns",
        f"Unrecognized CSV columns: {sorted(headers)}. Expected canonical "
        f"columns ({sorted(CANONICAL_COLUMNS)}) or Date/Description/Debit/Credit.",
    )


def _parse_csv_canonical(reader: csv.DictReader) -> ParseResult:
    rows: list[ParsedRow] = []
    for ordinal, raw_row in enumerate(reader, start=1):
        row = {_normalize_header(k): (v or "").strip() for k, v in raw_row.items() if k}
        locator = f"row {ordinal}"
        issues: list[str] = []

        txn_type = row.get("type", "").lower()
        if txn_type not in VALID_TYPES:
            issues.append("invalid_type")
            txn_type = "expense"

        currency = row.get("currency", "INR") or "INR"
        if currency != "INR":
            issues.append("unsupported_currency")
            currency = "INR"

        try:
            amount_paise = parse_amount_to_paise(row["amount"])
        except (MoneyError, KeyError):
            issues.append("invalid_amount")
            amount_paise = 0

        posted_date = row.get("posted_date", "")
        if not posted_date:
            issues.append("missing_posted_date")

        rows.append(
            ParsedRow(
                raw_description=row.get("description", ""),
                posted_date=posted_date,
                amount_paise=amount_paise,
                currency=currency,
                type=txn_type,
                reference=row.get("reference") or None,
                source_locator=locator,
                issue_codes=issues,
            )
        )
    return ParseResult(rows=rows, parser_id=PARSER_ID_CSV_CANONICAL, parser_version=CSV_PARSER_VERSION)


def _parse_csv_mapped_debit_credit(reader: csv.DictReader) -> ParseResult:
    """Generic bank-CSV convention: Date, Description, Debit, Credit. Debit
    becomes an expense, Credit becomes income -- a credit could really be a
    refund or transfer (not knowable from the columns alone), so every
    mapped row carries a 'direction_assumed' issue and is flagged for review
    downstream, never asserted as certain here."""
    rows: list[ParsedRow] = []
    for ordinal, raw_row in enumerate(reader, start=1):
        row = {_normalize_header(k): (v or "").strip() for k, v in raw_row.items() if k}
        locator = f"row {ordinal}"
        issues: list[str] = []

        debit_amount = _parse_optional_decimal(row.get("debit", ""))
        credit_amount = _parse_optional_decimal(row.get("credit", ""))

        if debit_amount and credit_amount:
            issues.append("debit_and_credit_both_set")
            amount_paise, txn_type = 0, "expense"
        elif debit_amount:
            amount_paise, txn_type = _decimal_to_paise(debit_amount), "expense"
            issues.append("direction_assumed")
        elif credit_amount:
            amount_paise, txn_type = _decimal_to_paise(credit_amount), "income"
            issues.append("direction_assumed")
        else:
            issues.append("missing_amount")
            amount_paise, txn_type = 0, "expense"

        if amount_paise is None:
            amount_paise = 0
            issues.append("invalid_amount")

        posted_date = row.get("date", "")
        if not posted_date:
            issues.append("missing_posted_date")

        rows.append(
            ParsedRow(
                raw_description=row.get("description", ""),
                posted_date=posted_date,
                amount_paise=amount_paise or 0,
                currency="INR",
                type=txn_type,
                reference=row.get("reference") or None,
                source_locator=locator,
                issue_codes=issues,
            )
        )
    return ParseResult(rows=rows, parser_id=PARSER_ID_CSV_MAPPED, parser_version=CSV_PARSER_VERSION)


def _parse_optional_decimal(text: str) -> Decimal | None:
    cleaned = (text or "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return value


def _decimal_to_paise(value: Decimal) -> int:
    paise = int(value * 100)
    return paise if 0 < paise <= MAX_AMOUNT_PAISE else 0


# ── PDF ─────────────────────────────────────────────────────────────────────────

PARSER_ID_PDF_SINGLE_LINE = "pdf_generic_single_line"
PARSER_ID_PDF_BLOCK = "pdf_generic_block"
PDF_PARSER_VERSION = "2.0"

MIN_EXTRACTED_CHARS_PER_PAGE = 20  # below this, treat the page as scanned/image-only
MIN_ROW_MATCH_RATIO = 0.5  # single-line format: fraction of non-blank lines that must match

# Format 1: one transaction per line -- "05/09/2026 Swiggy order 220.00 Dr"
_SINGLE_LINE_PATTERN = re.compile(
    r"^(?P<date>\d{2}[/-]\d{2}[/-]\d{4}|\d{4}-\d{2}-\d{2})\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s*"
    r"(?P<drcr>Dr|Cr)\s*$"
)

# Format 2: multi-line transaction blocks, seen in real Indian bank/credit-card
# statements where PDF text extraction linearizes a table into one value per
# line (date / description / category / amount Dr / secondary amount Cr).
_BLOCK_DATE_PATTERN = re.compile(r"^(?P<date>\d{2})/(?P<month>\d{2})/(?P<year>\d{4})$")
_BLOCK_AMOUNT_PATTERN = re.compile(r"^(?P<amount>[\d,]+\.\d{2})\s*(?P<drcr>Dr|Cr)$")
_BLOCK_MAX_LOOKAHEAD = 6

_TRANSFER_KEYWORDS = re.compile(
    r"PAYMENT RECEIVED|PAYMENT #|NEFT|IMPS|AUTOPAY|AUTO[- ]?DEBIT|NET ?BANKING|"
    r"\bMB PAYMENT\b|\bBBPS\b|CHEQUE DEPOSIT",
    re.IGNORECASE,
)


def _extract_text_pages(raw_bytes: bytes) -> list[str]:
    try:
        doc = fitz.open(stream=raw_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - PyMuPDF raises various types for a malformed file
        raise ParseError("unsupported_pdf_layout", "Could not read PDF structure") from exc

    if doc.is_encrypted:
        if not doc.authenticate(""):
            raise ParseError("encrypted_pdf_unsupported", "PDF is password-protected")

    return [page.get_text() for page in doc]


def parse_pdf(raw_bytes: bytes) -> ParseResult:
    pages = _extract_text_pages(raw_bytes)
    if not pages:
        raise ParseError("unsupported_pdf_layout", "PDF has no pages")

    avg_chars = sum(len(p) for p in pages) / len(pages)
    if avg_chars < MIN_EXTRACTED_CHARS_PER_PAGE:
        raise ParseError("scanned_pdf_unsupported", "PDF appears to be scanned/image-only")

    try:
        return _parse_pdf_single_line(pages)
    except ParseError:
        pass
    return _parse_pdf_block(pages)


def _parse_pdf_single_line(pages: list[str]) -> ParseResult:
    rows: list[ParsedRow] = []
    non_blank_lines = 0
    matched_lines = 0

    for page_number, page_text in enumerate(pages, start=1):
        for line_number, line in enumerate(page_text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            non_blank_lines += 1
            match = _SINGLE_LINE_PATTERN.match(stripped)
            if match is None:
                continue
            matched_lines += 1
            rows.append(_single_line_row_from_match(match, page_number, line_number))

    if non_blank_lines == 0 or (matched_lines / non_blank_lines) < MIN_ROW_MATCH_RATIO:
        raise ParseError(
            "unsupported_pdf_layout",
            f"Only {matched_lines}/{non_blank_lines} lines matched the single-line statement format",
        )
    return ParseResult(rows=rows, parser_id=PARSER_ID_PDF_SINGLE_LINE, parser_version=PDF_PARSER_VERSION)


def _normalize_single_line_date(date_text: str) -> str:
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_text):
        return date_text
    separator = "/" if "/" in date_text else "-"
    day, month, year = date_text.split(separator)
    return f"{year}-{month}-{day}"


def _single_line_row_from_match(match: re.Match, page_number: int, line_number: int) -> ParsedRow:
    locator = f"page {page_number} line {line_number}"
    amount_paise = _amount_to_paise(match.group("amount"), locator)
    txn_type = "expense" if match.group("drcr") == "Dr" else "income"
    return ParsedRow(
        raw_description=match.group("description").strip(),
        posted_date=_normalize_single_line_date(match.group("date")),
        amount_paise=amount_paise,
        currency="INR",
        type=txn_type,
        reference=None,
        source_locator=locator,
    )


def _parse_pdf_block(pages: list[str]) -> ParseResult:
    rows: list[ParsedRow] = []
    date_lines_seen = 0

    for page_number, page_text in enumerate(pages, start=1):
        lines = page_text.splitlines()
        i = 0
        while i < len(lines):
            date_match = _BLOCK_DATE_PATTERN.match(lines[i].strip())
            if date_match is None:
                i += 1
                continue
            date_lines_seen += 1

            description_parts: list[str] = []
            amount_match = None
            amount_line_index = None
            j = i + 1
            while j < len(lines) and j <= i + _BLOCK_MAX_LOOKAHEAD:
                candidate = lines[j].strip()
                found = _BLOCK_AMOUNT_PATTERN.match(candidate)
                if found:
                    amount_match = found
                    amount_line_index = j
                    break
                if candidate:
                    description_parts.append(candidate)
                j += 1

            if amount_match is None or not description_parts:
                i += 1
                continue
            # A real merchant description always has letters. A bare
            # date/number here means this block is a false match against a
            # summary-header figure, not an actual transaction row.
            if not any(re.search(r"[A-Za-z]", part) for part in description_parts):
                i += 1
                continue

            posted_date = f"{date_match['year']}-{date_match['month']}-{date_match['date']}"
            locator = f"page {page_number} line {i + 1}"
            rows.append(_block_row_from_match(amount_match, description_parts, posted_date, locator))

            next_index = amount_line_index + 1
            if next_index < len(lines) and _BLOCK_AMOUNT_PATTERN.match(lines[next_index].strip()):
                next_index += 1  # skip a secondary amount column (e.g. cashback), same block
            i = next_index

    if not rows or (date_lines_seen > 0 and len(rows) / date_lines_seen < MIN_ROW_MATCH_RATIO):
        raise ParseError(
            "unsupported_pdf_layout",
            f"Only {len(rows)}/{date_lines_seen} date-triggered blocks resolved to a transaction",
        )
    return ParseResult(rows=rows, parser_id=PARSER_ID_PDF_BLOCK, parser_version=PDF_PARSER_VERSION)


def _block_row_from_match(
    amount_match: re.Match, description_parts: list[str], posted_date: str, locator: str
) -> ParsedRow:
    amount_paise = _amount_to_paise(amount_match.group("amount"), locator)
    description = " | ".join(description_parts)
    issue_codes: list[str] = []

    if amount_match.group("drcr") == "Dr":
        txn_type = "expense"
    else:
        # A credit on a statement is a payment (transfer) or a merchant
        # refund -- never certain from the text alone, always flagged.
        txn_type = "transfer" if _TRANSFER_KEYWORDS.search(description) else "refund"
        issue_codes.append("credit_direction_assumed")

    return ParsedRow(
        raw_description=description,
        posted_date=posted_date,
        amount_paise=amount_paise,
        currency="INR",
        type=txn_type,
        reference=None,
        source_locator=locator,
        issue_codes=issue_codes,
    )


def _amount_to_paise(amount_text: str, locator: str) -> int:
    try:
        amount = Decimal(amount_text.replace(",", ""))
    except InvalidOperation as exc:
        raise ParseError("unsupported_pdf_layout", f"{locator}: malformed amount") from exc
    amount_paise = int(amount * 100)
    if amount_paise <= 0 or amount_paise > MAX_AMOUNT_PAISE:
        raise ParseError("unsupported_pdf_layout", f"{locator}: amount out of range")
    return amount_paise


def parse_statement(filename: str, raw_bytes: bytes) -> ParseResult:
    lower = filename.lower()
    if lower.endswith(".csv"):
        return parse_csv(raw_bytes)
    if lower.endswith(".pdf"):
        return parse_pdf(raw_bytes)
    raise ParseError("unsupported_file_type", f"Unsupported file extension: {filename}")
