"""Compare work: two reports of the same company side by side, and their narrative (spec §6, §9.6).

Both addresses read the two reports through ``reports.get_report`` (the public shape with the
saved summary): 404 ``기업 보고서를 찾을 수 없습니다.`` for a report that is not in scope, and
``NotReady("리포트", …)`` without DB settings, so the reports feature's name shows (spec §9.9).
Then ``logic.comparison`` checks the pair (422) and lays it out, the earlier report on the left.

The narrative (``POST /api/compare/analyze``):

1. the two reports are read and the pair checked (404, 422) as above; nothing is held yet and
   the saved analyses read along are not used;
2. ``analysis.hold_reports`` marks both reports busy (the marker analysis and delete share): one
   being analyzed, deleted or in another narrative → 409 ``선택한 보고서를 분석하거나 지우는
   중입니다. 잠시 후 다시 시도해 주세요.`` at once, nothing marked;
3. while both are held they are read again, so the saved analyses used below cannot change or go
   before the save; either missing → 422 ``선택한 두 보고서를 먼저 분석해 주세요.``;
4. a narrative saved for exactly this pair comes back as it is: no AI call, no key needed;
5. otherwise, inside ``analysis.ai_slot()`` (the web server's 2 AI calls, shared with analysis):
   ``analysis.phase2_llm()`` gives the AI client, model and per-call time limit (``NotReady("분석",
   …)`` when the key or the codex CLI is missing); ``diff_one`` writes the narrative with the
   earlier report as the previous one, ``same_publisher`` or ``cross_publisher``, and ``미상`` for
   an unknown publisher; ``analysis.save_comparison`` stores it on the later report's summary with
   ``comparison_details`` {metrics, previous_publisher, previous_published_at}. A later report keeps
   one narrative: another pair replaces it;
6. the two marks are freed whatever the outcome.

``GET /api/compare`` neither looks at the busy marker nor sets it.

Compare reads no Phase 2 setting and no table itself; the reads and the save run in worker threads.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException

from research_desk.features import analysis, reports

from .llm import diff_one
from .logic import comparison

NOT_ANALYZED = '선택한 두 보고서를 먼저 분석해 주세요.'
REPORTS_BUSY = '선택한 보고서를 분석하거나 지우는 중입니다. 잠시 후 다시 시도해 주세요.'
UNKNOWN_PUBLISHER = '미상'


def compare_reports(left_id: int, right_id: int) -> dict[str, Any]:
    """``GET /api/compare``: the comparison of two in-scope reports. Never calls the AI."""
    return comparison(reports.get_report(left_id), reports.get_report(right_id))


async def analyze_comparison(left_id: int, right_id: int) -> dict[str, Any]:
    """``POST /api/compare/analyze``: the comparison plus the narrative of this exact pair."""
    await _read_pair(left_id, right_id)   # 404 / 422 before anything is held
    async with analysis.hold_reports([left_id, right_id], REPORTS_BUSY):
        # read again while held: these saved analyses stay as they are until the save
        result = await _read_pair(left_id, right_id)
        left, right = result['left'], result['right']
        if not left['summary'] or not right['summary']:
            raise HTTPException(422, NOT_ANALYZED)
        if result['narrative']:
            return result
        match = 'same_publisher' if result['same_publisher'] else 'cross_publisher'
        async with analysis.ai_slot():
            llm, model, timeout_s = analysis.phase2_llm()
            async with llm as client:
                diff, _, _ = await diff_one(
                    client=client, model=model, prev_summary=left['summary'],
                    curr_summary=right['summary'], prev_match_type=match,
                    prev_report_id=left['id'],
                    prev_publisher=left['publisher'] or UNKNOWN_PUBLISHER,
                    curr_publisher=right['publisher'] or UNKNOWN_PUBLISHER,
                    timeout_s=timeout_s,
                )
            details = {'metrics': result['metrics'], 'previous_publisher': left['publisher'],
                       'previous_published_at': left['published_at']}
            await asyncio.to_thread(analysis.save_comparison, right['id'], left['id'], match,
                                    diff.diff_narrative, details)
    return result | {'narrative': diff.diff_narrative}


async def _read_pair(left_id: int, right_id: int) -> dict[str, Any]:
    """Both reports through the reports window (left first), laid out by ``comparison``."""
    left = await asyncio.to_thread(reports.get_report, left_id)
    right = await asyncio.to_thread(reports.get_report, right_id)
    return comparison(left, right)
