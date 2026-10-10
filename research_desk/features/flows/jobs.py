"""``flows update [--codes 005930,080220]``: the daily investor-flow collection (수급).

``register(subparsers)`` adds ``flows`` and its subcommand ``update``; the window exports it as
``register_jobs`` for ``cli.py``. The command function returns the exit code. It runs right after
the daily price update (``scripts/run-prices.ps1``) and works the same way: readiness, a run
record, the access token first, the 20 % rule, ``--codes``, one summary line.

Start-up, in this order:
- 4 = not ready, checked before anything else: the DB settings (SUPABASE_URL,
  SUPABASE_SERVICE_KEY), the KIS keys (KIS_APP_KEY, KIS_APP_SECRET), the stock list (KRX_CSV_PATH)
  loads. One Korean line on stderr; no DB, KIS or prices call, nothing written. A
  FLOWS_MAX_CALLS_PER_SEC that is not a number above 0 is a ValueError (exit 1), also before any
  call.
- 1 = refused, nothing changed, for a full run and ``--codes`` alike (a check shares the app key's
  KIS limit): the latest price update run (``prices.latest_run()``) is ``running`` and started less
  than ``RUN_ALIVE_FOR`` (2 hours) ago, or the latest flows run is. An older ``running`` record is a
  dead run and is ignored (left as it is). One line on stderr; no KIS client is made.
- The anchor day (기준일) is ``logic.anchor_day(now)``: today from 18:00 KST on a weekday, else the
  weekday before. One KIS client a run, paced at FLOWS_MAX_CALLS_PER_SEC, with the access-token
  file the price update uses (KIS_TOKEN_CACHE).

A full run (no ``--codes``) covers the stock list's valid codes (each once, its last row, in file
order), the calendar stock first:
1. a run record ``running`` (started_at, stocks_total);
2. the access token, once, before any stock. Without it no stock is asked: the run is failed at
   once, every stock counted failed (message ``KIS 접근 토큰을 받지 못했습니다 (<reason>). 아무 종목도
   받지 않았습니다``), and the KIS client does not ask for a token again;
3. the calendar stock 005930 (``logic.CALENDAR_CODE``), back over the latest 310 trading days
   (``logic.next_depth_anchor`` until None, then ``logic.calendar_days``: KIS answers only). It
   counts and is saved as a stock when the stock list has it. Without it (a failed or strange
   answer, no trading day) no other stock is asked and the run is failed, every stock counted
   failed (message ``기준 종목(005930)을 받지 못해 거래일을 정할 수 없습니다 (<reason>). 다른 종목은 받지
   않았습니다``): nothing is saved or ranked;
4. the coverage of the stored rows from the calendar's retention start (``investor_flow_coverage``)
   and the known data starts, each read once;
5. per stock, one after another: the latest answer (the 30 trading days ending at the anchor), then
   older answers while ``logic.next_fill_anchor`` asks for one. When every answer it needs came,
   the stock's trading rows are saved at once as raw rows (key, 11 amounts, fetched_at; never a
   rank or market cap column), and a data start the answers showed (padding before the listing) is
   recorded when it is new. A failed or strange answer, or no trading day at all, fails the stock:
   one warning line ``수급을 받지 못했습니다: <code> (<reason>)`` and nothing of it is saved. Once more
   than 20 % of the stocks have failed the run is failed whatever comes next, so the rest are not
   asked;
6. the status (``logic.run_status``). ok / partial only: the base trading day D (the latest trading
   day received) is ranked by one ``rank_investor_flows`` call with the market caps of the price
   snapshots dated D (``logic.day_market_caps``; a stock without one stays unranked that day, and a
   later run of the same day ranks it once its snapshot comes); when D then has ranked stocks it is
   put in the ranked-day list (a new day with every column, a listed day gets its count only); raw
   rows before ``logic.cleanup_cutoff(anchor, calendar)`` (the anchor day − 470 days, but never
   after the calendar's retention start, so no row the fill plan needs is deleted) and ranked days
   beyond the latest 60 (``logic.days_to_drop``) are deleted. failed: no ranking and no cleanup (the
   raw rows saved stay);
7. the run record is closed with the status, as_of (the latest trading day received), the counts,
   the days ranked (1 when D is a ranked day after this run, else 0) and a message (partial /
   failed: what it meant, the first 20 codes not received, the first reason);
8. one summary line ``수급 갱신 <word>(<status>): 기준일 <D|없음>, 성공 N종목, 실패 M종목, 순위 매긴 날
   K일[. <note>]``: stdout for ok / partial (exit 0), stderr for failed (exit 1).
An exception on the way closes the run record as failed and goes up: Python prints it and the
process ends with 1. Ctrl+C also closes it as failed and goes up, but the process then ends with the
interpreter's interrupt code, not 1 (on Windows 0xC000013A, which PowerShell shows as -1073741510).

``--codes`` is a check that writes nothing (no raw row, rank, data start or run record). After the
refusal check, the token comes first: without it nothing is asked and nothing goes to stdout (one
line on stderr, exit 1). Then each code, in the order given, is asked back over the rank's 250
trading days (``logic.next_depth_anchor(blocks, logic.WINDOW_DAYS)``, about 9 answers) and ranked
on its latest trading day by the same rule on this PC (``logic.trading_rows`` →
``logic.window_ranks``). The results are printed as JSON, then one summary line. A code not in the
stock list is not asked. Exit 0 when every code was received (summary on stdout), else 1 (stderr).

Heavy imports (the KIS client with its HTTP library, supabase-py, the prices window) happen when the
command runs: ``cli.py`` imports this module for every command. Output holds no key: KIS errors are
masked by the client. The command first sets stdout and stderr to write ``?`` for a character their
encoding lacks instead of failing: piped or redirected output on Korean Windows is cp949, and an
error text from KIS or the network can hold characters it has not. The run record keeps the text as
it was.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from itertools import chain
from typing import Any, Callable, NamedTuple, Optional, Sequence

from research_desk.core import settings as core_settings
from research_desk.core.settings import NotReady
from research_desk.domain.stocks import StockEntry, StockList, StockListError

from . import logic, service, store
from . import settings as flows_settings

logger = logging.getLogger(__name__)

EXIT_NOT_READY = 4
EXIT_FAILED = 1
RUN_ALIVE_FOR = timedelta(hours=2)   # a running record older than this is a dead run (adjustable)

NOT_READY = "수급 갱신을 시작하지 않았습니다. 이유: {reason}"
KIS_NOT_CONFIGURED = "KIS 접속 설정(KIS_APP_KEY, KIS_APP_SECRET)이 없습니다"
STOCK_LIST_UNREADABLE = "종목표 파일을 읽을 수 없습니다 ({cause})"
PRICES_RUNNING = "주가 갱신이 진행 중이라 수급 갱신을 시작하지 않았습니다. 주가 갱신이 끝난 뒤 다시 실행하세요."
FLOWS_RUNNING = "다른 수급 갱신(실행 {run_id})이 진행 중입니다. 끝난 뒤 다시 실행하세요."
TOKEN_FAILED = "KIS 접근 토큰을 받지 못했습니다 ({reason})"
RUN_WITHOUT_TOKEN = TOKEN_FAILED + ". 아무 종목도 받지 않았습니다"
CHECK_SAVED_NOTHING = "확인용 실행이라 아무것도 저장하지 않았습니다: "
CHECK_WITHOUT_TOKEN = CHECK_SAVED_NOTHING + RUN_WITHOUT_TOKEN
NO_CALENDAR = "기준 종목({code})을 받지 못해 거래일을 정할 수 없습니다 ({reason}). 다른 종목은 받지 않았습니다"
NOT_RECEIVED = "수급을 받지 못했습니다: %s (%s)"
NO_FLOWS = "받은 수급이 없습니다"
NOT_LISTED = "종목표에 없는 종목코드입니다"
CRASHED = "실행 중 오류로 멈췄습니다: {error}"
PARTIAL_NOTE = "못 받은 종목은 다음 실행에서 다시 받습니다"
FAILED_NOTE = "못 받은 종목이 {percent}%를 넘어 순위를 매기지 않았습니다"
SKIPPED_NOTE = "기준을 넘은 뒤 남은 {count}종목은 받지 않았습니다"
STATUS_WORDS = {logic.OK: "완료", logic.PARTIAL: "일부 완료", logic.FAILED: "실패"}
MESSAGE_CODES = 20    # codes named in a run record's message
REASON_CHARS = 300    # a failure reason is cut to this length

_CODE = re.compile(r"[0-9A-Z]{6}")


class _NotReadyToRun(Exception):
    """A start-up check failed; ``str()`` is the reason."""


def utc_now() -> datetime:
    """The current time, aware UTC. Tests replace this name."""
    return datetime.now(timezone.utc)


def kis_client(cfg: flows_settings.FlowsSettings) -> Any:
    """The KIS client of one run, paced at FLOWS_MAX_CALLS_PER_SEC. Tests replace this name."""
    from research_desk.core import kis   # the HTTP library loads only when the command runs

    return kis.KisClient(cfg.app_key, cfg.app_secret, max_calls_per_sec=cfg.max_calls_per_sec,
                         base_url=cfg.base_url, token_cache=cfg.token_cache)


# ── the command ──────────────────────────────────────────────────────────────

def code_list(text: str) -> list[str]:
    """``--codes``: comma-separated 6-character codes (digits and capital letters; lower case is
    raised), each once. Anything else is an argument error (exit 2)."""
    codes = [part.strip().upper() for part in text.split(",") if part.strip()]
    if not codes:
        raise argparse.ArgumentTypeError("종목코드를 쉼표로 나눠 적어 주세요 (예: 005930,080220)")
    bad = [code for code in codes if not _CODE.fullmatch(code)]
    if bad:
        raise argparse.ArgumentTypeError(f"종목코드는 숫자와 영문 대문자 6자리입니다: {', '.join(bad)}")
    return list(dict.fromkeys(codes))


def register(subparsers) -> argparse.ArgumentParser:
    """Add ``flows`` and its subcommand ``update`` (``func(args) -> exit code``)."""
    flows = subparsers.add_parser("flows", help="수급: update",
                                  description="투자자별 수급(한국투자증권 KIS) 관리")
    sub = flows.add_subparsers(dest="flows_command", required=True)
    description = ("종목표 전 종목의 투자자별 일별 순매수를 한국투자증권(KIS)에서 받아 수급 원자료를 채우고, "
                   "기준 거래일의 250거래일 순위를 매깁니다. "
                   "종료 코드: 0 성공(ok, partial), 1 실패(failed)·거절, 4 준비 문제.")
    update_parser = sub.add_parser("update", help="수급 갱신", description=description)
    update_parser.add_argument("--codes", type=code_list, metavar="005930,080220",
                               help="확인용: 이 종목들만 250거래일치를 받아 기준일 순위를 계산해 출력하고 "
                                    "아무것도 저장하지 않습니다")
    update_parser.set_defaults(func=update)
    return flows


def _tolerant(stream: Any) -> None:
    """Set ``stream`` to write ``?`` for a character its encoding lacks (same object, same
    encoding); a stream without ``reconfigure`` (a ``StringIO``) is left as it is."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")


def update(args: argparse.Namespace) -> int:
    """``flows update``: see the module docstring. Returns the exit code."""
    _tolerant(sys.stdout)
    _tolerant(sys.stderr)
    core_settings.load_env()
    try:
        stocks = _ready()
    except _NotReadyToRun as exc:
        print(NOT_READY.format(reason=exc), file=sys.stderr)
        return EXIT_NOT_READY
    cfg = flows_settings.load_settings()   # a malformed number: ValueError, before any call
    sb = service.connect()
    now = utc_now()
    refusal = _refusal(sb, now)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return EXIT_FAILED
    anchor = logic.anchor_day(now)
    codes = getattr(args, "codes", None)
    with kis_client(cfg) as kis:
        if codes is not None:
            return _check(kis, stocks, codes, anchor)
        return _run(sb, kis, _targets(stocks), anchor)


def _ready() -> StockList:
    """The start-up checks, in order: DB settings, KIS keys, the stock list."""
    try:
        service.db_settings()
    except NotReady as exc:
        raise _NotReadyToRun(exc.reason) from exc
    if not (flows_settings.app_key() and flows_settings.app_secret()):
        raise _NotReadyToRun(KIS_NOT_CONFIGURED)
    try:
        return StockList.load(core_settings.krx_csv_path())
    except StockListError as exc:   # the cause may name the file: shown on this PC's terminal only
        raise _NotReadyToRun(STOCK_LIST_UNREADABLE.format(cause=exc)) from exc


def _targets(stocks: StockList) -> list[StockEntry]:
    """Every stock of the list once (a repeated code keeps its last row), in file order; codes
    that are not 6 digits or capital letters are left out. The price update's rule."""
    return [entry for entry in stocks.by_code.values() if stocks.validate_code(entry.code)]


def _refusal(sb: Any, now: datetime) -> Optional[str]:
    """The line that refuses the start when a price update or another flows update is running
    (started less than RUN_ALIVE_FOR ago), else None."""
    from research_desk.features import prices   # the window's names, looked up when called

    last_prices = prices.latest_run()
    if (last_prices is not None and last_prices["last_run_status"] == logic.RUNNING
            and now - service.instant(last_prices["last_run_at"]) < RUN_ALIVE_FOR):
        return PRICES_RUNNING
    last = store.latest_run(sb)
    if (last is not None and last["status"] == logic.RUNNING
            and now - service.instant(last["started_at"]) < RUN_ALIVE_FOR):
        return FLOWS_RUNNING.format(run_id=last["run_id"])
    return None


def _token_problem(kis: Any) -> Optional[str]:
    """Get the run's KIS access token before any stock is asked: None when the client has it,
    else the reason (a KisError's text holds no key: the client masks it)."""
    try:
        kis.ensure_token()
    except Exception as exc:   # a KIS error or a strange answer: no stock can be asked without it
        return _reason(exc)
    return None


def _reason(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return text[:REASON_CHARS]


class _Stock(NamedTuple):
    blocks: list                 # the KIS answers, the latest first, in the order asked
    trading: logic.TradingRows   # their trading rows, oldest first, and the data start


def _fetch(kis: Any, code: str, anchor: date,
           next_anchor: Callable[[list], Optional[date]]) -> tuple[Optional[_Stock], Optional[str]]:
    """Ask ``code``'s answers: the latest at ``anchor``, then older ones while ``next_anchor(blocks)``
    gives an anchor. ``(stock, None)`` when every answer came and they hold a trading day,
    ``(None, reason)`` otherwise (a failed or strange answer stops the asking)."""
    blocks: list = []
    try:
        ask: Optional[date] = anchor
        while ask is not None:
            blocks.append(kis.investor_daily(code, ask))
            ask = next_anchor(blocks)
        trading = logic.trading_rows(chain.from_iterable(blocks))
    except Exception as exc:   # a KIS error or a strange answer fails this stock only
        return None, _reason(exc)
    if not trading.rows:
        return None, NO_FLOWS
    return _Stock(blocks, trading), None


# ── a full run ───────────────────────────────────────────────────────────────

class _Collected(NamedTuple):
    received: list[str]          # the codes received and saved
    days: list[date]             # the latest trading day of each of them
    failures: dict[str, str]     # code → reason, the stocks not received
    skipped: int                 # stocks not asked once the run had failed
    calendar_problem: Optional[str] = None
    calendar: Sequence[date] = ()   # the run's trading-day calendar (empty without one)


class _Outcome(NamedTuple):
    status: str
    as_of: Optional[date]
    stocks_ok: int
    days_ranked: int
    note: Optional[str]          # the summary line's ending
    message: Optional[str]       # the run record's message


def _run(sb: Any, kis: Any, entries: Sequence[StockEntry], anchor: date) -> int:
    total = len(entries)
    run_id = store.start_run(sb, started_at=utc_now().isoformat(), total=total)
    try:
        outcome = _work(sb, kis, run_id, entries, anchor)
        store.finish_run(sb, run_id, status=outcome.status, finished_at=utc_now().isoformat(),
                         as_of=outcome.as_of.isoformat() if outcome.as_of else None,
                         stocks_ok=outcome.stocks_ok, stocks_failed=total - outcome.stocks_ok,
                         days_ranked=outcome.days_ranked, message=outcome.message)
    except BaseException as exc:
        _close_crashed(sb, run_id, exc)
        raise
    line = (f"수급 갱신 {STATUS_WORDS[outcome.status]}({outcome.status}): 기준일 {outcome.as_of or '없음'}, "
            f"성공 {outcome.stocks_ok}종목, 실패 {total - outcome.stocks_ok}종목, "
            f"순위 매긴 날 {outcome.days_ranked}일")
    if outcome.note:
        line += f". {outcome.note}"
    print(line, file=sys.stderr if outcome.status == logic.FAILED else sys.stdout)
    return 0 if outcome.status in logic.SUCCESS_STATUSES else EXIT_FAILED


def _work(sb: Any, kis: Any, run_id: int, entries: Sequence[StockEntry], anchor: date) -> _Outcome:
    """Steps 2-6 of a full run."""
    problem = _token_problem(kis)
    if problem is not None:   # no stock is asked without the token
        text = RUN_WITHOUT_TOKEN.format(reason=problem)
        return _Outcome(logic.FAILED, None, 0, 0, text, text)
    codes = [entry.code for entry in entries]
    collected = _collect(sb, kis, codes, anchor)
    if collected.calendar_problem is not None:   # no calendar: no other stock was asked
        text = NO_CALENDAR.format(code=logic.CALENDAR_CODE, reason=collected.calendar_problem)
        return _Outcome(logic.FAILED, None, 0, 0, text, text)
    status = logic.run_status(len(codes), len(codes) - len(collected.received))
    as_of = logic.ranking_day(collected.days)
    days_ranked = 0
    if status != logic.FAILED:
        days_ranked = _rank_and_clean(sb, run_id, as_of, codes, anchor, collected.calendar)
    return _Outcome(status, as_of, len(collected.received), days_ranked, _note(status, collected.skipped),
                    _run_message(status, collected.failures, collected.skipped))


def _collect(sb: Any, kis: Any, codes: Sequence[str], anchor: date) -> _Collected:
    """Ask KIS stock by stock, the calendar stock first, and save each stock received."""
    calendar_code = logic.CALENDAR_CODE
    calendar_stock, problem = _fetch(kis, calendar_code, anchor, logic.next_depth_anchor)
    if calendar_stock is None:
        logger.warning(NOT_RECEIVED, calendar_code, problem)
        return _Collected([], [], {}, 0, problem)
    calendar = logic.calendar_days(calendar_stock.blocks)
    coverage = store.read_coverage(sb, logic.retention_start(calendar).isoformat())
    listed = store.read_listed_from(sb)
    received: list[str] = []
    days: list[date] = []
    if calendar_code in codes:
        days.append(_save(sb, calendar_code, calendar_stock.trading, listed))
        received.append(calendar_code)
    failures: dict[str, str] = {}
    rest = [code for code in codes if code != calendar_code]
    for i, code in enumerate(rest):
        stock, problem = _fetch(kis, code, anchor, _fill_plan(calendar, coverage.get(code), listed.get(code)))
        if stock is not None:
            days.append(_save(sb, code, stock.trading, listed))
            received.append(code)
            continue
        failures[code] = problem
        logger.warning(NOT_RECEIVED, code, problem)
        if logic.run_status(len(codes), len(failures)) == logic.FAILED:
            # failed whatever comes next
            return _Collected(received, days, failures, len(rest) - i - 1, calendar=calendar)
    return _Collected(received, days, failures, 0, calendar=calendar)


def _fill_plan(calendar: list[date], coverage: Optional[dict],
               listed_from: Optional[str]) -> Callable[[list], Optional[date]]:
    """The next older answer one stock needs to fill its retention range (``logic.next_fill_anchor``)."""
    return lambda blocks: logic.next_fill_anchor(calendar, coverage, listed_from, blocks)


def _save(sb: Any, code: str, trading: logic.TradingRows, listed: dict[str, str]) -> date:
    """Write one received stock's raw rows at once, and its data start when the answers showed a
    new one; returns its latest trading day."""
    fetched_at = utc_now().isoformat()
    rows = [logic.daily_row(code, row) | {"fetched_at": fetched_at} for row in trading.rows]
    store.upsert_daily(sb, rows)
    start = trading.data_start
    if start is not None and service.day(listed.get(code)) != start:
        store.upsert_listed_from(sb, code, start.isoformat(), fetched_at)
    return date.fromisoformat(rows[-1]["trade_date"])


def _rank_and_clean(sb: Any, run_id: int, day: Optional[date], codes: Sequence[str], anchor: date,
                    calendar: Sequence[date]) -> int:
    """Rank the base trading day with that day's market caps, list it when it has ranked stocks,
    then delete the raw rows before ``logic.cleanup_cutoff(anchor, calendar)`` and the ranked days
    beyond the latest 60. Returns the days ranked (0 or 1)."""
    from research_desk.features import prices   # the window's names, looked up when called

    days_ranked = 0
    if day is not None:
        text = day.isoformat()
        caps = logic.day_market_caps(prices.snapshots(list(codes)), day)
        store.rank_day(sb, text, caps, window=logic.WINDOW_DAYS, min_days=logic.MIN_DAYS)
        ranked = store.count_ranked(sb, text)
        if ranked > 0:
            if store.ranked_day(sb, text) is None:
                store.insert_ranked_day(sb, day=text, ranked_at=utc_now().isoformat(), stocks_ranked=ranked,
                                        run_id=run_id)
            else:
                store.update_ranked_day(sb, text, stocks_ranked=ranked)
            days_ranked = 1
    store.delete_daily_before(sb, logic.cleanup_cutoff(anchor, calendar).isoformat())
    drop = logic.days_to_drop(row["trade_date"] for row in store.read_ranked_days(sb))
    store.delete_ranked_days(sb, [old.isoformat() for old in drop])
    return days_ranked


def _note(status: str, skipped: int) -> Optional[str]:
    """What the status meant (None for ok), for the summary line and the run message."""
    if status == logic.PARTIAL:
        return PARTIAL_NOTE
    if status == logic.FAILED:
        text = FAILED_NOTE.format(percent=logic.MAX_FAILED_PERCENT)
        if skipped:
            text += ". " + SKIPPED_NOTE.format(count=skipped)
        return text
    return None


def _run_message(status: str, failures: dict[str, str], skipped: int) -> Optional[str]:
    """The run record's message: none for ok, else the note, the codes and the first reason."""
    if status == logic.OK or not failures:
        return None
    codes = list(failures)
    named = ", ".join(codes[:MESSAGE_CODES])
    if len(codes) > MESSAGE_CODES:
        named += f" 외 {len(codes) - MESSAGE_CODES}종목"
    return f"{_note(status, skipped)}. 못 받은 종목: {named}. 첫 실패 {codes[0]}: {failures[codes[0]]}"


def _close_crashed(sb: Any, run_id: int, exc: BaseException) -> None:
    """Close the run record as failed after an exception; the exception itself goes up."""
    try:
        store.finish_run(sb, run_id, status=logic.FAILED, finished_at=utc_now().isoformat(),
                         message=CRASHED.format(error=type(exc).__name__))
    except Exception as close_error:
        logger.warning("실행 기록 %s를 failed로 닫지 못했습니다: %s", run_id, type(close_error).__name__)


# ── --codes: a check that writes nothing ─────────────────────────────────────

def _check(kis: Any, stocks: StockList, codes: Sequence[str], anchor: date) -> int:
    problem = _token_problem(kis)
    if problem is not None:   # nothing is asked without the token
        print(CHECK_WITHOUT_TOKEN.format(reason=problem), file=sys.stderr)
        return EXIT_FAILED
    results: dict[str, dict] = {}
    for code in codes:
        if stocks.lookup(code) is None:
            results[code] = {"error": NOT_LISTED}
            continue
        stock, problem = _fetch(kis, code, anchor, _window_plan)
        results[code] = {"error": problem} if stock is None else _checked(code, stock.trading)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    received = [result for result in results.values() if "error" not in result]
    missed = len(results) - len(received)
    as_of = max((result["as_of"] for result in received), default=None)
    line = f"{CHECK_SAVED_NOTHING}기준일 {as_of or '없음'}, 받음 {len(received)}종목, 못 받음 {missed}종목"
    print(line, file=sys.stderr if missed else sys.stdout)
    return EXIT_FAILED if missed else 0


def _window_plan(blocks: list) -> Optional[date]:
    """The next older answer until the answers hold the rank's window (250 trading days)."""
    return logic.next_depth_anchor(blocks, logic.WINDOW_DAYS)


def _checked(code: str, trading: logic.TradingRows) -> dict:
    """One received stock's check: its latest trading day's amounts (won), 250-day ranks, window
    size and the 기관합계 identity gap (millions of won)."""
    rows = [logic.daily_row(code, row) for row in trading.rows]
    day = rows[-1]["trade_date"]
    count, ranks = logic.window_ranks(rows, day)
    latest = logic.investor_amounts(trading.rows[-1])
    return {
        "as_of": day,
        "window_days": count,
        "amounts": {key: logic.to_won(amount) for key, amount in latest.items()},
        "ranks": {key: (ranks[key] if ranks else None) for key in logic.INVESTOR_KEYS},
        "short_history": logic.short_history(count),
        "institution_gap": logic.institution_gap(latest),
    }
