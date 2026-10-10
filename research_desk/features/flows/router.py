"""Web addresses of the flows feature (수급). The window hands this router out through ``web_router()``,
so importing the window loads no FastAPI.

- ``GET /api/flows?investor=<key>&min_cap=<억 원>&date=YYYY-MM-DD&sort=score|rank|pct|amount&limit=<n>``
  → one ranked day's ranking ``{"date", "dates", "investor", "min_cap", "sort", "total", "rows"}``.
  ``investor``: one of the 11 investor keys (default ``pe_fund``); ``min_cap``: a whole number from
  0 to 10,000,000 (default 1000); ``date``: a real day written ``YYYY-MM-DD`` (default: the latest
  ranked day); ``sort`` default ``score``; ``limit``: 1 to 200 (default 50). Checks, in this order:
  422 (FastAPI's, before anything is prepared) for any other value; the service's readiness 503;
  503 without any ranked day; 404 ``그날의 수급 순위가 없습니다.`` for a day that is not one of the
  ranked days.
- ``GET /api/stocks/{code}/flows?period=1m|3m|6m|1y`` → one company's flows ``{"stock", "as_of",
  "market_cap", "window_days", "investors", "period", "series"}``. Checks, in this order: 422 for a
  code that is not six capital letters or digits (no zero-padding: ``5930`` is 422) or another
  period (default ``3m``); the readiness 503; 404 ``종목을 찾을 수 없습니다.`` for a code not in the
  stock list; 404 ``이 종목은 수급 자료가 없습니다.`` without any stored row (the screen compares this
  sentence as it is).

Both handlers are plain functions: the service reads the DB synchronously, so FastAPI runs them in
its thread pool. The service raises plain exceptions for the 404s and ``NotReady`` for readiness
(its own ``수급``, or the report-count and price windows' passed through); the web app answers
NotReady with its 503.
"""
import datetime
import re
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BeforeValidator

from . import logic
from .service import DEFAULT_PERIOD, PERIODS, DayNotRanked, NoFlowData, StockNotFound, get_service

CODE_PATTERN = r"^[0-9A-Z]{6}$"
MAX_MIN_CAP = 10_000_000   # 억 원
MAX_LIMIT = 200
DAY_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

DAY_NOT_RANKED = "그날의 수급 순위가 없습니다."
STOCK_NOT_FOUND = "종목을 찾을 수 없습니다."
NO_FLOW_DATA = "이 종목은 수급 자료가 없습니다."


def _day_text(value):
    """Only ``YYYY-MM-DD`` text goes on to the date check: pydantic alone would also take a timestamp
    such as ``0`` or a date with a time."""
    if isinstance(value, str) and not DAY_TEXT.fullmatch(value):
        raise ValueError("a day is written YYYY-MM-DD")
    return value


# A Literal of a tuple is the Literal of its items: the values come from one place.
Investor = Literal[logic.INVESTOR_KEYS]
Sort = Literal[logic.SORTS]
Period = Literal[tuple(PERIODS)]
Day = Annotated[datetime.date, BeforeValidator(_day_text)]

router = APIRouter()


@router.get("/api/flows")
def flows_ranking(investor: Investor = logic.DEFAULT_INVESTOR,
                  min_cap: Annotated[int, Query(ge=0, le=MAX_MIN_CAP)] = logic.DEFAULT_MIN_CAP,
                  date: Optional[Day] = None,
                  sort: Sort = logic.DEFAULT_SORT,
                  limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = logic.DEFAULT_LIMIT,
                  service=Depends(get_service)):
    try:
        return service.ranking(investor, min_cap, date, sort, limit)
    except DayNotRanked:
        raise HTTPException(404, DAY_NOT_RANKED) from None


@router.get("/api/stocks/{code}/flows")
def stock_flows(code: Annotated[str, Path(pattern=CODE_PATTERN)],
                period: Period = DEFAULT_PERIOD,
                service=Depends(get_service)):
    try:
        return service.stock_flows(code, period)
    except StockNotFound:
        raise HTTPException(404, STOCK_NOT_FOUND) from None
    except NoFlowData:
        raise HTTPException(404, NO_FLOW_DATA) from None
