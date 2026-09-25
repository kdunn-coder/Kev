"""Reading a CRM deal-registration export into canonical records.

Deliberately tolerant about input shape, because nobody wants to hand-rename
columns in a Salesforce export before every refresh:

  * header aliases cover the common Salesforce / deal-reg field names
  * numeric sort prefixes, currency symbols and thousands separators are stripped
  * ambiguous D/M vs M/D dates are inferred from the data, not guessed
  * every row that cannot be used is counted and reported, never dropped silently
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from .taxonomy import key

# ---------------------------------------------------------------------------
# Canonical fields
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = ("created_date", "rep")

OPTIONAL_FIELDS = (
    "deal_reg_id", "isr", "pod", "stage", "deal_type",
    "country", "amount", "partner", "account", "close_date",
)

ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

#: Header aliases per canonical field, most specific first. Matching is done on
#: `key()` form, and a header only needs to *contain* an alias to match, so
#: "Deal Registration: Created Date" matches the alias "deal registration created date"
#: as well as the looser "created date".
HEADER_ALIASES: dict[str, list[str]] = {
    "deal_reg_id": [
        "deal registration id", "deal reg id", "deal registration name",
        "deal reg name", "deal registration number", "registration id",
        "deal registration", "deal reg", "opportunity id", "opportunity 18 digit id",
        "record id", "id",
    ],
    "created_date": [
        "deal registration created date", "registration created date",
        "deal reg created date", "date submitted", "submitted date",
        "submission date", "registration date", "date registered",
        "created date", "create date", "createddate", "date created", "created",
        "opportunity created date",
    ],
    "close_date": [
        "close date", "closedate", "expected close date", "forecast close date",
    ],
    "rep": [
        "opportunity owner", "deal registration owner", "account executive",
        "assigned ae", "assigned rep", "sales rep", "ae name", "ae owner",
        "owner full name", "owner name", "record owner", "rep name",
        "owner", "ae", "assigned to", "sales representative",
    ],
    "isr": [
        "inside sales rep", "inside sales representative", "isr name",
        "sdr name", "bdr name", "isr owner", "isr", "sdr", "bdr",
        "inside sales", "sales development rep",
    ],
    "pod": [
        "sales territory", "sales pod", "pod name", "pod", "territory name",
        "territory", "sales team", "team name", "team", "sales region",
        "sub region", "subregion", "region", "sales area", "district", "segment",
    ],
    "stage": [
        "deal registration stage", "registration stage", "deal reg stage",
        "deal registration status", "registration status", "approval status",
        "opportunity stage", "stage name", "stagename", "stage", "status",
    ],
    "deal_type": [
        "opportunity type", "deal type", "deal registration type",
        "registration type", "business type", "opportunity record type",
        "type of business", "sales type", "type",
    ],
    "country": [
        "billing country", "account billing country", "shipping country",
        "account country", "customer country", "end user country",
        "country code", "country",
    ],
    "amount": [
        "amount converted", "amount in company currency", "converted amount",
        "expected revenue", "annual contract value", "acv", "tcv",
        "total contract value", "registered amount", "deal value",
        "opportunity amount", "amount", "value", "revenue",
    ],
    "partner": [
        "partner account name", "partner account", "reseller name",
        "reseller account", "distributor name", "distributor",
        "partner name", "partner", "reseller", "channel partner",
    ],
    "account": [
        "account name", "end user account", "end customer", "customer name",
        "account", "company name", "company",
    ],
}


@dataclass
class Record:
    """One deal registration, normalized."""

    deal_reg_id: str
    created_date: dt.date | None
    close_date: dt.date | None
    rep: str
    isr: str
    pod_raw: str
    stage_raw: str
    deal_type_raw: str
    country: str
    amount: float | None
    partner: str
    account: str
    row_number: int

    # Filled in by the analysis layer.
    pod: str = ""
    stage: str = ""
    outcome: str = ""
    deal_type: str = ""
    month: str = ""


@dataclass
class LoadResult:
    records: list[Record]
    #: canonical field -> the header it was read from (absent if unresolved)
    resolved_headers: dict[str, str] = field(default_factory=dict)
    headers: list[str] = field(default_factory=list)
    #: reason -> count, for rows that could not be used
    skipped: dict[str, int] = field(default_factory=dict)
    #: human-readable notes worth surfacing in the report
    notes: list[str] = field(default_factory=list)
    total_rows: int = 0

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


# ---------------------------------------------------------------------------
# Header resolution
# ---------------------------------------------------------------------------


def resolve_headers(
    headers: Sequence[str], overrides: dict[str, Any] | None = None
) -> tuple[dict[str, str], list[str]]:
    """Map canonical fields to actual headers.

    Returns (field -> header, notes). Explicit overrides win. Otherwise each
    field takes the header with the best alias match, and a header is never
    assigned to two fields -- more specific fields claim first, which is what
    keeps a lone "Type" column from being grabbed as `deal_type` when a
    dedicated "Opportunity Type" exists.
    """
    notes: list[str] = []
    resolved: dict[str, str] = {}
    header_by_key = {key(h): h for h in headers if str(h).strip()}
    taken: set[str] = set()

    overrides = overrides or {}
    for fld, raw in overrides.items():
        if fld not in ALL_FIELDS:
            notes.append(f"Ignoring unknown field '{fld}' in column mapping config.")
            continue
        if raw is None:
            taken.add(f"__forced_absent__{fld}")
            resolved.pop(fld, None)
            continue
        match = header_by_key.get(key(raw))
        if match is None:
            notes.append(
                f"Column mapping for '{fld}' points at header {raw!r}, which is not "
                f"in the file. Falling back to auto-detection for this field."
            )
            continue
        resolved[fld] = match
        taken.add(match)

    forced_absent = {f.removeprefix("__forced_absent__") for f in taken if f.startswith("__forced_absent__")}

    # Score every (field, header) pair, then assign greedily by best score.
    candidates: list[tuple[int, int, str, str]] = []
    for fld in ALL_FIELDS:
        if fld in resolved or fld in forced_absent:
            continue
        for alias_rank, alias in enumerate(HEADER_ALIASES.get(fld, [])):
            for hk, header in header_by_key.items():
                if not hk:
                    continue
                if hk == alias:
                    score = 1000
                elif hk.endswith(" " + alias) or hk.startswith(alias + " "):
                    score = 500 - len(hk)
                elif alias in hk.split() or f" {alias} " in f" {hk} ":
                    score = 300 - len(hk)
                else:
                    continue
                # Earlier aliases are more specific, so they score higher.
                candidates.append((score - alias_rank, -len(hk), fld, header))

    candidates.sort(reverse=True)
    for _score, _tie, fld, header in candidates:
        if fld in resolved or header in taken:
            continue
        resolved[fld] = header
        taken.add(header)

    return resolved, notes


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

_AMOUNT_STRIP = re.compile(r"[^\d,.\-()]")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_SLASH_DATE = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})")
_TEXT_MONTH_DATE = re.compile(
    r"^(\d{1,2})?\s*[-\s]*([A-Za-z]{3,9})[-\s,]+(\d{1,2})?[-\s,]*(\d{4})"
)

_MONTH_NAMES = {
    m: i
    for i, name in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"],
        start=1,
    )
    for m in (name, name[:3])
}


def parse_amount(value: object) -> float | None:
    """Parse a currency-ish cell. Returns None for blanks and unparseable text."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "-", "none", "null"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = _AMOUNT_STRIP.sub("", text).strip("()")
    if not text:
        return None
    # Decide which separator is the decimal point. European exports use
    # "1.234,56"; US exports use "1,234.56".
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        frac = text.rsplit(",", 1)[-1]
        # A 3-digit tail is a thousands group unless there are several groups.
        text = text.replace(",", ".") if len(frac) in (1, 2) else text.replace(",", "")
    try:
        amount = float(text)
    except ValueError:
        return None
    return -amount if negative else amount


def _parse_date_parts(text: str) -> tuple[int, int, int] | str | None:
    """Return (y, m, d), or the literal 'ambiguous'/'dmy'/'mdy' hint, or None.

    For slash dates the caller has to resolve D/M vs M/D, so this returns a
    tuple with a marker instead of committing.
    """
    text = text.strip()
    if not text:
        return None
    # Drop any time component.
    text = re.split(r"[T ]", text, maxsplit=1)[0] if _ISO_DATE.match(text) else text

    m = _ISO_DATE.match(text)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = _TEXT_MONTH_DATE.match(text)
    if m:
        month = _MONTH_NAMES.get(m.group(2).lower())
        if month:
            day = int(m.group(1) or m.group(3) or 1)
            return (int(m.group(4)), month, day)

    m = _SLASH_DATE.match(text)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000 if y < 70 else 1900
        return ("slash", a, b, y)  # type: ignore[return-value]

    return None


def _build_date(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


class DateParser:
    """Parses a column of dates, inferring D/M vs M/D order from the whole column.

    A single ambiguous value can never be resolved, but a column almost always
    can: if any row has a first component above 12 the order is dayfirst, and
    if any row has a second component above 12 it is monthfirst.
    """

    def __init__(self, order: str = "auto") -> None:
        self.order = order if order in {"auto", "dayfirst", "monthfirst"} else "auto"
        self._dayfirst: bool | None = {"dayfirst": True, "monthfirst": False}.get(self.order)
        self._saw_ambiguous = False

    def sniff(self, values: Sequence[str]) -> None:
        if self._dayfirst is not None:
            return
        for raw in values:
            parts = _parse_date_parts(str(raw))
            if not isinstance(parts, tuple) or parts[0] != "slash":
                continue
            _, a, b, _y = parts
            if a > 12 and b <= 12:
                self._dayfirst = True
                return
            if b > 12 and a <= 12:
                self._dayfirst = False
                return
        # Every slash date is <=12/<=12: genuinely ambiguous.
        self._saw_ambiguous = True

    @property
    def inferred_order(self) -> str:
        if self._dayfirst is None:
            return "monthfirst (assumed; column was ambiguous)"
        return "dayfirst" if self._dayfirst else "monthfirst"

    @property
    def was_ambiguous(self) -> bool:
        return self._saw_ambiguous and self.order == "auto"

    def parse(self, value: object) -> dt.date | None:
        if value is None:
            return None
        if isinstance(value, dt.datetime):
            return value.date()
        if isinstance(value, dt.date):
            return value
        parts = _parse_date_parts(str(value))
        if parts is None:
            return None
        if isinstance(parts, tuple) and parts[0] == "slash":
            _, a, b, y = parts
            # Default to monthfirst when the column never disambiguated: US
            # locale is the Salesforce default.
            dayfirst = self._dayfirst if self._dayfirst is not None else False
            d, m = (a, b) if dayfirst else (b, a)
            built = _build_date(y, m, d)
            # If that order is impossible for this row, the other one is right.
            return built if built else _build_date(y, d, m)
        y, m, d = parts  # type: ignore[misc]
        return _build_date(y, m, d)


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Read a CSV or XLSX file into (headers, row dicts)."""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise SystemExit(
                f"Reading {path.name} needs openpyxl. Either run "
                f"`pip install openpyxl`, or re-save the file as CSV."
            ) from exc
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows_iter = ws.iter_rows(values_only=True)
        headers = [str(h).strip() if h is not None else "" for h in next(rows_iter, [])]
        rows = [dict(zip(headers, r)) for r in rows_iter]
        wb.close()
        return headers, rows

    raw = path.read_bytes()
    # Strip a UTF-8 BOM, which Salesforce exports include and which otherwise
    # corrupts the first header name.
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = []
    for row in reader:
        # Salesforce reports append footer lines ("Confidential Information -
        # Do Not Distribute", grand totals) with mostly-empty cells.
        if all((v is None or str(v).strip() == "") for k, v in row.items() if k):
            continue
        rows.append({(k.strip() if k else k): v for k, v in row.items()})
    return headers, rows


def load(
    path: Path,
    *,
    mapping_overrides: dict[str, Any] | None = None,
    date_order: str = "auto",
    drop_rows_without_rep: bool = False,
) -> LoadResult:
    """Load and normalize a deal-registration export."""
    headers, rows = _read_rows(path)
    resolved, notes = resolve_headers(headers, mapping_overrides)
    result = LoadResult(records=[], resolved_headers=resolved, headers=headers,
                        notes=notes, total_rows=len(rows))

    missing = [f for f in REQUIRED_FIELDS if f not in resolved]
    if missing:
        raise SystemExit(
            "Could not find a column for required field(s): "
            + ", ".join(missing)
            + ".\nHeaders in the file:\n  "
            + "\n  ".join(headers)
            + "\n\nPin the right header in config/columns.yml under `mapping:`."
        )

    def cell(row: dict[str, Any], fld: str) -> Any:
        header = resolved.get(fld)
        return row.get(header) if header else None

    def text(row: dict[str, Any], fld: str) -> str:
        v = cell(row, fld)
        return "" if v is None else str(v).strip()

    created_parser = DateParser(date_order)
    created_parser.sniff([text(r, "created_date") for r in rows[:2000]])
    if created_parser.was_ambiguous:
        notes.append(
            "Every date in the created-date column has both day and month <= 12, "
            "so D/M vs M/D order could not be inferred. Assuming month-first "
            "(US/Salesforce default). Set `date_order: dayfirst` in "
            "config/columns.yml if that is wrong -- it shifts records between months."
        )
    close_parser = DateParser(date_order)
    if "close_date" in resolved:
        close_parser.sniff([text(r, "close_date") for r in rows[:2000]])

    for i, row in enumerate(rows, start=2):  # start=2: row 1 is the header
        created = created_parser.parse(cell(row, "created_date"))
        if created is None:
            result.skip("unparseable or blank created date")
            continue

        rep = text(row, "rep")
        if not rep and drop_rows_without_rep:
            result.skip("no owner/rep and drop_rows_without_rep is set")
            continue

        result.records.append(
            Record(
                deal_reg_id=text(row, "deal_reg_id") or f"row-{i}",
                created_date=created,
                close_date=close_parser.parse(cell(row, "close_date")),
                rep=rep,
                isr=text(row, "isr"),
                pod_raw=text(row, "pod"),
                stage_raw=text(row, "stage"),
                deal_type_raw=text(row, "deal_type"),
                country=text(row, "country"),
                amount=parse_amount(cell(row, "amount")),
                partner=text(row, "partner"),
                account=text(row, "account"),
                row_number=i,
            )
        )

    result.notes.append(f"Created-date column parsed as {created_parser.inferred_order}.")
    return result


def iter_months(start: str, end: str) -> Iterator[str]:
    """Yield 'YYYY-MM' strings from start to end inclusive."""
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    y, m = sy, sm
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            m = 1
            y += 1


def month_of(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def shift_month(month: str, delta: int) -> str:
    y, m = (int(x) for x in month.split("-"))
    total = y * 12 + (m - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"
