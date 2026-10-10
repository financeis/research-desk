"""Feature isolation in the assembled app (spec §9.9, §13 item 3).

A feature that cannot prepare answers 503 ``<기능 이름> 기능을 지금 쓸 수 없습니다: <이유>`` on its own
addresses only; the other features and ``/api/health`` keep answering 200. A feature that reads
through another feature's window shows the name of the feature that failed (coverage and compare
show ``리포트``; freshness, which has nothing of its own to prepare, shows ``주가``).

Peers (``유사 기업``) needs the DB settings, the stock list and a public build; its theme search
also needs OPENAI_API_KEY. A build that becomes public is used from the next request, without a
restart.

Flows (``수급``) needs the DB settings and the stock list; its ranking also needs a ranked day.
Without one only the ranking stops (the company tab and every other feature keep answering), and a
day that gets ranked is used from the next request, without a restart.

The real feature services run here, in states that stop before any connection: without DB
settings nothing connects; with them, ``core.db.supabase_client`` hands out an in-memory stand-in.
The companies service uses a temp favorites file.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from research_desk.core import db as core_db
from research_desk.features import coverage, prices
from research_desk.features.companies import service as companies_service
from research_desk.features.companies.service import CompaniesService
from research_desk.features.flows.tests import fakes as flows_fakes
from research_desk.features.flows.tests import web_world as flows_world
from research_desk.features.peers.tests import web_world as peers_world
from research_desk.features.review.tests.fakes import KEY, URL, FakeSupabase, waiting

from .fakes import (
    CATALOG,
    DB_REASON,
    INDEX_HTML,
    NO_PUBLIC_BUILD_REASON,
    NO_RANKED_DAY_REASON,
    OPENAI_KEY_REASON,
    STOCKS_REASON,
    not_ready,
    write_stock_csv,
)

# (method, address, JSON body)
REPORTS_ADDRESSES = [
    ('GET', '/api/stocks/016360/reports', None),
    ('GET', '/api/reports/1/pdf', None),
    ('POST', '/api/reports/1/analyze', None),
]
# compare and coverage read the reports through the reports window
COMPARE_ADDRESSES = [
    ('GET', '/api/compare?left=1&right=2', None),
    ('POST', '/api/compare/analyze', {'left': 1, 'right': 2}),
]
COVERAGE_ADDRESSES = [
    ('GET', '/api/market', None),
    ('GET', '/api/stocks/016360/activity?days=365', None),
]
REVIEW_ADDRESSES = [
    ('GET', '/api/review', None),
    ('GET', '/api/review/1/pdf', None),
    ('GET', '/api/review/1/preview', None),
    ('GET', '/api/review/1/pages/1', None),
    ('POST', '/api/review/1/action', {'action': 'verify'}),
]
PEERS_ADDRESSES = [
    ('GET', '/api/stocks/016360/peers', None),
    ('POST', '/api/peers/search', {'q': '레거시 DRAM'}),
]
# freshness reads the prices and reports windows (prices first)
FRESHNESS_ADDRESSES = [
    ('GET', '/api/freshness', None),
]
FLOWS_ADDRESSES = [
    ('GET', '/api/flows', None),
    ('GET', '/api/stocks/016360/flows', None),
]


class SharedDB:
    """One Supabase stand-in for review and flows: the reports table is review's stand-in, every
    other table flows' (each feature reads only its own tables)."""

    def __init__(self, reports_db, flows_db) -> None:
        self.reports_db, self.flows_db = reports_db, flows_db

    def table(self, name: str):
        return (self.reports_db if name == 'reports' else self.flows_db).table(name)


def answers(client, addresses) -> list[tuple[str, int, dict]]:
    return [(address, response.status_code, response.json())
            for method, address, body in addresses
            for response in [client.request(method, address, json=body)]]


def test_without_db_settings_only_the_features_that_need_the_db_stop(app, stock_csv, companies):
    with TestClient(app) as client:
        db_features = answers(client, REPORTS_ADDRESSES + COMPARE_ADDRESSES + COVERAGE_ADDRESSES)
        review = answers(client, REVIEW_ADDRESSES)
        peers = answers(client, PEERS_ADDRESSES)
        freshness = answers(client, FRESHNESS_ADDRESSES)
        flows = answers(client, FLOWS_ADDRESSES)
        health = client.get('/api/health')
        workspace = client.get('/api/workspace')
        favorite = client.put('/api/favorites/016360', json={'enabled': True})
        screen = client.get('/')
    reports_down = not_ready('리포트', DB_REASON)
    assert db_features == [(address, 503, reports_down) for _, address, _ in
                           REPORTS_ADDRESSES + COMPARE_ADDRESSES + COVERAGE_ADDRESSES]
    review_down = not_ready('검토', DB_REASON)
    assert review == [(address, 503, review_down) for _, address, _ in REVIEW_ADDRESSES]
    peers_down = not_ready('유사 기업', DB_REASON)
    assert peers == [(address, 503, peers_down) for _, address, _ in PEERS_ADDRESSES]
    prices_down = not_ready('주가', DB_REASON)   # the prices window's NotReady, passed through
    assert freshness == [(address, 503, prices_down) for _, address, _ in FRESHNESS_ADDRESSES]
    flows_down = not_ready('수급', DB_REASON)
    assert flows == [(address, 503, flows_down) for _, address, _ in FLOWS_ADDRESSES]
    assert (health.status_code, health.json()) == (200, {'status': 'ok'})
    assert (workspace.status_code, workspace.json()) == (200, {'stocks': CATALOG, 'favorites': []})
    assert (favorite.status_code, favorite.json()) == (200, {'favorites': ['016360']})
    assert (screen.status_code, screen.content) == (200, INDEX_HTML)


def test_an_unreadable_stock_list_stops_only_the_features_that_need_it(app, tmp_path, monkeypatch,
                                                                       favorites_path):
    missing = tmp_path / 'stocks' / 'krx.csv'
    monkeypatch.setenv('KRX_CSV_PATH', str(missing))
    db = FakeSupabase([waiting(1)])
    monkeypatch.setenv('SUPABASE_URL', URL)
    monkeypatch.setenv('SUPABASE_SERVICE_KEY', KEY)
    monkeypatch.setattr(core_db, 'supabase_client', lambda url, key: db)
    service = CompaniesService(favorites_path=favorites_path)
    app.dependency_overrides[companies_service.get_service] = lambda: service
    with TestClient(app) as client:
        workspace = client.get('/api/workspace')
        favorite = client.put('/api/favorites/016360', json={'enabled': True})
        listed = client.get('/api/stocks/016360/reports')
        market = client.get('/api/market')
        peers = [client.request(method, address, json=body) for method, address, body in PEERS_ADDRESSES]
        flows = [client.request(method, address, json=body) for method, address, body in FLOWS_ADDRESSES]
        health = client.get('/api/health')
        review = client.get('/api/review')
        # the cause fixed: the next request prepares again, without a restart
        write_stock_csv(missing)
        recovered = client.get('/api/workspace')
    companies_down = not_ready('기업 목록', STOCKS_REASON)
    assert (workspace.status_code, workspace.json()) == (503, companies_down)
    assert (favorite.status_code, favorite.json()) == (503, companies_down)
    assert (listed.status_code, listed.json()) == (503, not_ready('리포트', STOCKS_REASON))
    assert (market.status_code, market.json()) == (503, not_ready('커버리지', STOCKS_REASON))
    assert [(response.status_code, response.json()) for response in peers] == [
        (503, not_ready('유사 기업', STOCKS_REASON))] * len(PEERS_ADDRESSES)
    assert [(response.status_code, response.json()) for response in flows] == [
        (503, not_ready('수급', STOCKS_REASON))] * len(FLOWS_ADDRESSES)
    assert (health.status_code, health.json()) == (200, {'status': 'ok'})
    assert review.status_code == 200
    assert review.json()['remaining'] == 1 and review.json()['report']['id'] == 1
    assert (recovered.status_code, recovered.json()) == (200, {'stocks': CATALOG, 'favorites': []})
    # the answers name no path; the refused favorite wrote nothing
    for response in (workspace, favorite, listed, market, *peers, *flows):
        for path_text in ('krx.csv', str(tmp_path), json.dumps(str(tmp_path))[1:-1], tmp_path.as_posix()):
            assert path_text not in response.text
    assert not favorites_path.exists()


def test_peers_also_waits_for_a_public_build_and_its_search_for_openai_api_key(app, tmp_path, monkeypatch,
                                                                                companies):
    # The DB settings and the stock list are ready; the peers tables hold no public build yet.
    csv_path = peers_world.write_stock_list(tmp_path / 'peers-stocks.csv')
    db = peers_world.make_db(builds=[])
    monkeypatch.setenv('SUPABASE_URL', peers_world.URL)
    monkeypatch.setenv('SUPABASE_SERVICE_KEY', peers_world.KEY)
    monkeypatch.setenv('KRX_CSV_PATH', str(csv_path))
    monkeypatch.setattr(core_db, 'supabase_client', lambda url, key: db)
    # the peers list reads report counts and prices through those windows
    monkeypatch.setattr(coverage, 'report_counts', peers_world.Counts())
    monkeypatch.setattr(prices, 'snapshots', peers_world.Snapshots())
    with TestClient(app) as client:
        without_build = answers(client, PEERS_ADDRESSES)
        health = client.get('/api/health')
        workspace = client.get('/api/workspace')
        # a build becomes public: the next request uses it, without a restart
        db.table('peer_builds').insert(peers_world.build_row()).execute()
        listed = client.get(f'/api/stocks/{peers_world.SEED}/peers')
        search = client.post('/api/peers/search', json={'q': '레거시 DRAM'})   # no OPENAI_API_KEY
    no_build = not_ready('유사 기업', NO_PUBLIC_BUILD_REASON)
    assert without_build == [(address, 503, no_build) for _, address, _ in PEERS_ADDRESSES]
    assert (health.status_code, health.json()) == (200, {'status': 'ok'})
    assert workspace.status_code == 200
    assert listed.status_code == 200
    assert listed.json()['basis']['build_id'] == 1 and listed.json()['peers']
    assert (search.status_code, search.json()) == (503, not_ready('유사 기업', OPENAI_KEY_REASON))


def test_flows_also_waits_for_a_ranked_day_and_only_its_ranking_stops(app, stock_csv, companies, monkeypatch):
    # The DB settings and the stock list are ready; flows holds 016360's raw rows but no ranked day.
    days = [day for day in flows_fakes.MARKET if day <= flows_world.DAY_1][-30:]
    flows_db = flows_fakes.FakeSupabase(**{flows_fakes.DAILY: [flows_world.stored('016360', day) for day in days]})
    db = SharedDB(FakeSupabase([waiting(1)]), flows_db)
    monkeypatch.setenv('SUPABASE_URL', URL)
    monkeypatch.setenv('SUPABASE_SERVICE_KEY', KEY)
    monkeypatch.setattr(core_db, 'supabase_client', lambda url, key: db)
    # the flows addresses read report counts and adjusted closes through those windows
    monkeypatch.setattr(coverage, 'report_counts', flows_world.Counts())
    monkeypatch.setattr(prices, 'daily_closes', flows_world.Closes({}))
    with TestClient(app) as client:
        without_day = client.get('/api/flows')
        stock = client.get('/api/stocks/016360/flows')
        health = client.get('/api/health')
        workspace = client.get('/api/workspace')
        review = client.get('/api/review')
        screen = client.get('/')
        # a run ranks a day: the next request uses it, without a restart
        flows_db.add(flows_fakes.DAILY, flows_world.ranked('016360', flows_world.DAY, 50_000_000_000_000, 250,
                                                           1200, (4, 5, 6)))
        flows_db.add(flows_fakes.DAYS, flows_world.ranked_day(flows_world.DAY))
        ranked = client.get('/api/flows')
    assert (without_day.status_code, without_day.json()) == (503, not_ready('수급', NO_RANKED_DAY_REASON))
    assert stock.status_code == 200 and stock.json()['as_of'] is None
    assert stock.json()['stock'] == {'code': '016360', 'name': '삼성증권', 'market': 'KOSPI'}
    assert (health.status_code, health.json()) == (200, {'status': 'ok'})
    assert (workspace.status_code, workspace.json()) == (200, {'stocks': CATALOG, 'favorites': []})
    assert review.status_code == 200 and review.json()['remaining'] == 1
    assert (screen.status_code, screen.content) == (200, INDEX_HTML)
    assert ranked.status_code == 200
    assert ranked.json()['dates'] == [flows_world.DAY.isoformat()] and ranked.json()['total'] == 1
    assert [(row['code'], row['name'], row['rank']) for row in ranked.json()['rows']] == [('016360', '삼성증권', 4)]
