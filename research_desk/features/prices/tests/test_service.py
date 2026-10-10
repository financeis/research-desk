"""features.prices window: ``snapshots(codes)``, ``latest_run()`` and
``daily_closes(codes, since)`` (docs/business-rules.md, 주가 스냅샷).

The DB is the in-memory FakeSupabase handed out by core.db.supabase_client; without a test's
setup core.db.supabase_client refuses. Checked: the window shape (§12.1 price + market, percent),
codes without a snapshot left out, chunked reads, the latest run with the latest successful
as_of; the stored adjusted closes from a day on (oldest first, only codes with rows, in the order
asked, a plain string refused, paged over 1000 rows); readiness (NotReady("주가", …), retried on
the next call, .env values picked up, prepared once), creating the service reads nothing, and
importing the window loads no web, HTTP, KIS or MongoDB package.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import research_desk
from research_desk.core import db as core_db
from research_desk.core.settings import NotReady
from research_desk.features import prices
from research_desk.features.prices import logic
from research_desk.features.prices import service as service_module
from research_desk.features.prices.service import PricesService, get_service

from .fakes import KEY, URL, FakeSupabase

REPOSITORY = Path(research_desk.__file__).resolve().parent.parent
ENV = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_DB_URL", "KIS_APP_KEY", "KIS_APP_SECRET",
       "KIS_BASE_URL", "PRICES_MAX_CALLS_PER_SEC", "KRX_CSV_PATH")
DB_REASON = "DB 접속 설정(SUPABASE_URL, SUPABASE_SERVICE_KEY)이 없습니다"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No settings unless a test sets them, a fresh process-wide service, no real Supabase client."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(service_module, "_service", None)

    def refuse(url, key):
        raise AssertionError("no real Supabase client in these tests")

    monkeypatch.setattr(core_db, "supabase_client", refuse)


@pytest.fixture
def db(monkeypatch):
    """DB settings set; core.db.supabase_client hands out the stand-in and records each call."""
    fake = FakeSupabase()

    def supabase_client(url, key):
        fake.made.append((url, key))
        return fake

    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", KEY)
    monkeypatch.setattr(core_db, "supabase_client", supabase_client)
    return fake


SAMSUNG = {
    "stock_code": "005930", "market": "KOSPI", "as_of": "2026-10-08", "close": 61000,
    "market_cap": 364_000_000_000_000, "avg_value_20d": 1_234_567_890, "traded": True,
    "ret_1w": 1.5, "ret_1m": 2.5, "ret_3m": -4.0, "ret_6m": 9.0,
    "xret_1w": 0.5, "xret_1m": -1.0, "xret_3m": None, "xret_6m": 3.0,
    "flags": [], "updated_at": "2026-10-08T09:40:00+00:00",
}
JEJU = SAMSUNG | {"stock_code": "080220", "market": "KOSDAQ", "close": 25000, "traded": False,
                  "flags": ["no_data", "halted"]}


# ── snapshots ────────────────────────────────────────────────────────────────

def test_snapshots_give_the_screen_price_plus_market_per_code(db):
    db.add_snapshot(SAMSUNG)
    db.add_snapshot(JEJU)
    found = prices.snapshots(["080220", "005930", "000660"])
    assert list(found) == ["080220", "005930"]   # no snapshot for 000660: left out
    assert found["005930"] == {
        "market": "KOSPI", "as_of": "2026-10-08", "close": 61000, "market_cap": 364_000_000_000_000,
        "avg_value_20d": 1_234_567_890, "traded": True,
        "returns": {"1w": 1.5, "1m": 2.5, "3m": -4.0}, "excess": {"1w": 0.5, "1m": -1.0, "3m": None},
        "flags": [],
    }
    assert found["080220"]["market"] == "KOSDAQ" and found["080220"]["flags"] == ["no_data", "halted"]
    assert found["080220"]["traded"] is False


def test_snapshots_ask_each_code_once_in_chunks(db):
    for i in range(0, 600, 3):
        db.add_snapshot(SAMSUNG | {"stock_code": f"{i:06d}"})
    codes = [f"{i:06d}" for i in range(450)] * 2
    found = prices.snapshots(codes)
    assert list(found) == [f"{i:06d}" for i in range(0, 450, 3)]
    reads = db.queries("stock_price_snapshot", "select")
    assert [len(q.filter_values("in", "stock_code")[0]) for q in reads] == [200, 200, 50]


def test_snapshots_of_no_codes_need_no_db():
    assert prices.snapshots([]) == {}
    assert prices.snapshots(["", None]) == {}


def test_one_plain_string_is_not_a_list_of_codes(db):
    with pytest.raises(TypeError):
        prices.snapshots("005930")


def test_an_empty_snapshot_table_gives_an_empty_answer(db):
    assert prices.snapshots(["005930"]) == {}


# ── daily_closes ─────────────────────────────────────────────────────────────

def close(code, day, value):
    return {"stock_code": code, "trade_date": day, "close": value}


def test_daily_closes_give_the_stored_closes_from_since_oldest_first(db):
    for row in (close("005930", "2026-10-08", 61000), close("005930", "2026-10-06", 60500),
                close("005930", "2026-10-02", 59000), close("005930", "2026-10-07", 60000),
                close("080220", "2026-10-08", 25000), close("000660", "2026-09-30", 170000)):
        db.add_daily(row)
    found = prices.daily_closes(["080220", "000660", "005930", "080220", "123456"], "2026-10-06")
    assert found == {
        "080220": [{"date": "2026-10-08", "close": 25000}],
        "005930": [{"date": "2026-10-06", "close": 60500}, {"date": "2026-10-07", "close": 60000},
                   {"date": "2026-10-08", "close": 61000}],
    }   # 000660 has rows only before since and 123456 none: left out; each code once, in the order asked
    assert list(found) == ["080220", "005930"]


def test_daily_closes_take_since_as_a_date_too(db):
    db.add_daily(close("005930", "2026-10-06", 60500))
    db.add_daily(close("005930", "2026-10-05", 60100))
    assert prices.daily_closes(["005930"], date(2026, 10, 6)) == {"005930": [{"date": "2026-10-06", "close": 60500}]}
    assert db.queries("stock_price_daily", "select")[0].filter_values("gte", "trade_date") == ["2026-10-06"]


def test_daily_closes_give_whole_won(db):
    db.add_daily(close("005930", "2026-10-06", 60500))
    [day] = prices.daily_closes(["005930"], "2026-10-01")["005930"]
    assert isinstance(day["close"], int) and isinstance(day["date"], str)


def test_daily_closes_page_through_more_than_1000_rows(db):
    first = date(2024, 1, 1)
    for code in ("005930", "000660"):
        for n in range(700):
            db.add_daily(close(code, (first + timedelta(days=n)).isoformat(), 1000 + n))
    found = prices.daily_closes(["005930", "000660"], first)
    assert [len(found[code]) for code in ("005930", "000660")] == [700, 700]
    assert found["005930"][0] == {"date": "2024-01-01", "close": 1000}
    assert found["005930"][-1] == {"date": (first + timedelta(days=699)).isoformat(), "close": 1699}
    reads = db.queries("stock_price_daily", "select")
    assert [(q.offset, q.limit_n) for q in reads] == [(0, 1000), (1000, 1000)]
    assert len({id(q) for q in reads}) == 2   # a new query for each page


def test_daily_closes_of_no_codes_need_no_db():
    assert prices.daily_closes([], "2026-10-01") == {}
    assert prices.daily_closes(["", None], "2026-10-01") == {}


def test_daily_closes_refuse_one_plain_string(db):
    with pytest.raises(TypeError):
        prices.daily_closes("005930", "2026-10-01")
    assert db.made == []


@pytest.mark.parametrize("since,error", [(None, TypeError), ("2026/10/01", ValueError)])
def test_daily_closes_refuse_a_since_that_is_not_a_day(db, since, error):
    with pytest.raises(error):
        prices.daily_closes(["005930"], since)
    assert db.made == []


def test_an_empty_history_gives_an_empty_answer(db):
    assert prices.daily_closes(["005930"], "2026-10-01") == {}


# ── latest_run ───────────────────────────────────────────────────────────────

def run(run_id, day, status, as_of=None, started=None):
    return {"run_id": run_id, "started_at": started or f"{day}T09:30:00+00:00", "status": status,
            "as_of": as_of or day}


def test_no_run_gives_none(db):
    assert prices.latest_run() is None


def test_the_latest_run_with_the_as_of_of_the_latest_successful_run(db):
    db.add_run(run(1, "2026-10-06", "ok"))
    db.add_run(run(2, "2026-10-07", "partial"))
    db.add_run(run(3, "2026-10-08", "failed"))
    assert prices.latest_run() == {
        "last_run_at": datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc),
        "last_run_status": "failed",
        "as_of": date(2026, 10, 7),
    }


@pytest.mark.parametrize("status", ["ok", "partial"])
def test_a_successful_latest_run_gives_its_own_as_of(db, status):
    db.add_run(run(1, "2026-10-07", "ok"))
    db.add_run(run(2, "2026-10-08", status))
    assert prices.latest_run()["as_of"] == date(2026, 10, 8)
    assert prices.latest_run()["last_run_status"] == status


def test_a_running_run_is_the_latest_run(db):
    db.add_run(run(1, "2026-10-07", "ok"))
    db.add_run({"run_id": 2, "started_at": "2026-10-08T09:30:00+00:00", "status": "running"})
    assert prices.latest_run() == {"last_run_at": datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc),
                                   "last_run_status": "running", "as_of": date(2026, 10, 7)}


def test_without_a_successful_run_there_is_no_as_of(db):
    db.add_run(run(1, "2026-10-08", "failed"))
    assert prices.latest_run() == {"last_run_at": datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc),
                                   "last_run_status": "failed", "as_of": None}


def test_the_run_time_is_given_in_utc_whatever_offset_the_db_uses(db):
    db.add_run(run(1, "2026-10-08", "ok", started="2026-10-08T18:30:05.5+09:00"))
    last_run_at = prices.latest_run()["last_run_at"]
    assert last_run_at == datetime(2026, 10, 8, 9, 30, 5, 500000, tzinfo=timezone.utc)
    assert last_run_at.utcoffset().total_seconds() == 0


# ── readiness ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("missing", ["SUPABASE_URL", "SUPABASE_SERVICE_KEY"])
def test_without_db_settings_the_window_is_not_ready_until_they_come(db, monkeypatch, missing):
    monkeypatch.delenv(missing)
    for call in (lambda: prices.snapshots(["005930"]), prices.latest_run,
                 lambda: prices.daily_closes(["005930"], "2026-10-01")):
        with pytest.raises(NotReady) as excinfo:
            call()
        assert (excinfo.value.area, excinfo.value.reason) == ("주가", DB_REASON)
        assert str(excinfo.value) == f"주가 기능을 지금 쓸 수 없습니다: {DB_REASON}"
    assert db.made == []
    monkeypatch.setenv(missing, URL if missing == "SUPABASE_URL" else KEY)
    assert prices.latest_run() is None   # tried again on the next call
    assert prices.snapshots(["005930"]) == {}
    assert prices.daily_closes(["005930"], "2026-10-01") == {}
    assert db.made == [(URL, KEY)]       # prepared once, then kept


def test_db_settings_added_to_env_are_used_on_the_next_call(env_file, db, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL")
    monkeypatch.delenv("SUPABASE_SERVICE_KEY")
    with pytest.raises(NotReady):
        prices.latest_run()
    env_file.write_text("SUPABASE_URL=https://from-env-file.test\nSUPABASE_SERVICE_KEY=k\n", encoding="utf-8")
    assert prices.latest_run() is None
    assert db.made == [("https://from-env-file.test", "k")]


def test_the_db_client_is_prepared_once(db):
    db.add_snapshot(SAMSUNG)
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda _: prices.snapshots(["005930"]), range(16)))
    assert all(answer == {"005930": logic.price_view(SAMSUNG)} for answer in answers)
    assert db.made == [(URL, KEY)]


def test_creating_the_service_reads_nothing():
    # no settings at all, and core.db.supabase_client refuses: still no error
    created = PricesService()
    assert isinstance(created, PricesService)
    assert get_service() is get_service()


def test_the_window_names():
    assert prices.snapshots is service_module.snapshots
    assert prices.latest_run is service_module.latest_run
    assert prices.daily_closes is service_module.daily_closes
    assert service_module.AREA == "주가"


def test_the_window_looks_the_service_up_when_called(db, monkeypatch):
    other = PricesService()
    seen = threading.Event()
    monkeypatch.setattr(other, "latest_run", lambda: seen.set() or "from the other service")
    monkeypatch.setattr(service_module, "_service", other)
    assert prices.latest_run() == "from the other service" and seen.is_set()


# ── import ───────────────────────────────────────────────────────────────────

HEAVY = ("fastapi", "starlette", "uvicorn", "httpx", "httpcore", "supabase", "postgrest", "pymongo",
         "bson", "langgraph", "research_desk.core.kis", "research_desk.core.db",
         "research_desk.web.app")


def test_importing_the_window_loads_no_web_http_kis_or_mongo_package():
    """``cli.py`` imports every command's window for every run, so the window stays light."""
    code = ("import sys, research_desk.features.prices as prices\n"
            "assert callable(prices.snapshots) and callable(prices.latest_run) and callable(prices.daily_closes)\n"
            f"print(sorted(m for m in {HEAVY!r} if m in sys.modules))\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPOSITORY, capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
