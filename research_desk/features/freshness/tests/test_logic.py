"""features.freshness logic: the freshness rules of spec §12.3 as pure functions.

Checked:
- the expected trading day: in Korea, today when now is a weekday at or after 20:00 (19:59:59.999999
  is still the weekday before), else the nearest weekday before today — Monday morning gives the
  Friday before, Saturday and Sunday (also after 20:00) give Friday; "now" in any zone counts in
  Korea; the 20:00 is read when the rule runs, so it can be adjusted;
- the price states, first match wins: no successful run ever (no run at all, or only failed /
  running runs), the latest run failed (even with a current as_of), as_of before the expected
  trading day (with the holiday hint), else fresh; a running latest run is judged by as_of alone;
  as_of after the expected trading day is fresh; stale exactly when there is a note;
- the investor flows: the same rule with the flows sentences, in the same order (never succeeded,
  latest run failed, as_of behind with the holiday hint) and at the same 20:00 boundaries; a day the
  flows update was skipped (the price update failed) shows the behind note; a running latest run is
  judged by as_of; as_of after the expected trading day is fresh; stale exactly when there is a
  note; the 20:00 is the prices' one, read when the rule runs; prices and flows are judged apart;
- the reports: no in-scope report, the 3-day boundary measured from now (exactly 3 days is not
  older than 3 days), a sent_at after now, other zones, the adjustable days;
- the shape: the example answer with the flows block after the prices one, the key order, times
  as ISO text in Korea to the second, as_of as YYYY-MM-DD, None for what is not known; the failed
  status is the prices and the flows features' value; the inputs are not changed.

October 2026: Mon 5, Tue 6, Wed 7, Thu 8, Fri 9, Sat 10, Sun 11, Mon 12, Tue 13.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone

import pytest

from research_desk.features.flows import logic as flows_logic
from research_desk.features.freshness import logic
from research_desk.features.freshness.logic import (
    KST,
    expected_trading_day,
    flows_freshness,
    freshness_payload,
    iso_kst,
    price_freshness,
    report_freshness,
)
from research_desk.features.prices import logic as prices_logic

TUE, WED, THU, FRI = date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)
SAT, SUN, MON, NEXT_TUE = date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12), date(2026, 10, 13)

NEVER = '주가가 아직 한 번도 갱신되지 않았습니다'
LAST_RUN_FAILED = '마지막 주가 갱신이 실패했습니다'
HOLIDAY_HINT = '휴장일이면 정상입니다'
FLOWS_NEVER = '수급 자료가 아직 한 번도 갱신되지 않았습니다'
FLOWS_LAST_RUN_FAILED = '마지막 수급 갱신이 실패했습니다'

# The start of Thursday's 18:30 run, as the prices window gives it (aware, UTC).
RUN_STARTED = datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc)
# The start of Thursday's flows update, after the price run, as the flows window gives it (aware, UTC).
FLOWS_STARTED = datetime(2026, 10, 8, 9, 45, tzinfo=timezone.utc)
# The newest in-scope report's sent_at behind the example answer (09:12 in Korea).
LATEST_AT = datetime(2026, 10, 8, 0, 12, tzinfo=timezone.utc)


def kst(day: date, hour: int = 12, minute: int = 0, second: int = 0, micro: int = 0) -> datetime:
    """A moment in Korea."""
    return datetime(day.year, day.month, day.day, hour, minute, second, micro, tzinfo=KST)


# Thursday evening: the expected trading day is Thursday.
NOW = kst(THU, 20, 0)


def run(status: str = 'ok', as_of=THU, started: datetime = RUN_STARTED) -> dict:
    """The prices window's ``latest_run()`` answer."""
    return {'last_run_at': started, 'last_run_status': status, 'as_of': as_of}


def behind(day_text: str) -> str:
    return f'주가가 {day_text} 기준으로 밀려 있습니다. {HOLIDAY_HINT}'


def flows_run(status: str = 'ok', as_of=THU, started: datetime = FLOWS_STARTED) -> dict:
    """The flows window's ``latest_run()`` answer."""
    return {'last_run_at': started, 'last_run_status': status, 'as_of': as_of}


def flows_behind(day_text: str) -> str:
    return f'수급 자료가 {day_text} 기준으로 밀려 있습니다. {HOLIDAY_HINT}'


# ── the expected trading day ─────────────────────────────────────────────────

@pytest.mark.parametrize('now, expected', [
    (kst(THU, 19, 59, 59, 999999), WED),
    (kst(THU, 18, 30), WED),
    (kst(THU, 20, 0), THU),
    (kst(THU, 0, 0), WED),
    (kst(THU, 23, 59, 59), THU),
    (kst(MON, 0, 0), FRI),
    (kst(MON, 9, 0), FRI),
    (kst(MON, 19, 59), FRI),
    (kst(MON, 20, 0), MON),
    (kst(SAT, 10, 0), FRI),
    (kst(SAT, 20, 0), FRI),
    (kst(SAT, 22, 0), FRI),
    (kst(SUN, 0, 0), FRI),
    (kst(SUN, 23, 59, 59), FRI),
    (kst(NEXT_TUE, 8, 0), MON),
    (kst(WED, 9, 0), TUE),
], ids=['weekday 19:59:59.999999', 'weekday 18:30 while the price run may still be going',
        'weekday 20:00', 'weekday midnight', 'weekday late evening',
        'Monday midnight', 'Monday morning', 'Monday 19:59', 'Monday 20:00', 'Saturday morning',
        'Saturday 20:00', 'Saturday evening', 'Sunday midnight', 'Sunday late evening',
        'Tuesday morning', 'Wednesday morning'])
def test_the_expected_trading_day(now, expected):
    assert expected_trading_day(now) == expected


@pytest.mark.parametrize('now, expected', [
    (datetime(2026, 10, 8, 10, 59, 59, tzinfo=timezone.utc), WED),   # 19:59:59 in Korea
    (datetime(2026, 10, 8, 11, 0, tzinfo=timezone.utc), THU),        # 20:00 in Korea
    (datetime(2026, 10, 7, 23, 0, tzinfo=timezone.utc), WED),        # Thursday 08:00 in Korea
    (datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc), FRI),        # Saturday 00:00 in Korea
    (datetime(2026, 10, 11, 14, 59, tzinfo=timezone.utc), FRI),      # Sunday 23:59 in Korea
    (datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc), FRI),       # Monday 00:00 in Korea
], ids=['19:59:59 KST', '20:00 KST', 'Thursday morning KST', 'Saturday KST', 'Sunday KST',
        'Monday midnight KST'])
def test_the_expected_trading_day_counts_in_korea_whatever_the_zone_of_now(now, expected):
    assert expected_trading_day(now) == expected


def test_the_cutoff_is_20_00_and_read_when_the_rule_runs(monkeypatch):
    assert logic.CUTOFF == time(20, 0)
    monkeypatch.setattr(logic, 'CUTOFF', time(21, 0))
    assert expected_trading_day(kst(THU, 20, 0)) == WED
    assert expected_trading_day(kst(THU, 21, 0)) == THU


# ── prices: the four states ──────────────────────────────────────────────────

def test_no_run_at_all_is_stale_as_never_updated():
    assert price_freshness(None, NOW) == {'as_of': None, 'last_run_at': None, 'last_run_status': None,
                                          'stale': True, 'note': NEVER}


@pytest.mark.parametrize('status', ['failed', 'running'])
def test_runs_without_any_success_are_stale_as_never_updated(status):
    assert price_freshness(run(status, as_of=None), NOW) == {
        'as_of': None, 'last_run_at': '2026-10-08T18:30:00+09:00', 'last_run_status': status,
        'stale': True, 'note': NEVER}


def test_a_failed_latest_run_is_stale_even_with_a_current_as_of():
    assert price_freshness(run('failed', as_of=THU), NOW) == {
        'as_of': '2026-10-08', 'last_run_at': '2026-10-08T18:30:00+09:00', 'last_run_status': 'failed',
        'stale': True, 'note': LAST_RUN_FAILED}


def test_a_failed_latest_run_is_named_before_an_old_as_of():
    assert price_freshness(run('failed', as_of=date(2026, 9, 30)), NOW)['note'] == LAST_RUN_FAILED


@pytest.mark.parametrize('status', ['ok', 'partial'])
def test_an_as_of_before_the_expected_trading_day_is_stale_with_the_holiday_hint(status):
    assert price_freshness(run(status, as_of=WED), NOW) == {
        'as_of': '2026-10-07', 'last_run_at': '2026-10-08T18:30:00+09:00', 'last_run_status': status,
        'stale': True, 'note': '주가가 10월 7일 기준으로 밀려 있습니다. 휴장일이면 정상입니다'}


@pytest.mark.parametrize('status', ['ok', 'partial', 'running'])
def test_an_as_of_on_the_expected_trading_day_is_fresh(status):
    assert price_freshness(run(status, as_of=THU), NOW) == {
        'as_of': '2026-10-08', 'last_run_at': '2026-10-08T18:30:00+09:00', 'last_run_status': status,
        'stale': False, 'note': None}


def test_a_running_latest_run_is_judged_by_as_of_alone():
    assert price_freshness(run('running', as_of=THU), NOW)['stale'] is False
    result = price_freshness(run('running', as_of=WED), NOW)
    assert (result['stale'], result['note']) == (True, behind('10월 7일'))


def test_an_as_of_after_the_expected_trading_day_is_fresh():
    # a run by hand during trading hours: Thursday's prices on Thursday morning (expected: Wednesday)
    result = price_freshness(run('ok', as_of=THU), kst(THU, 10, 0))
    assert (result['stale'], result['note']) == (False, None)


@pytest.mark.parametrize('now, as_of, stale', [
    (kst(THU, 19, 59, 59, 999999), WED, False),   # before 20:00 Wednesday's prices are current
    (kst(THU, 18, 30), WED, False),               # the 18:30 run may still be going: not stale yet
    (kst(THU, 20, 0), WED, True),                 # from 20:00 Thursday's are expected
    (kst(THU, 20, 0), THU, False),
    (kst(MON, 9, 0), FRI, False),                 # Monday morning: Friday's are current
    (kst(MON, 9, 0), THU, True),
    (kst(MON, 20, 0), FRI, True),
    (kst(SAT, 12, 0), FRI, False),
    (kst(SAT, 12, 0), THU, True),
    (kst(SUN, 12, 0), FRI, False),
    (kst(SUN, 12, 0), THU, True),
], ids=['Thu 19:59 / Wed', 'Thu 18:30 / Wed', 'Thu 20:00 / Wed', 'Thu 20:00 / Thu', 'Mon morning / Fri',
        'Mon morning / Thu', 'Mon 20:00 / Fri', 'Sat / Fri', 'Sat / Thu', 'Sun / Fri', 'Sun / Thu'])
def test_the_as_of_rule_at_the_boundaries(now, as_of, stale):
    result = price_freshness(run('ok', as_of=as_of), now)
    assert result['stale'] is stale
    assert result['note'] == (behind(f'{as_of.month}월 {as_of.day}일') if stale else None)


def test_the_note_writes_the_as_of_as_month_and_day():
    result = price_freshness(run('ok', as_of=date(2025, 12, 30)), kst(date(2026, 1, 5), 20, 0))
    assert result['note'] == behind('12월 30일')


@pytest.mark.parametrize('answer', [
    None, run('failed', as_of=None), run('running', as_of=None), run('failed'), run('ok', as_of=WED),
    run('ok'), run('partial'), run('running'),
], ids=['no run', 'only failed', 'first running', 'failed', 'behind', 'ok', 'partial', 'running'])
def test_stale_exactly_when_there_is_a_note(answer):
    result = price_freshness(answer, NOW)
    assert result['stale'] is (result['note'] is not None)


# ── investor flows: the price rule with the flows sentences ──────────────────

def test_no_flows_run_at_all_is_stale_as_never_updated():
    assert flows_freshness(None, NOW) == {'as_of': None, 'last_run_at': None, 'last_run_status': None,
                                          'stale': True, 'note': FLOWS_NEVER}


@pytest.mark.parametrize('status', ['failed', 'running'])
def test_flows_runs_without_any_success_are_stale_as_never_updated(status):
    assert flows_freshness(flows_run(status, as_of=None), NOW) == {
        'as_of': None, 'last_run_at': '2026-10-08T18:45:00+09:00', 'last_run_status': status,
        'stale': True, 'note': FLOWS_NEVER}


def test_a_failed_latest_flows_run_is_stale_even_with_a_current_as_of():
    assert flows_freshness(flows_run('failed', as_of=THU), NOW) == {
        'as_of': '2026-10-08', 'last_run_at': '2026-10-08T18:45:00+09:00', 'last_run_status': 'failed',
        'stale': True, 'note': FLOWS_LAST_RUN_FAILED}


@pytest.mark.parametrize('status', ['ok', 'partial'])
def test_a_flows_as_of_before_the_expected_trading_day_is_stale_with_the_holiday_hint(status):
    assert flows_freshness(flows_run(status, as_of=WED), NOW) == {
        'as_of': '2026-10-07', 'last_run_at': '2026-10-08T18:45:00+09:00', 'last_run_status': status,
        'stale': True, 'note': '수급 자료가 10월 7일 기준으로 밀려 있습니다. 휴장일이면 정상입니다'}


@pytest.mark.parametrize('answer, note', [
    (flows_run('failed', as_of=None), FLOWS_NEVER),                         # 1 before 2
    (flows_run('running', as_of=None), FLOWS_NEVER),
    (flows_run('failed', as_of=date(2026, 9, 30)), FLOWS_LAST_RUN_FAILED),  # 2 before 3
    (flows_run('partial', as_of=date(2026, 9, 30)), flows_behind('9월 30일')),
    (flows_run('ok', as_of=THU), None),
], ids=['failed without success', 'running without success', 'failed with an old as_of', 'behind', 'fresh'])
def test_the_flows_notes_in_their_order(answer, note):
    assert flows_freshness(answer, NOW)['note'] == note


def test_a_day_whose_flows_update_was_skipped_shows_the_behind_note():
    # Thursday's price update failed, so the flows update did not run: the latest flows run is Wednesday's
    wednesday = flows_run('ok', as_of=WED, started=datetime(2026, 10, 7, 9, 45, tzinfo=timezone.utc))
    assert flows_freshness(wednesday, NOW) == {
        'as_of': '2026-10-07', 'last_run_at': '2026-10-07T18:45:00+09:00', 'last_run_status': 'ok',
        'stale': True, 'note': flows_behind('10월 7일')}


@pytest.mark.parametrize('status', ['ok', 'partial', 'running'])
def test_a_flows_as_of_on_the_expected_trading_day_is_fresh(status):
    assert flows_freshness(flows_run(status, as_of=THU), NOW) == {
        'as_of': '2026-10-08', 'last_run_at': '2026-10-08T18:45:00+09:00', 'last_run_status': status,
        'stale': False, 'note': None}


def test_a_running_latest_flows_run_is_judged_by_as_of_alone():
    assert flows_freshness(flows_run('running', as_of=THU), NOW)['stale'] is False
    result = flows_freshness(flows_run('running', as_of=WED), NOW)
    assert (result['stale'], result['note']) == (True, flows_behind('10월 7일'))


def test_a_flows_as_of_after_the_expected_trading_day_is_fresh():
    # a run by hand during trading hours: Thursday's flows on Thursday morning (expected: Wednesday)
    result = flows_freshness(flows_run('ok', as_of=THU), kst(THU, 10, 0))
    assert (result['stale'], result['note']) == (False, None)


@pytest.mark.parametrize('now, as_of, stale', [
    (kst(THU, 19, 59, 59, 999999), WED, False),   # before 20:00 Wednesday's flows are current
    (kst(THU, 18, 30), WED, False),               # the daily runs may still be going: not stale yet
    (kst(THU, 20, 0), WED, True),                 # from 20:00 Thursday's are expected
    (kst(THU, 20, 0), THU, False),
    (kst(MON, 9, 0), FRI, False),                 # Monday morning: Friday's are current
    (kst(MON, 9, 0), THU, True),
    (kst(MON, 19, 59), FRI, False),
    (kst(MON, 20, 0), FRI, True),
    (kst(SAT, 12, 0), FRI, False),
    (kst(SAT, 12, 0), THU, True),
    (kst(SAT, 20, 0), FRI, False),                # a weekend evening expects Friday still
    (kst(SUN, 12, 0), FRI, False),
    (kst(SUN, 23, 59, 59), FRI, False),
    (kst(SUN, 12, 0), THU, True),
], ids=['Thu 19:59 / Wed', 'Thu 18:30 / Wed', 'Thu 20:00 / Wed', 'Thu 20:00 / Thu', 'Mon morning / Fri',
        'Mon morning / Thu', 'Mon 19:59 / Fri', 'Mon 20:00 / Fri', 'Sat / Fri', 'Sat / Thu',
        'Sat 20:00 / Fri', 'Sun / Fri', 'Sun late evening / Fri', 'Sun / Thu'])
def test_the_flows_as_of_rule_at_the_boundaries(now, as_of, stale):
    result = flows_freshness(flows_run('ok', as_of=as_of), now)
    assert result['stale'] is stale
    assert result['note'] == (flows_behind(f'{as_of.month}월 {as_of.day}일') if stale else None)


def test_the_flows_follow_the_price_cutoff_read_when_the_rule_runs(monkeypatch):
    monkeypatch.setattr(logic, 'CUTOFF', time(21, 0))
    assert flows_freshness(flows_run('ok', as_of=WED), kst(THU, 20, 0))['stale'] is False
    assert flows_freshness(flows_run('ok', as_of=WED), kst(THU, 21, 0))['note'] == flows_behind('10월 7일')


def test_the_flows_note_writes_the_as_of_as_month_and_day():
    result = flows_freshness(flows_run('ok', as_of=date(2025, 12, 30)), kst(date(2026, 1, 5), 20, 0))
    assert result['note'] == flows_behind('12월 30일')


@pytest.mark.parametrize('answer', [
    None, flows_run('failed', as_of=None), flows_run('running', as_of=None), flows_run('failed'),
    flows_run('ok', as_of=WED), flows_run('ok'), flows_run('partial'), flows_run('running'),
], ids=['no run', 'only failed', 'first running', 'failed', 'behind', 'ok', 'partial', 'running'])
def test_flows_stale_exactly_when_there_is_a_note(answer):
    result = flows_freshness(answer, NOW)
    assert result['stale'] is (result['note'] is not None)


def test_prices_and_flows_are_judged_apart():
    # each block comes from its own window's answer, with its own sentences
    payload = freshness_payload(run('failed'), flows_run('ok', as_of=WED), LATEST_AT, NOW)
    assert (payload['prices']['note'], payload['flows']['note']) == (LAST_RUN_FAILED, flows_behind('10월 7일'))
    payload = freshness_payload(run('ok', as_of=WED), flows_run('failed'), LATEST_AT, NOW)
    assert (payload['prices']['note'], payload['flows']['note']) == (behind('10월 7일'), FLOWS_LAST_RUN_FAILED)
    payload = freshness_payload(run('ok'), None, LATEST_AT, NOW)
    assert (payload['prices']['stale'], payload['flows']['note']) == (False, FLOWS_NEVER)
    payload = freshness_payload(None, flows_run('ok'), LATEST_AT, NOW)
    assert (payload['prices']['note'], payload['flows']['stale']) == (NEVER, False)


# ── reports ──────────────────────────────────────────────────────────────────

def test_no_in_scope_report_is_stale():
    assert report_freshness(None, NOW) == {'latest_at': None, 'stale': True}


@pytest.mark.parametrize('age, stale', [
    (timedelta(0), False),
    (timedelta(days=2, hours=23, minutes=59), False),
    (timedelta(days=3), False),                     # exactly 3 days is not older than 3 days
    (timedelta(days=3, microseconds=1), True),
    (timedelta(days=3, seconds=1), True),
    (timedelta(days=30), True),
    (-timedelta(minutes=5), False),                 # a sent_at after now (clocks apart)
], ids=['now', 'just under 3 days', 'exactly 3 days', '3 days + 1 µs', '3 days + 1 s', '30 days',
        'after now'])
def test_the_report_three_day_rule(age, stale):
    assert report_freshness(NOW - age, NOW)['stale'] is stale


def test_the_report_rule_measures_from_now_whatever_the_zones():
    latest = datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc)          # Monday 20:00 in Korea
    assert report_freshness(latest, kst(THU, 20, 0)) == {'latest_at': '2026-10-05T20:00:00+09:00',
                                                         'stale': False}   # exactly 3 days
    assert report_freshness(latest, kst(THU, 20, 0, 1))['stale'] is True
    assert report_freshness(latest, datetime(2026, 10, 8, 11, 0, 1, tzinfo=timezone.utc))['stale'] is True


def test_the_report_days_are_three_and_read_when_the_rule_runs(monkeypatch):
    assert logic.REPORT_STALE_DAYS == 3
    monkeypatch.setattr(logic, 'REPORT_STALE_DAYS', 5)
    assert report_freshness(NOW - timedelta(days=4), NOW)['stale'] is False
    assert report_freshness(NOW - timedelta(days=5, seconds=1), NOW)['stale'] is True


# ── the shape ────────────────────────────────────────────────────────────────

# the example answer, with the flows block after the prices one
SPEC_EXAMPLE = {
    'prices': {'as_of': '2026-10-08', 'last_run_at': '2026-10-08T18:30:00+09:00', 'last_run_status': 'ok',
               'stale': False, 'note': None},
    'flows': {'as_of': '2026-10-08', 'last_run_at': '2026-10-08T18:45:00+09:00', 'last_run_status': 'ok',
              'stale': False, 'note': None},
    'reports': {'latest_at': '2026-10-08T09:12:00+09:00', 'stale': False},
    'checked_at': '2026-10-08T20:00:00+09:00',
}


def test_the_payload_of_spec_example_with_its_key_order():
    payload = freshness_payload(run('ok'), flows_run('ok'), LATEST_AT, NOW)
    assert payload == SPEC_EXAMPLE
    assert list(payload) == ['prices', 'flows', 'reports', 'checked_at']
    assert list(payload['prices']) == ['as_of', 'last_run_at', 'last_run_status', 'stale', 'note']
    assert list(payload['flows']) == ['as_of', 'last_run_at', 'last_run_status', 'stale', 'note']
    assert list(payload['reports']) == ['latest_at', 'stale']


def test_the_payload_when_nothing_is_known_yet():
    assert freshness_payload(None, None, None, NOW) == {
        'prices': {'as_of': None, 'last_run_at': None, 'last_run_status': None, 'stale': True,
                   'note': NEVER},
        'flows': {'as_of': None, 'last_run_at': None, 'last_run_status': None, 'stale': True,
                  'note': FLOWS_NEVER},
        'reports': {'latest_at': None, 'stale': True},
        'checked_at': '2026-10-08T20:00:00+09:00',
    }


def test_times_are_iso_text_in_korea_to_the_second():
    assert iso_kst(datetime(2026, 10, 8, 0, 12, 0, 654321, tzinfo=timezone.utc)) == '2026-10-08T09:12:00+09:00'
    assert iso_kst(datetime(2026, 10, 8, 23, 59, 59, tzinfo=KST)) == '2026-10-08T23:59:59+09:00'
    assert iso_kst(datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)) == '2026-10-09T00:00:00+09:00'
    assert iso_kst(None) is None


def test_the_payload_takes_times_of_any_zone_cut_to_the_second():
    payload = freshness_payload(
        run('ok', started=datetime(2026, 10, 8, 9, 30, 5, 500000, tzinfo=timezone.utc)),
        flows_run('ok', started=datetime(2026, 10, 8, 18, 45, 7, 999999, tzinfo=KST)),
        datetime(2026, 10, 8, 9, 12, tzinfo=KST),
        datetime(2026, 10, 8, 11, 0, 0, 999999, tzinfo=timezone.utc))
    assert payload['prices']['last_run_at'] == '2026-10-08T18:30:05+09:00'
    assert payload['flows']['last_run_at'] == '2026-10-08T18:45:07+09:00'
    assert payload['reports']['latest_at'] == '2026-10-08T09:12:00+09:00'
    assert payload['checked_at'] == '2026-10-08T20:00:00+09:00'


def test_the_failed_status_is_the_prices_features_value():
    assert logic.FAILED == prices_logic.FAILED


def test_the_failed_status_is_the_flows_features_value_too():
    assert logic.FAILED == flows_logic.FAILED


def test_the_inputs_are_not_changed():
    answer, flows_answer = run('failed', as_of=WED), flows_run('failed', as_of=WED)
    kept, flows_kept = deepcopy(answer), deepcopy(flows_answer)
    freshness_payload(answer, flows_answer, NOW - timedelta(days=1), NOW)
    assert (answer, flows_answer) == (kept, flows_kept)
