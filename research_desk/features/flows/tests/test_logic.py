"""features.flows logic: the investor mapping, trading days, the 250-day rank, the anchor day, the
calendar and the fill plan, the day's market caps, run status and the ranking table.

Pure functions, no DB, network, settings or files. The trading days here are weekdays (no
holidays): ``MARKET`` is 400 of them ending on Friday 2026-10-09 (``D``) and ``CALENDAR`` its
latest 310. A fake KIS answer (``block``) is the 30 market days ending at the anchor asked, with
closes of 0 before a listing day. Boundary values checked here:
- each investor's KIS fields, etc_orgt in both 기관합계 and 연기금, the identity gap;
- pre-listing padding (close 0 or blank) dropped and giving the data start, a halted day kept;
- the rank: ties share a rank and only larger days count, 59 / 60 rows, the window cut to the
  latest 250 rows up to the day, later rows left out;
- the anchor at 18:00, on the weekend and on Monday morning;
- the fill plan: complete coverage (one block), a new stock (11 blocks), a new listing (stops at
  its padding), a known data start, an interrupted first fill, a long outage (only the missed
  stretch), a hole, KIS having nothing older;
- the day's market cap only from a snapshot of that day;
- run status at the 20 % boundary, the same as the price update's;
- the ranking group, TS / CS / score, the average rank of ties, a group of one, the four sorts and
  their tie-breaks, limit / total / no, an empty group.
"""
from __future__ import annotations

import ast
import inspect
import sys
from datetime import date, datetime, time, timedelta, timezone
from itertools import chain
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from research_desk.features.flows import logic
from research_desk.features.prices import logic as prices_logic

KST = ZoneInfo("Asia/Seoul")
KEYS = ("pe_fund", "foreigner", "institution", "pension", "trust", "fin_invest", "insurance",
        "bank", "other_fin", "other_corp", "individual")
EOK = 100_000_000   # 억 원 in won


def weekdays(start, count):
    """``count`` weekdays from ``start`` on."""
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


MARKET = weekdays(date(2025, 3, 31), 400)   # the market's trading days, oldest first
D = MARKET[-1]                              # Friday 2026-10-09, the run's base trading day
CALENDAR = MARKET[-310:]                    # the calendar on D

# Every amount field of the KIS answer, each a distinct power of two, so a sum shows exactly which
# fields went into it. The last two belong to no investor group.
FIELDS = {
    "pe_fund_ntby_tr_pbmn": 1,
    "frgn_reg_ntby_pbmn": 2,
    "orgn_ntby_tr_pbmn": 4,
    "etc_orgt_ntby_tr_pbmn": 8,
    "fund_ntby_tr_pbmn": 16,
    "ivtr_ntby_tr_pbmn": 32,
    "scrt_ntby_tr_pbmn": 64,
    "insu_ntby_tr_pbmn": 128,
    "bank_ntby_tr_pbmn": 256,
    "mrbn_ntby_tr_pbmn": 512,
    "etc_corp_ntby_tr_pbmn": 1024,
    "prsn_ntby_tr_pbmn": 2048,
    "frgn_nreg_ntby_pbmn": 4096,    # 기타외국인
    "frgn_ntby_tr_pbmn": 8192,      # 외국인 (registered and other together)
}


def kis_row(day, close=61_000, **amounts):
    """One row of ``KisClient.investor_daily``: every amount field 0 unless given."""
    return {"date": day, "close": close, **{field: 0 for field in FIELDS}, **amounts}


def block(anchor, listed=None, history_from=None, missing=()):
    """A fake KIS answer: the 30 market days ending at ``anchor``, with closes of 0 before
    ``listed`` (KIS's padding before a listing), nothing before ``history_from``, and without the
    ``missing`` days (days KIS never returns)."""
    days = [day for day in MARKET if day <= anchor][-30:]
    return [kis_row(day, close=0 if listed is not None and day < listed else 61_000)
            for day in days if (history_from is None or day >= history_from) and day not in missing]


def fetched(blocks):
    """The trading days the blocks gave, oldest first."""
    return sorted({row["date"] for row in chain(*blocks) if row["close"]})


def coverage(days):
    """The coverage DB function's row for a stock with these stored days, counted from the
    retention start, dates as text (as the DB gives them); None when none is in the range."""
    kept = sorted(day for day in days if day >= CALENDAR[0])
    if not kept:
        return None
    return {"stock_code": "000001", "days": len(kept), "first_day": kept[0].isoformat(),
            "last_day": kept[-1].isoformat()}


def fill(stored=None, listed_from=None, **kis):
    """Fill one stock as the command does: the latest block at the anchor, then older blocks
    while the planner asks for them. → (the anchors asked, the blocks)."""
    anchors, blocks, anchor = [], [], CALENDAR[-1]
    while anchor is not None:
        assert len(anchors) < 40, "the planner never stopped"
        anchors.append(anchor)
        blocks.append(block(anchor, **kis))
        anchor = logic.next_fill_anchor(CALENDAR, stored, listed_from, blocks)
    return anchors, blocks


def deepen(size, **kis):
    """Fetch one stock until the planner has ``size`` trading days, newest block first.
    → (the anchors asked, the blocks)."""
    anchors, blocks, anchor = [], [], D
    while anchor is not None:
        assert len(anchors) < 40, "the planner never stopped"
        anchors.append(anchor)
        blocks.append(block(anchor, **kis))
        anchor = logic.next_depth_anchor(blocks, size)
    return anchors, blocks


def stored(day, **nets):
    """A stored raw row: every investor's amount 0 unless given."""
    return {"stock_code": "000001", "trade_date": day,
            **{f"net_{key}": nets.get(key, 0) for key in KEYS}}


def series(values, key="pe_fund"):
    """Stored rows with these amounts of one investor, one per market day, the last on D."""
    return [stored(day, **{key: value}) for day, value in zip(MARKET[-len(values):], values)]


def day_row(code, net, rank, *, cap=1_000 * EOK, n=250, investor="pe_fund", **columns):
    """A stored row of the ranking day: the investor's net buying (millions of won) and 250-day
    rank, the day's market cap (won) and window size; the other investors 0 and unranked."""
    row = {"stock_code": code, "trade_date": "2026-10-09", "market_cap": cap, "window_days": n,
           "ranked_at": "2026-10-09T10:00:00+00:00"}
    for key in KEYS:
        row[f"net_{key}"] = 0
        row[f"rank_{key}"] = None
    row[f"net_{investor}"] = net
    row[f"rank_{investor}"] = rank
    row.update(columns)
    return row


def codes(table):
    return [row["code"] for row in table["rows"]]


def test_the_test_market_ends_on_a_friday():
    assert D == date(2026, 10, 9) and D.weekday() == 4
    assert len(CALENDAR) == 310


# ── investors ────────────────────────────────────────────────────────────────

def test_the_eleven_investors_in_screen_order():
    assert logic.INVESTOR_KEYS == KEYS
    assert [investor.name for investor in logic.INVESTORS] == [
        "사모", "외국인", "기관합계", "연기금", "투신", "금융투자", "보험", "은행", "기타금융", "기타법인", "개인"]
    assert logic.INVESTOR_NAMES["pe_fund"] == "사모" and logic.INVESTOR_NAMES["individual"] == "개인"


def test_the_kis_fields_of_each_investor():
    assert {investor.key: investor.fields for investor in logic.INVESTORS} == {
        "pe_fund": ("pe_fund_ntby_tr_pbmn",),
        "foreigner": ("frgn_reg_ntby_pbmn",),
        "institution": ("orgn_ntby_tr_pbmn", "etc_orgt_ntby_tr_pbmn"),
        "pension": ("fund_ntby_tr_pbmn", "etc_orgt_ntby_tr_pbmn"),
        "trust": ("ivtr_ntby_tr_pbmn",),
        "fin_invest": ("scrt_ntby_tr_pbmn",),
        "insurance": ("insu_ntby_tr_pbmn",),
        "bank": ("bank_ntby_tr_pbmn",),
        "other_fin": ("mrbn_ntby_tr_pbmn",),
        "other_corp": ("etc_corp_ntby_tr_pbmn",),
        "individual": ("prsn_ntby_tr_pbmn",),
    }


def test_each_investor_sums_its_kis_fields():
    amounts = logic.investor_amounts(FIELDS)
    assert amounts == {
        "pe_fund": 1, "foreigner": 2,                 # registered foreigners only: no 4096, no 8192
        "institution": 4 + 8, "pension": 16 + 8,      # etc_orgt in both
        "trust": 32, "fin_invest": 64, "insurance": 128, "bank": 256, "other_fin": 512,
        "other_corp": 1024, "individual": 2048,
    }
    assert list(amounts) == list(KEYS)


def test_a_missing_or_blank_field_counts_as_zero():
    row = dict(FIELDS, pe_fund_ntby_tr_pbmn=None)
    del row["etc_orgt_ntby_tr_pbmn"]
    amounts = logic.investor_amounts(row)
    assert (amounts["pe_fund"], amounts["institution"], amounts["pension"]) == (0, 4, 16)
    assert logic.investor_amounts({"date": D, "close": 61_000}) == {key: 0 for key in KEYS}


def test_the_institution_gap_is_zero_when_the_identity_holds():
    # 금융투자 + 보험 + 투신 + 사모 + 은행 + 기타금융 + 연기금 = 기관합계
    parts = {"scrt_ntby_tr_pbmn": 10, "insu_ntby_tr_pbmn": -3, "ivtr_ntby_tr_pbmn": 5,
             "pe_fund_ntby_tr_pbmn": 7, "bank_ntby_tr_pbmn": -1, "mrbn_ntby_tr_pbmn": 2,
             "fund_ntby_tr_pbmn": 4}
    consistent = kis_row(D, **parts, orgn_ntby_tr_pbmn=sum(parts.values()), etc_orgt_ntby_tr_pbmn=6)
    assert logic.institution_gap(logic.investor_amounts(consistent)) == 0
    off = dict(consistent, orgn_ntby_tr_pbmn=sum(parts.values()) - 3)
    assert logic.institution_gap(logic.investor_amounts(off)) == 3
    assert logic.INSTITUTION_PARTS == ("fin_invest", "insurance", "trust", "pe_fund", "bank", "other_fin",
                                       "pension")


def test_etc_orgt_cancels_out_of_the_institution_gap():
    # (scrt + insu + ivtr + pe_fund + bank + mrbn + fund) − orgn, in millions of won
    assert logic.institution_gap(logic.investor_amounts(FIELDS)) == 64 + 128 + 32 + 1 + 256 + 512 + 16 - 4


def test_the_raw_row_holds_the_key_and_the_11_amounts_only():
    row = logic.daily_row("005930", kis_row(date(2026, 10, 8), **FIELDS))
    assert row == {
        "stock_code": "005930", "trade_date": "2026-10-08",
        "net_pe_fund": 1, "net_foreigner": 2, "net_institution": 12, "net_pension": 24,
        "net_trust": 32, "net_fin_invest": 64, "net_insurance": 128, "net_bank": 256,
        "net_other_fin": 512, "net_other_corp": 1024, "net_individual": 2048,
    }
    assert not [column for column in row if column.startswith("rank_")
                or column in ("market_cap", "window_days", "ranked_at", "fetched_at")]


def test_the_column_names():
    assert logic.net_column("pe_fund") == "net_pe_fund"
    assert logic.rank_column("other_corp") == "rank_other_corp"


# ── trading days ─────────────────────────────────────────────────────────────

def test_pre_listing_rows_are_dropped_and_give_the_data_start():
    rows = [kis_row(MARKET[-5], close=None), kis_row(MARKET[-4], close=0),
            kis_row(MARKET[-3]), kis_row(MARKET[-2]), kis_row(D)]
    result = logic.trading_rows(rows)
    assert [row["date"] for row in result.rows] == MARKET[-3:]
    assert result.padded is True and result.data_start == MARKET[-3]


def test_a_halted_day_is_a_trading_day():
    halted = kis_row(MARKET[-2])   # a close, every amount 0
    result = logic.trading_rows([kis_row(MARKET[-3], pe_fund_ntby_tr_pbmn=5), halted, kis_row(D)])
    assert [row["date"] for row in result.rows] == MARKET[-3:]
    assert result.padded is False and result.data_start is None
    assert logic.investor_amounts(halted) == {key: 0 for key in KEYS}


def test_a_zero_close_after_a_trading_day_is_dropped_but_is_no_listing():
    rows = [kis_row(MARKET[-3]), kis_row(MARKET[-2], close=0), kis_row(D, close=None)]
    result = logic.trading_rows(rows)
    assert [row["date"] for row in result.rows] == [MARKET[-3]]
    assert result.padded is False and result.data_start is None


def test_an_answer_of_padding_only_has_no_data_start():
    result = logic.trading_rows([kis_row(day, close=0) for day in MARKET[-30:]])
    assert result.rows == [] and result.padded is True and result.data_start is None


def test_rows_come_out_oldest_first_once_per_day():
    result = logic.trading_rows([kis_row(D, close=100), kis_row(MARKET[-2]), kis_row(D, close=101)])
    assert [(row["date"], row["close"]) for row in result.rows] == [(MARKET[-2], 61_000), (D, 101)]


def test_no_rows():
    assert logic.trading_rows([]) == ([], False, None)


# ── the 250-day rank ─────────────────────────────────────────────────────────

def test_the_reference_rank_takes_a_250_day_window_and_60_days_at_least():
    parameters = inspect.signature(logic.window_ranks).parameters
    assert list(parameters) == ["rows", "day", "window", "min_days"]
    assert parameters["window"].default == logic.WINDOW_DAYS == 250
    assert parameters["min_days"].default == logic.MIN_DAYS == 60


def test_only_days_with_more_net_buying_count_and_ties_share_a_rank():
    n, ranks = logic.window_ranks(series([50, 30, 30, 10] + [0] * 56 + [30]), D)
    assert n == 61
    assert ranks == {**{key: 1 for key in KEYS}, "pe_fund": 2}   # only the 50 is larger
    assert logic.window_ranks(series([50, 30, 50] + [0] * 57 + [50]), D)[1]["pe_fund"] == 1
    assert logic.window_ranks(series([50, 30, 30, 10] + [0] * 56 + [10]), D)[1]["pe_fund"] == 4


def test_days_of_net_selling_are_ranked_too():
    n, ranks = logic.window_ranks(series([5, 0, -1, -10] + [-20] * 56 + [-3]), D)
    assert (n, ranks["pe_fund"]) == (61, 4)


def test_fewer_than_60_days_give_no_rank():
    assert logic.window_ranks(series([1] * 59), D) == (59, None)
    assert logic.window_ranks(series([1] * 60), D) == (60, {key: 1 for key in KEYS})


def test_60_to_249_days_rank_with_a_short_history():
    n, ranks = logic.window_ranks(series(list(range(120))), D)
    assert (n, ranks["pe_fund"]) == (120, 1) and logic.short_history(n)
    n, ranks = logic.window_ranks(series(list(range(120, 0, -1))), D)
    assert (n, ranks["pe_fund"]) == (120, 120)


def test_the_window_is_the_latest_250_rows_up_to_the_day():
    # 300 rows: the 50 big days are older than the window and do not count
    assert logic.window_ranks(series([1000] * 50 + [10] * 249 + [5]), D) == (
        250, {**{key: 1 for key in KEYS}, "pe_fund": 250})
    assert logic.window_ranks(series([1000] + [10] * 249 + [5]), D)[1]["pe_fund"] == 250


def test_rows_after_the_day_are_left_out():
    rows = series([10] * 100 + [20] + [999] * 5)
    n, ranks = logic.window_ranks(rows, MARKET[-6])   # the day of the 20, five later days of 999
    assert (n, ranks["pe_fund"]) == (101, 1)


def test_every_investor_gets_its_own_rank():
    days = MARKET[-60:]
    rows = [stored(day, **{key: 100 if i < j else 0 for j, key in enumerate(KEYS)})
            for i, day in enumerate(days[:-1])]
    rows.append(stored(D, **{key: 50 for key in KEYS}))
    n, ranks = logic.window_ranks(rows, D)
    assert n == 60
    assert list(ranks) == list(KEYS)
    assert ranks == {key: j + 1 for j, key in enumerate(KEYS)}


def test_a_day_without_a_row_has_nothing_to_rank():
    assert logic.window_ranks(series([1] * 100)[:-1], D) == (0, None)
    assert logic.window_ranks(series([1] * 100), MARKET[0]) == (0, None)


def test_text_dates_and_any_row_order_give_the_same_ranks():
    rows = series([3, 1, 4, 1, 5] * 13)
    as_text = [dict(row, trade_date=row["trade_date"].isoformat()) for row in reversed(rows)]
    assert logic.window_ranks(as_text, D.isoformat()) == logic.window_ranks(rows, D) == (
        65, {key: 1 for key in KEYS})


def test_a_blank_amount_counts_as_zero():
    rows = series([10] * 60)
    rows[-1]["net_pe_fund"] = None
    assert logic.window_ranks(rows, D)[1]["pe_fund"] == 60


def test_the_window_and_the_minimum_can_be_given():
    assert logic.window_ranks(series([9, 1, 2, 3, 4]), D, window=3, min_days=3) == (
        3, {key: 1 for key in KEYS})
    assert logic.window_ranks(series([9, 5, 6, 1]), D, window=3, min_days=3)[1]["pe_fund"] == 3
    assert logic.window_ranks(series([9, 5, 6, 1]), D, window=3, min_days=4) == (3, None)
    with pytest.raises(ValueError):
        logic.window_ranks(series([1]), D, window=0)


def test_kis_rows_rank_through_the_raw_rows():
    answer = [kis_row(day, close=0) for day in MARKET[-90:-80]]
    answer += [kis_row(day, pe_fund_ntby_tr_pbmn=value) for day, value in zip(MARKET[-80:], range(80))]
    trading = logic.trading_rows(answer)
    assert trading.data_start == MARKET[-80]
    rows = [logic.daily_row("000001", row) for row in trading.rows]
    assert logic.window_ranks(rows, D) == (80, {key: 1 for key in KEYS})


# ── the anchor day ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("now,anchor", [
    (datetime(2026, 10, 13, 18, 0, tzinfo=KST), date(2026, 10, 13)),          # Tuesday 18:00: today
    (datetime(2026, 10, 13, 17, 59, 59, tzinfo=KST), date(2026, 10, 12)),     # before 18:00: Monday
    (datetime(2026, 10, 13, 0, 0, tzinfo=KST), date(2026, 10, 12)),
    (datetime(2026, 10, 12, 9, 0, tzinfo=KST), date(2026, 10, 9)),            # Monday morning: Friday
    (datetime(2026, 10, 12, 23, 0, tzinfo=KST), date(2026, 10, 12)),
    (datetime(2026, 10, 9, 18, 0, tzinfo=KST), date(2026, 10, 9)),            # Friday evening
    (datetime(2026, 10, 10, 20, 0, tzinfo=KST), date(2026, 10, 9)),           # Saturday: Friday
    (datetime(2026, 10, 11, 23, 59, tzinfo=KST), date(2026, 10, 9)),          # Sunday: Friday
    (datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc), date(2026, 10, 13)),  # 18:00 in Korea
    (datetime(2026, 10, 13, 8, 59, tzinfo=timezone.utc), date(2026, 10, 12)),
    (datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc), date(2026, 10, 9)),  # Monday 08:30 in Korea
])
def test_the_anchor_is_today_from_18_00_on_a_weekday_else_the_weekday_before(now, anchor):
    assert logic.anchor_day(now) == anchor


def test_the_anchor_cutoff_is_adjustable(monkeypatch):
    assert logic.ANCHOR_CUTOFF == time(18, 0)
    monkeypatch.setattr(logic, "ANCHOR_CUTOFF", time(19, 0))
    assert logic.anchor_day(datetime(2026, 10, 13, 18, 30, tzinfo=KST)) == date(2026, 10, 12)


# ── the calendar ─────────────────────────────────────────────────────────────

def test_the_calendar_values():
    assert logic.CALENDAR_DAYS == logic.WINDOW_DAYS + logic.REVIEW_DAYS == 310
    assert logic.BLOCK_DAYS == 30
    assert logic.CALENDAR_CODE == "005930"


def test_every_calendar_takes_11_blocks_of_the_calendar_stock():
    anchors, blocks = deepen(logic.CALENDAR_DAYS)
    assert len(anchors) == 11   # 11 × 30 = 330 days reach the 310th
    assert anchors[1] == MARKET[-30] - timedelta(days=1)   # the day before the earliest fetched
    assert logic.calendar_days(blocks) == MARKET[-310:]


def test_the_calendar_comes_from_kis_blocks_only():
    # Stored days are never taken: a gap inside them could not be seen.
    assert list(inspect.signature(logic.calendar_days).parameters) == ["blocks", "size"]
    assert list(inspect.signature(logic.next_depth_anchor).parameters) == ["blocks", "size"]
    assert logic.calendar_days([block(D)]) == MARKET[-30:]
    assert logic.next_depth_anchor([block(D)]) == MARKET[-30] - timedelta(days=1)


def test_a_short_answer_does_not_end_the_calendar():
    # KIS never returns MARKET[-45]: the second answer has 29 rows, and the planner goes on.
    anchors, blocks = deepen(logic.CALENDAR_DAYS, missing={MARKET[-45]})
    assert len(blocks[1]) == 29 and len(anchors) == 11
    assert logic.calendar_days(blocks) == [day for day in MARKET[-311:] if day != MARKET[-45]]


def test_the_calendar_stops_where_kis_has_nothing_older():
    anchors, blocks = deepen(logic.CALENDAR_DAYS, history_from=MARKET[-100])
    assert len(anchors) == 5 and blocks[-1] == []   # the fifth answer is empty
    assert logic.calendar_days(blocks) == MARKET[-100:]
    anchors, _ = deepen(logic.CALENDAR_DAYS, listed=MARKET[-45])
    assert len(anchors) == 2                        # pre-listing padding


def test_a_check_run_takes_9_blocks_for_250_days():
    anchors, blocks = deepen(logic.WINDOW_DAYS)
    assert len(anchors) == 9 and len(fetched(blocks)) == 270


def test_the_retention_range_is_the_calendars_latest_310_days():
    assert logic.retention_start(MARKET) == MARKET[-310]
    assert logic.retention_start(CALENDAR) == CALENDAR[0]
    assert logic.retention_start(MARKET[-100:]) == MARKET[-100]
    assert logic.retention_start([]) is None


# ── the fill plan ────────────────────────────────────────────────────────────

def test_complete_coverage_needs_only_the_latest_block():
    assert fill(coverage(MARKET[:-1]))[0] == [D]
    as_dates = {"stock_code": "000001", "days": 309, "first_day": CALENDAR[0], "last_day": CALENDAR[-2]}
    assert fill(as_dates)[0] == [D]


def test_a_new_stock_goes_back_to_the_retention_start():
    anchors, blocks = fill(None)
    assert len(anchors) == 11   # 11 × 30 = 330 days reach the 310th
    assert anchors[1:] == [MARKET[-30 * i] - timedelta(days=1) for i in range(1, 11)]
    assert fetched(blocks) == MARKET[-330:]
    assert logic.trading_rows(chain(*blocks)).data_start is None
    empty = {"stock_code": "000001", "days": 0, "first_day": None, "last_day": None}
    assert len(fill(empty)[0]) == 11


def test_a_new_listing_stops_at_its_padding():
    anchors, blocks = fill(None, listed=MARKET[-10])        # listed within the latest block
    assert anchors == [D]
    assert logic.trading_rows(chain(*blocks)).data_start == MARKET[-10]
    anchors, blocks = fill(None, listed=MARKET[-30])        # the older block is all padding
    assert len(anchors) == 2
    assert logic.trading_rows(chain(*blocks)).data_start == MARKET[-30]
    anchors, blocks = fill(None, listed=MARKET[-45])
    assert len(anchors) == 2
    trading = logic.trading_rows(chain(*blocks))
    assert trading.data_start == MARKET[-45] and len(trading.rows) == 45


def test_a_known_data_start_is_never_asked_past():
    listed = MARKET[-100]
    assert fill(coverage(MARKET[-100:-1]), listed_from=listed)[0] == [D]
    assert len(fill(coverage(MARKET[-100:-1]))[0]) == 11   # without it: back to the retention start
    # A hole: back to the data start only, though this KIS answer shows no padding
    holed = coverage([day for day in MARKET[-100:-1] if day != MARKET[-50]])
    anchors, blocks = fill(holed, listed_from=listed.isoformat())
    assert len(anchors) == 4   # 120 days reach the data start
    assert min(anchors) > listed and fetched(blocks)[0] <= listed


def test_after_an_interrupted_first_fill_only_the_unfinished_stocks_go_back():
    finished = coverage(MARKET[-331:-1])   # filled yesterday
    assert fill(finished)[0] == [D]
    assert len(fill(None)[0]) == 11                          # not reached before the stop
    assert len(fill(coverage(MARKET[-200:-1]))[0]) == 11     # starts after the retention start


def test_after_a_long_outage_only_the_missed_stretch_is_fetched():
    anchors, blocks = fill(coverage(MARKET[:-44]))   # stored up to MARKET[-45]
    assert len(anchors) == 2
    assert set(CALENDAR) <= set(MARKET[:-44]) | set(fetched(blocks))
    assert len(fill(coverage(MARKET[:-320]))[0]) == 11   # longer than the retention range


def test_stored_rows_ending_the_trading_day_before_the_latest_block_are_enough():
    assert fill(coverage(MARKET[:-30]))[0] == [D]          # the latest block starts on MARKET[-30]
    assert len(fill(coverage(MARKET[:-31]))[0]) == 2       # one trading day missing between


def test_a_hole_sends_the_stock_back_to_the_retention_start():
    holed = coverage([day for day in MARKET[:-1] if day != MARKET[-200]])
    assert len(fill(holed)[0]) == 11


def test_kis_having_nothing_older_ends_the_fill():
    anchors, blocks = fill(None, history_from=MARKET[-100])
    assert len(anchors) == 5 and blocks[-1] == []
    assert logic.trading_rows(chain(*blocks)).data_start is None   # no padding: no data start


def test_an_answer_without_an_older_day_ends_the_fill():
    first = [block(D)]
    assert logic.next_fill_anchor(CALENDAR, None, None, first) == MARKET[-30] - timedelta(days=1)
    assert logic.next_fill_anchor(CALENDAR, None, None, first + [block(D)]) is None
    assert logic.next_depth_anchor(first + [block(D)]) is None


def test_no_trading_day_and_an_empty_calendar():
    assert logic.next_fill_anchor(CALENDAR, None, None, [[]]) is None
    assert logic.next_depth_anchor([[]]) is None
    assert logic.calendar_days([[]]) == []
    assert logic.next_fill_anchor(CALENDAR, None, None, [block(D, listed=D + timedelta(days=3))]) is None
    with pytest.raises(ValueError):
        logic.next_fill_anchor([], None, None, [block(D)])


# ── retention ────────────────────────────────────────────────────────────────

def test_raw_rows_older_than_470_days_are_cut():
    # counted from the run's anchor day (기준일)
    assert list(inspect.signature(logic.raw_cutoff).parameters) == ["anchor"]
    assert logic.RAW_RETENTION_DAYS == 470
    assert logic.raw_cutoff(date(2026, 10, 9)) == date(2025, 6, 26)
    assert logic.raw_cutoff("2026-10-09") == date(2025, 6, 26)
    assert logic.raw_cutoff(D) < logic.retention_start(MARKET)   # the raw rows outlast the range


def test_only_the_latest_60_ranked_days_stay():
    assert logic.KEEP_RANKED_DAYS == 60
    days = MARKET[-62:]
    assert logic.days_to_drop(days) == MARKET[-62:-60]
    assert logic.days_to_drop(list(reversed(days)) + [day.isoformat() for day in days]) == MARKET[-62:-60]
    assert logic.days_to_drop(MARKET[-60:]) == []
    assert logic.days_to_drop([]) == []


# ── the ranking day and its market caps ──────────────────────────────────────

def test_the_ranking_day_is_the_latest_day_received():
    assert logic.ranking_day([MARKET[-3], D, MARKET[-2]]) == D
    assert logic.ranking_day(["2026-10-08", "2026-10-09"]) == date(2026, 10, 9)
    assert logic.ranking_day([]) is None


def test_only_a_snapshot_of_the_day_gives_a_market_cap():
    snapshots = {
        "000001": {"as_of": "2026-10-09", "market_cap": 500 * EOK},
        "000002": {"as_of": "2026-10-08", "market_cap": 400 * EOK},   # older (no_data): no cap
        "000003": {"as_of": "2026-10-09", "market_cap": None},
        "000004": {"as_of": None, "market_cap": 300 * EOK},
        "000005": {"as_of": "2026-10-09", "market_cap": 0},
        "000006": None,
    }
    assert logic.day_market_caps(snapshots, date(2026, 10, 9)) == {"000001": 500 * EOK}
    assert logic.day_market_caps(snapshots, "2026-10-08") == {"000002": 400 * EOK}
    assert logic.day_market_caps({}, D) == {}


# ── run status ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("total,failed,status", [
    (10, 0, "ok"), (10, 1, "partial"), (10, 2, "partial"), (10, 3, "failed"),
    (5, 1, "partial"), (5, 2, "failed"), (2558, 511, "partial"), (2558, 512, "failed"),
    (1, 1, "failed"), (0, 0, "ok"),
])
def test_more_than_20_percent_failed_is_a_failed_run(total, failed, status):
    assert logic.run_status(total, failed) == status


def test_the_run_status_rule_is_the_price_update_rule():
    assert logic.MAX_FAILED_PERCENT == prices_logic.MAX_FAILED_PERCENT == 20
    assert (logic.RUNNING, logic.OK, logic.PARTIAL, logic.FAILED) == (
        prices_logic.RUNNING, prices_logic.OK, prices_logic.PARTIAL, prices_logic.FAILED)
    assert logic.SUCCESS_STATUSES == prices_logic.SUCCESS_STATUSES
    for total in range(31):
        for failed in range(total + 1):
            assert logic.run_status(total, failed) == prices_logic.run_status(total, failed)


# ── the ranking table ────────────────────────────────────────────────────────

def test_the_ranking_defaults():
    assert logic.SORTS == ("score", "rank", "pct", "amount")
    assert (logic.DEFAULT_INVESTOR, logic.DEFAULT_MIN_CAP, logic.DEFAULT_SORT, logic.DEFAULT_LIMIT) == (
        "pe_fund", 1000, "score", 50)
    assert logic.REF_INVESTORS == ("pe_fund", "foreigner", "other_corp")
    assert (logic.WON_PER_MILLION, logic.WON_PER_EOK) == (1_000_000, 100_000_000)


def test_the_group_is_net_buying_with_a_rank_and_a_market_cap_at_or_over_the_floor():
    rows = [
        day_row("000001", 100, 5, cap=1_000 * EOK),        # exactly the floor: in
        day_row("000002", 0, 5),                            # no net buying: out
        day_row("000003", -50, 5),                          # net selling: out
        day_row("000004", 100, 5, cap=None),                # no market cap that day: out
        day_row("000005", 100, 5, cap=1_000 * EOK - 1),     # under the floor: out
        day_row("000006", 100, None, n=59),                 # under 60 days, no rank: out
        day_row("000007", 10, 3, cap=5_000 * EOK),          # in
    ]
    table = logic.ranking_table(rows)
    assert table["total"] == 2 and sorted(codes(table)) == ["000001", "000007"]
    assert sorted(codes(logic.ranking_table(rows, min_cap=0))) == ["000001", "000005", "000007"]
    assert logic.ranking_table([day_row("000008", 10, 1, cap=0)], min_cap=0)["total"] == 0


def test_the_group_and_the_rank_follow_the_chosen_investor():
    rows = [day_row("000001", 100, 5, investor="foreigner"), day_row("000002", 100, 7)]
    table = logic.ranking_table(rows, investor="foreigner")
    assert codes(table) == ["000001"] and table["rows"][0]["rank"] == 5
    assert table["rows"][0]["ref_ranks"] == {"pe_fund": None, "foreigner": 5, "other_corp": None}
    assert codes(logic.ranking_table(rows)) == ["000002"]


def test_ts_score_runs_from_100_for_the_first_to_0_for_the_last():
    assert logic.ts_score(250, 1) == 100.0
    assert logic.ts_score(250, 250) == 0.0
    assert logic.ts_score(101, 51) == 50.0
    assert logic.ts_score(100, 34) == pytest.approx(100 * 66 / 99)


def test_cs_scores_use_the_average_rank_of_ties():
    assert logic.cs_scores([0.1, 0.2, 0.2, 0.4]) == [0.0, 50.0, 50.0, 100.0]   # ranks 1, 2.5, 2.5, 4
    assert logic.cs_scores([0.4, 0.1, 0.2]) == [100.0, 0.0, 50.0]              # the input order kept
    assert logic.cs_scores([0.2, 0.2]) == [50.0, 50.0]
    assert logic.cs_scores([0.3]) == [100.0]
    assert logic.cs_scores([]) == []


def test_the_score_is_the_mean_of_the_ts_and_cs_scores():
    rows = [day_row("000001", 100, 1),               # pct 0.1, the smallest: CS 0; TS 100
            day_row("000002", 200, 125),             # pct 0.2, tied: CS 50
            day_row("000003", 200, 250),             # pct 0.2, tied: CS 50; TS 0
            day_row("000004", 400, 34, n=100)]       # pct 0.4, the largest: CS 100
    table = logic.ranking_table(rows)
    score = {row["code"]: row["score"] for row in table["rows"]}
    assert score == {
        "000001": pytest.approx(50.0),
        "000002": pytest.approx((100 * 125 / 249 + 50) / 2),
        "000003": pytest.approx(25.0),
        "000004": pytest.approx((100 * 66 / 99 + 100) / 2),
    }
    assert codes(table) == ["000004", "000002", "000001", "000003"]


def test_a_group_of_one_has_a_cs_score_of_100():
    [row] = logic.ranking_table([day_row("000001", 10, 125)])["rows"]
    assert row["score"] == pytest.approx((100 * 125 / 249 + 100) / 2)


def test_score_sort_breaks_ties_by_pct_then_amount_then_code():
    # the same score 50 three ways: TS 100 + CS 0, TS 50 + CS 50, TS 0 + CS 100
    rows = [day_row("000001", 100, 1), day_row("000002", 200, 51, n=101), day_row("000003", 300, 250)]
    table = logic.ranking_table(rows)
    assert [row["score"] for row in table["rows"]] == [50.0, 50.0, 50.0]
    assert codes(table) == ["000003", "000002", "000001"]
    # the same score and pct: the larger amount first, then the code
    rows = [day_row("000004", 100, 5), day_row("000005", 200, 5, cap=2_000 * EOK),
            day_row("000007", 100, 5), day_row("000006", 100, 5)]
    assert codes(logic.ranking_table(rows)) == ["000005", "000004", "000006", "000007"]


def test_truly_equal_scores_from_different_ts_and_cs_pairs_go_by_pct():
    # 250 stocks with 250 days; pct position p (1 = the smallest pct) and rank p − 10 give every stock
    # from p = 11 on the score 100 × (259 / 249) / 2, from 240 different TS / CS pairs. In floating
    # point those sums differ by a rounding step, so the order would follow the noise.
    rows = [day_row(f"{p:06d}", p, p - 10 if p > 10 else p + 240) for p in range(1, 251)]
    table = logic.ranking_table(rows, limit=250)
    assert codes(table) == [f"{p:06d}" for p in range(250, 10, -1)] + [f"{p:06d}" for p in range(10, 0, -1)]
    assert codes(table).index("000030") < codes(table).index("000020")
    scores = [row["score"] for row in table["rows"]]
    assert set(scores[:240]) == {12950 / 249} and set(scores[240:]) == {450 / 249}


def test_equal_ts_scores_from_different_window_sizes_go_by_pct():
    rows = [day_row("000001", 100, 28),             # TS 100 × 222 / 249, pct 0.1
            day_row("000002", 200, 10, n=84)]       # TS 100 × 74 / 83, the same; pct 0.2
    assert codes(logic.ranking_table(rows, sort="rank")) == ["000002", "000001"]


def test_percents_are_compared_exactly():
    # Two percents one rounding step apart (the same float): the larger goes first and gets the
    # higher CS rank, though the other has the better TS.
    rows = [day_row("000001", 1, 1, cap=10**17 + 1),   # the smaller pct, TS 100
            day_row("000002", 1, 2, cap=10**17)]       # the larger pct, TS 100 × 248 / 249
    assert codes(logic.ranking_table(rows, sort="pct")) == ["000002", "000001"]
    table = logic.ranking_table(rows)
    assert codes(table) == ["000002", "000001"]
    assert [row["score"] for row in table["rows"]] == pytest.approx([(100 * 248 / 249 + 100) / 2, 50.0])
    assert table["rows"][0]["pct"] == table["rows"][1]["pct"] == 1e-9


def test_rank_sort_is_ts_then_pct_then_amount_then_code():
    rows = [
        day_row("000001", 900, 2),                      # second-best TS, the largest pct
        day_row("000002", 100, 1),                      # TS 100 (1st of 250), pct 0.1
        day_row("000003", 200, 1, n=100),               # TS 100 (1st of 100), pct 0.2
        day_row("000004", 100, 7),                      # pct 0.1, 1억
        day_row("000005", 100, 7),                      # the same as 000004 but the code
        day_row("000006", 200, 7, cap=2_000 * EOK),     # pct 0.1, 2억
    ]
    assert codes(logic.ranking_table(rows, sort="rank")) == [
        "000003", "000002", "000001", "000006", "000004", "000005"]


def test_pct_sort_is_pct_then_ts_then_amount_then_code():
    rows = [
        day_row("000001", 100, 1),                      # pct 0.1, TS 100, 1억
        day_row("000002", 300, 250),                    # pct 0.3, TS 0
        day_row("000003", 200, 9, cap=2_000 * EOK),     # pct 0.1, 2억
        day_row("000004", 100, 9),                      # pct 0.1, 1억
        day_row("000005", 200, 9, cap=2_000 * EOK),     # the same as 000003 but the code
    ]
    assert codes(logic.ranking_table(rows, sort="pct")) == ["000002", "000001", "000003", "000005", "000004"]


def test_amount_sort_is_amount_then_pct_then_code_without_the_ts_score():
    rows = [
        day_row("000001", 500, 200, cap=5_000 * EOK),   # 5억, pct 0.1
        day_row("000002", 300, 250),                    # 3억, pct 0.3, the worst TS
        day_row("000003", 500, 100, cap=2_500 * EOK),   # 5억, pct 0.2
        day_row("000004", 300, 1),                      # 3억, pct 0.3, the best TS: still by code
    ]
    assert codes(logic.ranking_table(rows, sort="amount")) == ["000003", "000001", "000002", "000004"]


def test_limit_cuts_the_rows_but_not_the_total():
    rows = [day_row(f"00000{i}", 100 * i, i) for i in range(1, 6)]
    table = logic.ranking_table(rows, sort="amount", limit=2)
    assert table["total"] == 5
    assert codes(table) == ["000005", "000004"] and [row["no"] for row in table["rows"]] == [1, 2]
    assert [row["no"] for row in logic.ranking_table(rows, sort="amount")["rows"]] == [1, 2, 3, 4, 5]
    assert logic.ranking_table(rows, limit=0) == {"total": 5, "rows": []}


def test_an_empty_group_gives_no_rows():
    assert logic.ranking_table([]) == {"total": 0, "rows": []}
    assert logic.ranking_table([day_row("000001", -1, 1)]) == {"total": 0, "rows": []}


def test_a_table_row_in_won_and_percent():
    row = day_row("178320", 14_150, 2, cap=12_345 * EOK, rank_foreigner=60)   # 141.5억 into 1.2345조
    assert logic.ranking_table([row])["rows"] == [{
        "no": 1, "code": "178320", "market_cap": 1_234_500_000_000, "amount": 14_150_000_000,
        "pct": pytest.approx(14_150_000_000 * 100 / 1_234_500_000_000),
        "rank": 2, "window_days": 250, "short_history": False,
        "score": pytest.approx((100 * 248 / 249 + 100) / 2),
        "ref_ranks": {"pe_fund": 2, "foreigner": 60, "other_corp": None},
    }]


def test_a_short_history_row_and_a_text_market_cap():
    [short] = logic.ranking_table([day_row("000001", 10, 3, n=120)])["rows"]
    assert (short["window_days"], short["short_history"]) == (120, True)
    [text] = logic.ranking_table([day_row("000002", 10, 3, cap="100000000000")])["rows"]
    assert text["market_cap"] == 100_000_000_000


@pytest.mark.parametrize("arguments", [{"investor": "pension_fund"}, {"sort": "name"}, {"limit": -1}])
def test_an_unknown_investor_or_sort_or_a_negative_limit_is_an_error(arguments):
    with pytest.raises(ValueError):
        logic.ranking_table([day_row("000001", 10, 1)], **arguments)


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("listed,market", [
    ("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ"), ("KOSDAQ GLOBAL", "KOSDAQ"), (" KOSDAQ GLOBAL ", "KOSDAQ"),
    ("KONEX", None), ("", None), (None, None),
])
def test_kosdaq_global_counts_as_kosdaq(listed, market):
    assert logic.market_of(listed) == market == prices_logic.market_of(listed)


def test_millions_of_won_become_won():
    assert logic.to_won(14_150) == 14_150_000_000
    assert logic.to_won(-3) == -3_000_000
    assert logic.to_won(None) is None


def test_short_history_is_a_window_under_250_days():
    assert logic.short_history(249) is True and logic.short_history(250) is False


def test_the_logic_module_imports_only_the_standard_library():
    tree = ast.parse(Path(logic.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import: the calculations stand alone"
            names.add(node.module.split(".")[0])
    io = {"os", "io", "pathlib", "shutil", "socket", "subprocess", "sqlite3", "tempfile", "urllib", "http"}
    assert names <= set(sys.stdlib_module_names) - io, names
