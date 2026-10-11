"""A narrative and the busy-report marker it shares with analysis and delete (spec §3.9, §5.3, §6).

At the service level, with the reports window and the AI replaced (``world``, ``ai``); the marker
is analysis' real one, through its window ``analysis.hold_reports``:
- a report being analyzed or deleted (marked busy) → 409 with compare's text, at once: no saved
  narrative reused, no AI, nothing saved, the other report not left marked;
- the 404 and 422 shape checks come first and mark nothing;
- while a narrative is made both reports are busy: analysis' window refuses them, and another
  narrative with one of them is 409;
- the two marks are freed after every outcome;
- the saved analyses used are read while both reports are held, so a change made before the hold
  is the one used;
- GET /api/compare neither looks at the marker nor sets it.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException

from research_desk.core.settings import NotReady
from research_desk.features import analysis, reports
from research_desk.features.analysis import service as analysis_service
from research_desk.features.compare import service
from research_desk.features.compare.prompts import render_diff_messages

from .fakes import (BUSY, IN_FLIGHT, REPORT_NOT_FOUND, analysed, metric, reports_are_free, tagged,
                    until)

LIMIT_S = 5  # bound on awaits that would hang if the behaviour under test broke
NOT_ANALYZED = '선택한 두 보고서를 먼저 분석해 주세요.'


def pair(world, narrative=None):
    """Two analysed KB reports on 삼성증권: 1 (February) and 3 (May, maybe with a narrative for 1)."""
    world.add(tagged(1, '2026-02-09'), analysed(1, 70000, metric(100)))
    saved = ({'prev_report_id': 1, 'prev_match_type': 'same_publisher', 'diff_narrative': narrative}
             if narrative else {})
    world.add(tagged(3, '2026-05-11'), analysed(3, 85000, metric(120), **saved))


async def refused(coroutine) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        await asyncio.wait_for(coroutine, LIMIT_S)
    return exc.value


# ── a busy report ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('narrative', [None, '저장된 해석'], ids=['new', 'saved'])
@pytest.mark.parametrize('busy', [1, 3], ids=['earlier busy', 'later busy'])
@pytest.mark.parametrize('left, right', [(1, 3), (3, 1)])
async def test_a_busy_report_makes_the_narrative_409(world, ai, busy, narrative, left, right):
    pair(world, narrative=narrative)
    async with analysis.hold_reports([busy], IN_FLIGHT):   # being analyzed or deleted
        error = await refused(service.analyze_comparison(left, right))
        assert (error.status_code, error.detail) == (409, BUSY)
        # a saved narrative is not given back either: the busy check comes first
        assert ai.prepared == 0 and ai.calls == [] and world.saved == []
        # read for the shape checks only, not again
        assert world.reads == [left, right]
        other = 3 if busy == 1 else 1
        await reports_are_free(other)   # the other report was not left marked
    await reports_are_free(1, 3)


@pytest.mark.parametrize('left, right, status, detail', [
    (1, 9, 404, REPORT_NOT_FOUND), (9, 1, 404, REPORT_NOT_FOUND),
    (1, 1, 422, '서로 다른 보고서 두 개를 선택해 주세요.'),
    (1, 5, 422, '같은 기업의 보고서를 선택해 주세요.'),
    (6, 1, 422, '금융 비교는 단일종목 보고서 두 개를 선택해 주세요.'),
], ids=['missing right', 'missing left', 'same report', 'other company', 'industry'])
async def test_the_shape_checks_come_before_the_busy_check(world, ai, left, right, status, detail):
    pair(world)
    world.add(tagged(5, '2026-04-01', codes=('005930',)), analysed(5, 1000))
    world.add(tagged(6, '2026-04-02', report_type='산업'), analysed(6))
    async with analysis.hold_reports([1], IN_FLIGHT):
        error = await refused(service.analyze_comparison(left, right))
        assert (error.status_code, error.detail) == (status, detail)
    # and they mark nothing
    error = await refused(service.analyze_comparison(left, right))
    assert (error.status_code, error.detail) == (status, detail)
    await reports_are_free(1, 3, 5, 6)
    assert ai.prepared == 0 and world.saved == []


# ── while a narrative is made ────────────────────────────────────────────────

async def test_while_a_narrative_is_made_both_reports_are_busy(world, ai):
    pair(world)
    world.add(tagged(5, '2026-06-01'), analysed(5, 90000))
    world.add(tagged(7, '2026-07-01'), analysed(7, 95000))
    world.add(tagged(8, '2026-08-01'), analysed(8, 99000))
    ai.gate = asyncio.Event()
    task = asyncio.create_task(service.analyze_comparison(1, 3))
    try:
        await until(lambda: len(ai.calls) == 1)
        for rid in (1, 3):   # what an analysis or a delete gets (with analysis' own text)
            with pytest.raises(HTTPException) as exc:
                async with analysis.hold_reports([rid], IN_FLIGHT):
                    pass
            assert (exc.value.status_code, exc.value.detail) == (409, IN_FLIGHT)
        # the same pair again, or another pair with one of the two: compare's text
        for left, right in ((1, 3), (3, 1), (1, 5), (5, 3)):
            error = await refused(service.analyze_comparison(left, right))
            assert (error.status_code, error.detail) == (409, BUSY)
        assert len(ai.calls) == 1 and world.saved == []
        await reports_are_free(5)   # the refused pairs left nothing marked
        # a pair without the two goes on to the AI (the second slot)
        other = asyncio.create_task(service.analyze_comparison(7, 8))
        await until(lambda: len(ai.calls) == 2)
    finally:
        ai.gate.set()
    result = await asyncio.wait_for(task, LIMIT_S)
    assert result['narrative'] == ai.narrative
    assert (await asyncio.wait_for(other, LIMIT_S))['narrative'] == ai.narrative
    assert sorted(saved[:2] for saved in world.saved) == [(3, 1), (8, 7)]
    await reports_are_free(1, 3, 5, 7, 8)


# ── the marks are freed ──────────────────────────────────────────────────────

def _new(world, ai):
    pair(world)


def _saved(world, ai):
    pair(world, narrative='저장된 해석')


def _not_analysed(world, ai):
    pair(world)
    del world.summaries[3]


def _ai_error(world, ai):
    pair(world)
    ai.error = RuntimeError('provider rejected the key')


def _refusal(world, ai):
    pair(world)
    ai.refusal = 'refusal (cyber): x'


# (setup, None for an answer or the error expected)
OUTCOMES = {
    'new narrative': (_new, None),
    'saved narrative': (_saved, None),
    'not analysed': (_not_analysed, (HTTPException, NOT_ANALYZED)),
    'AI failure': (_ai_error, (RuntimeError, 'provider rejected')),
    'refusal': (_refusal, (RuntimeError, '거부')),
}


@pytest.mark.parametrize('setup, raised', list(OUTCOMES.values()), ids=list(OUTCOMES))
async def test_the_marks_are_freed_after_every_outcome(world, ai, setup, raised):
    setup(world, ai)
    for _ in range(2):   # and the next request is not refused
        if raised is None:
            await asyncio.wait_for(service.analyze_comparison(1, 3), LIMIT_S)
        else:
            error, text = raised
            with pytest.raises(error, match=text):
                await asyncio.wait_for(service.analyze_comparison(1, 3), LIMIT_S)
        await reports_are_free(1, 3)


async def test_the_marks_are_freed_without_a_model_key(world):
    pair(world)   # the real analysis.phase2_llm: no key → NotReady("분석", …)
    for _ in range(2):
        with pytest.raises(NotReady, match='분석 기능을 지금 쓸 수 없습니다'):
            await asyncio.wait_for(service.analyze_comparison(1, 3), LIMIT_S)
        await reports_are_free(1, 3)


async def test_a_not_analysed_report_is_422_only_after_the_busy_check(world, ai):
    pair(world)
    del world.summaries[3]
    async with analysis.hold_reports([3], IN_FLIGHT):   # its first analysis is running
        error = await refused(service.analyze_comparison(1, 3))
        assert (error.status_code, error.detail) == (409, BUSY)
    error = await refused(service.analyze_comparison(1, 3))
    assert (error.status_code, error.detail) == (422, NOT_ANALYZED)
    await reports_are_free(1, 3)


# ── the saved analyses are read under the hold ───────────────────────────────

async def test_the_saved_analyses_are_read_while_both_reports_are_held(world, ai, monkeypatch):
    pair(world)
    marked = []

    def get_report(rid):
        marked.append(set(analysis_service._analyzing))   # the marker as this read starts
        return world.get_report(rid)

    monkeypatch.setattr(reports, 'get_report', get_report)
    await service.analyze_comparison(3, 1)
    # first the shape checks (nothing held), then both reports read again while held
    assert world.reads == [3, 1, 3, 1]
    assert marked == [set(), set(), {1, 3}, {1, 3}]


async def test_an_analysis_saved_before_the_hold_is_used(world, ai):
    pair(world)
    fresh = analysed(3, 90000, metric(130))
    del world.summaries[3]   # report 3 is not analysed when its row is first read …

    def analysed_meanwhile(rid):   # … and its analysis is saved right after the shape checks
        if world.reads == [1, 3]:
            world.summaries[3] = deepcopy(fresh)   # (the save then changes this copy)

    world.after_read = analysed_meanwhile
    result = await service.analyze_comparison(1, 3)
    assert result['narrative'] == ai.narrative   # not 422
    assert result['right']['summary'] == fresh
    assert [(m['previous'], m['current']) for m in result['metrics']] == [(100, 130)]
    assert result['target_price_change']['delta'] == 20000
    system, user = render_diff_messages(world.public(1)['summary'], fresh, 'same_publisher', 1,
                                        'KB', 'KB')
    assert [(c['system'], c['user']) for c in ai.calls] == [(system['content'], user['content'])]
    ((_, _, _, _, details),) = world.saved
    assert details['metrics'] == result['metrics']


async def test_a_reanalysis_before_the_hold_drops_the_saved_narrative(world, ai):
    pair(world, narrative='옛 분석으로 쓴 해석')
    fresh = analysed(3, 95000, metric(140))   # re-analysed: no comparison fields

    def reanalysed_meanwhile(rid):
        if world.reads == [1, 3]:
            world.summaries[3] = deepcopy(fresh)   # (the save then changes this copy)

    world.after_read = reanalysed_meanwhile
    result = await service.analyze_comparison(1, 3)
    assert result['narrative'] == ai.narrative != '옛 분석으로 쓴 해석'
    assert len(ai.calls) == 1
    system, user = render_diff_messages(world.public(1)['summary'], fresh, 'same_publisher', 1,
                                        'KB', 'KB')
    assert (ai.calls[0]['system'], ai.calls[0]['user']) == (system['content'], user['content'])


async def test_a_delete_before_the_hold_is_422(world, ai):
    pair(world)

    def deleted_meanwhile(rid):
        if world.reads == [1, 3]:
            del world.summaries[1]

    world.after_read = deleted_meanwhile
    error = await refused(service.analyze_comparison(1, 3))
    assert (error.status_code, error.detail) == (422, NOT_ANALYZED)
    assert ai.prepared == 0 and world.saved == []
    await reports_are_free(1, 3)


# ── GET /api/compare ─────────────────────────────────────────────────────────

async def test_the_comparison_neither_looks_at_nor_sets_the_marker(world, ai):
    pair(world, narrative='저장된 해석')
    expected = service.compare_reports(1, 3)
    await reports_are_free(1, 3)   # GET set nothing
    async with analysis.hold_reports([1, 3], IN_FLIGHT):
        assert service.compare_reports(3, 1) == expected   # GET does not look
        error = await refused(reports_are_free(1))          # and both are still held
        assert error.status_code == 409
    await reports_are_free(1, 3)
    assert ai.prepared == 0 and world.saved == []
