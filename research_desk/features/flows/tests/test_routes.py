"""The two flows web addresses (수급) through a small app with this feature's router and the web app's
NotReady → 503 answer, over the world of ``web_world``.

``GET /api/flows`` (the ranking):
- the answer's keys and units (whole won, percent), names and markets from the stock list, report
  counts of the returned codes over 365 days, the ranked days newest first (at most 60);
- the group, its four sorts, ``limit`` and ``total``, the floor in 억 원 (inclusive), another
  investor, another ranked day, an empty group and a one-stock group;
- the check order: format 422 (before anything is prepared) → readiness 503 → no ranked day 503
  (not remembered) → 404 for a day not in the ranked days.

``GET /api/stocks/{code}/flows`` (the company tab):
- the answer's keys and units, the 11 investors of the stock's latest ranked day, the series of
  the period's trading days (oldest first) with the adjusted closes (null where there is none);
- a stock never ranked (as_of null), one listed under 60 trading days (no ranks), one with fewer
  rows than the period, newer unranked rows;
- the check order: format 422 → readiness 503 → 404 not in the stock list → 404 no rows.

Harness rules: a readiness failure is a 503 with a fixed sentence on these addresses only and
recovers on the next request, values added to ``.env`` count, creating the service reads nothing,
one service for the process, the report-count and price windows' NotReady passes through
unchanged, no key, URL or path in any answer, plain (thread-pool) handlers, the /openapi.json
entries.
"""
from __future__ import annotations

import inspect
import json
import os
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import research_desk.features.flows.router as router_module
from research_desk.core import db as core_db
from research_desk.core.settings import NotReady
from research_desk.features import coverage, prices
from research_desk.features.flows import logic
from research_desk.features.flows import service as service_module
from research_desk.features.flows.service import FlowsService, get_service

from .fakes import DAILY, DAYS, KEY, KEYS, MARKET, NET, URL
from .web_world import (
    CLOSE_GAPS,
    DAY,
    DAY_1,
    DAY_2,
    FOREIGNER_GROUP_BY_SCORE,
    NEVER_RANKED,
    NEW_LISTING,
    NEXT_DAY,
    NO_ROWS,
    NOT_LISTED,
    PE_GROUP_BY_AMOUNT,
    PE_GROUP_BY_PCT,
    PE_GROUP_BY_RANK,
    PE_GROUP_BY_SCORE,
    SEED,
    SEED_AMOUNTS,
    SEED_CAP,
    SEED_DAYS,
    SEED_RANKS,
    Closes,
    Counts,
    build_app,
    close_of,
    closes_table,
    counts_record,
    make_db,
    ranked_day,
    stored,
    write_stock_list,
)

DB_REASON = 'DB 접속 설정(SUPABASE_URL, SUPABASE_SERVICE_KEY)이 없습니다'
STOCKS_REASON = '종목표 파일을 읽을 수 없습니다'
NO_RANKED_DAY_REASON = '아직 순위를 매긴 날이 없습니다(python -m research_desk flows update)'
STOCK_NOT_FOUND = {'detail': '종목을 찾을 수 없습니다.'}
NO_FLOW_DATA = {'detail': '이 종목은 수급 자료가 없습니다.'}
DAY_NOT_RANKED = {'detail': '그날의 수급 순위가 없습니다.'}


def not_ready(reason, area='수급'):
    return {'detail': f'{area} 기능을 지금 쓸 수 없습니다: {reason}'}


RANKING_KEYS = {'date', 'dates', 'investor', 'min_cap', 'sort', 'total', 'rows'}
ROW_KEYS = {'no', 'code', 'name', 'market', 'market_cap', 'amount', 'pct', 'rank', 'window_days',
            'short_history', 'score', 'ref_ranks', 'coverage'}
COVERAGE_KEYS = {'stock_reports', 'sector_mentions', 'other_research', 'last_stock_report_date', 'brokers', 'label'}
TAB_KEYS = {'stock', 'as_of', 'market_cap', 'window_days', 'investors', 'period', 'series'}
NAMES = ('사모', '외국인', '기관합계', '연기금', '투신', '금융투자', '보험', '은행', '기타금융', '기타법인', '개인')
RANKED_DAYS = [DAY.isoformat(), DAY_1.isoformat(), DAY_2.isoformat()]
ENV = ('SUPABASE_URL', 'SUPABASE_SERVICE_KEY', 'SUPABASE_DB_URL', 'KRX_CSV_PATH')
WON = 1_000_000   # won per million won (the stored unit)


def refuse(*args, **kwargs):
    raise AssertionError('no real Supabase client, report counts or closes in these tests')


@pytest.fixture(autouse=True)
def isolation(monkeypatch, tmp_path):
    """No settings unless a test sets them, a temp current folder (the stock list's default path is
    relative), a fresh process-wide service, and no real Supabase client, report counts or closes."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(service_module, '_service', None)
    monkeypatch.setattr(core_db, 'supabase_client', refuse)
    monkeypatch.setattr(coverage, 'report_counts', refuse)
    monkeypatch.setattr(prices, 'daily_closes', refuse)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Everything ready: DB settings, the stock list, the flows tables, report counts and closes
    (the window functions replaced), and a client of a test app on a new service."""
    csv_path = write_stock_list(tmp_path / 'KRX_stocks_data.csv')
    db = make_db()
    made = []

    def supabase_client(url, key):
        made.append((url, key))
        return db

    monkeypatch.setenv('SUPABASE_URL', URL)
    monkeypatch.setenv('SUPABASE_SERVICE_KEY', KEY)
    monkeypatch.setenv('KRX_CSV_PATH', str(csv_path))
    monkeypatch.setattr(core_db, 'supabase_client', supabase_client)
    counts, closes = Counts(), Closes()
    monkeypatch.setattr(coverage, 'report_counts', counts)
    monkeypatch.setattr(prices, 'daily_closes', closes)
    service = FlowsService()
    return SimpleNamespace(db=db, made=made, csv=csv_path, counts=counts, closes=closes, service=service,
                           client=TestClient(build_app(service)), tmp=tmp_path)


def ranking(world, **params):
    return world.client.get('/api/flows', params=params)


def tab(world, code=SEED, **params):
    return world.client.get(f'/api/stocks/{code}/flows', params=params)


def by_code(body):
    return {row['code']: row for row in body['rows']}


def unprepared_client():
    """A client whose service has nothing to prepare with: no DB settings, no stock list."""
    return TestClient(build_app(FlowsService()))


# ── GET /api/flows: the answer ───────────────────────────────────────────────

def test_the_ranking_has_the_spec_keys_and_the_latest_ranked_day(world):
    response = ranking(world)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == RANKING_KEYS
    assert (body['date'], body['investor'], body['min_cap'], body['sort']) == (DAY.isoformat(), 'pe_fund', 1000,
                                                                              'score')
    assert body['dates'] == RANKED_DAYS
    assert body['total'] == 7
    assert [row['code'] for row in body['rows']] == PE_GROUP_BY_SCORE
    assert [row['no'] for row in body['rows']] == [1, 2, 3, 4, 5, 6, 7]
    for row in body['rows']:
        assert set(row) == ROW_KEYS
        assert set(row['ref_ranks']) == {'pe_fund', 'foreigner', 'other_corp'}
        assert set(row['coverage']) == COVERAGE_KEYS and row['coverage']['label'] in {'none', 'few', 'covered'}


def test_money_is_whole_won_and_pct_a_percent(world):
    seed = by_code(ranking(world).json())[SEED]
    assert seed['market_cap'] == 1_234_500_000_000 and isinstance(seed['market_cap'], int)
    assert seed['amount'] == 14_150_000_000 and isinstance(seed['amount'], int)   # 14,150 백만 원 = 141.5 억 원
    assert seed['pct'] == 14_150_000_000 * 100 / 1_234_500_000_000                 # 1.1462… %
    assert seed['pct'] == pytest.approx(1.146213, abs=1e-6)
    assert (seed['rank'], seed['window_days'], seed['short_history']) == (2, 250, False)
    # TS 100 × 248 ÷ 249, CS 100 (the largest pct of the group); not rounded
    assert seed['score'] == pytest.approx((100 * 248 / 249 + 100) / 2, abs=1e-9)
    assert seed['ref_ranks'] == {'pe_fund': 2, 'foreigner': 60, 'other_corp': 95}
    assert (seed['name'], seed['market']) == ('서진시스템', 'KOSDAQ')
    assert seed['coverage'] == counts_record(SEED)


def test_names_and_markets_come_from_the_stock_list(world):
    rows = by_code(ranking(world).json())
    assert (rows['005930']['name'], rows['005930']['market']) == ('삼성전자', 'KOSPI')
    assert (rows['950130']['name'], rows['950130']['market']) == ('해외상장사', 'KOSDAQ')    # KOSDAQ GLOBAL
    assert (rows['217270']['name'], rows['217270']['market']) == ('코넥스회사', None)        # another market
    assert (rows[NOT_LISTED]['name'], rows[NOT_LISTED]['market']) == ('', None)           # not in the list
    assert rows['005930']['coverage'] == counts_record('005930')


def test_short_histories_and_missing_reference_ranks_show(world):
    rows = by_code(ranking(world).json())
    assert (rows['247540']['window_days'], rows['247540']['short_history']) == (120, True)
    assert rows['247540']['score'] == pytest.approx((100 * 115 / 119 + 75) / 2)   # TS over its 120 days
    assert rows['950130']['ref_ranks'] == {'pe_fund': 10, 'foreigner': None, 'other_corp': 4}


def test_the_rows_are_the_ranking_rules_rows_with_names_markets_and_report_counts(world):
    day_rows = world.db.rows(DAILY, trade_date=DAY.isoformat())
    for investor in KEYS:
        for sort in ('score', 'rank', 'pct', 'amount'):
            body = ranking(world, investor=investor, sort=sort, min_cap=0).json()
            expected = logic.ranking_table(day_rows, investor, 0, sort, 50)
            assert (body['investor'], body['sort'], body['total']) == (investor, sort, expected['total'])
            assert [{key: value for key, value in row.items() if key not in ('name', 'market', 'coverage')}
                    for row in body['rows']] == expected['rows'], (investor, sort)


@pytest.mark.parametrize('sort,codes', [
    ('score', PE_GROUP_BY_SCORE),
    ('rank', PE_GROUP_BY_RANK),
    ('pct', PE_GROUP_BY_PCT),         # 247540 and 950130 have the same pct: the larger TS first
    ('amount', PE_GROUP_BY_AMOUNT),
])
def test_each_sort_orders_the_group(world, sort, codes):
    body = ranking(world, sort=sort).json()
    assert (body['sort'], body['total']) == (sort, 7)
    assert [row['code'] for row in body['rows']] == codes
    assert [row['no'] for row in body['rows']] == list(range(1, 8))


def test_limit_cuts_the_rows_but_not_the_total(world):
    body = ranking(world, limit=2).json()
    assert body['total'] == 7
    assert [(row['no'], row['code']) for row in body['rows']] == [(1, SEED), (2, '247540')]
    assert [row['code'] for row in ranking(world, limit=3, sort='amount').json()['rows']] == PE_GROUP_BY_AMOUNT[:3]
    assert len(ranking(world, limit=200).json()['rows']) == 7


def test_the_floor_is_in_eok_won_and_takes_its_own_value(world):
    everything = ranking(world, min_cap=0).json()
    assert (everything['min_cap'], everything['total']) == (0, 8)
    assert '111110' in by_code(everything)                          # 500 억 원 counts without a floor
    at_floor = ranking(world, min_cap=5000).json()                  # 5,000 억 원 = 500,000,000,000 won
    assert [row['code'] for row in at_floor['rows']] == [SEED, '247540', '000660', '005930']   # 247540: exactly


def test_an_empty_group_is_200_without_rows(world):
    response = ranking(world, min_cap=10_000_000)                   # 1,000조 원: nobody
    assert response.status_code == 200
    body = response.json()
    assert (body['total'], body['rows'], body['dates']) == (0, [], RANKED_DAYS)
    assert world.counts.calls == []                                 # no code to count reports for


def test_a_one_stock_group_gets_the_full_cross_section_score(world):
    body = ranking(world, min_cap=2_000_000).json()                 # 200조 원: 005930 only
    assert body['total'] == 1
    (row,) = body['rows']
    assert (row['no'], row['code']) == (1, '005930')
    assert row['score'] == pytest.approx((100 * 220 / 249 + 100) / 2)   # CS 100 in a group of one


def test_another_investor_makes_another_group(world):
    body = ranking(world, investor='foreigner').json()
    assert (body['investor'], body['total']) == ('foreigner', 3)
    assert [row['code'] for row in body['rows']] == FOREIGNER_GROUP_BY_SCORE
    samsung = by_code(body)['005930']
    assert (samsung['amount'], samsung['rank']) == (90_000_000_000, 1)
    assert samsung['pct'] == 90_000_000_000 * 100 / 400_000_000_000_000
    assert samsung['ref_ranks'] == {'pe_fund': 30, 'foreigner': 1, 'other_corp': 120}   # whatever investor


def test_a_date_picks_that_ranked_day(world):
    body = ranking(world, date=DAY_1.isoformat()).json()
    assert (body['date'], body['dates'], body['total']) == (DAY_1.isoformat(), RANKED_DAYS, 2)
    assert [row['code'] for row in body['rows']] == [SEED, '005930']
    samsung = by_code(body)['005930']
    assert (samsung['amount'], samsung['market_cap'], samsung['rank']) == (41_000_000_000, 399_000_000_000_000, 33)
    assert ranking(world, date=DAY_2.isoformat()).json()['total'] == 0   # 178320 sold that day


def test_report_counts_are_asked_once_for_the_returned_codes_over_365_days(world):
    ranking(world)
    assert world.counts.calls == [(PE_GROUP_BY_SCORE, {'days': 365})]
    ranking(world, limit=2, sort='amount')
    assert world.counts.calls[1:] == [(PE_GROUP_BY_AMOUNT[:2], {'days': 365})]


def test_the_dates_are_the_latest_60_ranked_days_newest_first(world):
    older = [day for day in MARKET if day < DAY_2][-62:]
    for day in older:
        world.db.add(DAYS, ranked_day(day))
    everyday = sorted([DAY, DAY_1, DAY_2, *older], reverse=True)
    body = ranking(world).json()
    assert body['dates'] == [day.isoformat() for day in everyday[:60]]
    assert body['date'] == DAY.isoformat()
    assert ranking(world, date=everyday[59].isoformat()).status_code == 200    # listed, no rows: empty
    response = ranking(world, date=everyday[60].isoformat())                   # beyond the 60
    assert (response.status_code, response.json()) == (404, DAY_NOT_RANKED)


@pytest.mark.parametrize('params', [{'min_cap': 0}, {'min_cap': 10_000_000}, {'limit': 1}, {'limit': 200}])
def test_the_ends_of_the_ranges_are_allowed(world, params):
    assert ranking(world, **params).status_code == 200


# ── GET /api/flows: the check order ──────────────────────────────────────────

@pytest.mark.parametrize('query', [
    'investor=pension_fund', 'investor=PE_FUND', 'investor=',
    'sort=name', 'sort=SCORE', 'sort=',
    'limit=0', 'limit=201', 'limit=x', 'limit=1.5', 'limit=',
    'min_cap=-1', 'min_cap=10000001', 'min_cap=x', 'min_cap=1.5', 'min_cap=',
    'date=2026-10-8', 'date=20261008', 'date=2026-02-30', 'date=2026-13-01', 'date=0', 'date=1700000000',
    'date=2026-10-08T00:00:00', 'date=x', 'date=',
])
def test_a_malformed_ranking_request_is_422_before_anything_is_prepared(query):
    response = unprepared_client().get(f'/api/flows?{query}')
    assert response.status_code == 422
    assert isinstance(response.json()['detail'], list)


def test_readiness_comes_before_the_ranked_day_checks():
    assert unprepared_client().get('/api/flows?date=1999-01-01').json() == not_ready(DB_REASON)


def test_without_a_ranked_day_the_ranking_is_503_and_that_is_not_remembered(world):
    world.db.tables[DAYS].clear()
    for params in ({}, {'date': DAY.isoformat()}, {'date': '1999-01-01'}):
        response = ranking(world, **params)
        assert (response.status_code, response.json()) == (503, not_ready(NO_RANKED_DAY_REASON))
    assert tab(world).status_code == 200                           # the tab needs no ranked day
    assert world.client.get('/api/health').json() == {'status': 'ok'}
    world.db.add(DAYS, ranked_day(DAY))                            # a run ranks a day: no restart
    response = ranking(world)
    assert response.status_code == 200 and response.json()['dates'] == [DAY.isoformat()]


@pytest.mark.parametrize('day', [NEXT_DAY.isoformat(), '2026-10-05', '2026-10-10', '1999-01-01'])
def test_a_day_not_in_the_ranked_days_is_404(world, day):
    # 2026-10-05: 178320 has a row, but the day was never ranked
    response = ranking(world, date=day)
    assert (response.status_code, response.json()) == (404, DAY_NOT_RANKED)


# ── GET /api/stocks/{code}/flows: the answer ─────────────────────────────────

def test_the_tab_has_the_spec_keys_and_the_latest_ranked_day(world):
    response = tab(world)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == TAB_KEYS
    assert body['stock'] == {'code': SEED, 'name': '서진시스템', 'market': 'KOSDAQ'}
    assert (body['as_of'], body['market_cap'], body['window_days'], body['period']) == (
        DAY.isoformat(), SEED_CAP, 250, '3m')
    assert [(entry['key'], entry['name']) for entry in body['investors']] == list(zip(KEYS, NAMES))
    assert all(set(entry) == {'key', 'name', 'amount', 'pct', 'rank'} for entry in body['investors'])
    assert set(body['series']) == {'dates', 'close', 'flows'}
    assert list(body['series']['flows']) == list(KEYS)


def test_the_investors_are_the_latest_ranked_days_amounts_pcts_and_ranks(world):
    investors = {entry['key']: entry for entry in tab(world).json()['investors']}
    for key in KEYS:
        amount = SEED_AMOUNTS[key] * WON
        assert investors[key]['amount'] == amount and isinstance(amount, int)
        assert investors[key]['pct'] == amount * 100 / SEED_CAP
        assert investors[key]['rank'] == SEED_RANKS[key]
    assert investors['pe_fund'] == {'key': 'pe_fund', 'name': '사모', 'amount': 14_150_000_000,
                                    'pct': pytest.approx(1.146213, abs=1e-6), 'rank': 2}
    assert investors['foreigner']['amount'] == -500_000_000 and investors['foreigner']['pct'] < 0
    assert (investors['trust']['amount'], investors['trust']['pct']) == (0, 0)


@pytest.mark.parametrize('period,days', [('1m', 21), ('3m', 63), ('6m', 126), ('1y', 250)])
def test_the_series_holds_the_periods_trading_days_oldest_first(world, period, days):
    body = tab(world, period=period).json()
    series = body['series']
    expected = [day.isoformat() for day in SEED_DAYS[-days:]]     # 260 stored: the latest ones
    assert body['period'] == period
    assert series['dates'] == expected
    rows = {row['trade_date']: row for row in world.db.rows(DAILY, stock_code=SEED)}
    for key, net in zip(KEYS, NET):
        assert series['flows'][key] == [rows[day][net] * WON for day in expected], key
    assert series['close'] == [closes_table()[SEED].get(day) for day in expected]
    assert world.closes.calls == [([SEED], expected[0])]          # the closes from the first day on


def test_closes_missing_from_the_price_history_are_null(world):
    series = tab(world, period='1m').json()['series']
    gaps = [i for i, day in enumerate(series['dates']) if date.fromisoformat(day) in CLOSE_GAPS]
    assert len(gaps) == 2
    assert [series['close'][i] for i in gaps] == [None, None]
    assert all(close == close_of(date.fromisoformat(day))
               for i, (day, close) in enumerate(zip(series['dates'], series['close'])) if i not in gaps)


def test_a_stock_without_any_close_has_a_null_close_line(world):
    series = tab(world, NEVER_RANKED).json()['series']
    assert series['close'] == [None] * len(series['dates']) and series['dates']


def test_as_of_is_the_latest_ranked_day_even_with_newer_unranked_rows(world):
    world.db.add(DAILY, stored(SEED, NEXT_DAY))       # received, but no market cap that day: not ranked
    body = tab(world, period='1m').json()
    assert (body['as_of'], body['market_cap'], body['window_days']) == (DAY.isoformat(), SEED_CAP, 250)
    assert body['investors'][0]['amount'] == 14_150_000_000
    assert body['series']['dates'][-1] == NEXT_DAY.isoformat() and len(body['series']['dates']) == 21


def test_as_of_is_looked_for_beyond_the_periods_rows(world):
    # a month of days received without a market cap (no price snapshot): the 1m series holds no ranked day
    later = [day for day in MARKET if day > DAY][:22]
    for later_day in later:
        world.db.add(DAILY, stored(SEED, later_day))
    body = tab(world, period='1m').json()
    assert body['series']['dates'] == [later_day.isoformat() for later_day in later[-21:]]
    assert (body['as_of'], body['market_cap'], body['investors'][0]['rank']) == (DAY.isoformat(), SEED_CAP, 2)


def test_a_stock_never_ranked_has_a_null_as_of_and_null_investor_values(world):
    body = tab(world, NEVER_RANKED).json()
    assert body['stock'] == {'code': NEVER_RANKED, 'name': '미평가회사', 'market': 'KOSDAQ'}
    assert (body['as_of'], body['market_cap'], body['window_days']) == (None, None, None)
    assert body['investors'] == [{'key': key, 'name': name, 'amount': None, 'pct': None, 'rank': None}
                                 for key, name in zip(KEYS, NAMES)]
    rows = world.db.rows(DAILY, stock_code=NEVER_RANKED)
    assert body['series']['dates'] == [row['trade_date'] for row in rows]   # 30 of the 63 asked
    assert body['series']['flows']['pe_fund'] == [row[NET[0]] * WON for row in rows]


def test_under_60_trading_days_there_are_no_ranks_but_the_day_and_its_amounts_show(world):
    body = tab(world, NEW_LISTING, period='1y').json()
    assert (body['as_of'], body['market_cap'], body['window_days']) == (DAY.isoformat(), 900_000_000_000, 40)
    investors = {entry['key']: entry for entry in body['investors']}
    assert [entry['rank'] for entry in investors.values()] == [None] * 11
    assert investors['pe_fund']['amount'] == 600_000_000
    assert investors['pe_fund']['pct'] == 600_000_000 * 100 / 900_000_000_000
    assert len(body['series']['dates']) == 40                     # every stored day, fewer than 250
    assert body['series']['close'] == [close_of(date.fromisoformat(day)) for day in body['series']['dates']]
    assert len(tab(world, NEW_LISTING, period='1m').json()['series']['dates']) == 21


@pytest.mark.parametrize('code,market', [('950130', 'KOSDAQ'), ('217270', None), ('005930', 'KOSPI')])
def test_the_tabs_market_follows_the_same_rule(world, code, market):
    assert tab(world, code).json()['stock']['market'] == market


# ── GET /api/stocks/{code}/flows: the check order ────────────────────────────

@pytest.mark.parametrize('path', [
    '/api/stocks/5930/flows', '/api/stocks/abc/flows', '/api/stocks/0059300/flows', '/api/stocks/a05930/flows',
    '/api/stocks/05930 /flows',
    f'/api/stocks/{SEED}/flows?period=2m', f'/api/stocks/{SEED}/flows?period=',
    f'/api/stocks/{SEED}/flows?period=3M', f'/api/stocks/{SEED}/flows?period=1w',
    f'/api/stocks/{SEED}/flows?period=63',
])
def test_a_malformed_tab_request_is_422_before_anything_is_prepared(path):
    response = unprepared_client().get(path)
    assert response.status_code == 422
    assert isinstance(response.json()['detail'], list)


def test_letters_in_a_code_are_fine(world):
    assert tab(world, '0001A0').json() == STOCK_NOT_FOUND


def test_readiness_comes_before_the_404s():
    assert unprepared_client().get('/api/stocks/ZZZZZZ/flows').json() == not_ready(DB_REASON)


@pytest.mark.parametrize('code', [NOT_LISTED, 'ZZZZZZ'])
def test_a_code_not_in_the_stock_list_is_404_before_the_rows_check(world, code):
    # 999990 has rows; the stock list comes first
    response = tab(world, code)
    assert (response.status_code, response.json()) == (404, STOCK_NOT_FOUND)


def test_a_listed_stock_without_rows_is_404_with_the_sentence_the_screen_compares(world):
    response = tab(world, NO_ROWS)
    assert (response.status_code, response.json()) == (404, NO_FLOW_DATA)
    assert world.closes.calls == []


# ── other features' windows ──────────────────────────────────────────────────

def test_the_report_count_windows_not_ready_passes_through(world, monkeypatch):
    def reports_not_ready(*args, **kwargs):
        raise NotReady('리포트', DB_REASON)

    monkeypatch.setattr(coverage, 'report_counts', reports_not_ready)
    response = ranking(world)
    assert (response.status_code, response.json()) == (503, not_ready(DB_REASON, '리포트'))
    assert tab(world).status_code == 200                           # the tab counts no reports


def test_the_price_windows_not_ready_passes_through(world, monkeypatch):
    def prices_not_ready(*args, **kwargs):
        raise NotReady('주가', DB_REASON)

    monkeypatch.setattr(prices, 'daily_closes', prices_not_ready)
    response = tab(world)
    assert (response.status_code, response.json()) == (503, not_ready(DB_REASON, '주가'))
    assert ranking(world).status_code == 200                       # the ranking reads no closes


# ── readiness (harness rules) ────────────────────────────────────────────────

def test_without_db_settings_both_addresses_are_503_and_they_recover(world, monkeypatch):
    monkeypatch.delenv('SUPABASE_SERVICE_KEY')
    for response in (ranking(world), tab(world)):
        assert (response.status_code, response.json()) == (503, not_ready(DB_REASON))
    assert world.client.get('/api/health').json() == {'status': 'ok'}
    assert world.made == []
    monkeypatch.setenv('SUPABASE_SERVICE_KEY', KEY)
    assert ranking(world).status_code == 200 and tab(world).status_code == 200
    assert world.made == [(URL, KEY)]                              # prepared once, then kept


def test_an_unreadable_stock_list_is_503_with_the_fixed_sentence_and_recovers(world, monkeypatch, caplog):
    missing = world.tmp / 'nowhere' / 'stocks.csv'
    monkeypatch.setenv('KRX_CSV_PATH', str(missing))
    for response in (ranking(world), tab(world)):
        assert (response.status_code, response.json()) == (503, not_ready(STOCKS_REASON))
        assert 'nowhere' not in response.text and 'stocks.csv' not in response.text
    assert 'nowhere' in caplog.text                                # the cause stays in the local log
    write_stock_list(world.tmp / 'later.csv')
    monkeypatch.setenv('KRX_CSV_PATH', str(world.tmp / 'later.csv'))
    assert ranking(world).status_code == 200 and tab(world).status_code == 200


def test_a_stock_list_without_its_version_file_only_logs_a_warning(world, caplog):
    assert ranking(world).status_code == 200
    assert '종목표 버전 정보 파일이 없습니다' in caplog.text


def test_the_db_is_prepared_before_the_stock_list(world, monkeypatch):
    monkeypatch.delenv('SUPABASE_URL')
    monkeypatch.setenv('KRX_CSV_PATH', str(world.tmp / 'nowhere.csv'))
    assert ranking(world).json() == not_ready(DB_REASON)
    assert tab(world).json() == not_ready(DB_REASON)


def test_values_added_to_dotenv_count_on_the_next_request(world, env_file, monkeypatch):
    for name in ('SUPABASE_URL', 'SUPABASE_SERVICE_KEY', 'KRX_CSV_PATH'):
        monkeypatch.delenv(name)
    assert ranking(world).json() == not_ready(DB_REASON)
    env_file.write_text(f'SUPABASE_URL={URL}\nSUPABASE_SERVICE_KEY={KEY}\n', encoding='utf-8')
    assert ranking(world).json() == not_ready(STOCKS_REASON)      # the default path is not there
    with env_file.open('a', encoding='utf-8') as f:
        f.write(f'KRX_CSV_PATH={world.csv}\n')
    assert ranking(world).status_code == 200


def test_creating_the_service_reads_nothing(env_file):
    env_file.write_text(f'SUPABASE_URL={URL}\nSUPABASE_SERVICE_KEY={KEY}\nKRX_CSV_PATH=stocks.csv\n',
                        encoding='utf-8')
    FlowsService()
    assert get_service() is get_service()
    assert 'SUPABASE_URL' not in os.environ and 'KRX_CSV_PATH' not in os.environ   # .env not read
    # (core.db.supabase_client refuses in every test here: no client was made either.)


def test_the_router_uses_one_service_for_the_process(world):
    client = TestClient(build_app())
    assert client.get('/api/flows').status_code == 200
    assert client.get(f'/api/stocks/{SEED}/flows').status_code == 200
    assert world.made == [(URL, KEY)]
    assert isinstance(service_module._service, FlowsService)


def test_the_handlers_are_plain_functions_run_in_the_thread_pool():
    # They make sync DB calls: an async handler would stop the whole server meanwhile.
    for handler in (router_module.flows_ranking, router_module.stock_flows):
        assert not inspect.iscoroutinefunction(handler), handler.__name__


def test_no_answer_carries_a_key_a_url_or_a_local_path(world, monkeypatch):
    texts = [ranking(world).text, ranking(world, date='1999-01-01').text, tab(world).text, tab(world, NO_ROWS).text,
             tab(world, 'ZZZZZZ').text]
    monkeypatch.setenv('KRX_CSV_PATH', str(world.tmp / 'gone.csv'))
    fresh = TestClient(build_app(FlowsService()))
    texts += [fresh.get('/api/flows').text, fresh.get(f'/api/stocks/{SEED}/flows').text]
    for text in texts:
        assert KEY not in text and URL not in text and 'gone.csv' not in text
        json.loads(text)


def test_the_openapi_entries_of_the_two_addresses():
    spec = unprepared_client().get('/openapi.json').json()
    listing = spec['paths']['/api/flows']['get']
    assert (listing['summary'], listing['operationId']) == ('Flows Ranking', 'flows_ranking_api_flows_get')
    assert [(p['name'], p['in'], p['required'], p['schema'].get('default')) for p in listing['parameters']] == [
        ('investor', 'query', False, 'pe_fund'), ('min_cap', 'query', False, 1000), ('date', 'query', False, None),
        ('sort', 'query', False, 'score'), ('limit', 'query', False, 50)]
    investor, min_cap, _, sort, limit = (p['schema'] for p in listing['parameters'])
    assert investor['enum'] == list(KEYS) and sort['enum'] == ['score', 'rank', 'pct', 'amount']
    assert (min_cap['minimum'], min_cap['maximum'], limit['minimum'], limit['maximum']) == (0, 10_000_000, 1, 200)
    stock = spec['paths']['/api/stocks/{code}/flows']['get']
    assert (stock['summary'], stock['operationId']) == ('Stock Flows', 'stock_flows_api_stocks__code__flows_get')
    assert [(p['name'], p['in'], p['required'], p['schema'].get('default')) for p in stock['parameters']] == [
        ('code', 'path', True, None), ('period', 'query', False, '3m')]
    assert stock['parameters'][0]['schema']['pattern'] == '^[0-9A-Z]{6}$'
    assert stock['parameters'][1]['schema']['enum'] == ['1m', '3m', '6m', '1y']
