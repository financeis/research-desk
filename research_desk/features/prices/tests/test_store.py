"""features.prices store: the three price tables over Supabase REST (the FakeSupabase stand-in).

Checked: snapshot upserts in batches on stock_code, reads by code in chunks (each answer under
the 1000-row limit), the full read of stored returns paged 1000 rows at a time with a new query
per page, run records (insert as running, finish, the latest run, the latest successful run),
and the history (``stock_price_daily``): upserts in batches on (stock_code, trade_date) with
nothing sent back, the stored closes on given days read day by day in chunks of codes, the
delete of the rows before a day.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from research_desk.features.prices import store

from .fakes import FakeDBError, FakeSupabase


def code(i: int) -> str:
    return f"{i:06d}"


def snap(i: int, **values) -> dict:
    return {"stock_code": code(i), "market": "KOSPI", "as_of": "2026-10-08", "close": 1000 + i,
            "ret_1w": float(i), "flags": [], **values}


# ── snapshots ────────────────────────────────────────────────────────────────

def test_snapshot_rows_are_upserted_on_the_stock_code_in_batches():
    db = FakeSupabase()
    store.upsert_snapshots(db, [snap(i) for i in range(1200)])
    upserts = db.queries(store.SNAPSHOT_TABLE, "upsert")
    assert [len(q.rows_sent) for q in upserts] == [500, 500, 200]
    assert {q.on_conflict for q in upserts} == {"stock_code"}
    assert len(db.tables[store.SNAPSHOT_TABLE]) == 1200
    assert db.snapshot(code(7))["close"] == 1007


def test_no_rows_send_nothing():
    db = FakeSupabase()
    store.upsert_snapshots(db, [])
    assert db.executed == []


def test_an_upsert_of_some_columns_leaves_the_others():
    db = FakeSupabase(snapshots=[snap(1, flags=["halted"], updated_at="2026-10-07T09:40:00+00:00")])
    store.upsert_snapshots(db, [{"stock_code": code(1), "flags": ["no_data", "halted"]}])
    row = db.snapshot(code(1))
    assert row["flags"] == ["no_data", "halted"]
    assert (row["close"], row["ret_1w"], row["updated_at"]) == (1001, 1.0, "2026-10-07T09:40:00+00:00")


def test_snapshots_are_read_by_code_in_chunks():
    db = FakeSupabase(snapshots=[snap(i) for i in range(1200)])
    wanted = [code(i) for i in range(0, 2400, 2)]   # 1200 codes, half of them stored
    found = store.read_snapshots(db, wanted)
    assert sorted(found) == [code(i) for i in range(0, 1200, 2)]
    assert found[code(4)]["close"] == 1004 and found[code(4)]["stock_code"] == code(4)
    reads = db.queries(store.SNAPSHOT_TABLE, "select")
    assert [len(q.filter_values("in", "stock_code")[0]) for q in reads] == [200] * 6
    assert all(q.columns == list(store.SNAPSHOT_COLUMNS) for q in reads)


def test_a_snapshot_read_can_ask_for_some_columns():
    db = FakeSupabase(snapshots=[snap(1, flags=["halted"])])
    assert store.read_snapshots(db, [code(1)], columns=("stock_code", "flags")) == {
        code(1): {"stock_code": code(1), "flags": ["halted"]}}


def test_reading_no_codes_asks_nothing():
    db = FakeSupabase()
    assert store.read_snapshots(db, []) == {}
    assert db.executed == []


def test_all_stored_returns_are_read_1000_rows_a_page():
    db = FakeSupabase(snapshots=[snap(i, ret_6m=1.5) for i in range(2558)])
    rows = store.read_all_returns(db)
    assert [row["stock_code"] for row in rows] == [code(i) for i in range(2558)]
    assert rows[5] == {"stock_code": code(5), "market": "KOSPI", "ret_1w": 5.0, "ret_1m": None,
                       "ret_3m": None, "ret_6m": 1.5}
    reads = db.queries(store.SNAPSHOT_TABLE, "select")
    assert [(q.offset, q.limit_n) for q in reads] == [(0, 1000), (1000, 1000), (2000, 1000)]
    assert all(q.orders == [("stock_code", False)] for q in reads)
    assert len({id(q) for q in reads}) == 3   # a new query for each page


def test_a_full_last_page_is_followed_by_an_empty_one():
    db = FakeSupabase(snapshots=[snap(i) for i in range(1000)])
    assert len(store.read_all_returns(db)) == 1000
    assert len(db.queries(store.SNAPSHOT_TABLE, "select")) == 2


# ── run records ──────────────────────────────────────────────────────────────

def test_a_run_starts_as_running_and_gets_its_id():
    db = FakeSupabase(runs=[{"run_id": 4, "status": "ok"}])
    run_id = store.start_run(db, started_at="2026-10-08T09:30:00+00:00", total=2558)
    assert run_id == 5
    assert db.runs()[-1] == {
        "run_id": 5, "started_at": "2026-10-08T09:30:00+00:00", "finished_at": None, "status": "running",
        "as_of": None, "stocks_total": 2558, "stocks_ok": None, "stocks_failed": None, "message": None}


def test_finishing_a_run_writes_its_outcome_only_to_that_run():
    db = FakeSupabase(runs=[{"run_id": 1, "status": "running"}, {"run_id": 2, "status": "running"}])
    store.finish_run(db, 2, status="partial", finished_at="2026-10-08T09:45:00+00:00", as_of="2026-10-08",
                     stocks_ok=9, stocks_failed=1, message="못 받은 종목 1개")
    first, second = db.runs()
    assert first["status"] == "running" and first["finished_at"] is None
    assert {k: second[k] for k in ("status", "finished_at", "as_of", "stocks_ok", "stocks_failed",
                                   "message")} == {
        "status": "partial", "finished_at": "2026-10-08T09:45:00+00:00", "as_of": "2026-10-08",
        "stocks_ok": 9, "stocks_failed": 1, "message": "못 받은 종목 1개"}


RUNS = [
    {"run_id": 1, "started_at": "2026-10-06T09:30:00+00:00", "status": "ok", "as_of": "2026-10-06"},
    {"run_id": 2, "started_at": "2026-10-07T09:30:00+00:00", "status": "partial", "as_of": "2026-10-07"},
    {"run_id": 3, "started_at": "2026-10-08T09:30:00+00:00", "status": "failed", "as_of": "2026-10-08"},
]


def test_the_latest_run_and_the_latest_successful_run():
    db = FakeSupabase(runs=list(reversed(RUNS)))
    assert store.latest_run(db)["run_id"] == 3
    assert store.latest_successful_run(db)["run_id"] == 2
    for q in db.queries(store.RUNS_TABLE):
        assert q.orders == [("started_at", True), ("run_id", True)] and q.limit_n == 1


def test_a_tie_on_the_start_time_goes_to_the_later_run_id():
    db = FakeSupabase(runs=[{"run_id": 7, "started_at": "2026-10-08T09:30:00+00:00", "status": "ok"},
                            {"run_id": 8, "started_at": "2026-10-08T09:30:00+00:00", "status": "failed"}])
    assert store.latest_run(db)["run_id"] == 8
    assert store.latest_successful_run(db)["run_id"] == 7


def test_no_runs_give_none():
    db = FakeSupabase(runs=[{"run_id": 1, "status": "failed"}, {"run_id": 2, "status": "running"}])
    assert store.latest_run(FakeSupabase()) is None
    assert store.latest_successful_run(db) is None


# ── the adjusted-close history ───────────────────────────────────────────────

FIRST = date(2025, 9, 23)


def day(i: int) -> str:
    return (FIRST + timedelta(days=i)).isoformat()


def close_row(i: int, n: int, close: int) -> dict:
    return {"stock_code": code(i), "trade_date": day(n), "close": close}


def test_the_history_table_and_its_key():
    assert store.HISTORY_TABLE == "stock_price_daily"
    assert store.HISTORY_KEY == "stock_code,trade_date"


def test_history_rows_are_upserted_on_the_stock_and_day_500_a_request_with_nothing_sent_back():
    db = FakeSupabase()
    rows = [close_row(i // 300, i % 300, 1000 + i) for i in range(1200)]
    store.upsert_history(db, iter(rows))   # any iterable, read lazily
    upserts = db.queries(store.HISTORY_TABLE, "upsert")
    assert [len(q.rows_sent) for q in upserts] == [500, 500, 200]
    assert {q.on_conflict for q in upserts} == {"stock_code,trade_date"}
    assert {q.returning for q in upserts} == {"minimal"}
    assert db.history()[code(3)][day(299)] == 2199
    assert sum(len(days) for days in db.history().values()) == 1200


def test_an_upserted_day_replaces_the_stored_close_and_leaves_the_other_days():
    db = FakeSupabase(daily=[close_row(1, 0, 100), close_row(1, 1, 101), close_row(2, 0, 7)])
    store.upsert_history(db, [close_row(1, 1, 50), close_row(1, 2, 52)])
    assert db.history() == {code(1): {day(0): 100, day(1): 50, day(2): 52}, code(2): {day(0): 7}}


def test_no_history_rows_send_nothing():
    db = FakeSupabase()
    store.upsert_history(db, iter([]))
    assert db.executed == []


def test_a_history_close_must_be_whole_won():
    db = FakeSupabase()
    with pytest.raises(FakeDBError):
        store.upsert_history(db, [close_row(1, 0, 100.5)])
    assert db.history() == {}


def test_the_stored_closes_on_given_days_are_read_day_by_day_200_codes_a_request():
    db = FakeSupabase(daily=[close_row(i, 10, 5000 + i) for i in range(0, 450, 2)]
                      + [close_row(0, 0, 400), close_row(1, 0, 401), close_row(2, 3, 9)])
    found = store.read_history_on(db, {day(10): [code(i) for i in range(450)], day(0): [code(0), code(1), code(2)]})
    assert found[code(4)] == {day(10): 5004}
    assert found[code(0)] == {day(10): 5000, day(0): 400}
    assert found[code(1)] == {day(0): 401}          # nothing on day 10
    assert code(3) not in found and found[code(2)] == {day(10): 5002}   # day 3 was not asked
    reads = db.queries(store.HISTORY_TABLE, "select")
    assert [(q.filter_values("eq", "trade_date"), len(q.filter_values("in", "stock_code")[0])) for q in reads] == [
        ([day(10)], 200), ([day(10)], 200), ([day(10)], 50), ([day(0)], 3)]
    assert all(q.columns == ["stock_code", "trade_date", "close"] for q in reads)


def test_reading_on_no_days_asks_nothing():
    db = FakeSupabase()
    assert store.read_history_on(db, {}) == {}
    assert store.read_history_on(db, {day(0): []}) == {}
    assert db.executed == []


def test_the_history_before_a_day_is_deleted_in_one_request_with_nothing_sent_back():
    db = FakeSupabase(daily=[close_row(1, 0, 100), close_row(1, 1, 101), close_row(2, 0, 7), close_row(2, 5, 8)])
    store.delete_history_before(db, day(1))
    [delete] = db.queries(store.HISTORY_TABLE, "delete")
    assert delete.filters == [("lt", "trade_date", day(1))] and delete.returning == "minimal"
    assert db.history() == {code(1): {day(1): 101}, code(2): {day(5): 8}}   # the day itself is kept
