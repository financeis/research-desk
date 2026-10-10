"""Investor-flow calculations (수급). Pure: no DB, network, settings or files.

Units. Net buying amounts are millions of won as KIS gives them (백만 원; the DB's ``net_<key>``
columns hold the same numbers); ``to_won`` turns them into whole won (× ``WON_PER_MILLION``).
Market caps are whole won; the ranking floor ``min_cap`` is in 억 원 (× ``WON_PER_EOK``). Percents
are floats (0.1146 means 0.1146 %) and are never rounded here. Days are ``date`` objects; inputs
also take "YYYY-MM-DD" text (the DB's form). The adjustable numbers are the module constants.

Investors (``INVESTORS``: ``Investor(key, name, fields)`` in screen order, the KIS fields summed):
pe_fund 사모 = pe_fund_ntby_tr_pbmn; foreigner 외국인 = frgn_reg_ntby_pbmn (KRX '외국인' is the
registered foreigners; 기타외국인 frgn_nreg is in no group); institution 기관합계 =
orgn_ntby_tr_pbmn + etc_orgt_ntby_tr_pbmn; pension 연기금 = fund_ntby_tr_pbmn +
etc_orgt_ntby_tr_pbmn; trust 투신 = ivtr_ntby_tr_pbmn; fin_invest 금융투자 = scrt_ntby_tr_pbmn;
insurance 보험 = insu_ntby_tr_pbmn; bank 은행 = bank_ntby_tr_pbmn; other_fin 기타금융 =
mrbn_ntby_tr_pbmn; other_corp 기타법인 = etc_corp_ntby_tr_pbmn; individual 개인 = prsn_ntby_tr_pbmn.

- ``investor_amounts(row)`` → ``{key: amount}`` (all 11, in order) of one KIS row
  (``KisClient.investor_daily``); a missing or blank (None) field counts as 0.
- ``institution_gap(amounts)`` = (금융투자 + 보험 + 투신 + 사모 + 은행 + 기타금융 + 연기금) − 기관합계
  (``INSTITUTION_PARTS``), millions of won; 0 when KIS's numbers add up. With the mapping above it
  equals scrt + insu + ivtr + pe_fund + bank + mrbn + fund − orgn (etc_orgt cancels out).
- ``daily_row(code, row)`` → the raw row to store for one trading row: ``{"stock_code",
  "trade_date": "YYYY-MM-DD", "net_<key>": amount, …11}`` and nothing else. The market cap, the
  window size and the ranks are written only by the DB function, so they are never in it.
- ``net_column(key)`` / ``rank_column(key)`` → ``"net_<key>"`` / ``"rank_<key>"``.

Trading days (``trading_rows(rows)`` → ``TradingRows(rows, padded, data_start)``). A stock's trading
days are the days KIS returned with a close above 0, so a halted day (a close, every amount 0) is
one. A day with a close of 0 or none is never stored. Such days before the first trading day of the
rows given (any such day, when the rows have no trading day) are KIS's zero padding before the
listing: ``padded`` is then true and ``data_start`` is the earliest day with a close above 0 in the
rows (None when there is none). A zero-close day after a trading day is dropped too but is no sign
of a listing. ``rows``: the trading rows, oldest first,
one per day (the last row of a day kept). Give it one KIS answer or several together.

Window and rank (``window_ranks(rows, day, window=250, min_days=60)`` → ``(N, ranks)``), the
reference calculation of the DB function ``rank_investor_flows``. ``rows`` are one stock's stored
rows in any order (``trade_date`` and ``net_<key>``; a None amount counts as 0; one row per day,
the last kept). The window of ``day`` is the latest ``window`` rows dated on or before it, the day
included; N is its row count. Each investor's rank is 1 + the number of window days whose amount is
larger than the day's: equal amounts share a rank, only larger days count, and days of net selling
are ranked too. ``ranks`` is ``{key: rank}`` (all 11, in order) when N ≥ ``min_days``, else None.
Rows after ``day`` are left out. Without a row on ``day`` the answer is ``(0, None)``: there is
nothing to rank (the DB function writes no row either). ``short_history(N)`` is N < ``WINDOW_DAYS``.

Anchor day (``anchor_day(now)``; ``now`` aware in any zone, counted in Korea): today when it is a
weekday at or after ``ANCHOR_CUTOFF`` (18:00), else the nearest weekday before today (Monday
morning and the weekend give Friday). It knows weekdays only; for a weekday holiday KIS answers up
to the trading day before. One KIS call gives the ``BLOCK_DAYS`` (30) trading days ending at the
anchor asked.

Calendar and filling. ``blocks`` are the KIS answers (row lists) of one stock in this run, newest
first, in the order asked: the latest block (asked at the run's anchor), then each older block,
asked at the anchor these functions return; asking stops when they return None. An answer that
brings no day older than the blocks before it (an empty answer included) means KIS has nothing
older. A short answer (fewer than ``BLOCK_DAYS`` rows) alone does not: a day KIS never returns, or
an answer cut short, would otherwise end the history early.

- ``calendar_days(blocks, size=310)``: the trading-day calendar, the latest ``size`` trading days,
  oldest first, of the blocks of the calendar stock ``CALENDAR_CODE`` (005930). It comes from this
  run's KIS answers only. Stored days are never taken, since a gap inside them could not be seen,
  so every run asks the calendar stock back about 11 blocks (330 days for 310).
- ``next_depth_anchor(blocks, size=310)``: the anchor of the next older block until the blocks hold
  ``size`` trading days (the calendar; ``size=WINDOW_DAYS`` gives a check run's 250 days, about 9
  blocks). None then, or when the blocks met pre-listing padding, gave no trading day, or the last
  answer brought no older day.
- ``retention_start(calendar)``: the first day of the retention range, the earliest of the
  calendar's latest ``CALENDAR_DAYS`` (310 = the 250-day window + 60 days to look back) days; None
  for an empty calendar.
- ``next_fill_anchor(calendar, coverage, listed_from, blocks)``: whether a stock needs an older
  block, given as the anchor to ask next (the day before the earliest day fetched so far), or None.
  ``calendar``: ``calendar_days`` (dates, ascending, not empty; empty is a ValueError).
  ``coverage``: the stock's row of the DB function ``investor_flow_coverage(retention_start(
  calendar))`` (``{"days", "first_day", "last_day"}`` of its stored rows from the retention start
  on), or None when it has none there. ``listed_from``: its known data start or None. The needed
  start is the retention start, or the data start when that is later. The answer is None when the
  blocks met pre-listing padding (the data start is reached: ``trading_rows`` gives it), gave no
  trading day, or the last block brought no day older than the blocks before (KIS has nothing
  older); when the earliest fetched day is on or before the needed start; or when the stored rows
  cover every calendar day from the needed start to the fetched days: the first stored day is on or
  before the needed start, ``days`` is at least the number of calendar days from the needed start to
  the last stored day (no hole), and no calendar day lies between the last stored day and the
  earliest fetched day. Otherwise the stock goes on back. So a stock stored up to yesterday asks one
  block; a new stock asks back to the needed start (11 blocks for 310 days) or until its padding;
  after a long outage only the blocks over the missed stretch are asked; a hole, or stored rows that
  start after the needed start without a known data start, send the stock back to the needed start.
  A day KIS never returns therefore makes every run ask the stock back to the needed start until
  that day leaves the range; what KIS gives is stored as it came.
- ``raw_cutoff(anchor)`` = the run's anchor day (기준일, ``anchor_day``; the caller passes it) −
  ``RAW_RETENTION_DAYS`` (470 calendar days). ``cleanup_cutoff(anchor, calendar)``: raw rows dated
  before it are deleted — the earlier of ``raw_cutoff(anchor)`` and ``retention_start(calendar)``
  (``raw_cutoff`` alone for an empty calendar), so a row on or after the retention start is never
  deleted. 470 days alone is not enough for that: 310 trading days span about 450 to 465 calendar
  days, and a run on a weekday holiday moves the anchor (``anchor_day`` gives the holiday) while
  KIS's calendar stays, so anchor − 470 can come within a few days of the retention start, or pass
  it. Past it, the cleanup would delete rows the fill plan needs: every stock would then be asked
  back to the retention start on every run (about 11 answers each) and those rows deleted again.
  ``days_to_drop(days, keep=60)``: the ranked days beyond the latest ``keep``
  (``KEEP_RANKED_DAYS``), oldest first.

Ranking day and market caps. The day to rank is the run's base trading day D, the latest trading day
received (``ranking_day(days)``; None without days). ``day_market_caps(snapshots, day)`` →
``{code: market_cap}`` from the prices window's ``snapshots()`` answer, only for the snapshots whose
``as_of`` is that day and whose market cap is above 0. A stock with an older snapshot or none has no
market cap that day: no older cap is used and none is worked out from prices.

Run status (``run_status(total, failed)``, the price update's rule): no failure → ``ok``; failures
up to ``MAX_FAILED_PERCENT`` (20) % of the stocks, exactly 20 % included → ``partial``; more →
``failed``.

Ranking table (``ranking_table(rows, investor="pe_fund", min_cap=1000, sort="score", limit=50)`` →
``{"total", "rows"}``), from one day's stored rows (``stock_code``, ``market_cap`` in won or None,
``window_days``, ``net_<key>``, ``rank_<key>``). The group: the rows whose chosen investor's amount is
above 0, whose market cap is there, above 0 and at least ``min_cap`` 억 원, and that have the
investor's rank and window size. pct = the amount in won ÷ the market cap × 100. The TS score
(``ts_score(N, rank)``) = 100 × (N − rank) ÷ (N − 1): 100 for the first, 0 for the N-th (100 for a
one-day window). The CS score (``cs_scores(pcts)``) = 100 × (r − 1) ÷ (M − 1), r the rank of the
row's pct from the smallest in the group (equal pcts get their average rank), M the group size; 100
when M = 1. score = (TS + CS) ÷ 2. Sorts (``SORTS``), larger first and codes ascending: ``score``
score → pct → amount → code; ``rank`` TS → pct → amount → code; ``pct`` pct → TS → amount → code;
``amount`` amount → pct → code. pct, TS, CS and score are worked out and compared exactly (as
fractions): truly equal values tie and go on to the next key, and only exactly equal pcts share a CS
rank. The returned ``pct`` and ``score`` (and ``ts_score`` / ``cs_scores``) are those exact values
rounded once to floats. ``total`` is the group size; ``rows`` are the first ``limit`` rows
in that order, ``no`` counted from 1, each ``{"no", "code", "market_cap" (won), "amount" (won),
"pct", "rank", "window_days", "short_history", "score", "ref_ranks": {"pe_fund", "foreigner",
"other_corp"}}`` (a missing reference rank is None). An unknown investor or sort, or a negative
limit, is a ValueError.

``market_of(listed)``: the stock list's market as KOSPI or KOSDAQ (KOSDAQ GLOBAL counts as
KOSDAQ), else None — the prices rule.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any, Iterable, Mapping, NamedTuple, Optional, Sequence
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


class Investor(NamedTuple):
    key: str
    name: str
    fields: tuple[str, ...]


INVESTORS: tuple[Investor, ...] = (
    Investor("pe_fund", "사모", ("pe_fund_ntby_tr_pbmn",)),
    Investor("foreigner", "외국인", ("frgn_reg_ntby_pbmn",)),
    Investor("institution", "기관합계", ("orgn_ntby_tr_pbmn", "etc_orgt_ntby_tr_pbmn")),
    Investor("pension", "연기금", ("fund_ntby_tr_pbmn", "etc_orgt_ntby_tr_pbmn")),
    Investor("trust", "투신", ("ivtr_ntby_tr_pbmn",)),
    Investor("fin_invest", "금융투자", ("scrt_ntby_tr_pbmn",)),
    Investor("insurance", "보험", ("insu_ntby_tr_pbmn",)),
    Investor("bank", "은행", ("bank_ntby_tr_pbmn",)),
    Investor("other_fin", "기타금융", ("mrbn_ntby_tr_pbmn",)),
    Investor("other_corp", "기타법인", ("etc_corp_ntby_tr_pbmn",)),
    Investor("individual", "개인", ("prsn_ntby_tr_pbmn",)),
)
INVESTOR_KEYS: tuple[str, ...] = tuple(investor.key for investor in INVESTORS)
INVESTOR_NAMES: dict[str, str] = {investor.key: investor.name for investor in INVESTORS}
# 금융투자 + 보험 + 투신 + 사모 + 은행 + 기타금융 + 연기금 = 기관합계
INSTITUTION_PARTS: tuple[str, ...] = (
    "fin_invest", "insurance", "trust", "pe_fund", "bank", "other_fin", "pension")
# The reference ranks every ranking row shows, whatever investor is chosen.
REF_INVESTORS: tuple[str, ...] = ("pe_fund", "foreigner", "other_corp")
DEFAULT_INVESTOR = "pe_fund"

# ── the adjustable values ────────────────────────────────────────────────────
WINDOW_DAYS = 250           # the rank's window, trading days
MIN_DAYS = 60               # fewer days in the window: no rank
REVIEW_DAYS = 60            # ranked days that can be looked at again
CALENDAR_DAYS = WINDOW_DAYS + REVIEW_DAYS   # the retention range, trading days
BLOCK_DAYS = 30             # trading days in one KIS answer
RAW_RETENTION_DAYS = 470    # calendar days of raw rows kept before the run's anchor day
KEEP_RANKED_DAYS = 60       # entries kept in the ranked-day list
ANCHOR_CUTOFF = time(18, 0)  # from this time on a weekday, that day's flows are asked
MAX_FAILED_PERCENT = 20     # more stocks than this percent failed: the run failed
CALENDAR_CODE = "005930"    # the stock whose trading days are the calendar (삼성전자)

RUNNING, OK, PARTIAL, FAILED = "running", "ok", "partial", "failed"
SUCCESS_STATUSES: tuple[str, ...] = (OK, PARTIAL)

WON_PER_MILLION = 1_000_000
WON_PER_EOK = 100_000_000

SORTS: tuple[str, ...] = ("score", "rank", "pct", "amount")
DEFAULT_SORT = "score"
DEFAULT_MIN_CAP = 1000      # 억 원
DEFAULT_LIMIT = 50

_MARKET_OF = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ", "KOSDAQ GLOBAL": "KOSDAQ"}


def net_column(key: str) -> str:
    return f"net_{key}"


def rank_column(key: str) -> str:
    return f"rank_{key}"


def market_of(listed: Optional[str]) -> Optional[str]:
    """The market of a stock-list market: KOSPI, KOSDAQ (KOSDAQ GLOBAL too), else None."""
    return _MARKET_OF.get((listed or "").strip())


def to_won(amount: Optional[int]) -> Optional[int]:
    """Millions of won as whole won; None stays None."""
    return None if amount is None else int(amount) * WON_PER_MILLION


def short_history(window_days: int) -> bool:
    """A window of fewer than ``WINDOW_DAYS`` days."""
    return window_days < WINDOW_DAYS


# ── investors ────────────────────────────────────────────────────────────────

def investor_amounts(row: Mapping[str, Any]) -> dict[str, int]:
    """``{key: amount}`` of one KIS row, millions of won; a missing or blank field counts as 0."""
    return {investor.key: sum(_amount(row.get(field)) for field in investor.fields)
            for investor in INVESTORS}


def institution_gap(amounts: Mapping[str, int]) -> int:
    """(금융투자 + 보험 + 투신 + 사모 + 은행 + 기타금융 + 연기금) − 기관합계, millions of won."""
    return sum(_amount(amounts.get(key)) for key in INSTITUTION_PARTS) - _amount(amounts.get("institution"))


def daily_row(code: str, row: Mapping[str, Any]) -> dict:
    """The raw row to store for one KIS trading row: the key and the 11 amounts only."""
    return {"stock_code": code, "trade_date": _as_date(row["date"]).isoformat(),
            **{net_column(key): amount for key, amount in investor_amounts(row).items()}}


# ── trading days ─────────────────────────────────────────────────────────────

class TradingRows(NamedTuple):
    rows: list            # the trading rows (close above 0), oldest first, one per day
    padded: bool          # zero-close days before the first trading day: padding before a listing
    data_start: Optional[date]   # when padded: the earliest trading day, else None


def trading_rows(rows: Iterable[Mapping[str, Any]]) -> TradingRows:
    """The trading rows of KIS answers, and the data start when they show pre-listing padding."""
    trading: dict[date, Mapping[str, Any]] = {}
    blank: list[date] = []
    for row in rows:
        day = _as_date(row["date"])
        if _is_trading(row):
            trading[day] = row
        else:
            blank.append(day)
    days = sorted(trading)
    first = days[0] if days else None
    padded = bool(blank) and (first is None or min(blank) < first)
    return TradingRows([trading[day] for day in days], padded, first if padded else None)


# ── the 250-day rank ─────────────────────────────────────────────────────────

def window_ranks(rows: Iterable[Mapping[str, Any]], day: Any, window: int = WINDOW_DAYS,
                 min_days: int = MIN_DAYS) -> tuple[int, Optional[dict[str, int]]]:
    """``(N, {key: rank} or None)`` of ``day`` from a stock's stored rows (see the module text)."""
    if window < 1:
        raise ValueError("the window needs at least one day")
    target = _as_date(day)
    by_day: dict[date, Mapping[str, Any]] = {}
    for row in rows:
        row_day = _as_date(row["trade_date"])
        if row_day <= target:
            by_day[row_day] = row
    if target not in by_day:
        return 0, None
    days = sorted(by_day)[-window:]
    count = len(days)
    if count < min_days:
        return count, None
    today = by_day[target]
    ranks = {}
    for key in INVESTOR_KEYS:
        column = net_column(key)
        value = _amount(today.get(column))
        ranks[key] = 1 + sum(1 for other in days if _amount(by_day[other].get(column)) > value)
    return count, ranks


# ── the anchor day ───────────────────────────────────────────────────────────

def anchor_day(now: datetime) -> date:
    """The day to ask KIS up to at ``now`` (aware, any zone), counted in Korea: today on a weekday
    at or after ``ANCHOR_CUTOFF``, else the nearest weekday before today."""
    local = now.astimezone(KST)
    today = local.date()
    if today.weekday() < 5 and local.time() >= ANCHOR_CUTOFF:
        return today
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


# ── the calendar and filling ─────────────────────────────────────────────────

def calendar_days(blocks: Sequence[Sequence[Mapping[str, Any]]], size: int = CALENDAR_DAYS) -> list[date]:
    """The latest ``size`` trading days of this run's KIS blocks, oldest first (no stored day)."""
    if size < 1:
        raise ValueError("the calendar needs at least one day")
    return _fetched(blocks)[0][-size:]


def next_depth_anchor(blocks: Sequence[Sequence[Mapping[str, Any]]],
                      size: int = CALENDAR_DAYS) -> Optional[date]:
    """The anchor of the next older block until the blocks hold ``size`` trading days, else None."""
    days, padded, progressed = _fetched(blocks)
    if padded or not days or not progressed or len(days) >= size:
        return None
    return days[0] - timedelta(days=1)


def retention_start(calendar: Sequence[date]) -> Optional[date]:
    """The earliest of the calendar's latest ``CALENDAR_DAYS`` days; None for an empty calendar."""
    if not calendar:
        return None
    return _as_date(calendar[max(0, len(calendar) - CALENDAR_DAYS)])


def next_fill_anchor(calendar: Sequence[date], coverage: Optional[Mapping[str, Any]], listed_from: Any,
                     blocks: Sequence[Sequence[Mapping[str, Any]]]) -> Optional[date]:
    """The anchor of the next older block a stock needs to fill its retention range, or None."""
    if not calendar:
        raise ValueError("the calendar is empty")
    days, padded, progressed = _fetched(blocks)
    if padded or not days or not progressed:
        return None
    earliest = days[0]
    needed = retention_start(calendar)
    listed = _as_date(listed_from)
    if listed is not None and listed > needed:
        needed = listed
    if earliest <= needed or _stored_reach(calendar, coverage, needed, earliest):
        return None
    return earliest - timedelta(days=1)


def raw_cutoff(anchor: Any) -> date:
    """The run's anchor day (the caller passes it) − ``RAW_RETENTION_DAYS``."""
    return _as_date(anchor) - timedelta(days=RAW_RETENTION_DAYS)


def cleanup_cutoff(anchor: Any, calendar: Sequence[Any]) -> date:
    """Raw rows dated before this day are deleted: the earlier of ``raw_cutoff(anchor)`` and the
    calendar's ``retention_start``, so a row the fill plan needs is never deleted;
    ``raw_cutoff(anchor)`` for an empty calendar."""
    cutoff = raw_cutoff(anchor)
    start = retention_start(calendar)
    return cutoff if start is None or cutoff <= start else start


def days_to_drop(days: Iterable[Any], keep: int = KEEP_RANKED_DAYS) -> list[date]:
    """The ranked days beyond the latest ``keep``, oldest first."""
    ordered = sorted({day for day in map(_as_date, days) if day is not None})
    return ordered[:-keep] if keep > 0 else ordered


# ── the ranking day and its market caps ──────────────────────────────────────

def ranking_day(days: Iterable[Any]) -> Optional[date]:
    """The latest of the trading days received; None without any."""
    found = [day for day in map(_as_date, days) if day is not None]
    return max(found) if found else None


def day_market_caps(snapshots: Mapping[str, Optional[Mapping[str, Any]]], day: Any) -> dict[str, int]:
    """``{code: market_cap}`` of the snapshots of ``day`` with a market cap above 0."""
    target = _as_date(day)
    caps: dict[str, int] = {}
    for code, price in snapshots.items():
        if not price or _as_date(price.get("as_of")) != target:
            continue
        cap = _whole(price.get("market_cap"))
        if cap is not None and cap > 0:
            caps[code] = cap
    return caps


# ── run status ───────────────────────────────────────────────────────────────

def run_status(total: int, failed: int) -> str:
    """``ok`` without failures, ``partial`` up to MAX_FAILED_PERCENT % failed, else ``failed``."""
    if failed <= 0:
        return OK
    if failed * 100 <= MAX_FAILED_PERCENT * total:
        return PARTIAL
    return FAILED


# ── the ranking table ────────────────────────────────────────────────────────

def ts_score(window_days: int, rank: int) -> float:
    """100 × (N − rank) ÷ (N − 1) as a float; 100 for a one-day window."""
    return float(_ts_exact(window_days, rank))


def cs_scores(pcts: Sequence[Any]) -> list[float]:
    """Each pct's 100 × (r − 1) ÷ (M − 1) as a float, r its rank from the smallest (equal values:
    their average rank), in the input order; [100.0] for one value."""
    return [float(score) for score in _cs_exact(pcts)]


def _ts_exact(window_days: int, rank: int) -> Fraction:
    if window_days <= 1:
        return Fraction(100)
    return Fraction(100 * (window_days - rank), window_days - 1)


def _cs_exact(pcts: Sequence[Any]) -> list[Fraction]:
    count = len(pcts)
    if count <= 1:
        return [Fraction(100)] * count
    order = sorted(range(count), key=lambda i: pcts[i])
    scores = [Fraction(0)] * count
    start = 0
    while start < count:
        end = start
        while end + 1 < count and pcts[order[end + 1]] == pcts[order[start]]:
            end += 1
        # places start..end (from 0) share the average rank (start + end) / 2 + 1
        score = Fraction(100 * (start + end), 2 * (count - 1))
        for position in range(start, end + 1):
            scores[order[position]] = score
        start = end + 1
    return scores


_SORT_KEYS = {
    "score": lambda entry: (-entry["score"], -entry["pct"], -entry["amount"], entry["code"]),
    "rank": lambda entry: (-entry["ts"], -entry["pct"], -entry["amount"], entry["code"]),
    "pct": lambda entry: (-entry["pct"], -entry["ts"], -entry["amount"], entry["code"]),
    "amount": lambda entry: (-entry["amount"], -entry["pct"], entry["code"]),
}


def ranking_table(rows: Iterable[Mapping[str, Any]], investor: str = DEFAULT_INVESTOR,
                  min_cap: int = DEFAULT_MIN_CAP, sort: str = DEFAULT_SORT,
                  limit: int = DEFAULT_LIMIT) -> dict:
    """``{"total", "rows"}`` of one day's ranking (see the module text)."""
    if investor not in INVESTOR_KEYS:
        raise ValueError(f"unknown investor: {investor!r}")
    if sort not in _SORT_KEYS:
        raise ValueError(f"unknown sort: {sort!r}")
    if limit < 0:
        raise ValueError("limit must not be negative")
    floor = min_cap * WON_PER_EOK
    net_key, rank_key = net_column(investor), rank_column(investor)
    group = []
    for row in rows:
        net, rank, window_days = row.get(net_key), row.get(rank_key), row.get("window_days")
        cap = _whole(row.get("market_cap"))
        if net is None or net <= 0 or rank is None or window_days is None:
            continue
        if cap is None or cap <= 0 or cap < floor:
            continue
        amount = int(net) * WON_PER_MILLION
        days, place = int(window_days), int(rank)
        # exact values: a float sum of TS and CS would order truly equal scores by rounding noise
        group.append({"row": row, "code": str(row["stock_code"]), "market_cap": cap, "amount": amount,
                      "pct": Fraction(amount * 100, cap), "rank": place, "window_days": days,
                      "ts": _ts_exact(days, place)})
    for entry, cs in zip(group, _cs_exact([entry["pct"] for entry in group])):
        entry["score"] = (entry["ts"] + cs) / 2
    group.sort(key=_SORT_KEYS[sort])
    return {"total": len(group),
            "rows": [_table_row(no, entry) for no, entry in enumerate(group[:limit], start=1)]}


def _table_row(no: int, entry: Mapping[str, Any]) -> dict:
    row = entry["row"]
    return {
        "no": no,
        "code": entry["code"],
        "market_cap": entry["market_cap"],
        "amount": entry["amount"],
        "pct": float(entry["pct"]),
        "rank": entry["rank"],
        "window_days": entry["window_days"],
        "short_history": short_history(entry["window_days"]),
        "score": float(entry["score"]),
        "ref_ranks": {key: _whole(row.get(rank_column(key))) for key in REF_INVESTORS},
    }


# ── small helpers ────────────────────────────────────────────────────────────

def _is_trading(row: Mapping[str, Any]) -> bool:
    close = row.get("close")
    return close is not None and close > 0


def _fetched(blocks: Sequence[Sequence[Mapping[str, Any]]]) -> tuple[list[date], bool, bool]:
    """(the blocks' trading days, oldest first; whether they met pre-listing padding; whether the
    last block brought a day older than the blocks before it)."""
    merged = trading_rows(row for rows in blocks for row in rows)
    days = [_as_date(row["date"]) for row in merged.rows]
    progressed = True
    if len(blocks) > 1:
        before = [_as_date(row["date"]) for rows in blocks[:-1] for row in rows if _is_trading(row)]
        last = [_as_date(row["date"]) for row in blocks[-1] if _is_trading(row)]
        progressed = bool(last) and (not before or min(last) < min(before))
    return days, merged.padded, progressed


def _stored_reach(calendar: Sequence[date], coverage: Optional[Mapping[str, Any]], needed: date,
                  earliest: date) -> bool:
    """Whether the stored rows cover every calendar day from ``needed`` up to ``earliest``."""
    if not coverage:
        return False
    count = int(coverage.get("days") or 0)
    first, last = _as_date(coverage.get("first_day")), _as_date(coverage.get("last_day"))
    if count <= 0 or first is None or last is None or first > needed:
        return False
    if count < bisect_right(calendar, last) - bisect_left(calendar, needed):
        return False   # a hole
    return bisect_left(calendar, earliest) <= bisect_right(calendar, last)   # no day in between


def _as_date(value: Any) -> Optional[date]:
    """A date from a date, a datetime or "YYYY-MM-DD…" text; None stays None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _amount(value: Any) -> int:
    return 0 if value is None else int(value)


def _whole(value: Any) -> Optional[int]:
    """A DB number (int, float or numeric text) as an int, rounded half up; None stays None."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return int(Decimal(str(value)).to_integral_value(rounding=ROUND_HALF_UP))
