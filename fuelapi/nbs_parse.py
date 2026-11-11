"""Parse NBS "Price Watch" Excel workbooks into state-level observations.

NBS has changed the layout of these workbooks many times (title rows, zone
sub-totals, side tables, one sheet per fuel, 50-column time series ...), so the
parser is structural rather than positional:

1. find the header row: the first row with two or more date cells;
2. the date columns are the contiguous run of date cells in that row;
3. every later row whose label (left of the date columns) is a state name is a
   data row; a row labelled AVERAGE / NATIONAL is the reported national figure.

Anything else in the sheet (zone tables, "states with the highest price",
year-on-year rows) is ignored.
"""
from __future__ import annotations

import datetime as dt
import io
import re
from dataclasses import dataclass, field

import openpyxl

from . import states

# Fuels this parser publishes. LPG and kerosene workbooks use undated
# "Average of ..." pivot headers and other units (kg, gallon); they are
# recognised and skipped rather than guessed at.
SUPPORTED_FUELS = ("pms", "ago")

PRICE_MIN = 20.0  # NGN per litre; guards against percentages / garbage cells
PRICE_MAX = 20000.0
MIN_STATES_PER_PERIOD = 30

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_RE = re.compile(
    r"(?<![a-z])(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")(?![a-z])"
)
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_STATE_HEADINGS = {"state", "states", "statelabel", "statename"}
_NATIONAL_LABELS = {"average", "national", "nationalaverage", "nigeria", "nationalavg"}


@dataclass(frozen=True)
class Observation:
    fuel: str
    state: str  # state slug
    period: str  # "YYYY-MM"
    price: float


@dataclass
class SheetResult:
    sheet: str
    fuel: str | None
    report_period: str | None = None  # month this sheet was published for
    periods: list[str] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    national: dict[str, float] = field(default_factory=dict)  # period -> reported avg
    warnings: list[str] = field(default_factory=list)
    skipped: str | None = None  # reason, if the sheet produced no data
    name_mismatch: bool = False  # sheet/file name disagrees with its month columns


@dataclass
class WorkbookResult:
    filename: str
    sheets: list[SheetResult] = field(default_factory=list)

    @property
    def observations(self) -> list[Observation]:
        return [o for s in self.sheets for o in s.observations]


def period_from_text(text: str) -> str | None:
    """Extract a single unambiguous "YYYY-MM" from a file or sheet name.

    Returns None when there is no month/year, or more than one month (e.g.
    "PMS_Report_AUG_SEPT_2025", "DIESEL JUNE 2015 - JULY 2022").
    """
    t = re.sub(r"[_\-.]+", " ", text.lower())
    months = {_MONTHS[m] for m in _MONTH_RE.findall(t)}
    years = set(_YEAR_RE.findall(t))
    if len(months) != 1 or len(years) != 1:
        return None
    return f"{int(next(iter(years))):04d}-{next(iter(months)):02d}"


def detect_fuel(text: str) -> str | None:
    """Classify text as 'pms', 'ago', 'lpg', 'dpk' (kerosene) or None."""
    t = re.sub(r"[_\-.]+", " ", text.lower())
    if "diesel" in t or "automotive gas oil" in t or re.search(r"\bago\b", t):
        return "ago"
    if "kerosene" in t or re.search(r"\b(nhk|hhk|dpk)\b", t):
        return "dpk"
    if "liquefied" in t or "cooking gas" in t or re.search(r"\b(lpg|gas)\b", t):
        return "lpg"
    if "premium motor" in t or "petrol" in t or re.search(r"\b(pms|fuel)\b", t):
        return "pms"
    return None


def _cell_period(value: object, max_year: int) -> str | None:
    """Return "YYYY-MM" if a header cell holds a month, else None."""
    if isinstance(value, dt.datetime):
        y, m = value.year, value.month
    elif isinstance(value, dt.date):
        y, m = value.year, value.month
    elif isinstance(value, str):
        # Pivot-table exports label months as "Average of Jun-22".
        t = re.sub(r"^(average|avg|mean)\s+of\s+", "", value.strip().lower())
        m1 = re.fullmatch(r"([a-z]{3,9})[\s\-',.]*(\d{2}|\d{4})", t)
        m2 = re.fullmatch(r"(\d{4})[\-/](\d{1,2})(?:[\-/]\d{1,2})?", t)
        if m1 and m1.group(1) in _MONTHS:
            y = int(m1.group(2))
            y = y + 2000 if y < 100 else y
            m = _MONTHS[m1.group(1)]
        elif m2:
            y, m = int(m2.group(1)), int(m2.group(2))
        else:
            return None
    else:
        return None
    if not (2010 <= y <= max_year and 1 <= m <= 12):
        return None
    return f"{y:04d}-{m:02d}"


def _to_price(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        t = value.replace(",", "").replace("₦", "").replace("N", "").strip()
        try:
            return float(t)
        except ValueError:
            return None
    return None


def _find_header(rows: list[tuple], max_year: int) -> tuple[int, list[tuple[int, str]]] | None:
    """Locate the header row. Returns (row index, [(column index, period), ...])."""
    for r, row in enumerate(rows):
        dated = [(c, _cell_period(v, max_year)) for c, v in enumerate(row)]
        dated = [(c, p) for c, p in dated if p]
        if not dated:
            continue
        if len(dated) < 2:
            # A single month column only counts as a header when the row also
            # has a "State" column heading (2021-23 raw-export sheets).
            labels = {states.normalize(v) for v in row if isinstance(v, str)}
            if not labels & _STATE_HEADINGS:
                continue
        # Keep only the contiguous run starting at the first date cell, so a
        # second table further right on the sheet is never mixed in.
        run = [dated[0]]
        for c, p in dated[1:]:
            if c == run[-1][0] + 1:
                run.append((c, p))
            else:
                break
        if run[0][0] >= 1:
            return r, run
    return None


def _row_label(row: tuple, first_date_col: int) -> str | None:
    for v in row[:first_date_col]:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def parse_sheet(title: str, rows: list[tuple], filename: str, today: dt.date | None = None,
                context_names: tuple[str, ...] = ()) -> SheetResult:
    max_year = (today or dt.date.today()).year + 1

    # Fuel: trust the sheet's own title/intro text first, then the filename.
    intro = " ".join(
        str(row[0]) for row in rows[:4] if row and isinstance(row[0], str)
    )
    fuel = detect_fuel(title) or detect_fuel(intro) or detect_fuel(filename)
    result = SheetResult(sheet=title, fuel=fuel)

    if fuel is None:
        result.skipped = "could not tell which fuel this sheet covers"
        return result
    if fuel not in SUPPORTED_FUELS:
        result.skipped = f"fuel '{fuel}' is not supported yet"
        return result

    header = _find_header(rows, max_year)
    if header is None:
        result.skipped = "no header row with month columns found"
        return result
    header_row, date_cols = header
    first_date_col = date_cols[0][0]

    seen_periods: dict[str, int] = {}
    for c, p in date_cols:
        if p in seen_periods:
            result.warnings.append(f"duplicate month column {p}; keeping the first")
        else:
            seen_periods[p] = c

    per_period: dict[str, dict[str, float]] = {p: {} for p in seen_periods}
    states_seen: set[str] = set()
    for row in rows[header_row + 1:]:
        label = _row_label(row, first_date_col)
        if label is None:
            continue
        state = states.lookup(label)
        is_national = states.normalize(label) in _NATIONAL_LABELS
        if state is None and not is_national:
            continue
        if state is not None:
            states_seen.add(state.slug)
        for period, c in seen_periods.items():
            price = _to_price(row[c]) if c < len(row) else None
            if price is None:
                continue
            if not (PRICE_MIN <= price <= PRICE_MAX):
                result.warnings.append(
                    f"{label} {period}: value {price!r} outside {PRICE_MIN}-{PRICE_MAX}, dropped"
                )
                continue
            if is_national:
                result.national.setdefault(period, price)
            elif state.slug in per_period[period]:
                result.warnings.append(f"{label} {period}: duplicate row, keeping the first")
            else:
                per_period[period][state.slug] = price
        if is_national and len(states_seen) >= MIN_STATES_PER_PERIOD:
            # The national row closes the main table. Older workbooks put
            # "states with the highest price" lists underneath it in the same
            # columns; those must not be read as data rows.
            break

    for period in sorted(per_period):
        values = per_period[period]
        if not values:
            continue
        if len(values) < MIN_STATES_PER_PERIOD:
            result.warnings.append(
                f"{period}: only {len(values)} states found, month dropped"
            )
            result.national.pop(period, None)
            continue
        result.periods.append(period)
        result.observations.extend(
            Observation(fuel=fuel, state=slug, period=period, price=price)
            for slug, price in sorted(values.items())
        )

    if not result.periods:
        result.skipped = "no month had enough state rows"
        return result

    # Cross-check the month columns against what the sheet, the workbook and
    # (for ZIPs) the archive are called. NBS misnames things often enough that
    # one agreeing name is accepted; if names exist and none agrees - e.g. a
    # workbook named APRIL whose columns stop at MARCH - we cannot know which
    # is right, so the caller quarantines the sheet.
    latest = max(result.periods)
    named = [p for p in map(period_from_text, (title, filename, *context_names)) if p]
    result.report_period = latest
    if named and latest not in named:
        result.name_mismatch = True
        result.report_period = named[0]
        result.warnings.append(
            f"name says {named[0]} but the newest month column is {latest}"
        )
    return result


def parse_workbook(data: bytes, filename: str, today: dt.date | None = None,
                   context_names: tuple[str, ...] = ()) -> WorkbookResult:
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    result = WorkbookResult(filename=filename)
    for ws in wb.worksheets:
        rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
        if not any(any(c is not None for c in r) for r in rows):
            continue
        result.sheets.append(parse_sheet(ws.title, rows, filename, today, context_names))
    return result
