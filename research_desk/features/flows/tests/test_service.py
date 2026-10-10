"""features.flows window: ``latest_run()`` and the DB preparation behind it.

The DB is the in-memory FakeSupabase handed out by core.db.supabase_client; without a test's setup
core.db.supabase_client refuses. Checked: the latest run with the latest successful as_of (the same
shape as the prices window), readiness (NotReady("수급", …), tried again on the next call, .env
values picked up, prepared once), creating the service reads nothing.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pytest

from research_desk.core import db as core_db
from research_desk.core.settings import NotReady
from research_desk.features import flows
from research_desk.features.flows import service as service_module
from research_desk.features.flows.service import FlowsService, get_service

from .fakes import KEY, RUNS, URL, FakeSupabase

ENV = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_DB_URL")
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


def run(run_id, day, status, as_of=None, started=None):
    return {"run_id": run_id, "started_at": started or f"{day}T10:00:00+00:00", "status": status,
            "as_of": as_of or day}


# ── latest_run ───────────────────────────────────────────────────────────────

def test_no_run_gives_none(db):
    assert flows.latest_run() is None


def test_the_latest_run_with_the_as_of_of_the_latest_successful_run(db):
    db.add(RUNS, run(1, "2026-10-06", "ok"))
    db.add(RUNS, run(2, "2026-10-07", "partial"))
    db.add(RUNS, run(3, "2026-10-08", "failed"))
    assert flows.latest_run() == {
        "last_run_at": datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc),
        "last_run_status": "failed",
        "as_of": date(2026, 10, 7),
    }


@pytest.mark.parametrize("status", ["ok", "partial"])
def test_a_successful_latest_run_gives_its_own_as_of(db, status):
    db.add(RUNS, run(1, "2026-10-07", "ok"))
    db.add(RUNS, run(2, "2026-10-08", status))
    assert flows.latest_run()["as_of"] == date(2026, 10, 8)
    assert flows.latest_run()["last_run_status"] == status


def test_a_running_run_is_the_latest_run(db):
    db.add(RUNS, run(1, "2026-10-07", "ok"))
    db.add(RUNS, {"run_id": 2, "started_at": "2026-10-08T10:00:00+00:00", "status": "running"})
    assert flows.latest_run() == {"last_run_at": datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc),
                                  "last_run_status": "running", "as_of": date(2026, 10, 7)}


def test_without_a_successful_run_there_is_no_as_of(db):
    db.add(RUNS, run(1, "2026-10-08", "failed"))
    assert flows.latest_run() == {"last_run_at": datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc),
                                  "last_run_status": "failed", "as_of": None}


def test_the_run_time_is_given_in_utc_whatever_offset_the_db_uses(db):
    db.add(RUNS, run(1, "2026-10-08", "ok", started="2026-10-08T19:00:05.5+09:00"))
    last_run_at = flows.latest_run()["last_run_at"]
    assert last_run_at == datetime(2026, 10, 8, 10, 0, 5, 500000, tzinfo=timezone.utc)
    assert last_run_at.utcoffset().total_seconds() == 0


# ── readiness ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("missing", ["SUPABASE_URL", "SUPABASE_SERVICE_KEY"])
def test_without_db_settings_the_window_is_not_ready_until_they_come(db, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(NotReady) as excinfo:
        flows.latest_run()
    assert (excinfo.value.area, excinfo.value.reason) == ("수급", DB_REASON)
    assert str(excinfo.value) == f"수급 기능을 지금 쓸 수 없습니다: {DB_REASON}"
    assert db.made == []
    monkeypatch.setenv(missing, URL if missing == "SUPABASE_URL" else KEY)
    assert flows.latest_run() is None   # tried again on the next call
    assert flows.latest_run() is None
    assert db.made == [(URL, KEY)]      # prepared once, then kept


def test_db_settings_added_to_env_are_used_on_the_next_call(env_file, db, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL")
    monkeypatch.delenv("SUPABASE_SERVICE_KEY")
    with pytest.raises(NotReady):
        flows.latest_run()
    env_file.write_text("SUPABASE_URL=https://from-env-file.test\nSUPABASE_SERVICE_KEY=k\n", encoding="utf-8")
    assert flows.latest_run() is None
    assert db.made == [("https://from-env-file.test", "k")]


def test_the_db_client_is_prepared_once(db):
    db.add(RUNS, run(1, "2026-10-08", "ok"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda _: flows.latest_run(), range(16)))
    assert all(answer["as_of"] == date(2026, 10, 8) for answer in answers)
    assert db.made == [(URL, KEY)]


def test_creating_the_service_reads_nothing():
    # no settings at all, and core.db.supabase_client refuses: still no error
    created = FlowsService()
    assert isinstance(created, FlowsService)
    assert get_service() is get_service()


def test_the_window_names():
    assert flows.latest_run is service_module.latest_run
    assert service_module.AREA == "수급"


def test_the_window_looks_the_service_up_when_called(db, monkeypatch):
    other = FlowsService()
    seen = threading.Event()
    monkeypatch.setattr(other, "latest_run", lambda: seen.set() or "from the other service")
    monkeypatch.setattr(service_module, "_service", other)
    assert flows.latest_run() == "from the other service" and seen.is_set()


@pytest.mark.parametrize("value,moment", [
    ("2026-10-08T10:00:00+00:00", datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)),
    ("2026-10-08T19:00:00+09:00", datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)),
    ("2026-10-08T10:00:00", datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)),   # no offset: UTC
    (datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc), datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)),
])
def test_a_db_time_becomes_an_aware_utc_datetime(value, moment):
    assert service_module.instant(value) == moment
    assert service_module.instant(value).utcoffset().total_seconds() == 0
