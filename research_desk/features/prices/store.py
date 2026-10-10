"""DB reads and writes of the three price tables (Supabase REST).

This feature owns ``stock_price_snapshot`` (one row per stock, the latest snapshot) and
``price_update_runs`` (one row per ``prices update`` run), spec §14 / migration 008, and
``stock_price_daily`` (the adjusted-close history: one row per stock and day, primary key
``(stock_code, trade_date)``), migration 009. Every function takes the supabase-py client as its
first argument. Values are stored as the logic makes them: returns in percent, money in whole won,
dates as ``YYYY-MM-DD``, times as ISO text with an offset.

- Snapshot upserts go ``UPSERT_ROWS`` rows a request, on ``stock_code``. A row changes only in the
  columns sent, so a ``{stock_code, flags}`` upsert leaves the values as they were.
- History upserts go ``UPSERT_ROWS`` rows a request, on ``(stock_code, trade_date)``, every row
  with all three columns, and ask for nothing back (``return=minimal``: a first fill writes a
  year for every stock). The rows before a day are deleted in one request, nothing sent back.
- Reads by code ask ``CODES_PER_QUERY`` codes a request (a short request address; each answer is
  under Supabase's 1000-row limit). The stored closes on given days are read day by day, the
  same way. The full read of stored returns pages ``PAGE`` rows at a time in stock-code order,
  and so does the read of codes' history from a day on (``CODES_PER_QUERY`` codes at a time, by
  stock code then day), with a new query for each page: postgrest-py's ``range()`` adds
  offset/limit to the query instead of replacing them.
- A run starts as ``running`` and is finished with its status, counts and message. "Latest" is the
  latest ``started_at``, then the higher ``run_id``.

Not atomic: a run's upserts are separate requests. If one fails half way, the stocks written so
far keep their new values and the run is closed as ``failed``; the next run writes them all again.
"""
from __future__ import annotations

from itertools import islice
from typing import Any, Iterable, Mapping, Optional, Sequence

from .logic import SUCCESS_STATUSES

SNAPSHOT_TABLE = "stock_price_snapshot"
RUNS_TABLE = "price_update_runs"
HISTORY_TABLE = "stock_price_daily"

PAGE = 1000            # Supabase REST answers at most 1000 rows a request
CODES_PER_QUERY = 200  # codes in one in_() filter
UPSERT_ROWS = 500      # rows in one upsert request
MINIMAL = "minimal"    # Prefer: return=minimal, the written or deleted rows are not sent back

SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "stock_code", "market", "as_of", "close", "market_cap", "avg_value_20d", "traded",
    "ret_1w", "ret_1m", "ret_3m", "ret_6m", "xret_1w", "xret_1m", "xret_3m", "xret_6m",
    "flags", "source", "updated_at",
)
RETURN_COLUMNS: tuple[str, ...] = ("stock_code", "market", "ret_1w", "ret_1m", "ret_3m", "ret_6m")
RUN_COLUMNS: tuple[str, ...] = (
    "run_id", "started_at", "finished_at", "status", "as_of", "stocks_total", "stocks_ok",
    "stocks_failed", "message",
)
HISTORY_COLUMNS: tuple[str, ...] = ("stock_code", "trade_date", "close")
HISTORY_KEY = "stock_code,trade_date"   # the primary key: the history upserts' conflict target


def _columns(names: Sequence[str]) -> str:
    return ", ".join(names)


# ── snapshots ────────────────────────────────────────────────────────────────

def upsert_snapshots(sb, rows: Sequence[Mapping[str, Any]]) -> None:
    """Insert or update snapshot rows by stock_code, ``UPSERT_ROWS`` a request. All rows of one
    call should carry the same columns (a column missing from one row would be written as null)."""
    rows = list(rows)
    for start in range(0, len(rows), UPSERT_ROWS):
        sb.table(SNAPSHOT_TABLE).upsert(rows[start:start + UPSERT_ROWS], on_conflict="stock_code").execute()


def read_snapshots(sb, codes: Sequence[str], columns: Sequence[str] = SNAPSHOT_COLUMNS) -> dict[str, dict]:
    """``{stock_code: row}`` of the stored snapshots of ``codes`` (distinct codes; absent ones are
    left out), ``CODES_PER_QUERY`` codes a request."""
    codes = list(codes)
    found: dict[str, dict] = {}
    for start in range(0, len(codes), CODES_PER_QUERY):
        result = (sb.table(SNAPSHOT_TABLE)
                    .select(_columns(columns))
                    .in_("stock_code", codes[start:start + CODES_PER_QUERY])
                    .execute())
        found.update((row["stock_code"], row) for row in result.data or [])
    return found


def read_all_returns(sb) -> list[dict]:
    """Every stored snapshot's stock_code, market and ret_*, in stock-code order, ``PAGE`` rows a
    request until a short page."""
    rows: list[dict] = []
    offset = 0
    while True:
        result = (sb.table(SNAPSHOT_TABLE)
                    .select(_columns(RETURN_COLUMNS))
                    .order("stock_code")
                    .range(offset, offset + PAGE - 1)
                    .execute())
        page = result.data or []
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        offset += PAGE


# ── the adjusted-close history ───────────────────────────────────────────────

def upsert_history(sb, rows: Iterable[Mapping[str, Any]]) -> None:
    """Insert or update history rows ``{stock_code, trade_date, close}`` by (stock_code,
    trade_date), ``UPSERT_ROWS`` a request, nothing sent back. ``rows`` is read as it goes (a
    generator keeps a first fill's year of rows for every stock out of memory)."""
    rows = iter(rows)
    while batch := list(islice(rows, UPSERT_ROWS)):
        sb.table(HISTORY_TABLE).upsert(batch, on_conflict=HISTORY_KEY, returning=MINIMAL).execute()


def read_history_on(sb, codes_by_day: Mapping[str, Sequence[str]]) -> dict[str, dict[str, Any]]:
    """``{stock_code: {trade_date: close}}`` of the stored history rows on the given days: for each
    day (``YYYY-MM-DD``) its codes, ``CODES_PER_QUERY`` a request (an answer holds one row per code
    at most). A code without a row on a day has no entry for it."""
    found: dict[str, dict[str, Any]] = {}
    for day, codes in codes_by_day.items():
        codes = list(codes)
        for start in range(0, len(codes), CODES_PER_QUERY):
            result = (sb.table(HISTORY_TABLE)
                        .select(_columns(HISTORY_COLUMNS))
                        .eq("trade_date", day)
                        .in_("stock_code", codes[start:start + CODES_PER_QUERY])
                        .execute())
            for row in result.data or []:
                found.setdefault(row["stock_code"], {})[str(row["trade_date"])[:10]] = row["close"]
    return found


def delete_history_before(sb, day: str) -> None:
    """Delete every history row with a trade_date before ``day`` (``YYYY-MM-DD``; that day is
    kept), one request, nothing sent back."""
    sb.table(HISTORY_TABLE).delete(returning=MINIMAL).lt("trade_date", day).execute()


def read_history(sb, codes: Sequence[str], since: str) -> list[dict]:
    """The stored history rows of ``codes`` (distinct) from ``since`` (``YYYY-MM-DD``, that day
    included) on, by stock_code then trade_date: ``CODES_PER_QUERY`` codes at a time, each read
    ``PAGE`` rows a request until a short page, with a new query for each page."""
    codes = list(codes)
    rows: list[dict] = []
    for start in range(0, len(codes), CODES_PER_QUERY):
        chunk = codes[start:start + CODES_PER_QUERY]
        offset = 0
        while True:
            result = (sb.table(HISTORY_TABLE)
                        .select(_columns(HISTORY_COLUMNS))
                        .in_("stock_code", chunk)
                        .gte("trade_date", since)
                        .order("stock_code")
                        .order("trade_date")
                        .range(offset, offset + PAGE - 1)
                        .execute())
            page = result.data or []
            rows.extend(page)
            if len(page) < PAGE:
                break
            offset += PAGE
    return rows


# ── run records ──────────────────────────────────────────────────────────────

def start_run(sb, *, started_at: str, total: int) -> int:
    """Record a new ``running`` run; returns its run_id."""
    result = (sb.table(RUNS_TABLE)
                .insert({"started_at": started_at, "status": "running", "stocks_total": total})
                .execute())
    return result.data[0]["run_id"]


def finish_run(sb, run_id: int, *, status: str, finished_at: str, as_of: Optional[str] = None,
               stocks_ok: Optional[int] = None, stocks_failed: Optional[int] = None,
               message: Optional[str] = None) -> None:
    """Close run ``run_id`` with its status, end time, as_of, counts and message."""
    (sb.table(RUNS_TABLE)
       .update({"status": status, "finished_at": finished_at, "as_of": as_of, "stocks_ok": stocks_ok,
                "stocks_failed": stocks_failed, "message": message})
       .eq("run_id", run_id)
       .execute())


def _latest(query) -> Optional[dict]:
    result = query.order("started_at", desc=True).order("run_id", desc=True).limit(1).execute()
    return result.data[0] if result.data else None


def latest_run(sb) -> Optional[dict]:
    """The latest run record (any status), or None."""
    return _latest(sb.table(RUNS_TABLE).select(_columns(RUN_COLUMNS)))


def latest_successful_run(sb) -> Optional[dict]:
    """The latest ``ok`` / ``partial`` run record, or None."""
    return _latest(sb.table(RUNS_TABLE).select(_columns(RUN_COLUMNS)).in_("status", list(SUCCESS_STATUSES)))
