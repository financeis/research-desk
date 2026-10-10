"""``flows update [--codes]``: the command registered by ``register_jobs``.

The DB is the in-memory FakeSupabase handed out by core.db.supabase_client (the four flows tables
and the two DB functions of migration 009), KIS is FakeKis handed out by ``jobs.kis_client`` (a
fake market of weekdays; one answer = the 30 trading days ending at the anchor asked), the prices
window's ``latest_run`` / ``snapshots`` are FakePrices, the time is ``jobs.utc_now``; without a
test's setup each of them refuses. Every setting the command reads is deleted first and the
current folder is a temp folder. The base clock is Friday 2026-10-09 19:00 KST, so the anchor day is
that Friday (``D``).

Checked:
- the command shape: help 0, argument errors 2 (``--codes`` checked by argparse);
- readiness (exit 4, one Korean line on stderr, neither KIS nor the DB nor the prices window
  touched): DB settings, KIS keys, the stock list, in that order; a bad call rate is a ValueError
  before any call;
- refusals (exit 1, one line, nothing changed, no KIS client), for the full run and ``--codes``: a
  price update running, another flows update running (with its run id); a ``running`` record two
  hours old or older is dead and ignored; prices first;
- the access token: asked once, first (while the run record is running); without it no stock is
  asked, the run is failed at once (every stock failed). Also run with the real KIS client against
  a KIS stand-in (an httpx MockTransport): one token attempt and no stock request when the token
  is refused (403) or fails (5xx), no key in any output, and a full run storing what KIS answered
  (the client's field names feed the investor mapping);
- the calendar stock 005930: asked first back over 310 trading days (11 answers), counted and stored
  as a stock; without it no other stock is asked and the run is failed with its message;
- per stock: the latest answer, then older answers until the retention range is filled (a first
  run: back to the calendar's 310th day; a daily run: one answer; an interrupted fill: only the
  unfinished stocks go back; a data start ends the asking and is recorded once); a stock's rows are
  saved at once when every answer came, as raw rows only (key, 11 amounts, fetched_at), and nothing
  of a stock with one failed answer is saved; a day KIS never returns does not fail the stock;
- status: ok, partial up to exactly 20 %, failed beyond (no more stocks asked; no ranking, no
  cleanup; the raw rows received stay);
- ok / partial: the base trading day D ranked once with the market caps of the snapshots dated D
  only; a stock without one stays unranked and is ranked by a later run of the same day once its
  snapshot comes, ranked rows keep their values; the ranked-day list gets D (insert, or the count
  only), not when nothing is ranked; raw rows before the cutoff (470 days before the anchor, but
  never after the calendar's retention start) and ranked days beyond the latest 60 are deleted;
- the run record (running → ok / partial / failed with as_of, counts, days ranked, message), a
  crash or an interrupt closing it as failed, the summary line (stdout for ok / partial, stderr for
  failed) and exit codes;
- ``--codes``: 250 trading days per code, the JSON (as_of, window_days, amounts in won, ranks,
  short_history, institution_gap), nothing written; exit 0 only when every code was received;
- the KIS client's settings, .env values added later, no key in any output, cp949 output.
"""
from __future__ import annotations

import argparse
import functools
import io
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from research_desk.core import db as core_db
from research_desk.core import kis as core_kis
from research_desk.core.kis import KisError
from research_desk.features import prices
from research_desk.features.flows import jobs, logic, register_jobs

from .fakes import (CLOSE, COVERAGE_FUNCTION, DAILY, DAYS, FIELDS, KEY, MARKET, NET, RANK, RANK_FUNCTION, RUNS,
                    STOCKS as STOCKS_TABLE, URL, FakeDBError, FakeKis, FakePrices, FakeSupabase, amounts)

ENV = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_DB_URL", "KIS_APP_KEY", "KIS_APP_SECRET",
       "KIS_BASE_URL", "KIS_TOKEN_CACHE", "FLOWS_MAX_CALLS_PER_SEC", "PRICES_MAX_CALLS_PER_SEC", "KRX_CSV_PATH")
APP_KEY, APP_SECRET = "PSfakeAppKey0123456789", "fakeAppSecret/0123456789+abc=="
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)        # Friday 19:00 KST
D = date(2026, 10, 9)
NEXT_NOW = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)   # Monday 19:00 KST
D2 = date(2026, 10, 12)
PAST = [day for day in MARKET if day <= D]
CALENDAR = PAST[-310:]
CAP = 500_000_000_000   # 5천억 원
SAMSUNG = "005930"

HEADER_LINE = '"종목\n코드",종목명,시장,산업명(대),산업명(중),주요제품\n'
STOCKS = [(SAMSUNG, "KOSPI"), ("000660", "KOSPI"), ("080220", "KOSDAQ")]
CODES = [code for code, _ in STOCKS]
TEN = [(SAMSUNG, "KOSPI")] + [(f"00000{i}", "KOSDAQ") for i in range(1, 10)]
NOT_READY = "수급 갱신을 시작하지 않았습니다. 이유: "
DB_REASON = "DB 접속 설정(SUPABASE_URL, SUPABASE_SERVICE_KEY)이 없습니다"
KIS_REASON = "KIS 접속 설정(KIS_APP_KEY, KIS_APP_SECRET)이 없습니다"
PRICES_RUNNING = "주가 갱신이 진행 중이라 수급 갱신을 시작하지 않았습니다. 주가 갱신이 끝난 뒤 다시 실행하세요."
PARTIAL_NOTE = "못 받은 종목은 다음 실행에서 다시 받습니다"
FAILED_NOTE = "못 받은 종목이 20%를 넘어 순위를 매기지 않았습니다"
CHECKED = "확인용 실행이라 아무것도 저장하지 않았습니다: "
RAW = {"stock_code", "trade_date", *NET, "fetched_at"}

# The real factories, kept before the fixtures below replace them.
REAL_KIS_CLIENT = jobs.kis_client
REAL_UTC_NOW = jobs.utc_now


def flows_running(run_id: int) -> str:
    return f"다른 수급 갱신(실행 {run_id})이 진행 중입니다. 끝난 뒤 다시 실행하세요."


def write_stocks(path, stocks) -> None:
    lines_ = "".join(f"{code},회사{code},{market},산업,세부,제품\n" for code, market in stocks)
    path.write_bytes((HEADER_LINE + lines_).encode("utf-8"))


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="research_desk")
    register_jobs(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(list(argv))


def update(*argv: str) -> int:
    args = parse("flows", "update", *argv)
    return args.func(args)


def refuse(*args, **kwargs):
    raise AssertionError("no real Supabase, KIS or prices window in these tests")


def lines(text: str) -> list[str]:
    return text.splitlines()


def back(n: int) -> date:
    """The anchor of the n-th older answer of a stock asked at D: the day before PAST[-30n]."""
    return PAST[-30 * n] - timedelta(days=1)


FULL_ANCHORS = [D] + [back(n) for n in range(1, 11)]   # 11 answers reach the 310th trading day
CHECK_ANCHORS = [D] + [back(n) for n in range(1, 9)]   # 9 answers reach 250 trading days


def kis_error(code: str) -> KisError:
    return KisError(f"KIS investor trading {code} failed after 3 retries: HTTP 500", status=500)


def reason(code: str) -> str:
    return f"KisError: KIS investor trading {code} failed after 3 retries: HTTP 500"


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)   # the default stock list path never reaches the real file
    monkeypatch.setattr(core_db, "supabase_client", refuse)
    monkeypatch.setattr(jobs, "kis_client", refuse)
    monkeypatch.setattr(jobs, "utc_now", lambda: NOW)
    monkeypatch.setattr(prices, "latest_run", refuse)
    monkeypatch.setattr(prices, "snapshots", refuse)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Everything set: DB and KIS settings, a stock list, the DB, KIS and prices stand-ins."""
    csv = tmp_path / "krx.csv"
    write_stocks(csv, STOCKS)
    for name, value in (("SUPABASE_URL", URL), ("SUPABASE_SERVICE_KEY", KEY), ("KIS_APP_KEY", APP_KEY),
                        ("KIS_APP_SECRET", APP_SECRET), ("KRX_CSV_PATH", str(csv))):
        monkeypatch.setenv(name, value)
    db, kis, kis_made, fake_prices = FakeSupabase(now=NOW.isoformat()), FakeKis(), [], FakePrices()

    def supabase_client(url, key):
        db.made.append((url, key))
        return db

    def kis_client(cfg):
        kis_made.append(cfg)
        return kis

    monkeypatch.setattr(core_db, "supabase_client", supabase_client)
    monkeypatch.setattr(jobs, "kis_client", kis_client)
    monkeypatch.setattr(prices, "latest_run", fake_prices.latest_run)
    monkeypatch.setattr(prices, "snapshots", fake_prices.snapshots)
    state = SimpleNamespace(db=db, kis=kis, kis_made=kis_made, prices=fake_prices, csv=csv, codes=list(CODES))

    def stocks(entries):
        write_stocks(csv, entries)
        state.codes = [code for code, _ in entries]

    def caps(day=D, codes=None, cap=CAP):
        for code in (state.codes if codes is None else codes):
            fake_prices.snapshot(code, day.isoformat(), cap)

    def clock(moment):
        monkeypatch.setattr(jobs, "utc_now", lambda: moment)

    state.stocks, state.caps, state.clock = stocks, caps, clock
    return state


def stored(db, code: str) -> list[date]:
    return [date.fromisoformat(day) for day in db.raw_days(code)]


def raw_upserts(db) -> list:
    return db.queries(DAILY, "upsert")


# ── the command ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("argv,usage", [
    (["flows", "--help"], "usage: research_desk flows"),
    (["flows", "update", "--help"], "usage: research_desk flows update"),
])
def test_help_exits_0(argv, usage, capsys):
    with pytest.raises(SystemExit) as excinfo:
        parse(*argv)
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith(usage)
    if argv[1] == "update":
        assert "--codes 005930,080220" in out


@pytest.mark.parametrize("argv", [
    ["flows"], ["flows", "bogus"], ["flows", "update", "--codes"], ["flows", "update", "--codes", ""],
    ["flows", "update", "--codes", ","], ["flows", "update", "--codes", "12345"],
    ["flows", "update", "--codes", "005930,abc"], ["flows", "update", "--codes", "0059300"],
    ["flows", "update", "extra"],
], ids=lambda argv: " ".join(argv) or "nothing")
def test_argument_errors_exit_2_with_the_usage(argv, capsys):
    with pytest.raises(SystemExit) as excinfo:
        parse(*argv)
    assert excinfo.value.code == 2
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("usage: research_desk")


def test_codes_are_split_upper_cased_and_kept_once():
    assert parse("flows", "update", "--codes", " 005930 ,0126z0,005930,").codes == ["005930", "0126Z0"]
    assert parse("flows", "update").codes is None


# ── readiness: exit 4, nothing touched ───────────────────────────────────────

def not_ready(world, capsys, *argv) -> str:
    assert update(*argv) == 4
    out, err = capsys.readouterr()
    assert out == ""
    assert world.db.made == [] and world.db.executed == []
    assert world.kis_made == [] and world.kis.calls == [] and world.kis.token_asked == 0
    assert world.prices.calls == []
    assert len(lines(err)) == 1
    return err


@pytest.mark.parametrize("missing", ["SUPABASE_URL", "SUPABASE_SERVICE_KEY"])
@pytest.mark.parametrize("argv", [[], ["--codes", "005930"]], ids=["all", "codes"])
def test_without_db_settings_nothing_happens(world, capsys, monkeypatch, missing, argv):
    monkeypatch.delenv(missing)
    monkeypatch.delenv("KIS_APP_KEY")   # the DB comes first
    assert not_ready(world, capsys, *argv) == NOT_READY + DB_REASON + "\n"


@pytest.mark.parametrize("missing", ["KIS_APP_KEY", "KIS_APP_SECRET"])
@pytest.mark.parametrize("argv", [[], ["--codes", "005930"]], ids=["all", "codes"])
def test_without_kis_keys_nothing_happens(world, capsys, monkeypatch, missing, argv):
    monkeypatch.setenv(missing, "")
    world.csv.unlink()   # the keys come before the stock list
    assert not_ready(world, capsys, *argv) == NOT_READY + KIS_REASON + "\n"


@pytest.mark.parametrize("break_it", [
    lambda csv: csv.unlink(),
    lambda csv: csv.write_bytes(HEADER_LINE.replace("종목명", "회사명").encode("utf-8")),
    lambda csv: csv.write_bytes((HEADER_LINE + "005930,삼성전자,KOSPI,a,b,c\n").encode("cp949")),
], ids=["missing", "wrong header", "not utf-8"])
def test_an_unreadable_stock_list_stops_before_anything(world, capsys, break_it):
    break_it(world.csv)
    assert not_ready(world, capsys).startswith(NOT_READY + "종목표 파일을 읽을 수 없습니다 (")


def test_without_a_stock_list_setting_the_default_path_is_read_from_the_current_folder(world, capsys,
                                                                                         monkeypatch):
    monkeypatch.delenv("KRX_CSV_PATH")   # the current folder is a temp folder without the file
    assert "docs" in not_ready(world, capsys)


@pytest.mark.parametrize("argv", [[], ["--codes", "005930"]], ids=["all", "codes"])
def test_a_call_rate_that_is_not_a_number_is_an_error_before_any_call(world, monkeypatch, argv):
    monkeypatch.setenv("FLOWS_MAX_CALLS_PER_SEC", "fast")
    with pytest.raises(ValueError, match="FLOWS_MAX_CALLS_PER_SEC"):
        update(*argv)
    assert world.db.made == [] and world.kis_made == [] and world.prices.calls == []


# ── refusals: exit 1, nothing changed ────────────────────────────────────────

def prices_run(status: str, age: timedelta) -> dict:
    return {"last_run_at": NOW - age, "last_run_status": status, "as_of": date(2026, 10, 8)}


def own_run(run_id: int, status: str, age: timedelta) -> dict:
    return {"run_id": run_id, "started_at": (NOW - age).isoformat(), "status": status}


def refused(world, capsys, argv, line: str) -> None:
    before = world.db.runs()
    assert update(*argv) == 1
    assert capsys.readouterr() == ("", line + "\n")
    assert world.kis_made == [] and world.kis.calls == [] and world.kis.token_asked == 0
    assert world.db.writes() == [] and world.db.runs() == before
    assert ("snapshots", CODES) not in world.prices.calls


ALMOST_TWO_HOURS = timedelta(hours=2) - timedelta(seconds=1)


@pytest.mark.parametrize("argv", [[], ["--codes", "005930"]], ids=["all", "codes"])
def test_a_running_price_update_refuses_the_start(world, capsys, argv):
    world.prices.run = prices_run("running", ALMOST_TWO_HOURS)
    refused(world, capsys, argv, PRICES_RUNNING)


@pytest.mark.parametrize("argv", [[], ["--codes", "005930"]], ids=["all", "codes"])
def test_another_running_flows_update_refuses_the_start(world, capsys, argv):
    world.db.add(RUNS, own_run(6, "ok", timedelta(days=1)))
    world.db.add(RUNS, own_run(7, "running", ALMOST_TWO_HOURS))
    refused(world, capsys, argv, flows_running(7))


def test_the_price_update_is_checked_first(world, capsys):
    world.prices.run = prices_run("running", timedelta(minutes=5))
    world.db.add(RUNS, own_run(7, "running", timedelta(minutes=5)))
    refused(world, capsys, [], PRICES_RUNNING)
    assert world.db.queries(RUNS) == []   # stopped before its own runs were read


@pytest.mark.parametrize("argv", [[], ["--codes", "005930"]], ids=["all", "codes"])
def test_a_running_record_two_hours_old_is_dead_and_ignored(world, capsys, argv):
    world.prices.run = prices_run("running", timedelta(hours=2))
    world.db.add(RUNS, own_run(7, "running", timedelta(hours=2)))
    assert update(*argv) == 0
    runs = world.db.runs()
    assert runs[0]["status"] == "running"   # ignored, not closed
    if not argv:
        assert [run["run_id"] for run in runs] == [7, 8] and runs[1]["status"] == "ok"


@pytest.mark.parametrize("status", ["ok", "partial", "failed"])
def test_finished_runs_refuse_nothing(world, status):
    world.prices.run = prices_run(status, timedelta(minutes=1))
    world.db.add(RUNS, own_run(3, status, timedelta(minutes=1)))
    assert update() == 0
    assert world.db.runs()[-1]["status"] == "ok"


# ── a first run ──────────────────────────────────────────────────────────────

def test_a_first_run_fills_the_retention_range_and_ranks_the_base_day(world, capsys):
    world.caps()
    assert update() == 0
    assert capsys.readouterr() == ("수급 갱신 완료(ok): 기준일 2026-10-09, 성공 3종목, 실패 0종목, 순위 매긴 날 1일\n", "")

    # the calendar stock first, then the others in file order; each back to the 310th day
    assert world.kis.codes() == CODES
    assert all(world.kis.anchors(code) == FULL_ANCHORS for code in CODES)
    assert all(stored(world.db, code) == PAST[-330:] for code in CODES)
    assert world.kis.closed

    # raw rows only: the key, the 11 amounts and fetched_at
    assert raw_upserts(world.db) and all(set(row) == RAW for q in raw_upserts(world.db) for row in q.rows_sent)
    row = world.db.row(DAILY, "000660", "2026-10-08")
    kis_row = amounts("000660", date(2026, 10, 8))
    assert row["net_pe_fund"] == kis_row["pe_fund_ntby_tr_pbmn"]
    assert row["net_institution"] == kis_row["orgn_ntby_tr_pbmn"] + kis_row["etc_orgt_ntby_tr_pbmn"]
    assert row["fetched_at"] == NOW.isoformat() and row["ranked_at"] is None   # not the base day

    # the coverage and the data starts are read once, from the calendar's retention start
    [coverage] = world.db.rpcs(COVERAGE_FUNCTION)
    assert coverage.params == {"p_since": CALENDAR[0].isoformat()}
    assert len(world.db.queries(STOCKS_TABLE, "select")) == 1

    # the base day ranked once with that day's market caps
    [rank] = world.db.rpcs(RANK_FUNCTION)
    assert rank.params == {"p_day": "2026-10-09", "p_caps": {code: CAP for code in CODES},
                           "p_window": 250, "p_min_days": 60}
    for code in CODES:
        day_row = world.db.row(DAILY, code, "2026-10-09")
        rows = world.db.rows(DAILY, stock_code=code)
        n, ranks = logic.window_ranks(rows, "2026-10-09")
        assert (day_row["market_cap"], day_row["window_days"], day_row["ranked_at"]) == (CAP, 250, NOW.isoformat())
        assert {key: day_row[f"rank_{key}"] for key in logic.INVESTOR_KEYS} == ranks
    assert world.prices.calls == [("latest_run",), ("snapshots", CODES)]
    assert world.db.rows(DAYS) == [{"trade_date": "2026-10-09", "ranked_at": NOW.isoformat(),
                                    "stocks_ranked": 3, "run_id": 1}]
    assert world.db.runs() == [{
        "run_id": 1, "started_at": NOW.isoformat(), "finished_at": NOW.isoformat(), "status": "ok",
        "as_of": "2026-10-09", "stocks_total": 3, "stocks_ok": 3, "stocks_failed": 0, "days_ranked": 1,
        "message": None}]


def test_the_run_record_is_running_while_kis_is_asked(world, monkeypatch):
    seen = []
    world.kis.during = lambda call: seen.append([run["status"] for run in world.db.runs()])
    ticks = iter(NOW + timedelta(seconds=i) for i in range(1000))
    monkeypatch.setattr(jobs, "utc_now", lambda: next(ticks))   # a clock that moves on every call
    assert update() == 0
    assert len(seen) == 33 and all(statuses == ["running"] for statuses in seen)
    inserts, updates = world.db.queries(RUNS, "insert"), world.db.queries(RUNS, "update")
    assert [q.payload["status"] for q in inserts] == ["running"] and inserts[0].payload["stocks_total"] == 3
    assert [q.payload["status"] for q in updates] == ["ok"]
    [run] = world.db.runs()
    assert run["started_at"] < run["finished_at"]


def test_the_calendar_stock_is_asked_first_once_wherever_it_is_listed(world):
    world.stocks([("000660", "KOSPI"), (SAMSUNG, "KOSPI"), ("080220", "KOSDAQ"), (SAMSUNG, "KOSPI"),
                  ("12345", "KOSPI")])
    assert update() == 0
    assert world.kis.codes() == [SAMSUNG, "000660", "080220"]
    assert world.kis.anchors(SAMSUNG) == FULL_ANCHORS   # not asked again as a stock
    assert stored(world.db, SAMSUNG) == PAST[-330:]
    assert world.db.runs()[0]["stocks_total"] == 3


def test_without_the_calendar_stock_in_the_list_it_still_gives_the_calendar_only(world):
    world.stocks([("000660", "KOSPI"), ("080220", "KOSDAQ")])
    world.caps()
    assert update() == 0
    assert world.kis.codes() == [SAMSUNG, "000660", "080220"]
    assert stored(world.db, SAMSUNG) == [] and stored(world.db, "000660") == PAST[-330:]
    [run] = world.db.runs()
    assert (run["stocks_total"], run["stocks_ok"], run["days_ranked"]) == (2, 2, 1)


def test_a_day_kis_never_returns_is_stored_as_it_came(world):
    world.kis.listing("000660", missing={PAST[-45]})
    assert update() == 0
    assert stored(world.db, "000660") == [day for day in PAST[-330:] if day != PAST[-45]]
    assert world.db.runs()[0]["status"] == "ok"


def test_on_a_holiday_the_base_day_is_the_trading_day_before(world, capsys):
    world.kis.market = [day for day in MARKET if day != D]   # 2026-10-09 한글날
    world.caps(date(2026, 10, 8))
    assert update() == 0
    assert world.kis.anchors(SAMSUNG)[0] == D   # asked at the anchor; KIS answers up to the day before
    [rank] = world.db.rpcs(RANK_FUNCTION)
    assert rank.params["p_day"] == "2026-10-08" and len(rank.params["p_caps"]) == 3
    assert world.db.runs()[0]["as_of"] == "2026-10-08"
    assert capsys.readouterr().out.startswith("수급 갱신 완료(ok): 기준일 2026-10-08,")


def test_before_18_00_the_anchor_is_the_weekday_before(world):
    world.clock(datetime(2026, 10, 9, 8, 59, tzinfo=timezone.utc))   # 17:59 KST
    assert update() == 0
    assert world.kis.anchors(SAMSUNG)[0] == date(2026, 10, 8)
    assert world.db.runs()[0]["as_of"] == "2026-10-08"


# ── the next runs ────────────────────────────────────────────────────────────

def test_a_daily_run_asks_one_answer_per_stock_and_the_calendar_stock_back(world, capsys):
    world.caps()
    assert update() == 0
    world.clock(NEXT_NOW)
    world.caps(D2)
    world.kis.calls.clear()
    assert update() == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "수급 갱신 완료(ok): 기준일 2026-10-12, 성공 3종목, 실패 0종목, 순위 매긴 날 1일")
    assert world.kis.anchors(SAMSUNG)[0] == D2 and len(world.kis.anchors(SAMSUNG)) == 11
    assert world.kis.anchors("000660") == [D2] and world.kis.anchors("080220") == [D2]
    assert stored(world.db, "000660") == PAST[-330:] + [D2]
    assert [row["trade_date"] for row in world.db.rows(DAYS)] == ["2026-10-09", "2026-10-12"]
    assert world.db.row(DAILY, "000660", "2026-10-12")["window_days"] == 250


def test_an_interrupted_fill_goes_on_with_the_unfinished_stocks_only(world):
    world.stocks(STOCKS + [("000100", "KOSDAQ")])

    def interrupt(call):
        if call[1] == "080220" and len(world.kis.anchors("080220")) == 3:
            raise KeyboardInterrupt

    world.kis.during = interrupt
    with pytest.raises(KeyboardInterrupt):
        update()
    [run] = world.db.runs()
    assert (run["status"], run["message"]) == ("failed", "실행 중 오류로 멈췄습니다: KeyboardInterrupt")
    assert stored(world.db, "000660") == PAST[-330:]   # finished stocks stay
    assert stored(world.db, "080220") == [] and stored(world.db, "000100") == []
    world.kis.during = None
    world.kis.calls.clear()
    assert update() == 0
    assert world.kis.anchors(SAMSUNG) == FULL_ANCHORS and world.kis.anchors("000660") == [D]
    assert world.kis.anchors("080220") == FULL_ANCHORS and world.kis.anchors("000100") == FULL_ANCHORS
    assert all(stored(world.db, code) == PAST[-330:] for code in ("080220", "000100"))


def test_a_data_start_ends_the_asking_and_is_recorded_once(world):
    world.stocks(STOCKS + [("000100", "KOSDAQ"), ("000200", "KOSDAQ")])
    world.kis.listing("000100", listed=PAST[-45])   # its second answer meets the padding
    world.kis.listing("000200", listed=PAST[-10])   # padding in every latest answer
    assert update() == 0
    assert world.kis.anchors("000100") == [D, back(1)] and world.kis.anchors("000200") == [D]
    assert stored(world.db, "000100") == PAST[-45:] and stored(world.db, "000200") == PAST[-10:]
    assert world.db.rows(STOCKS_TABLE) == [
        {"stock_code": "000100", "listed_from": PAST[-45].isoformat(), "updated_at": NOW.isoformat()},
        {"stock_code": "000200", "listed_from": PAST[-10].isoformat(), "updated_at": NOW.isoformat()}]
    world.clock(NEXT_NOW)
    world.kis.calls.clear()
    assert update() == 0
    assert world.kis.anchors("000100") == [D2] and world.kis.anchors("000200") == [D2]
    assert len(world.db.queries(STOCKS_TABLE, "upsert")) == 2   # the same data start is not written again


# ── one stock's rows: all or nothing ─────────────────────────────────────────

def test_a_stock_with_one_failed_answer_saves_nothing_and_the_others_stay(world, capsys, caplog):
    world.stocks(TEN)
    world.caps()
    world.kis.fail("000003", kis_error("000003"), call=2)   # its older answer fails
    with caplog.at_level(logging.WARNING, logger=jobs.__name__):
        assert update() == 0
    out, _ = capsys.readouterr()
    assert out == ("수급 갱신 일부 완료(partial): 기준일 2026-10-09, 성공 9종목, 실패 1종목, 순위 매긴 날 1일. "
                   f"{PARTIAL_NOTE}\n")
    assert world.kis.anchors("000003") == [D, back(1)]
    assert stored(world.db, "000003") == []
    assert all(stored(world.db, code) == PAST[-330:] for code, _ in TEN if code != "000003")
    warned = [record.getMessage() for record in caplog.records if record.name == jobs.__name__]
    assert warned == [f"수급을 받지 못했습니다: 000003 ({reason('000003')})"]
    [run] = world.db.runs()
    assert (run["status"], run["stocks_ok"], run["stocks_failed"], run["days_ranked"]) == ("partial", 9, 1, 1)
    assert run["message"] == f"{PARTIAL_NOTE}. 못 받은 종목: 000003. 첫 실패 000003: {reason('000003')}"
    assert world.db.row(DAYS, "2026-10-09")["stocks_ranked"] == 9


@pytest.mark.parametrize("answer", [ValueError("time data 'x' does not match format '%Y%m%d'"), KeyError("date")])
def test_a_strange_answer_fails_that_stock_only(world, answer):
    world.stocks(TEN)
    world.kis.fail("000004", answer)
    assert update() == 0
    assert world.db.runs()[0]["status"] == "partial" and stored(world.db, "000004") == []


def test_a_stock_without_any_trading_day_is_not_received(world):
    world.stocks(TEN)
    world.kis.listing("000004", history_from=D + timedelta(days=1))   # KIS answers nothing
    assert update() == 0
    [run] = world.db.runs()
    assert run["stocks_failed"] == 1 and run["message"].endswith("첫 실패 000004: 받은 수급이 없습니다")


# ── the 20 % rule ────────────────────────────────────────────────────────────

def failing(world, positions):
    world.stocks(TEN)
    world.caps()
    for i in positions:
        world.kis.fail(TEN[i][0], kis_error(TEN[i][0]))


def test_exactly_20_percent_failed_is_partial_and_ranks(world):
    failing(world, [3, 6])
    assert update() == 0
    assert world.db.runs()[0]["status"] == "partial"
    assert len(world.db.rpcs(RANK_FUNCTION)) == 1 and world.db.queries(DAILY, "delete")


def test_more_than_20_percent_failed_ranks_and_cleans_nothing(world, capsys):
    failing(world, [2, 5, 9])
    world.db.add_raw("999990", date(2025, 1, 2))   # older than the cutoff: stays in a failed run
    assert update() == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert lines(err)[-1] == ("수급 갱신 실패(failed): 기준일 2026-10-09, 성공 7종목, 실패 3종목, 순위 매긴 날 0일. "
                              f"{FAILED_NOTE}")
    assert world.db.rpcs(RANK_FUNCTION) == [] and world.db.queries(DAYS) == []
    assert world.db.queries(DAILY, "delete") == [] and stored(world.db, "999990") == [date(2025, 1, 2)]
    assert ("snapshots", world.codes) not in world.prices.calls
    received = [code for i, (code, _) in enumerate(TEN) if i not in (2, 5, 9)]
    assert all(stored(world.db, code) == PAST[-330:] for code in received)   # the raw rows stay
    [run] = world.db.runs()
    assert (run["status"], run["as_of"], run["stocks_ok"], run["stocks_failed"], run["days_ranked"]) == (
        "failed", "2026-10-09", 7, 3, 0)
    assert run["message"] == (f"{FAILED_NOTE}. 못 받은 종목: 000002, 000005, 000009. "
                              f"첫 실패 000002: {reason('000002')}")


def test_a_run_stops_asking_once_it_has_failed(world, capsys):
    failing(world, [1, 2, 3])
    assert update() == 1
    assert world.kis.codes() == [SAMSUNG, "000001", "000002", "000003"]
    [run] = world.db.runs()
    assert (run["status"], run["stocks_ok"], run["stocks_failed"]) == ("failed", 1, 9)
    assert lines(capsys.readouterr().err)[-1] == (
        "수급 갱신 실패(failed): 기준일 2026-10-09, 성공 1종목, 실패 9종목, 순위 매긴 날 0일. "
        f"{FAILED_NOTE}. 기준을 넘은 뒤 남은 6종목은 받지 않았습니다")
    assert "기준을 넘은 뒤 남은 6종목은 받지 않았습니다" in run["message"]


def test_the_run_message_names_at_most_20_codes(world):
    many = [(SAMSUNG, "KOSPI")] + [(f"{i:06d}", "KOSDAQ") for i in range(1, 110)]
    world.stocks(many)
    world.kis.history_from = PAST[-40]   # short histories keep this test quick
    for code, _ in many[1:22]:
        world.kis.fail(code, kis_error(code))
    assert update() == 0   # 21 of 110: partial
    message = world.db.runs()[0]["message"]
    assert ", ".join(code for code, _ in many[1:21]) + " 외 1종목. 첫 실패 000001: " in message
    assert "000021" not in message


# ── the calendar stock ───────────────────────────────────────────────────────

def calendar_failed(reason_text: str) -> str:
    return (f"기준 종목(005930)을 받지 못해 거래일을 정할 수 없습니다 ({reason_text}). "
            "다른 종목은 받지 않았습니다")


@pytest.mark.parametrize("call", [1, 5], ids=["latest answer", "an older answer"])
def test_without_the_calendar_stock_no_other_stock_is_asked_and_the_run_fails(world, capsys, call):
    world.caps()
    world.kis.fail(SAMSUNG, kis_error(SAMSUNG), call=call)
    assert update() == 1
    assert world.kis.codes() == [SAMSUNG] and len(world.kis.anchors(SAMSUNG)) == call
    out, err = capsys.readouterr()
    assert out == ""
    assert lines(err)[-1] == ("수급 갱신 실패(failed): 기준일 없음, 성공 0종목, 실패 3종목, 순위 매긴 날 0일. "
                              + calendar_failed(reason(SAMSUNG)))
    assert world.db.runs() == [{
        "run_id": 1, "started_at": NOW.isoformat(), "finished_at": NOW.isoformat(), "status": "failed",
        "as_of": None, "stocks_total": 3, "stocks_ok": 0, "stocks_failed": 3, "days_ranked": 0,
        "message": calendar_failed(reason(SAMSUNG))}]
    assert [q.table for q in world.db.executed] == [RUNS, RUNS, RUNS]   # its runs read, start, finish
    assert world.prices.calls == [("latest_run",)]


def test_a_calendar_stock_without_any_trading_day_fails_the_run(world):
    world.kis.listing(SAMSUNG, history_from=D + timedelta(days=1))
    assert update() == 1
    assert world.kis.codes() == [SAMSUNG]
    assert world.db.runs()[0]["message"] == calendar_failed("받은 수급이 없습니다")


# ── the base day's market caps and the ranked days ───────────────────────────

def test_only_the_snapshots_of_the_base_day_give_market_caps(world):
    world.stocks(STOCKS + [("000100", "KOSDAQ"), ("000200", "KOSDAQ"), ("000300", "KOSDAQ")])
    world.prices.snapshot(SAMSUNG, "2026-10-09", CAP)
    world.prices.snapshot("000660", "2026-10-08", CAP)   # an older snapshot (no_data): no cap that day
    world.prices.snapshot("080220", "2026-10-09", None)
    world.prices.snapshot("000100", "2026-10-09", 0)
    world.prices.snapshot("000200", "2026-10-09", 2 * CAP)   # 000300 has no snapshot
    assert update() == 0
    [rank] = world.db.rpcs(RANK_FUNCTION)
    assert rank.params["p_caps"] == {SAMSUNG: CAP, "000200": 2 * CAP}
    assert world.db.row(DAILY, "000660", "2026-10-09")["ranked_at"] is None
    assert world.db.row(DAILY, "000660", "2026-10-09")["market_cap"] is None
    assert world.db.row(DAYS, "2026-10-09")["stocks_ranked"] == 2


def test_a_stock_without_a_cap_is_ranked_by_a_later_run_of_the_same_day(world):
    world.prices.snapshot(SAMSUNG, "2026-10-09", CAP)
    world.prices.snapshot("000660", "2026-10-09", CAP)
    world.prices.snapshot("080220", "2026-10-08", CAP)
    assert update() == 0
    first = world.db.row(DAILY, SAMSUNG, "2026-10-09")
    assert world.db.row(DAYS, "2026-10-09") == {"trade_date": "2026-10-09", "ranked_at": NOW.isoformat(),
                                                "stocks_ranked": 2, "run_id": 1}
    # the price update ran again and got 080220; the same day's flows update runs again
    world.prices.snapshot("080220", "2026-10-09", 3 * CAP)
    world.prices.snapshot(SAMSUNG, "2026-10-09", 9 * CAP)   # a ranked day keeps its values
    world.kis.values[(SAMSUNG, D)] = {"pe_fund_ntby_tr_pbmn": 12_345}
    world.db.now = later = "2026-10-09T11:00:00+00:00"
    world.clock(NOW + timedelta(hours=1))
    assert update() == 0
    again = world.db.row(DAILY, SAMSUNG, "2026-10-09")
    assert again["net_pe_fund"] == 12_345   # the day received again is written again
    assert {name: again[name] for name in ("market_cap", "window_days", "ranked_at", *RANK)} == {
        name: first[name] for name in ("market_cap", "window_days", "ranked_at", *RANK)}
    late = world.db.row(DAILY, "080220", "2026-10-09")
    assert (late["market_cap"], late["window_days"], late["ranked_at"]) == (3 * CAP, 250, later)
    assert world.db.row(DAYS, "2026-10-09") == {"trade_date": "2026-10-09", "ranked_at": NOW.isoformat(),
                                                "stocks_ranked": 3, "run_id": 1}   # the count only
    assert world.db.queries(DAYS, "update")[0].payload == {"stocks_ranked": 3}
    assert world.db.runs()[1]["days_ranked"] == 1


def test_a_day_without_any_ranked_stock_is_not_listed(world, capsys):
    world.caps(date(2026, 10, 8))   # the price update did not run that day
    assert update() == 0
    assert capsys.readouterr().out == ("수급 갱신 완료(ok): 기준일 2026-10-09, 성공 3종목, 실패 0종목, "
                                       "순위 매긴 날 0일\n")
    [rank] = world.db.rpcs(RANK_FUNCTION)
    assert rank.params["p_caps"] == {}
    assert world.db.rows(DAYS) == [] and world.db.queries(DAYS, "insert") == []
    assert world.db.runs()[0]["days_ranked"] == 0
    assert world.db.queries(DAILY, "delete")   # the cleanup still runs


def test_new_listings_under_60_days_get_a_cap_but_no_rank_and_no_listed_day(world):
    world.stocks([(SAMSUNG, "KOSPI"), ("000100", "KOSDAQ")])
    world.kis.listing(SAMSUNG, listed=PAST[-59])
    world.kis.listing("000100", listed=PAST[-20])
    world.caps()
    assert update() == 0
    row = world.db.row(DAILY, "000100", "2026-10-09")
    assert (row["market_cap"], row["window_days"], row["rank_pe_fund"]) == (CAP, 20, None)
    assert world.db.rows(DAYS) == []


def test_old_raw_rows_and_ranked_days_beyond_60_are_cleaned_up(world):
    world.caps()
    world.db.add_raw("999990", date(2025, 6, 25))   # before the cutoff: anchor − 470 days = 2025-06-26
    world.db.add_raw("999990", date(2025, 6, 26))
    for day in PAST[-61:-1]:
        world.db.add(DAYS, {"trade_date": day.isoformat(), "ranked_at": NOW.isoformat(), "stocks_ranked": 1})
    assert update() == 0
    assert stored(world.db, "999990") == [date(2025, 6, 26)]
    [delete] = world.db.queries(DAILY, "delete")
    assert delete.filter_values("lt", "trade_date") == [logic.raw_cutoff(D).isoformat()] == ["2025-06-26"]
    days = [row["trade_date"] for row in world.db.rows(DAYS)]
    assert len(days) == 60 and days[0] == PAST[-60].isoformat() and days[-1] == "2026-10-09"


def test_the_cleanup_never_deletes_rows_inside_the_retention_range(world, monkeypatch):
    # anchor − N days later than the calendar's retention start (as on a run during a long holiday,
    # when the anchor moves on and the calendar stays): the cutoff is the retention start instead
    monkeypatch.setattr(logic, "RAW_RETENTION_DAYS", 30)
    assert logic.raw_cutoff(D) > CALENDAR[0]
    world.caps()
    world.db.add_raw("999990", PAST[-311])   # before the retention start
    world.db.add_raw("999990", PAST[-310])   # its first day
    world.db.add_raw("999990", PAST[-200])
    assert update() == 0
    [delete] = world.db.queries(DAILY, "delete")
    assert delete.filter_values("lt", "trade_date") == [CALENDAR[0].isoformat()]
    assert stored(world.db, "999990") == [PAST[-310], PAST[-200]]
    for code in CODES:   # the first run's 330 days, cut back to the retention range only
        assert stored(world.db, code) == CALENDAR


# ── the access token: once a run, before any stock ───────────────────────────

TOKEN_REASON = "KisError: KIS access token failed: HTTP 403 EGW00133 접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)"


def run_without_token(reason_text: str) -> str:
    return f"KIS 접근 토큰을 받지 못했습니다 ({reason_text}). 아무 종목도 받지 않았습니다"


def token_error() -> KisError:
    return KisError(TOKEN_REASON.removeprefix("KisError: "), code="EGW00133", status=403)


@pytest.mark.parametrize("argv,runs", [([], ["running"]), (["--codes", "005930,080220"], [])], ids=["all", "codes"])
def test_the_token_is_asked_once_before_any_stock(world, argv, runs):
    seen = []
    world.kis.token_during = lambda: seen.append((list(world.kis.calls), [run["status"] for run in world.db.runs()]))
    assert update(*argv) == 0
    assert world.kis.token_asked == 1
    assert seen == [([], runs)]   # no stock asked yet; a full run's record is already running


def test_without_the_token_a_run_asks_no_stock_and_fails_at_once(world, capsys):
    world.caps()
    world.kis.token_error = token_error()
    assert update() == 1
    assert world.kis.token_asked == 1 and world.kis.calls == [] and world.kis.closed
    out, err = capsys.readouterr()
    assert out == ""
    assert lines(err) == ["수급 갱신 실패(failed): 기준일 없음, 성공 0종목, 실패 3종목, 순위 매긴 날 0일. "
                          + run_without_token(TOKEN_REASON)]
    assert world.db.runs() == [{
        "run_id": 1, "started_at": NOW.isoformat(), "finished_at": NOW.isoformat(), "status": "failed",
        "as_of": None, "stocks_total": 3, "stocks_ok": 0, "stocks_failed": 3, "days_ranked": 0,
        "message": run_without_token(TOKEN_REASON)}]
    assert [q.table for q in world.db.executed] == [RUNS, RUNS, RUNS]


def test_without_the_token_codes_ask_nothing_and_write_nothing(world, capsys):
    world.kis.token_error = token_error()
    assert update("--codes", "005930,080220") == 1
    assert world.kis.token_asked == 1 and world.kis.calls == [] and world.kis.closed
    assert capsys.readouterr() == ("", CHECKED + run_without_token(TOKEN_REASON) + "\n")
    assert world.db.writes() == [] and world.db.runs() == []


def test_an_interrupt_while_the_token_is_asked_closes_the_run_as_failed(world):
    world.kis.token_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        update()
    [run] = world.db.runs()
    assert run["status"] == "failed" and run["message"] == "실행 중 오류로 멈췄습니다: KeyboardInterrupt"
    assert world.kis.calls == [] and world.db.tables[DAILY] == {}


# ── with the real KIS client ─────────────────────────────────────────────────

class KisServer:
    """The KIS server behind a real ``KisClient`` (an httpx MockTransport: nothing leaves this PC).

    The token request gets ``token_answer()``. An investor request gets the 30 market days ending at
    its FID_INPUT_DATE_1 (from ``history_from`` on), newest first, numbers as text, the way KIS sends
    them. ``paths`` lists every request. ``clock`` and ``sleep`` are the client's: sleeping moves the
    clock (nothing waits) and ``sleeps`` keeps each wait.
    """

    def __init__(self) -> None:
        self.token_answer = lambda: httpx.Response(200, json={"access_token": "fake-access-token"})
        self.history_from = None
        self.paths: list[str] = []
        self.now = 0.0
        self.sleeps: list[float] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path == core_kis.TOKEN_PATH:
            return self.token_answer()
        assert request.url.path == core_kis.INVESTOR_DAILY_PATH
        assert request.headers["tr_id"] == core_kis.INVESTOR_DAILY_TR_ID
        code = request.url.params["FID_INPUT_ISCD"]
        anchor = datetime.strptime(request.url.params["FID_INPUT_DATE_1"], "%Y%m%d").date()
        days = [day for day in MARKET if day <= anchor and (self.history_from is None or day >= self.history_from)]
        rows = [{"stck_bsop_date": day.strftime("%Y%m%d"), "stck_clpr": str(CLOSE),
                 **{name: str(value) for name, value in amounts(code, day).items()}}
                for day in reversed(days[-30:])]
        return httpx.Response(200, json={"rt_cd": "0", "msg_cd": "MCA00000", "output1": {}, "output2": rows})

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def kis_server(world, monkeypatch):
    """``jobs.kis_client`` makes the real KIS client again, talking to a KisServer."""
    server = KisServer()
    monkeypatch.setenv("KIS_BASE_URL", "https://kis.example.invalid")   # never the real service
    monkeypatch.setattr(core_kis, "KisClient", functools.partial(
        core_kis.KisClient, transport=httpx.MockTransport(server.handle), sleep=server.sleep, clock=server.clock))
    monkeypatch.setattr(jobs, "kis_client", REAL_KIS_CLIENT)
    return server


def test_a_run_with_the_real_kis_client_stores_what_kis_answered(world, kis_server, capsys):
    world.stocks([(SAMSUNG, "KOSPI"), ("000660", "KOSPI")])
    world.caps()
    kis_server.history_from = PAST[-40]   # 40 trading days of history keep this quick
    assert update() == 0
    assert kis_server.paths[0] == core_kis.TOKEN_PATH and kis_server.paths.count(core_kis.TOKEN_PATH) == 1
    # 005930: 30 days, 10 older, then nothing older; 000660: back to the calendar's first day
    assert kis_server.paths.count(core_kis.INVESTOR_DAILY_PATH) == 3 + 2
    assert stored(world.db, SAMSUNG) == PAST[-40:] and stored(world.db, "000660") == PAST[-40:]
    row, kis_row = world.db.row(DAILY, "000660", "2026-10-09"), amounts("000660", D)
    assert row["net_foreigner"] == kis_row["frgn_reg_ntby_pbmn"]
    assert row["net_pension"] == kis_row["fund_ntby_tr_pbmn"] + kis_row["etc_orgt_ntby_tr_pbmn"]
    assert row["net_individual"] == kis_row["prsn_ntby_tr_pbmn"]
    assert (row["market_cap"], row["window_days"], row["rank_pe_fund"]) == (CAP, 40, None)   # under 60 days
    assert capsys.readouterr().out.startswith("수급 갱신 완료(ok): 기준일 2026-10-09, 성공 2종목, 실패 0종목,")


TOKEN_FAILURES = {   # the token answer, then one attempt's token requests (the try and its retries) and waits
    "403": (lambda: httpx.Response(403, json={"error_code": "EGW00133",
                                              "error_description": "접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)"}),
            1, [], TOKEN_REASON),
    "5xx": (lambda: httpx.Response(503, text="Service Unavailable"), 1 + core_kis.MAX_RETRIES, [1.0, 2.0, 4.0],
            "KisError: KIS access token failed after 3 retries: HTTP 503"),
}


@pytest.mark.parametrize("answer,requests,waits,reason_text", list(TOKEN_FAILURES.values()), ids=list(TOKEN_FAILURES))
def test_with_the_real_client_a_run_asks_kis_for_one_token_and_no_stock(world, kis_server, capsys, answer,
                                                                        requests, waits, reason_text):
    kis_server.token_answer = answer
    assert update() == 1
    assert kis_server.paths == [core_kis.TOKEN_PATH] * requests   # one attempt for the whole run, no stock
    assert kis_server.sleeps == pytest.approx(waits)
    out, err = capsys.readouterr()
    assert out == ""
    assert lines(err) == ["수급 갱신 실패(failed): 기준일 없음, 성공 0종목, 실패 3종목, 순위 매긴 날 0일. "
                          + run_without_token(reason_text)]
    assert world.db.runs()[0]["message"] == run_without_token(reason_text)


@pytest.mark.parametrize("answer,requests,waits,reason_text", list(TOKEN_FAILURES.values()), ids=list(TOKEN_FAILURES))
def test_with_the_real_client_codes_ask_kis_for_one_token_and_no_stock(world, kis_server, capsys, answer,
                                                                       requests, waits, reason_text):
    kis_server.token_answer = answer
    assert update("--codes", "005930,080220") == 1
    assert kis_server.paths == [core_kis.TOKEN_PATH] * requests
    assert capsys.readouterr() == ("", CHECKED + run_without_token(reason_text) + "\n")
    assert world.db.writes() == [] and world.db.runs() == []


def test_a_refused_token_that_repeats_the_keys_shows_and_stores_no_key(world, kis_server, capsys):
    kis_server.token_answer = lambda: httpx.Response(403, json={
        "error_code": "EGW00103", "error_description": f"유효하지 않은 AppKey입니다 {APP_KEY} {APP_SECRET}"})
    assert update() == 1
    assert update("--codes", "005930") == 1
    out, err = capsys.readouterr()
    shown = out + err + str(world.db.runs())
    assert "EGW00103" in shown
    for secret in (APP_KEY, APP_SECRET, KEY):
        assert secret not in shown


# ── crashes ──────────────────────────────────────────────────────────────────

def test_a_db_error_while_saving_closes_the_run_as_failed_and_goes_up(world):
    world.db.fail_next("upsert")
    with pytest.raises(FakeDBError):
        update()
    [run] = world.db.runs()
    assert run["status"] == "failed" and run["finished_at"] == NOW.isoformat()
    assert run["message"] == "실행 중 오류로 멈췄습니다: FakeDBError"
    assert world.kis.closed and world.kis.codes() == [SAMSUNG]


def test_a_db_error_while_ranking_closes_the_run_as_failed(world):
    world.caps()

    def timed_out(params):
        raise FakeDBError("canceling statement due to statement timeout")

    world.db.rank = timed_out   # the coverage read passes; the ranking call fails
    with pytest.raises(FakeDBError, match="timeout"):
        update()
    [run] = world.db.runs()
    assert run["status"] == "failed" and run["message"] == "실행 중 오류로 멈췄습니다: FakeDBError"
    assert all(stored(world.db, code) == PAST[-330:] for code in CODES)   # the raw rows stay


def test_a_run_record_that_cannot_be_written_stops_before_kis(world):
    world.db.fail_next("insert")
    with pytest.raises(FakeDBError):
        update()
    assert world.kis.calls == [] and world.kis.token_asked == 0 and world.db.runs() == []


def test_a_run_that_cannot_be_closed_still_shows_the_first_error(world):
    world.db.fail_next("upsert")
    world.db.fail_next("update")
    with pytest.raises(FakeDBError, match="upsert"):
        update()
    assert world.db.runs()[0]["status"] == "running"


# ── --codes: a check that writes nothing ─────────────────────────────────────

def printed(out: str):
    *body, summary = lines(out)
    return json.loads("\n".join(body)), summary


def expected_check(code: str, anchors=CHECK_ANCHORS) -> dict:
    """What the check prints for a stock with a full history: the same rule on the same days."""
    days = sorted({day for anchor in anchors for day in [d for d in PAST if d <= anchor][-30:]})
    rows = [logic.daily_row(code, {"date": day, **amounts(code, day)}) for day in days]
    n, ranks = logic.window_ranks(rows, D)
    latest = logic.investor_amounts(amounts(code, D))
    return {"as_of": "2026-10-09", "window_days": n,
            "amounts": {key: value * 1_000_000 for key, value in latest.items()},
            "ranks": ranks, "short_history": n < 250, "institution_gap": logic.institution_gap(latest)}


def test_codes_print_the_base_day_ranks_and_write_nothing(world, capsys):
    assert update("--codes", "080220,005930") == 0
    out, err = capsys.readouterr()
    results, summary = printed(out)
    assert err == ""
    assert summary == CHECKED + "기준일 2026-10-09, 받음 2종목, 못 받음 0종목"
    assert list(results) == ["080220", "005930"]
    assert results["080220"] == expected_check("080220")
    assert results["005930"]["window_days"] == 250 and results["005930"]["short_history"] is False
    assert list(results["005930"]["amounts"]) == list(logic.INVESTOR_KEYS)
    assert list(results["005930"]["ranks"]) == list(logic.INVESTOR_KEYS)
    assert world.kis.codes() == ["080220", "005930"]
    assert all(world.kis.anchors(code) == CHECK_ANCHORS for code in ("080220", "005930"))
    assert world.db.writes() == [] and world.db.runs() == []
    assert [q.table for q in world.db.executed] == [RUNS]   # the refusal check only
    assert world.prices.calls == [("latest_run",)]


def test_codes_show_the_institution_gap(world, capsys):
    world.kis.values[("080220", D)] = {name: 0 for name in FIELDS} | {
        "scrt_ntby_tr_pbmn": 10, "orgn_ntby_tr_pbmn": 7, "etc_orgt_ntby_tr_pbmn": 2, "fund_ntby_tr_pbmn": 1}
    assert update("--codes", "080220") == 0
    results, _ = printed(capsys.readouterr().out)
    # (금융투자 10 + 연기금 1 + 2) − 기관합계 (7 + 2) = 4, millions of won
    assert results["080220"]["institution_gap"] == 4
    assert results["080220"]["amounts"]["institution"] == 9_000_000


def test_codes_of_a_new_listing_show_a_short_history_without_ranks(world, capsys):
    world.kis.listing("080220", listed=PAST[-45])
    assert update("--codes", "080220") == 0
    results, _ = printed(capsys.readouterr().out)
    assert results["080220"]["window_days"] == 45 and results["080220"]["short_history"] is True
    assert results["080220"]["ranks"] == {key: None for key in logic.INVESTOR_KEYS}
    assert world.kis.anchors("080220") == [D, back(1)]


def test_codes_with_one_not_received_exit_1(world, capsys):
    world.kis.fail("080220", kis_error("080220"), call=4)
    assert update("--codes", "005930,080220") == 1
    out, err = capsys.readouterr()
    results = json.loads(out)
    assert results["080220"] == {"error": reason("080220")}
    assert results["005930"]["window_days"] == 250
    assert lines(err)[-1] == CHECKED + "기준일 2026-10-09, 받음 1종목, 못 받음 1종목"
    assert world.db.writes() == []


def test_a_code_not_in_the_stock_list_is_not_asked(world, capsys):
    assert update("--codes", "999999,005930") == 1
    out, err = capsys.readouterr()
    results = json.loads(out)
    assert results["999999"] == {"error": "종목표에 없는 종목코드입니다"}
    assert world.kis.codes() == ["005930"]
    assert lines(err)[-1] == CHECKED + "기준일 2026-10-09, 받음 1종목, 못 받음 1종목"


def test_codes_none_received_have_no_base_day(world, capsys):
    assert update("--codes", "999999") == 1
    assert lines(capsys.readouterr().err)[-1] == CHECKED + "기준일 없음, 받음 0종목, 못 받음 1종목"


# ── the KIS client and .env ──────────────────────────────────────────────────

def test_the_kis_client_gets_the_keys_address_call_rate_and_token_file(monkeypatch):
    made = []

    class Recorder:
        def __init__(self, app_key, app_secret, **options):
            made.append((app_key, app_secret, options))

    monkeypatch.setattr(core_kis, "KisClient", Recorder)
    cfg = SimpleNamespace(app_key=APP_KEY, app_secret=APP_SECRET, base_url="https://kis.example.invalid",
                          max_calls_per_sec=2.5, token_cache="C:/shared/state/kis_token.json")
    assert isinstance(REAL_KIS_CLIENT(cfg), Recorder)
    assert made == [(APP_KEY, APP_SECRET, {"max_calls_per_sec": 2.5, "base_url": "https://kis.example.invalid",
                                           "token_cache": "C:/shared/state/kis_token.json"})]


def test_the_clock_is_aware_utc():
    now = REAL_UTC_NOW()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


def test_one_kis_client_a_run_with_the_settings(world, monkeypatch):
    monkeypatch.setenv("FLOWS_MAX_CALLS_PER_SEC", "4")
    monkeypatch.setenv("KIS_BASE_URL", "https://kis.example.invalid")
    monkeypatch.setenv("KIS_TOKEN_CACHE", "C:/shared/state/kis_token.json")
    assert update() == 0
    [cfg] = world.kis_made
    assert (cfg.app_key, cfg.app_secret, cfg.max_calls_per_sec, cfg.base_url, cfg.token_cache) == (
        APP_KEY, APP_SECRET, 4.0, "https://kis.example.invalid", "C:/shared/state/kis_token.json")


def test_values_added_to_env_are_used(env_file, world, monkeypatch, capsys):
    for name in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "KIS_APP_KEY", "KIS_APP_SECRET"):
        monkeypatch.delenv(name)
    assert update() == 4
    assert capsys.readouterr().err == NOT_READY + DB_REASON + "\n"
    env_file.write_text("SUPABASE_URL=https://from-env-file.test\nSUPABASE_SERVICE_KEY=k\n"
                        f"KIS_APP_KEY={APP_KEY}\nKIS_APP_SECRET={APP_SECRET}\nFLOWS_MAX_CALLS_PER_SEC=3\n",
                        encoding="utf-8")
    assert update() == 0
    assert world.db.made == [("https://from-env-file.test", "k")]
    assert (world.kis_made[0].app_key, world.kis_made[0].max_calls_per_sec) == (APP_KEY, 3.0)


def test_no_key_is_printed_or_stored(world, capsys):
    failing(world, [1, 2, 3])
    update()
    update("--codes", "000001,000005")
    out, err = capsys.readouterr()
    for secret in (APP_KEY, APP_SECRET, KEY):
        assert secret not in out and secret not in err
        assert all(secret not in str(run) for run in world.db.runs())


# ── output on a cp949 console or pipe ────────────────────────────────────────

FOREIGN = "잠시 후 – réessayez"   # an error text from KIS or the network can hold what cp949 lacks
REPLACED = "잠시 후 ? r?essayez"


def cp949_pipes(monkeypatch) -> SimpleNamespace:
    """Point stdout and stderr at cp949 pipes with strict errors, as a pipe or a redirect is on
    this PC. Called in the test body: pytest puts its own capture back between a fixture and the
    test."""
    pipes = SimpleNamespace(out=io.TextIOWrapper(io.BytesIO(), encoding="cp949"),
                            err=io.TextIOWrapper(io.BytesIO(), encoding="cp949"))
    monkeypatch.setattr(sys, "stdout", pipes.out)
    monkeypatch.setattr(sys, "stderr", pipes.err)
    return pipes


def written(stream) -> str:
    stream.flush()
    return stream.buffer.getvalue().decode("cp949").replace("\r\n", "\n")


def test_a_run_writes_its_summary_line_on_a_cp949_pipe(world, monkeypatch):
    world.kis.token_error = KisError(f"KIS access token failed: HTTP 403 EGW00133 {FOREIGN}", status=403)
    pipes = cp949_pipes(monkeypatch)
    assert update() == 1
    assert written(pipes.out) == ""
    assert lines(written(pipes.err)) == [
        "수급 갱신 실패(failed): 기준일 없음, 성공 0종목, 실패 3종목, 순위 매긴 날 0일. "
        + run_without_token(f"KisError: KIS access token failed: HTTP 403 EGW00133 {REPLACED}")]
    # Only the output is replaced: the run record keeps the text.
    assert world.db.runs()[0]["message"] == run_without_token(
        f"KisError: KIS access token failed: HTTP 403 EGW00133 {FOREIGN}")


def test_codes_write_their_results_on_a_cp949_pipe(world, monkeypatch):
    world.kis.fail("080220", KisError(f"KIS investor trading 080220 failed: {FOREIGN}", status=500))
    pipes = cp949_pipes(monkeypatch)
    assert update("--codes", "005930,080220") == 1
    results = json.loads(written(pipes.out))
    assert results["080220"] == {"error": f"KisError: KIS investor trading 080220 failed: {REPLACED}"}
    assert results["005930"]["window_days"] == 250
    assert lines(written(pipes.err))[-1].startswith(CHECKED)
