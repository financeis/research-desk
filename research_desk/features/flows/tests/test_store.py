"""features.flows store: the four flows tables and the two DB functions over Supabase REST (the
FakeSupabase stand-in of migration 009).

Checked: raw upserts in batches of 500 on (stock_code, trade_date) carrying only the key, the 11
amounts and fetched_at (so the market cap, window size and ranks the DB function wrote stay; a rank
or market cap column is refused before any request), every read that can pass 1000 rows paged with
a new query for each page (a day's rows, the data starts, the ranked days, the coverage function's
answer), one stock's latest rows, the exact count of a day's ranked rows, the ranking call, the
ranked-day list (insert, update of stocks_ranked only, delete), the raw-row cutoff, run records.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from research_desk.features.flows import store

from .fakes import COVERAGE_FUNCTION, DAILY, DAYS, NET, RANK, RANK_FUNCTION, RUNS, STOCKS, FakeDBError, FakeSupabase

NOW = "2026-10-09T10:00:00+00:00"
D = "2026-10-09"


def code(i: int) -> str:
    return f"{i:06d}"


def raw(i: int, day: str = D, **values) -> dict:
    """A raw row as the command sends it: the key, the 11 amounts, fetched_at."""
    return {"stock_code": code(i), "trade_date": day, **{name: i for name in NET},
            "fetched_at": NOW, **values}


def ranked(i: int, day: str = D, **values) -> dict:
    """A stored row the DB function has ranked."""
    return raw(i, day) | {"market_cap": 10**12 + i, "window_days": 250, "ranked_at": NOW,
                          **{name: 7 for name in RANK}} | values


# ── raw rows ─────────────────────────────────────────────────────────────────

def test_the_columns_of_a_raw_row():
    assert store.RAW_COLUMNS == ("stock_code", "trade_date", *NET, "fetched_at")
    assert store.DAILY_COLUMNS == ("stock_code", "trade_date", *NET, "market_cap", "window_days", *RANK,
                                   "ranked_at", "fetched_at")


def test_raw_rows_are_upserted_on_the_stock_and_day_500_a_request():
    db = FakeSupabase()
    store.upsert_daily(db, [raw(i) for i in range(1200)])
    upserts = db.queries(DAILY, "upsert")
    assert [len(q.rows_sent) for q in upserts] == [500, 500, 200]
    assert {q.on_conflict for q in upserts} == {"stock_code,trade_date"}
    assert {q.returning for q in upserts} == {"minimal"}
    assert len(db.tables[DAILY]) == 1200
    assert db.row(DAILY, code(7), D)["net_pe_fund"] == 7


def test_a_raw_upsert_leaves_the_market_cap_window_and_ranks_as_they_were():
    db = FakeSupabase(**{DAILY: [ranked(1)]})
    store.upsert_daily(db, [raw(1, net_pe_fund=-5, fetched_at="2026-10-10T10:00:00+00:00")])
    row = db.row(DAILY, code(1), D)
    assert (row["net_pe_fund"], row["fetched_at"]) == (-5, "2026-10-10T10:00:00+00:00")
    assert (row["market_cap"], row["window_days"], row["rank_pe_fund"], row["ranked_at"]) == (
        10**12 + 1, 250, 7, NOW)


@pytest.mark.parametrize("extra", ["market_cap", "window_days", "rank_pe_fund", "ranked_at", "bogus"])
def test_a_raw_row_with_any_other_column_is_refused_before_any_request(extra):
    db = FakeSupabase()
    with pytest.raises(ValueError, match=extra):
        store.upsert_daily(db, [raw(1), raw(2) | {extra: None}])
    assert db.executed == []


def test_no_raw_rows_send_nothing():
    db = FakeSupabase()
    store.upsert_daily(db, [])
    assert db.executed == []


def test_raw_rows_before_a_day_are_deleted():
    db = FakeSupabase(**{DAILY: [raw(1, "2025-06-25"), raw(1, "2025-06-26"), raw(2, "2025-01-02"),
                                 raw(2, "2026-10-09")]})
    store.delete_daily_before(db, "2025-06-26")
    assert sorted(db.tables[DAILY]) == [(code(1), "2025-06-26"), (code(2), "2026-10-09")]
    [delete] = db.queries(DAILY, "delete")
    assert delete.filter_values("lt", "trade_date") == ["2025-06-26"] and delete.returning == "minimal"


def test_a_days_rows_are_read_1000_a_page_in_code_order():
    db = FakeSupabase(**{DAILY: [ranked(i) for i in range(2500)] + [raw(i, "2026-10-08") for i in range(5)]})
    rows = store.read_day(db, D)
    assert [row["stock_code"] for row in rows] == [code(i) for i in range(2500)]
    assert rows[3] == ranked(3)
    reads = db.queries(DAILY, "select")
    assert [(q.offset, q.limit_n) for q in reads] == [(0, 1000), (1000, 1000), (2000, 1000)]
    assert all(q.orders == [("stock_code", False)] and q.filter_values("eq", "trade_date") == [D]
               for q in reads)
    assert len({id(q) for q in reads}) == 3   # a new query for each page


def test_a_full_last_page_is_followed_by_an_empty_one():
    db = FakeSupabase(**{DAILY: [raw(i) for i in range(1000)]})
    assert len(store.read_day(db, D, columns=("stock_code", "net_pe_fund"))) == 1000
    assert len(db.queries(DAILY, "select")) == 2


def test_one_stocks_latest_rows_come_newest_first():
    first = date(2026, 1, 1)
    days = [(first + timedelta(days=i)).isoformat() for i in range(300)]
    db = FakeSupabase(**{DAILY: [raw(1, day) for day in days] + [raw(2, day) for day in days[:10]]})
    rows = store.read_stock(db, code(1), 250)
    assert [row["trade_date"] for row in rows] == list(reversed(days))[:250]
    [read] = db.queries(DAILY, "select")
    assert read.orders == [("trade_date", True)] and read.limit_n == 250
    assert read.filter_values("eq", "stock_code") == [code(1)]
    assert store.read_stock(db, code(3), 21) == []
    with pytest.raises(ValueError):
        store.read_stock(db, code(1), store.PAGE + 1)


def test_the_ranked_rows_of_a_day_are_counted_by_the_server():
    rows = [ranked(i) for i in range(1500)]
    rows += [raw(2000), ranked(2001, rank_pe_fund=None), ranked(2002, market_cap=None), ranked(1, "2026-10-08")]
    db = FakeSupabase(**{DAILY: rows})
    assert store.count_ranked(db, D) == 1500   # past the 1000-row limit: counted, not fetched
    [read] = db.queries(DAILY, "select")
    assert (read.count, read.head) == ("exact", True)
    assert store.count_ranked(db, "2026-10-07") == 0


# ── the DB functions ─────────────────────────────────────────────────────────

def test_a_day_of_a_few_stocks_is_ranked_in_one_call_with_the_caps_and_the_window():
    days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(282)]   # up to D
    db = FakeSupabase(**{DAILY: [raw(1, day) for day in days]})
    assert store.rank_day(db, D, {code(1): 5 * 10**11}, window=250, min_days=60) == 1
    [call] = db.rpcs(RANK_FUNCTION)
    assert call.params == {"p_day": D, "p_caps": {code(1): 5 * 10**11}, "p_window": 250, "p_min_days": 60}
    row = db.row(DAILY, code(1), D)
    assert (row["market_cap"], row["window_days"], row["rank_pe_fund"], row["ranked_at"]) == (5 * 10**11, 250, 1, NOW)


def test_a_day_of_many_stocks_is_ranked_RANK_STOCKS_stocks_a_call():
    # one call over every stock outran the API's 8-second statement limit (2026-10-11)
    caps = {code(i): (i + 1) * 10**9 for i in range(2 * store.RANK_STOCKS + 1)}
    db = FakeSupabase(**{DAILY: [raw(i) for i in range(len(caps))] + [raw(9999)]})   # 9999: no cap
    assert store.rank_day(db, D, caps, window=250, min_days=1) == len(caps)
    calls = db.rpcs(RANK_FUNCTION)
    assert [len(call.params["p_caps"]) for call in calls] == [store.RANK_STOCKS, store.RANK_STOCKS, 1]
    assert [c for call in calls for c in call.params["p_caps"]] == list(caps)   # each stock once, in order
    assert all(call.params["p_caps"][c] == caps[c] for call in calls for c in call.params["p_caps"])
    assert all((call.params["p_day"], call.params["p_window"], call.params["p_min_days"]) == (D, 250, 1)
               for call in calls)
    assert all(db.row(DAILY, c, D)["market_cap"] == cap for c, cap in caps.items())
    assert db.row(DAILY, code(9999), D)["ranked_at"] is None


def test_a_failed_ranking_call_keeps_the_stocks_ranked_before_it():
    caps = {code(i): 10**12 for i in range(store.RANK_STOCKS + 1)}
    db = FakeSupabase(**{DAILY: [raw(i) for i in range(len(caps))]})
    real_rank, calls = db.rank, []

    def rank(params):   # the first call ranks, the second runs out of time
        calls.append(params)
        if len(calls) > 1:
            raise FakeDBError("canceling statement due to statement timeout")
        return real_rank(params)

    db.rank = rank
    with pytest.raises(FakeDBError):
        store.rank_day(db, D, caps, window=250, min_days=1)
    ranked = [c for c in caps if db.row(DAILY, c, D)["ranked_at"] is not None]
    assert ranked == list(caps)[:store.RANK_STOCKS]
    assert db.row(DAILY, code(store.RANK_STOCKS), D)["ranked_at"] is None


def test_the_coverage_is_read_through_the_db_function_1000_a_page():
    db = FakeSupabase(**{DAILY: [raw(i, day) for i in range(2100) for day in ("2026-10-08", D)]
                         + [raw(5, "2025-01-02")]})
    found = store.read_coverage(db, "2025-06-01")
    assert list(found) == [code(i) for i in range(2100)]
    assert found[code(5)] == {"stock_code": code(5), "days": 2, "first_day": "2026-10-08", "last_day": D}
    calls = db.rpcs(COVERAGE_FUNCTION)
    assert [(q.offset, q.limit_n) for q in calls] == [(0, 1000), (1000, 1000), (2000, 1000)]
    assert all(q.params == {"p_since": "2025-06-01"} and q.orders == [("stock_code", False)] for q in calls)
    assert len({id(q) for q in calls}) == 3


def test_no_stored_rows_give_no_coverage():
    assert store.read_coverage(FakeSupabase(), D) == {}


# ── data starts ──────────────────────────────────────────────────────────────

def test_the_known_data_starts_are_read_1000_a_page():
    rows = [{"stock_code": code(i), "listed_from": "2026-08-03"} for i in range(1100)]
    rows += [{"stock_code": code(5000), "listed_from": None}]
    db = FakeSupabase(**{STOCKS: rows})
    found = store.read_listed_from(db)
    assert len(found) == 1100 and found[code(3)] == "2026-08-03" and code(5000) not in found
    reads = db.queries(STOCKS, "select")
    assert [(q.offset, q.limit_n) for q in reads] == [(0, 1000), (1000, 1000)]
    assert len({id(q) for q in reads}) == 2


def test_a_data_start_is_upserted_on_the_stock_code():
    db = FakeSupabase(**{STOCKS: [{"stock_code": code(1), "listed_from": "2026-01-02",
                                   "updated_at": "2026-10-01T10:00:00+00:00"}]})
    store.upsert_listed_from(db, code(1), "2026-08-03", NOW)
    store.upsert_listed_from(db, code(2), "2026-09-01", NOW)
    assert db.rows(STOCKS) == [{"stock_code": code(1), "listed_from": "2026-08-03", "updated_at": NOW},
                               {"stock_code": code(2), "listed_from": "2026-09-01", "updated_at": NOW}]
    assert {q.on_conflict for q in db.queries(STOCKS, "upsert")} == {"stock_code"}


# ── the ranked days ──────────────────────────────────────────────────────────

def day_entry(i: int) -> dict:
    return {"trade_date": (date(2023, 1, 1) + timedelta(days=i)).isoformat(), "ranked_at": NOW,
            "stocks_ranked": 100 + i, "run_id": i}


def test_the_ranked_days_are_read_newest_first_1000_a_page():
    db = FakeSupabase(**{DAYS: [day_entry(i) for i in range(1200)]})
    rows = store.read_ranked_days(db)
    assert [row["trade_date"] for row in rows] == [day_entry(i)["trade_date"] for i in reversed(range(1200))]
    assert rows[0] == day_entry(1199)
    reads = db.queries(DAYS, "select")
    assert [(q.offset, q.limit_n) for q in reads] == [(0, 1000), (1000, 1000)]
    assert all(q.orders == [("trade_date", True)] for q in reads)


def test_one_ranked_day_is_found_or_none():
    db = FakeSupabase(**{DAYS: [day_entry(1)]})
    assert store.ranked_day(db, day_entry(1)["trade_date"]) == day_entry(1)
    assert store.ranked_day(db, D) is None


def test_a_new_ranked_day_is_inserted_whole_and_an_old_one_gets_its_count_only():
    db = FakeSupabase(**{DAYS: [day_entry(1)]})
    store.insert_ranked_day(db, day=D, ranked_at=NOW, stocks_ranked=2400, run_id=9)
    assert db.row(DAYS, D) == {"trade_date": D, "ranked_at": NOW, "stocks_ranked": 2400, "run_id": 9}
    store.update_ranked_day(db, day_entry(1)["trade_date"], stocks_ranked=2500)
    assert db.row(DAYS, day_entry(1)["trade_date"]) == day_entry(1) | {"stocks_ranked": 2500}
    [update] = db.queries(DAYS, "update")
    assert update.payload == {"stocks_ranked": 2500}


def test_ranked_days_are_deleted_by_day():
    db = FakeSupabase(**{DAYS: [day_entry(i) for i in range(5)]})
    store.delete_ranked_days(db, [day_entry(0)["trade_date"], day_entry(1)["trade_date"]])
    assert [row["trade_date"] for row in db.rows(DAYS)] == [day_entry(i)["trade_date"] for i in range(2, 5)]
    store.delete_ranked_days(db, [])
    assert len(db.queries(DAYS, "delete")) == 1   # nothing to delete: no request


# ── run records ──────────────────────────────────────────────────────────────

def test_a_run_starts_as_running_and_gets_its_id():
    db = FakeSupabase(**{RUNS: [{"run_id": 4, "status": "ok"}]})
    run_id = store.start_run(db, started_at=NOW, total=2558)
    assert run_id == 5
    assert db.runs()[-1] == {
        "run_id": 5, "started_at": NOW, "finished_at": None, "status": "running", "as_of": None,
        "stocks_total": 2558, "stocks_ok": None, "stocks_failed": None, "days_ranked": None, "message": None}


def test_finishing_a_run_writes_its_outcome_only_to_that_run():
    db = FakeSupabase(**{RUNS: [{"run_id": 1, "status": "running"}, {"run_id": 2, "status": "running"}]})
    store.finish_run(db, 2, status="partial", finished_at="2026-10-09T10:45:00+00:00", as_of=D,
                     stocks_ok=9, stocks_failed=1, days_ranked=1, message="못 받은 종목 1개")
    first, second = db.runs()
    assert first["status"] == "running" and first["finished_at"] is None
    assert {name: second[name] for name in ("status", "finished_at", "as_of", "stocks_ok", "stocks_failed",
                                            "days_ranked", "message")} == {
        "status": "partial", "finished_at": "2026-10-09T10:45:00+00:00", "as_of": D, "stocks_ok": 9,
        "stocks_failed": 1, "days_ranked": 1, "message": "못 받은 종목 1개"}


RUN_ROWS = [
    {"run_id": 1, "started_at": "2026-10-06T09:30:00+00:00", "status": "ok", "as_of": "2026-10-06"},
    {"run_id": 2, "started_at": "2026-10-07T09:30:00+00:00", "status": "partial", "as_of": "2026-10-07"},
    {"run_id": 3, "started_at": "2026-10-08T09:30:00+00:00", "status": "failed", "as_of": "2026-10-08"},
]


def test_the_latest_run_and_the_latest_successful_run():
    db = FakeSupabase(**{RUNS: list(reversed(RUN_ROWS))})
    assert store.latest_run(db)["run_id"] == 3
    assert store.latest_successful_run(db)["run_id"] == 2
    for q in db.queries(RUNS):
        assert q.orders == [("started_at", True), ("run_id", True)] and q.limit_n == 1


def test_a_tie_on_the_start_time_goes_to_the_later_run_id():
    db = FakeSupabase(**{RUNS: [{"run_id": 7, "started_at": "2026-10-08T09:30:00+00:00", "status": "ok"},
                                {"run_id": 8, "started_at": "2026-10-08T09:30:00+00:00", "status": "failed"}]})
    assert store.latest_run(db)["run_id"] == 8
    assert store.latest_successful_run(db)["run_id"] == 7


def test_no_runs_give_none():
    db = FakeSupabase(**{RUNS: [{"run_id": 1, "status": "failed"}, {"run_id": 2, "status": "running"}]})
    assert store.latest_run(FakeSupabase()) is None
    assert store.latest_successful_run(db) is None
