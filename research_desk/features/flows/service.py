"""Flows service (수급): the DB preparation, the run records other features read, and the web answers
(one ranked day's ranking, one company's flows).

Readiness. What a preparation makes is kept for the life of the process; a failed one is not
remembered, so a value added to ``.env`` or a stock list file put in place counts on the next call.
Creating the service reads nothing; each preparation runs under a lock, so it happens once however
many threads ask at the same time.
- The DB: ``core.settings.load_env()``, then SUPABASE_URL and SUPABASE_SERVICE_KEY →
  ``core.db.supabase_client``. Without them: ``NotReady("수급", "DB 접속 설정(SUPABASE_URL,
  SUPABASE_SERVICE_KEY)이 없습니다")``. ``latest_run`` needs only this.
- The stock list, for the web answers, after the DB: ``load_env()``, then
  ``domain.stocks.StockList.load(core.settings.krx_csv_path())``. Unreadable: ``NotReady("수급",
  "종목표 파일을 읽을 수 없습니다")``; the path and the cause go to the local log only. A list whose
  content differs from its version file (or without one) only logs a warning.

The ranking (``ranking``, ``GET /api/flows``), after the readiness: the ranked days, read on every
request, newest first, at most ``logic.KEEP_RANKED_DAYS``. Without any: ``NotReady("수급", "아직
순위를 매긴 날이 없습니다(python -m research_desk flows update)")``, not remembered. The day asked
(default: the latest) must be one of them, else ``DayNotRanked``. Then that day's rows
(``store.read_day``, the columns the table needs) go through ``logic.ranking_table`` (the group, the
scores, the sort and the limit); each returned row gets its name and market from the stock list
(``""`` and None for a code not in it; the market by ``logic.market_of``) and its report counts, from
one ``coverage.report_counts(codes, days=365)`` call for the returned codes (no call without rows).

A company's flows (``stock_flows``, ``GET /api/stocks/{code}/flows``), after the readiness:
``StockNotFound`` for a code not in the stock list (looked up as given, no zero-padding),
``NoFlowData`` without any stored row. Then, from the stock's latest ``STOCK_ROWS`` (250) rows
(``store.read_stock``, newest first):
- ``as_of`` is the latest of them the DB function ranked (``ranked_at`` set), with its market cap
  (won) and window size; each of the 11 investors (``logic.INVESTORS`` order) has that day's amount
  in won, the amount as a percent of that market cap (None without a cap above 0) and its rank
  (None under 60 days, the window size still given). Without a ranked row all of these are None.
- The series is the period's latest rows (``PERIODS``: 21 / 63 / 126 / 250 trading days, fewer
  when the stock has fewer), oldest first: the dates, the 11 amounts in won, and the adjusted closes
  from one ``prices.daily_closes([code], since=<first date>)`` call (None for a day without one).

The router turns ``StockNotFound`` / ``NoFlowData`` / ``DayNotRanked`` into its 404 sentences; this
module raises plain exceptions. The report-count window's NotReady (``리포트``, …) and the price
window's (``주가``) pass through unchanged. Any other error after the readiness (a DB outage) stays
that request's error. The web calls neither KIS nor an AI.

Light imports only: no FastAPI here, and neither ``core.db`` (supabase-py, and with it an HTTP
library) nor the ``coverage`` / ``prices`` windows at the top (the coverage window binds its router,
so it loads FastAPI), because ``cli.py`` imports the flows window, and with it this module, for every
command. ``core.db`` is imported when the DB is prepared and the two windows inside the functions
that use them. ``flows update`` opens its own client with ``connect()``.

No run yet is not a readiness problem: ``latest_run`` answers None.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from threading import Lock
from typing import Any, Mapping, Optional, Sequence

from research_desk.core import settings as core_settings
from research_desk.core.settings import NotReady
from research_desk.domain.stocks import (
    VERSION_INVALID,
    VERSION_MISMATCH,
    VERSION_MISSING,
    StockList,
    StockListError,
)

from . import logic, store

logger = logging.getLogger(__name__)

AREA = "수급"
DB_NOT_CONFIGURED = "DB 접속 설정(SUPABASE_URL, SUPABASE_SERVICE_KEY)이 없습니다"
STOCK_LIST_UNREADABLE = "종목표 파일을 읽을 수 없습니다"
NO_RANKED_DAY = "아직 순위를 매긴 날이 없습니다(python -m research_desk flows update)"

_VERSION_PROBLEMS = {
    VERSION_MISSING: "종목표 버전 정보 파일이 없습니다",
    VERSION_MISMATCH: "종목표 파일 내용이 버전 정보와 다릅니다",
    VERSION_INVALID: "종목표 버전 정보 파일을 읽을 수 없습니다",
}

COVERAGE_DAYS = 365   # the report counts' look-back, days (the same as the peers answers')
# The company tab's periods, in trading days.
PERIODS: dict[str, int] = {"1m": 21, "3m": 63, "6m": 126, "1y": 250}
DEFAULT_PERIOD = "3m"
STOCK_ROWS = max(PERIODS.values())   # the tab reads a stock's latest rows, as many as the longest period
STOCK_COLUMNS: tuple[str, ...] = ("trade_date", *store.NET_COLUMNS, "market_cap", "window_days",
                                  *store.RANK_COLUMNS, "ranked_at")


class StockNotFound(Exception):
    """The code is not in the stock list."""


class NoFlowData(Exception):
    """The stock has no stored row."""


class DayNotRanked(Exception):
    """The day asked is not one of the ranked days."""


def db_settings() -> tuple[str, str]:
    """``(SUPABASE_URL, SUPABASE_SERVICE_KEY)`` now; NotReady("수급", …) when either is missing."""
    url, key = core_settings.supabase_url(), core_settings.supabase_service_key()
    if not (url and key):
        raise NotReady(AREA, DB_NOT_CONFIGURED)
    return url, key


def connect() -> Any:
    """Re-read ``.env``, then open the Supabase REST client. NotReady without the DB settings."""
    core_settings.load_env()
    url, key = db_settings()
    from research_desk.core import db   # supabase-py loads only when the DB is used

    return db.supabase_client(url, key)


def load_stock_list() -> StockList:
    """Re-read ``.env``, then load the stock list from ``KRX_CSV_PATH``. NotReady when it cannot be
    read; a version problem only logs a warning."""
    core_settings.load_env()
    path = core_settings.krx_csv_path()
    try:
        stocks = StockList.load(path)
    except StockListError as exc:
        # The answer carries a fixed sentence; the path and the cause go to the local log only.
        logger.warning("수급 기능이 종목표를 읽지 못했습니다. 다음 요청 때 다시 시도합니다: %s", exc)
        raise NotReady(AREA, STOCK_LIST_UNREADABLE) from exc
    check = stocks.verify()
    if not check.ok:
        logger.warning("%s: %s. 수급 기능은 경고만 남기고 그대로 동작합니다. 종목표를 바꿨다면 "
                       "python -m research_desk stocks set-version --as-of <자료 기준일, YYYY-MM-DD>를 실행하세요.",
                       _VERSION_PROBLEMS.get(check.reason, check.reason), path)
    return stocks


def instant(value: Any) -> datetime:
    """A DB time (ISO text, or a datetime) as an aware datetime in UTC; no offset means UTC."""
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def day(value: Any) -> Optional[date]:
    """A DB date (``YYYY-MM-DD`` text, or a date) as a date; None stays None."""
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def ranking_columns(investor: str) -> tuple[str, ...]:
    """The columns ``logic.ranking_table`` reads for ``investor``: its amount and rank, the market
    cap, the window size and the reference ranks, each once."""
    return tuple(dict.fromkeys(("stock_code", "market_cap", "window_days", logic.net_column(investor),
                                logic.rank_column(investor), *map(logic.rank_column, logic.REF_INVESTORS))))


class FlowsService:
    """Flows reads for one process. Creating it reads nothing; the first read prepares the DB (and
    the first web answer the stock list)."""

    def __init__(self) -> None:
        self._client: Any = None
        self._stocks: Optional[StockList] = None
        self._prepare_lock = Lock()

    def client(self) -> Any:
        """The Supabase REST client. Prepares it now if needed; NotReady without DB settings."""
        client = self._client
        if client is None:
            with self._prepare_lock:
                client = self._client
                if client is None:
                    client = self._client = connect()
        return client

    def stock_list(self) -> StockList:
        """The stock list. Loads it now if needed; NotReady when it cannot be read."""
        stocks = self._stocks
        if stocks is None:
            with self._prepare_lock:
                stocks = self._stocks
                if stocks is None:
                    stocks = self._stocks = load_stock_list()
        return stocks

    def prepare(self) -> tuple[Any, StockList]:
        """The web answers' readiness: the DB first, then the stock list."""
        return self.client(), self.stock_list()

    def latest_run(self) -> Optional[dict]:
        """The latest run's start and status with the latest successful run's as_of; None without
        any run."""
        sb = self.client()
        last = store.latest_run(sb)
        if last is None:
            return None
        success = last if last["status"] in logic.SUCCESS_STATUSES else store.latest_successful_run(sb)
        return {
            "last_run_at": instant(last["started_at"]),
            "last_run_status": last["status"],
            "as_of": day(success["as_of"]) if success is not None else None,
        }

    # ── GET /api/flows ───────────────────────────────────────────────────────

    def ranking(self, investor: str = logic.DEFAULT_INVESTOR, min_cap: int = logic.DEFAULT_MIN_CAP,
                trade_day: Any = None, sort: str = logic.DEFAULT_SORT, limit: int = logic.DEFAULT_LIMIT) -> dict:
        """One ranked day's ranking (``trade_day``: a date, ``YYYY-MM-DD`` or None for the latest;
        see the module docstring)."""
        if investor not in logic.INVESTOR_KEYS or sort not in logic.SORTS:
            raise ValueError(f"unknown investor or sort: {investor!r}, {sort!r}")
        sb, stocks = self.prepare()
        days = [day(row["trade_date"]).isoformat() for row in store.read_ranked_days(sb)][:logic.KEEP_RANKED_DAYS]
        if not days:
            raise NotReady(AREA, NO_RANKED_DAY)
        chosen = days[0] if trade_day is None else day(trade_day).isoformat()
        if chosen not in days:
            raise DayNotRanked(chosen)
        table = logic.ranking_table(store.read_day(sb, chosen, ranking_columns(investor)), investor, min_cap,
                                    sort, limit)
        counts = _report_counts([row["code"] for row in table["rows"]])
        return {"date": chosen, "dates": days, "investor": investor, "min_cap": min_cap, "sort": sort,
                "total": table["total"], "rows": [_ranking_row(row, stocks, counts) for row in table["rows"]]}

    # ── GET /api/stocks/{code}/flows ─────────────────────────────────────────

    def stock_flows(self, code: str, period: str = DEFAULT_PERIOD) -> dict:
        """One company's flows (see the module docstring)."""
        if period not in PERIODS:
            raise ValueError(f"unknown period: {period!r}")
        sb, stocks = self.prepare()
        entry = stocks.lookup(code)
        if entry is None:
            raise StockNotFound(code)
        rows = store.read_stock(sb, code, STOCK_ROWS, STOCK_COLUMNS)   # newest first
        if not rows:
            raise NoFlowData(code)
        latest = next((row for row in rows if row.get("ranked_at") is not None), None)
        series = rows[:PERIODS[period]][::-1]
        dates = [day(row["trade_date"]).isoformat() for row in series]
        closes = _closes(code, dates[0])
        return {
            "stock": {"code": code, "name": entry.name, "market": logic.market_of(entry.market)},
            "as_of": day(latest["trade_date"]).isoformat() if latest is not None else None,
            "market_cap": _number(latest.get("market_cap")) if latest is not None else None,
            "window_days": _number(latest.get("window_days")) if latest is not None else None,
            "investors": [_investor_line(investor, latest) for investor in logic.INVESTORS],
            "period": period,
            "series": {
                "dates": dates,
                "close": [closes.get(listed) for listed in dates],
                "flows": {key: [logic.to_won(row.get(logic.net_column(key))) for row in series]
                          for key in logic.INVESTOR_KEYS},
            },
        }


# ── answer pieces ────────────────────────────────────────────────────────────

def _number(value: Any) -> Optional[int]:
    """A DB integer (bigint, smallint) as an int; None stays None."""
    return None if value is None else int(value)


def _ranking_row(row: Mapping[str, Any], stocks: StockList, counts: Mapping[str, Any]) -> dict:
    """A ``logic.ranking_table`` row with its name, market and report counts."""
    entry = stocks.lookup(row["code"])
    return {
        "no": row["no"],
        "code": row["code"],
        "name": entry.name if entry is not None else "",
        "market": logic.market_of(entry.market) if entry is not None else None,
        **{key: row[key] for key in ("market_cap", "amount", "pct", "rank", "window_days", "short_history",
                                     "score", "ref_ranks")},
        "coverage": counts.get(row["code"]),
    }


def _investor_line(investor: logic.Investor, row: Optional[Mapping[str, Any]]) -> dict:
    """One investor's amount (won), pct of the market cap and rank on the ranked ``row``; all None
    without one."""
    amount = pct = rank = None
    if row is not None:
        amount = logic.to_won(row.get(logic.net_column(investor.key)))
        cap = _number(row.get("market_cap"))
        if amount is not None and cap is not None and cap > 0:
            pct = amount * 100 / cap
        rank = _number(row.get(logic.rank_column(investor.key)))
    return {"key": investor.key, "name": investor.name, "amount": amount, "pct": pct, "rank": rank}


def _report_counts(codes: Sequence[str]) -> dict:
    """The coverage window's report counts of ``codes`` over the last ``COVERAGE_DAYS`` days; no
    call for no code."""
    if not codes:
        return {}
    from research_desk.features import coverage   # its window binds its router (FastAPI): web answers only

    return coverage.report_counts(list(codes), days=COVERAGE_DAYS)


def _closes(code: str, since: str) -> dict[str, int]:
    """``{"YYYY-MM-DD": adjusted close}`` of ``code`` from ``since`` on, from the prices window."""
    from research_desk.features import prices   # the window's names, looked up when called

    return {day(item["date"]).isoformat(): item["close"]
            for item in prices.daily_closes([code], since=since).get(code, [])}


# ── the process-wide service and the window functions ────────────────────────

_service: Optional[FlowsService] = None
_service_lock = Lock()


def get_service() -> FlowsService:
    """The process-wide service, created on first use (creating it reads nothing).

    The router depends on this, so every request shares the prepared DB client and stock list.
    Tests override it (``app.dependency_overrides[get_service]``) or replace ``_service``.
    """
    global _service
    with _service_lock:
        if _service is None:
            _service = FlowsService()
        return _service


def latest_run() -> Optional[dict]:
    """See the window (``research_desk.features.flows``)."""
    return get_service().latest_run()
