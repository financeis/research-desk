"""DB reads and writes of the four flows tables and the two DB functions (Supabase REST).

This feature owns (migration 009):
- ``investor_flow_daily``: one row per stock and trading day. ``net_<key>`` are the 11 investors'
  net buying in millions of won as KIS gives it (NOT NULL); ``market_cap`` (won), ``window_days``,
  ``rank_<key>`` and ``ranked_at`` are written only by the DB function ``rank_investor_flows``;
  ``fetched_at`` is when the row was last received;
- ``investor_flow_runs``: one row per ``flows update`` run;
- ``investor_flow_days``: the ranked days (``trade_date``, ``ranked_at``, ``stocks_ranked``,
  ``run_id``);
- ``investor_flow_stocks``: each stock's data start (``listed_from``) once KIS showed the padding
  before its listing;
and calls the DB functions ``rank_investor_flows(p_day, p_caps, p_window, p_min_days)`` (returns the
number of rows it ranked) and ``investor_flow_coverage(p_since)`` (per stock: ``days``,
``first_day``, ``last_day`` of its rows from ``p_since`` on, in stock-code order).

Every function takes the supabase-py client first. Dates go and come as ``YYYY-MM-DD`` text, times
as ISO text with an offset.

- Raw rows are upserted ``UPSERT_ROWS`` a request on (stock_code, trade_date) and carry exactly the
  key, the 11 amounts and ``fetched_at`` (``RAW_COLUMNS``; any other column is a ValueError before
  any request). An upsert changes only the columns it sends, so the market cap, window size and
  ranks of a day the DB function ranked stay as they were; sending them would clear them. The
  same day received again is written again with the values received.
- Every read that can pass Supabase's 1000-row limit pages ``PAGE`` rows at a time in a fixed
  order, with a new query for each page: postgrest-py's ``range()`` adds offset/limit to the query
  instead of replacing them. The coverage function's answer is paged the same way (the limit holds
  for RPC answers too). One stock's latest rows are one request of at most ``PAGE`` rows.
- A day's ranked stocks (``rank_pe_fund`` and ``market_cap`` both set) are counted by the server
  (``count='exact'``, no row fetched).
- A run starts as ``running`` and is finished with its status, counts and message. "Latest" is the
  latest ``started_at``, then the higher ``run_id``.

Not atomic across requests: a stock's raw rows are one request (a stock has at most about 330),
and each request stands alone. If one fails, what was written before stays and the run is closed
as ``failed``; the next run asks again.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence

from . import logic

DAILY_TABLE = "investor_flow_daily"
RUNS_TABLE = "investor_flow_runs"
DAYS_TABLE = "investor_flow_days"
STOCKS_TABLE = "investor_flow_stocks"
RANK_FUNCTION = "rank_investor_flows"
COVERAGE_FUNCTION = "investor_flow_coverage"

PAGE = 1000          # Supabase REST answers at most 1000 rows a request
UPSERT_ROWS = 500    # rows in one upsert request
MINIMAL = "minimal"  # a write whose answer is not needed returns no rows

NET_COLUMNS: tuple[str, ...] = tuple(logic.net_column(key) for key in logic.INVESTOR_KEYS)
RANK_COLUMNS: tuple[str, ...] = tuple(logic.rank_column(key) for key in logic.INVESTOR_KEYS)
RAW_COLUMNS: tuple[str, ...] = ("stock_code", "trade_date", *NET_COLUMNS, "fetched_at")
DAILY_COLUMNS: tuple[str, ...] = ("stock_code", "trade_date", *NET_COLUMNS, "market_cap", "window_days",
                                  *RANK_COLUMNS, "ranked_at", "fetched_at")
RUN_COLUMNS: tuple[str, ...] = (
    "run_id", "started_at", "finished_at", "status", "as_of", "stocks_total", "stocks_ok",
    "stocks_failed", "days_ranked", "message",
)
DAY_COLUMNS: tuple[str, ...] = ("trade_date", "ranked_at", "stocks_ranked", "run_id")
# A ranked stock: its 사모 rank (all 11 ranks are written together) and the day's market cap.
RANKED_COLUMN = logic.rank_column("pe_fund")


def _columns(names: Sequence[str]) -> str:
    return ", ".join(names)


def _paged(query: Callable[[], Any]) -> list[dict]:
    """Every row of ``query()``, ``PAGE`` rows a request until a short page. ``query`` builds the
    ordered query anew for each page (see the module docstring)."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = query().range(offset, offset + PAGE - 1).execute().data or []
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        offset += PAGE


# ── raw rows ─────────────────────────────────────────────────────────────────

def upsert_daily(sb, rows: Sequence[Mapping[str, Any]]) -> None:
    """Insert or update raw rows by (stock_code, trade_date), ``UPSERT_ROWS`` a request. Each row
    holds exactly ``RAW_COLUMNS``; a row with any other column is a ValueError and nothing is sent."""
    rows = list(rows)
    expected = set(RAW_COLUMNS)
    for row in rows:
        if set(row) != expected:
            others = sorted(set(row) ^ expected)
            raise ValueError(f"a raw flows row holds exactly the key, the 11 amounts and fetched_at; "
                             f"not as sent: {others}")
    for start in range(0, len(rows), UPSERT_ROWS):
        (sb.table(DAILY_TABLE)
           .upsert(rows[start:start + UPSERT_ROWS], on_conflict="stock_code,trade_date", returning=MINIMAL)
           .execute())


def delete_daily_before(sb, day: str) -> None:
    """Delete the raw rows dated before ``day``."""
    sb.table(DAILY_TABLE).delete(returning=MINIMAL).lt("trade_date", day).execute()


def read_day(sb, day: str, columns: Sequence[str] = DAILY_COLUMNS) -> list[dict]:
    """``columns`` of every row of ``day``, in stock-code order, ``PAGE`` rows a request."""
    return _paged(lambda: sb.table(DAILY_TABLE).select(_columns(columns)).eq("trade_date", day)
                  .order("stock_code"))


def read_stock(sb, code: str, limit: int, columns: Sequence[str] = DAILY_COLUMNS) -> list[dict]:
    """``columns`` of the latest ``limit`` rows of one stock, newest first (one request; ``limit``
    at most ``PAGE``, else a ValueError)."""
    if not 0 < limit <= PAGE:
        raise ValueError(f"limit must be from 1 to {PAGE}, got {limit!r}")
    result = (sb.table(DAILY_TABLE).select(_columns(columns)).eq("stock_code", code)
                .order("trade_date", desc=True).limit(limit).execute())
    return result.data or []


def count_ranked(sb, day: str) -> int:
    """How many stocks are ranked on ``day`` (``rank_pe_fund`` and ``market_cap`` set): an exact
    count from the server; no row is fetched."""
    result = (sb.table(DAILY_TABLE)
                .select("stock_code", count="exact", head=True)
                .eq("trade_date", day)
                .not_.is_(RANKED_COLUMN, "null")
                .not_.is_("market_cap", "null")
                .execute())
    return int(result.count or 0)


# ── the DB functions ─────────────────────────────────────────────────────────

def rank_day(sb, day: str, caps: Mapping[str, int], *, window: int = logic.WINDOW_DAYS,
             min_days: int = logic.MIN_DAYS) -> int:
    """Have ``rank_investor_flows`` rank the unranked rows of ``day`` of the stocks in ``caps``
    (``{code: market cap in won}``) over ``window`` days with ``min_days`` at least; the number of
    rows it ranked."""
    result = sb.rpc(RANK_FUNCTION, {"p_day": day, "p_caps": dict(caps), "p_window": window,
                                    "p_min_days": min_days}).execute()
    return int(result.data or 0)


def read_coverage(sb, since: str) -> dict[str, dict]:
    """``{stock_code: {"stock_code", "days", "first_day", "last_day"}}`` of the stored rows from
    ``since`` on (``investor_flow_coverage``), ``PAGE`` rows a request; stocks without such rows are
    left out."""
    rows = _paged(lambda: sb.rpc(COVERAGE_FUNCTION, {"p_since": since}).order("stock_code"))
    return {row["stock_code"]: row for row in rows}


# ── data starts ──────────────────────────────────────────────────────────────

def read_listed_from(sb) -> dict[str, str]:
    """``{stock_code: listed_from}`` of every stock with a known data start, ``PAGE`` rows a request."""
    rows = _paged(lambda: sb.table(STOCKS_TABLE).select("stock_code, listed_from")
                  .not_.is_("listed_from", "null").order("stock_code"))
    return {row["stock_code"]: row["listed_from"] for row in rows}


def upsert_listed_from(sb, code: str, listed_from: str, updated_at: str) -> None:
    """Record ``code``'s data start."""
    (sb.table(STOCKS_TABLE)
       .upsert({"stock_code": code, "listed_from": listed_from, "updated_at": updated_at},
               on_conflict="stock_code", returning=MINIMAL)
       .execute())


# ── the ranked days ──────────────────────────────────────────────────────────

def read_ranked_days(sb) -> list[dict]:
    """Every ranked day's row, newest first, ``PAGE`` rows a request."""
    return _paged(lambda: sb.table(DAYS_TABLE).select(_columns(DAY_COLUMNS)).order("trade_date", desc=True))


def ranked_day(sb, day: str) -> Optional[dict]:
    """The ranked-day row of ``day``, or None."""
    result = sb.table(DAYS_TABLE).select(_columns(DAY_COLUMNS)).eq("trade_date", day).execute()
    return result.data[0] if result.data else None


def insert_ranked_day(sb, *, day: str, ranked_at: str, stocks_ranked: int, run_id: int) -> None:
    """Add ``day`` to the ranked days with every column."""
    (sb.table(DAYS_TABLE)
       .insert({"trade_date": day, "ranked_at": ranked_at, "stocks_ranked": stocks_ranked, "run_id": run_id},
               returning=MINIMAL)
       .execute())


def update_ranked_day(sb, day: str, *, stocks_ranked: int) -> None:
    """Set the ranked stock count of a day already in the list (its time and run stay)."""
    sb.table(DAYS_TABLE).update({"stocks_ranked": stocks_ranked}, returning=MINIMAL).eq("trade_date", day).execute()


def delete_ranked_days(sb, days: Sequence[str]) -> None:
    """Take ``days`` off the ranked days (no request for none)."""
    if days:
        sb.table(DAYS_TABLE).delete(returning=MINIMAL).in_("trade_date", list(days)).execute()


# ── run records ──────────────────────────────────────────────────────────────

def start_run(sb, *, started_at: str, total: int) -> int:
    """Record a new ``running`` run; returns its run_id."""
    result = (sb.table(RUNS_TABLE)
                .insert({"started_at": started_at, "status": logic.RUNNING, "stocks_total": total})
                .execute())
    return result.data[0]["run_id"]


def finish_run(sb, run_id: int, *, status: str, finished_at: str, as_of: Optional[str] = None,
               stocks_ok: Optional[int] = None, stocks_failed: Optional[int] = None,
               days_ranked: Optional[int] = None, message: Optional[str] = None) -> None:
    """Close run ``run_id`` with its status, end time, as_of, counts and message."""
    (sb.table(RUNS_TABLE)
       .update({"status": status, "finished_at": finished_at, "as_of": as_of, "stocks_ok": stocks_ok,
                "stocks_failed": stocks_failed, "days_ranked": days_ranked, "message": message},
               returning=MINIMAL)
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
    return _latest(sb.table(RUNS_TABLE).select(_columns(RUN_COLUMNS))
                   .in_("status", list(logic.SUCCESS_STATUSES)))
