"""A small flows world for the web tests: a stock list file, the flows tables (``FakeSupabase``)
with three ranked days, one stock's year of rows, report counts and adjusted closes, and an app with
this feature's router and the web app's NotReady → 503 answer.

Ranked days: ``DAY`` (2026-10-08, the latest), ``DAY_1`` (2026-10-07) and ``DAY_2`` (2026-10-06).
The rows of ``DAY`` (amounts in millions of won, caps in won, ``N`` = window_days):

======  ===========  =====  ===================  ===  =================================
code    stock list   pe     market cap           N    ranks pe / foreigner / other_corp
======  ===========  =====  ===================  ===  =================================
178320  KOSDAQ       14150  1,234,500,000,000    250  2 / 60 / 95 (foreigner −500)
005930  KOSPI        50000  400,000,000,000,000  250  30 / 1 / 120 (foreigner 90000)
000660  KOSPI        30000  100,000,000,000,000  250  1 / 3 / 200 (foreigner 1200)
247540  KOSDAQ        2000  500,000,000,000      120  5 / 7 / 8
950130  KOSDAQ GL.     800  200,000,000,000      250  10 / None / 4
217270  KONEX          300  150,000,000,000      250  40 / 50 / 60
999990  (not listed)  1000  300,000,000,000      250  20 / 21 / 22
111110  KOSDAQ         900  50,000,000,000       250  3 / 3 / 3 (below 1,000 억)
222220  KOSPI         −700  800,000,000,000      250  200 / 15 / 30 (foreigner 400)
440110  KOSDAQ         600  900,000,000,000       40  none (listed 40 trading days ago)
950200  KOSDAQ         500  —                    —    never ranked (30 rows)
======  ===========  =====  ===================  ===  =================================

So the 사모 group at the 1,000 억 floor is the first seven rows; worked out by hand (TS = 100 × (N −
rank) ÷ (N − 1); CS from the pcts 1.1462 / 0.0125 / 0.03 / 0.4 / 0.4 / 0.2 / 0.3333 %, the two 0.4
sharing the average place 5.5):

- score: 178320 (99.80), 247540 (85.82), 950130 (85.69), 999990 (71.18), 217270 (58.84),
  000660 (58.33), 005930 (44.18);
- rank (TS): 000660, 178320, 247540, 950130, 999990, 005930, 217270;
- pct: 178320, then 247540 before 950130 (same pct, larger TS), 999990, 217270, 000660, 005930;
- amount: 005930, 000660, 178320, 247540, 999990, 950130, 217270.

The 외국인 group is 222220 (97.19), 005930 (75.0), 000660 (49.60). ``DAY_1`` has rows of 178320
(사모 5000, cap 1,210,000,000,000, rank 70) and 005930 (41000, 399,000,000,000,000, rank 33): its
사모 group is 178320, 005930. ``DAY_2`` has a row of 178320 only (사모 −300). 178320 (``SEED``) has
260 stored days up to ``DAY`` (the oldest 10 beyond the 250 read), its adjusted closes missing on
``CLOSE_GAPS``; 440110 has 40 days, 950200 30 days and no close; 000100 is listed without any row.
"""
from __future__ import annotations

import csv
from datetime import date
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from research_desk.core.settings import NotReady
from research_desk.domain.stocks import HEADER
from research_desk.features import flows

from .fakes import DAILY, DAYS, KEYS, MARKET, NET, RANK, FakeSupabase

DAY, DAY_1, DAY_2 = date(2026, 10, 8), date(2026, 10, 7), date(2026, 10, 6)
NEXT_DAY = date(2026, 10, 9)              # a weekday after the latest ranked day
RANKED_AT = "2026-10-08T10:30:00+00:00"
SEED = "178320"
NEW_LISTING = "440110"                    # 40 trading days: ranked without ranks
NEVER_RANKED = "950200"                   # 30 trading days, none ranked, no close
NO_ROWS = "000100"                        # listed, nothing stored
NOT_LISTED = "999990"                     # rows, but not in the stock list

# code, name, market, 산업명(대), 산업명(중), 주요제품
STOCKS = [
    (SEED, "서진시스템", "KOSDAQ", "전자부품", "전자부품", "ESS"),
    ("005930", "삼성전자", "KOSPI", "반도체", "메모리반도체", "DRAM"),
    ("000660", "SK하이닉스", "KOSPI", "반도체", "메모리반도체", "DRAM, HBM"),
    ("247540", "이차전지소재", "KOSDAQ", "화학", "2차전지소재", "양극재"),
    ("950130", "해외상장사", "KOSDAQ GLOBAL", "제약", "진단", "진단키트"),
    ("217270", "코넥스회사", "KONEX", "소프트웨어", "게임", "게임"),
    ("111110", "소형회사", "KOSDAQ", "기계", "기계", "부품"),
    ("222220", "순매도회사", "KOSPI", "화학", "화학", "소재"),
    (NEW_LISTING, "신규상장", "KOSDAQ", "반도체", "비메모리", "SSD 컨트롤러"),
    (NEVER_RANKED, "미평가회사", "KOSDAQ", "제약", "바이오", "신약"),
    (NO_ROWS, "자료없음", "KOSPI", "건설", "건설", "건설"),
]

PE_GROUP_BY_SCORE = [SEED, "247540", "950130", NOT_LISTED, "217270", "000660", "005930"]
PE_GROUP_BY_RANK = ["000660", SEED, "247540", "950130", NOT_LISTED, "005930", "217270"]
PE_GROUP_BY_PCT = [SEED, "247540", "950130", NOT_LISTED, "217270", "000660", "005930"]
PE_GROUP_BY_AMOUNT = ["005930", "000660", SEED, "247540", NOT_LISTED, "950130", "217270"]
FOREIGNER_GROUP_BY_SCORE = ["222220", "005930", "000660"]

SEED_DAYS = [day for day in MARKET if day <= DAY][-260:]
CLOSE_GAPS = {date(2026, 10, 5), date(2026, 9, 24)}   # days without an adjusted close
SEED_CAP = 1_234_500_000_000
SEED_RANKS = {key: 100 + i for i, key in enumerate(KEYS)} | {"pe_fund": 2, "foreigner": 60, "other_corp": 95}
SEED_AMOUNTS = {key: 0 for key in KEYS} | {"pe_fund": 14150, "foreigner": -500, "other_corp": 300,
                                           "individual": -13950}


def write_stock_list(path, rows=STOCKS):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(rows)
    return path


# ── the stored rows ──────────────────────────────────────────────────────────

def history_amount(code: str, day: date, index: int) -> int:
    """A fixed, varied, signed amount (millions of won) of one investor on one day."""
    return (day.toordinal() * 31 + index * 17 + sum(map(ord, code)) * 7) % 2001 - 1000


def stored(code: str, day: date, *, cap: Optional[int] = None, window: Optional[int] = None,
           ranks: Optional[dict] = None, amounts: Optional[dict] = None) -> dict:
    """One stored row of ``code`` on ``day``: the 11 amounts in millions of won (``amounts`` by key,
    else the fixed history amounts); ranked when ``cap`` is given: that market cap (won), the window
    size ``window`` and the ranks ``ranks`` ({key: rank}, the keys left out None; None = no ranks)."""
    if amounts is None:
        values = {key: history_amount(code, day, i) for i, key in enumerate(KEYS)}
    else:
        assert set(amounts) <= set(KEYS), amounts
        values = {key: amounts.get(key, 0) for key in KEYS}
    row = {"stock_code": code, "trade_date": day.isoformat(),
           **{net: values[key] for key, net in zip(KEYS, NET)}}
    if cap is not None:
        row |= {"market_cap": cap, "window_days": window, "ranked_at": RANKED_AT,
                **{rank: (ranks or {}).get(key) for key, rank in zip(KEYS, RANK)}}
    return row


def ranked(code: str, day: date, cap: int, window: int, pe_fund: int, ranks: tuple, **amounts: int) -> dict:
    """A ranked row: 사모 amount ``pe_fund``, ``ranks`` = (pe_fund, foreigner, other_corp), the other
    amounts 0 unless given."""
    pe_rank, foreigner_rank, other_corp_rank = ranks
    return stored(code, day, cap=cap, window=window,
                  ranks={"pe_fund": pe_rank, "foreigner": foreigner_rank, "other_corp": other_corp_rank},
                  amounts={"pe_fund": pe_fund, **amounts})


def day_rows() -> list[dict]:
    """The rows of ``DAY`` other than the seed's (see the module table)."""
    return [
        ranked("005930", DAY, 400_000_000_000_000, 250, 50000, (30, 1, 120), foreigner=90000),
        ranked("000660", DAY, 100_000_000_000_000, 250, 30000, (1, 3, 200), foreigner=1200),
        ranked("247540", DAY, 500_000_000_000, 120, 2000, (5, 7, 8)),
        ranked("950130", DAY, 200_000_000_000, 250, 800, (10, None, 4)),
        ranked("217270", DAY, 150_000_000_000, 250, 300, (40, 50, 60)),
        ranked(NOT_LISTED, DAY, 300_000_000_000, 250, 1000, (20, 21, 22)),
        ranked("111110", DAY, 50_000_000_000, 250, 900, (3, 3, 3)),
        ranked("222220", DAY, 800_000_000_000, 250, -700, (200, 15, 30), foreigner=400),
    ]


def seed_rows() -> list[dict]:
    """178320's 260 days: unranked history, then ``DAY_2``, ``DAY_1`` and ``DAY`` ranked."""
    rows = [stored(SEED, day) for day in SEED_DAYS[:-3]]
    rows.append(stored(SEED, DAY_2, cap=1_200_000_000_000, window=250,
                       ranks={key: 50 + i for i, key in enumerate(KEYS)},
                       amounts={"pe_fund": -300, "foreigner": 700}))
    rows.append(stored(SEED, DAY_1, cap=1_210_000_000_000, window=250,
                       ranks={key: 70 + i for i, key in enumerate(KEYS)},
                       amounts={"pe_fund": 5000, "foreigner": 100}))
    rows.append(stored(SEED, DAY, cap=SEED_CAP, window=250, ranks=SEED_RANKS, amounts=SEED_AMOUNTS))
    return rows


def short_rows(code: str, count: int) -> list[dict]:
    """``count`` unranked days of ``code`` up to ``DAY``."""
    return [stored(code, day) for day in [day for day in MARKET if day <= DAY][-count:]]


def new_listing_rows() -> list[dict]:
    """440110's 40 days; ``DAY`` ranked with N = 40, so without ranks."""
    rows = short_rows(NEW_LISTING, 40)
    rows[-1] = stored(NEW_LISTING, DAY, cap=900_000_000_000, window=40, ranks=None,
                      amounts={"pe_fund": 600, "foreigner": -250, "individual": -350})
    return rows


def ranked_day(day: date, stocks: int = 9, run_id: int = 1) -> dict:
    return {"trade_date": day.isoformat(), "ranked_at": RANKED_AT, "stocks_ranked": stocks, "run_id": run_id}


def make_db(days: Optional[list[date]] = None) -> FakeSupabase:
    """The flows tables of the world; ``days`` replaces the ranked-day list (newest or not)."""
    daily = (seed_rows() + day_rows() + new_listing_rows() + short_rows(NEVER_RANKED, 30)
             + [ranked("005930", DAY_1, 399_000_000_000_000, 250, 41000, (33, 2, 110), foreigner=80000)])
    listed = [DAY, DAY_1, DAY_2] if days is None else days
    return FakeSupabase(**{DAILY: daily, DAYS: [ranked_day(day) for day in listed]})


# ── report counts and adjusted closes ────────────────────────────────────────

def counts_record(code: str) -> dict:
    """The report counts the stand-in gives ``code`` (the coverage window's shape)."""
    many = code in ("005930", "000660")
    return {"stock_reports": 5 if many else 0, "sector_mentions": 2, "other_research": 1,
            "last_stock_report_date": "2026-10-01" if many else None, "brokers": 3 if many else 0,
            "label": "covered" if many else "none"}


class Counts:
    """Stand-in for ``coverage.report_counts``: every code asked; ``calls`` keeps the codes and the
    options of each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, codes, **options):
        assert not isinstance(codes, str), "one plain string is a TypeError in the real window"
        codes = list(codes)
        self.calls.append((codes, options))
        return {code: counts_record(code) for code in codes}


def close_of(day: date) -> int:
    return 50_000 + (day.toordinal() % 97) * 10


def closes_table() -> dict[str, dict[str, int]]:
    """``{code: {"YYYY-MM-DD": close}}``: the seed's days but ``CLOSE_GAPS``, the new listing's days."""
    seed = {day.isoformat(): close_of(day) for day in SEED_DAYS if day not in CLOSE_GAPS}
    listing = {row["trade_date"]: close_of(date.fromisoformat(row["trade_date"])) for row in new_listing_rows()}
    return {SEED: seed, NEW_LISTING: listing}


class Closes:
    """Stand-in for ``prices.daily_closes(codes, since)``: the stored closes from ``since`` on, oldest
    first, for the codes that have any; ``calls`` keeps (codes, since)."""

    def __init__(self, table: Optional[dict] = None) -> None:
        self.table = closes_table() if table is None else table
        self.calls: list[tuple[list[str], Any]] = []

    def __call__(self, codes, since):
        assert not isinstance(codes, str), "one plain string is a TypeError in the real window"
        codes = list(codes)
        self.calls.append((codes, since))
        first = since if isinstance(since, str) else since.isoformat()
        found = {}
        for code in codes:
            days = sorted(day for day in self.table.get(code, {}) if day >= first)
            if days:
                found[code] = [{"date": day, "close": self.table[code][day]} for day in days]
        return found


# ── the app ──────────────────────────────────────────────────────────────────

def build_app(service: Any = None) -> FastAPI:
    """This feature's router (through the window's ``web_router()``), a health address, and the
    NotReady → 503 answer the web app adds. Without ``service`` the process-wide one is used."""
    app = FastAPI()
    app.include_router(flows.web_router())

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    if service is not None:
        from research_desk.features.flows.service import get_service

        app.dependency_overrides[get_service] = lambda: service

    @app.exception_handler(NotReady)
    async def not_ready(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    return app
