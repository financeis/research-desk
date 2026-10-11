"""Extraction prompt.

Ported from langgraph_tagger/analytics/llm_summary/tests/test_prompts.py
(the extraction tests; the diff prompt tests moved with the diff prompt to compare).
New: the page-image request (same system text, pages attached as images).
"""
import json
import re

from research_desk.features.analysis.prompts import (
    PAGE_IMAGE_NOTE, render_extraction_messages, render_page_image_messages,
)


def test_extraction_includes_normalization_rules():
    msgs = render_extraction_messages(
        report_metadata={'publisher': '삼성증권', 'stock_codes': ['005930'],
                         'published_at': '2026-05-05', 'title': 'X'},
        pages_text='--- Page 1 ---\n본문 ...',
    )
    sys = ''.join(m['content'] for m in msgs if m['role'] == 'system')
    user = ''.join(m['content'] for m in msgs if m['role'] == 'user')
    # 핵심 룰들이 prompt에 들어가 있는지 sanity check
    assert 'target_price' in sys.lower() or '목표주가' in sys
    assert 'BUY' in sys or '매수' in sys  # recommendation mapping
    assert 'Trading Buy' in sys             # 한국 sell-side 표현 포함
    assert 'Outperform' in sys
    assert 'metadata' in sys.lower()         # trust metadata 한 줄
    assert '005930' in user                  # metadata가 user msg에
    assert '--- Page 1 ---' in user          # page-numbered text 포함


def test_extraction_traps_section():
    msgs = render_extraction_messages(
        report_metadata={}, pages_text='--- Page 1 ---\n')
    sys = ''.join(m['content'] for m in msgs if m['role'] == 'system')
    assert 'current price' in sys.lower() or '현재가' in sys
    assert 'market cap' in sys.lower() or '시가총액' in sys


def test_extraction_messages_shape():
    # system first, then the user payload: metadata JSON (Korean kept) + page text.
    metadata = {'publisher': '삼성증권', 'stock_codes': ['005930']}
    pages = '--- Page 1 ---\n본문\n'
    system, user = render_extraction_messages(metadata, pages)
    assert system['role'] == 'system' and user['role'] == 'user'
    assert user['content'] == (
        f"<report_metadata>\n{json.dumps(metadata, ensure_ascii=False, indent=2)}\n</report_metadata>\n\n"
        f"<report_pages>\n{pages}\n</report_pages>"
    )
    assert '<report_pages>' not in system['content']
    # the system text is the same for every report (cacheable)
    assert render_extraction_messages({}, '')[0] == system


# ── page-image request (business-rules 리포트 1건 재무 분석) ───────────────────────────────────────────

METADATA = {'publisher': '삼성증권', 'stock_codes': ['005930'],
            'published_at': '2026-05-05', 'title': '반도체 업황 점검'}


def _metadata_block(metadata):
    return (f"<report_metadata>\n{json.dumps(metadata, ensure_ascii=False, indent=2)}"
            f"\n</report_metadata>\n\n")


def test_page_image_system_is_the_text_path_system():
    # Not one character different: the provider caches the system prompt.
    text_system, _ = render_extraction_messages(METADATA, '--- Page 1 ---\n본문\n')
    image_system, _ = render_page_image_messages(METADATA, [1, 2])
    assert image_system == text_system
    assert image_system['role'] == 'system'
    assert render_page_image_messages({}, [7])[0] == text_system


def test_page_image_user_payload_shape():
    _, user = render_page_image_messages(METADATA, [1, 2, 4])
    assert user['role'] == 'user'
    assert user['content'] == (
        _metadata_block(METADATA)
        + f"<report_pages>\n{PAGE_IMAGE_NOTE}\n\n"
        "--- Page 1 ---\n--- Page 2 ---\n--- Page 4 ---\n"
        "</report_pages>"
    )


def test_page_image_metadata_block_matches_the_text_path():
    _, text_user = render_extraction_messages(METADATA, 'X')
    _, image_user = render_page_image_messages(METADATA, [1])
    block = _metadata_block(METADATA)
    assert text_user['content'].startswith(block)
    assert image_user['content'].startswith(block)


def test_page_image_markers_follow_the_given_order():
    # Physical page numbers (1-based), in attachment order; skipped pages stay skipped.
    _, user = render_page_image_messages(METADATA, [1, 2, 4])
    assert re.findall(r'--- Page (\d+) ---', user['content']) == ['1', '2', '4']
    _, user = render_page_image_messages(METADATA, [3, 10, 11])
    assert re.findall(r'--- Page (\d+) ---', user['content']) == ['3', '10', '11']


def test_page_image_note_is_fixed_and_says_images_are_attached():
    assert PAGE_IMAGE_NOTE.strip() == PAGE_IMAGE_NOTE and '\n' not in PAGE_IMAGE_NOTE
    assert 'image' in PAGE_IMAGE_NOTE.lower()
    assert not re.search(r'--- Page \d+ ---', PAGE_IMAGE_NOTE)
    for pages in ([1], [1, 2, 4], [5, 6]):
        _, user = render_page_image_messages(METADATA, pages)
        assert user['content'].count(PAGE_IMAGE_NOTE) == 1
    # The note stays out of the system text (which must equal the text path's).
    assert PAGE_IMAGE_NOTE not in render_page_image_messages(METADATA, [1])[0]['content']
