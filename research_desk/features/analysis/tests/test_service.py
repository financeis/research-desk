"""Analysis service — the window reports and compare use (DB, AI and files faked).

Ported: the analysis part of
langgraph_tagger/workspace/tests/test_migration.py::test_selected_cached_reports_only_and_non_company_rejection
(reuse, rejection of non-단일종목 reports, concurrent request 409). Reading the
row (404) is the reports feature's part.

New (spec §9.5, §9.9, §13 item 3):
- the model key / codex CLI is checked only right before the AI call, so reuse,
  summaries_for, 409 and 422 work without keys;
- ai_slot() lets 2 AI calls run at a time and analysis waits for a free slot;
- the saved summary has today's fields; DB readiness gives NotReady("분석", …).

Page-image analysis, re-analysis, delete and the busy marker:
- one decision over every page's raw text: text path or image path (exactly at
  a limit is the text path);
- image path: model check, pages 1..20 drawn (undrawable pages left out), the
  same system message, ``--- Page N ---`` markers with the real page numbers,
  no number grounding, confidence at most medium, its own time limit and no
  retry after a timeout; an empty image answer is 422 and saves nothing;
- ``force`` skips only the reuse; any failure leaves the DB as it was;
- saving and deleting clear the comparisons citing the report first;
- one busy marker for analysis, delete and ``hold_reports``.
Every environment variable involved is set or deleted here.
"""
import asyncio
import inspect
import json
import threading
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi import HTTPException

from research_desk.core import db as core_db
from research_desk.core import llm as core_llm
from research_desk.core import pdf as core_pdf
from research_desk.core.llm import LLMClient, StructuredResult, TransientLLMError
from research_desk.core.settings import NotReady
from research_desk.features import analysis
from research_desk.features.analysis import service
from research_desk.features.analysis.financials import FinancialDetails
from research_desk.features.analysis.prompts import (
    render_extraction_messages, render_page_image_messages,
)
from research_desk.features.analysis.schemas import ExtractionResult

ENV = ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'LLM_MODEL_PHASE2', 'OPENAI_MODEL_PHASE2',
       'PHASE2_PER_REPORT_TIMEOUT_S', 'PHASE2_MAX_INPUT_TOKENS', 'PHASE2_SUMMARY_VERSION',
       'PHASE2_IMAGE_TIMEOUT_S', 'PHASE2_MAX_CONCURRENT', 'SUPABASE_URL', 'SUPABASE_SERVICE_KEY',
       'SUPABASE_DB_URL', 'STORAGE_BASE_DIR', 'CODEX_BIN', 'ANTHROPIC_EFFORT',
       'CODEX_REASONING_EFFORT', 'LANGSMITH_TRACING', 'LANGSMITH_API_KEY')

LIMIT_S = 5  # bound on awaits that would hang if the behaviour under test broke

IN_FLIGHT = '이 보고서는 분석 중입니다. 잠시 후 새로고침해 주세요.'
NOT_COMPANY = '금융 정보 분석은 단일종목 보고서를 선택해 주세요.'
NO_TEXT = 'PDF에서 읽을 수 있는 텍스트가 없습니다.'
EMPTY_IMAGE = '그림으로도 리포트 내용을 읽지 못했습니다.'
DB_MISSING = 'DB 접속 설정(SUPABASE_URL, SUPABASE_SERVICE_KEY)이 없습니다'
CLEARED = {'prev_report_id': None, 'prev_match_type': None, 'diff_narrative': None,
           'comparison_details': None}
METADATA = {'publisher': 'KB', 'stock_codes': ['016360'], 'published_at': '2026-05-11'}


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeQuery:
    """Chainable stand-in for one supabase-py query on report_summaries."""

    def __init__(self, db, table):
        self.db, self.table = db, table
        self.kind, self.payload, self.on_conflict = None, None, None
        self.ids, self.filters, self.neqs = None, {}, {}

    def select(self, cols):
        self.kind = 'select'
        return self

    def upsert(self, payload, on_conflict=None):
        self.kind, self.payload, self.on_conflict = 'upsert', payload, on_conflict
        return self

    def update(self, payload):
        self.kind, self.payload = 'update', payload
        return self

    def delete(self):
        self.kind = 'delete'
        return self

    def in_(self, col, values):
        assert col == 'report_id'
        self.ids = list(values)
        return self

    def eq(self, col, value):
        self.filters[col] = value
        return self

    def neq(self, col, value):
        self.neqs[col] = value
        return self

    def _matches(self, row):
        # like SQL: a NULL cell never passes = or <>
        return (all(row.get(col) is not None and row.get(col) == value
                    for col, value in self.filters.items())
                and all(row.get(col) is not None and row.get(col) != value
                        for col, value in self.neqs.items()))

    def execute(self):
        assert self.table == 'report_summaries'
        if self.kind == self.db.hold_kind:   # runs in a worker thread: block it there
            self.db.holding = True
            assert self.db.hold.wait(LIMIT_S), 'the test never released the DB'
        if self.kind in self.db.fail:
            raise self.db.fail[self.kind]
        self.db.executed.append(self)
        rows = self.db.rows
        if self.kind == 'select':
            data = [dict(rows[i]) for i in self.ids if i in rows
                    and rows[i].get('summary_version') == self.filters['summary_version']]
        elif self.kind == 'upsert':
            rows[self.payload['report_id']] = dict(self.payload)
            data = [dict(self.payload)]
        elif self.kind == 'update':
            matched = [rid for rid, r in rows.items() if self._matches(r)]
            for rid in matched:
                rows[rid].update(self.payload)
            data = [dict(rows[rid]) for rid in matched]
        else:  # delete
            matched = [rid for rid, r in rows.items() if self._matches(r)]
            data = [rows.pop(rid) for rid in matched]
        return SimpleNamespace(data=data)


class FakeSupabase:
    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.executed: list[FakeQuery] = []
        self.fail: dict[str, Exception] = {}      # query kind → error raised by execute()
        self.hold_kind = None                     # query kind that waits for ``hold``
        self.hold = threading.Event()
        self.holding = False

    def table(self, name):
        return FakeQuery(self, name)

    def add_summary(self, rid, **fields):
        row = {'report_id': rid, 'summary_version': 'llm-summary@1.0',
               'one_line_summary': f'saved {rid}', 'financial_details': {'metrics': []},
               **fields}
        self.rows[rid] = row
        return dict(row)

    def of(self, kind):
        return [q for q in self.executed if q.kind == kind]

    def writes(self):
        return [q for q in self.executed if q.kind != 'select']


async def until(condition, timeout_s=5.0):
    # Poll by wall-clock time, not a fixed number of loop ticks: PDF reading runs in
    # a worker thread, so a busy machine can need more ticks before the AI is called.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not condition():
        if loop.time() > deadline:
            raise AssertionError('condition not reached')
        await asyncio.sleep(0.005)


def row(rid, report_type='단일종목', file_path='r.pdf', **extra):
    return {'id': rid, 'report_type': report_type, 'file_path': file_path, 'publisher': 'KB',
            'stock_codes': ['016360'], 'published_at': '2026-05-11', 'title': f'report {rid}',
            'tagging_status': 'auto', **extra}


# Text-path PDFs. A PDF goes to the AI as page images when its pages average under
# 300 Latin letters and under 50 Hangul (the default font has no Hangul). These five
# lines add 330 Latin letters and no digits to a page, so they ground no number.
FILLER = '\n'.join(['The analyst keeps the rating and explains the outlook for the company in detail.'] * 5)


def write_pdf(folder, name, pages, filler=True):
    doc = pymupdf.open()
    for body in pages:
        doc.new_page().insert_text((72, 72), f'{body}\n{FILLER}' if filler else body)
    doc.save(str(folder / name))
    doc.close()
    return name


def write_image_pdf(folder, name, pages):
    """A PDF of ``pages`` pages with shapes and no text; page n is 72 + 4n points wide,
    so each page's picture is different."""
    doc = pymupdf.open()
    for n in range(1, pages + 1):
        page = doc.new_page(width=72 + 4 * n, height=72)
        page.draw_rect(pymupdf.Rect(10, 10, 60, 60), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
    doc.save(str(folder / name))
    doc.close()
    return name


def _lines(alphabet, count, per_line):
    text = (alphabet * (count // len(alphabet) + 1))[:count]
    return [text[i:i + per_line] for i in range(0, count, per_line)]


def write_counted_pdf(folder, name, pages):
    """Each page holds exactly ``(hangul, latin)`` letters, in lines that fit the page."""
    doc = pymupdf.open()
    for hangul, latin in pages:
        page = doc.new_page()
        y = 72
        for line in _lines('가나다라마바사아자차카타파하', hangul, 20):
            page.insert_text((72, y), line, fontname='korea')
            y += 20
        for line in _lines('abcdefghij', latin, 50):
            page.insert_text((72, y), line)
            y += 20
    doc.save(str(folder / name))
    doc.close()
    texts = core_pdf.page_texts(folder / name)
    counted = [(sum('가' <= c <= '힣' for c in t), sum(c.isascii() and c.isalpha() for c in t))
               for t in texts]
    assert counted == [tuple(p) for p in pages]   # the PDF holds what the test claims
    return name


def fail_drawing(monkeypatch, fails):
    """Pages whose number satisfies ``fails`` cannot be drawn; returns the drawing calls."""
    real = pymupdf.Page.get_pixmap
    calls = []

    def get_pixmap(page, *args, **kwargs):
        calls.append(page.number + 1)
        if fails(page.number + 1):
            raise RuntimeError('damaged page')
        return real(page, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, 'get_pixmap', get_pixmap)
    return calls


FD_INPUT = {
    'metrics': [
        {'metric': '영업이익', 'fiscal_period': '2026', 'value': 1234, 'previous_value': 1100,
         'unit': '십억원', 'currency': 'KRW', 'accounting_basis': '연결', 'value_type': '추정',
         'scenario': '기본', 'evidence': {'page': 1, 'quote': 'OP 2026F 1,234'},
         'previous_evidence': {'page': 2, 'quote': 'Revision OP 1,100 to 1,234'}},
        {'metric': '매출액', 'fiscal_period': '2026', 'value': 999, 'previous_value': None,
         'unit': '십억원', 'currency': 'KRW', 'accounting_basis': '연결', 'value_type': '추정',
         'scenario': '기본', 'evidence': {'page': 1, 'quote': 'Sales 999'}},
    ],
    'valuation': {'method': 'PER', 'target_horizon': '12개월', 'explanation': 'EPS x PER',
                  'assumptions': [],
                  'change_drivers': [{'category': '배수 변경', 'explanation': '배수 상향',
                                      'evidence': {'page': 1, 'quote': 'Target 85,000'}}]},
    'theses': [], 'catalysts': [],
    'rating': {'current_label': 'Buy', 'previous_label': None, 'definition': None,
               'horizon': None, 'evidence': None},
}
FD_NO_METRICS = {**FD_INPUT, 'metrics': []}


def extraction(**changes):
    values = dict(
        financial_details=FD_INPUT, target_price_new=85000, target_price_old=70000,
        target_price_dir='불변',  # wrong on purpose: arithmetic makes it 상향
        recommendation='매수', recommendation_dir='유지', one_line_summary='목표주가 상향',
        positive_points=['수주 증가'], risk_points=[], target_price_raw='85,000원',
        recommendation_raw='Buy', source_pages=[2, 1], extraction_confidence='high',
    )
    values.update(changes)
    return ExtractionResult.model_validate(values)


def empty_answer(**changes):
    """An image answer with nothing in it (no target, N/A, no metrics, low)."""
    values = dict(financial_details=None, target_price_new=None, target_price_old=None,
                  target_price_dir='N/A', recommendation='N/A', recommendation_dir='N/A',
                  one_line_summary='읽을 수 없음', positive_points=[], target_price_raw=None,
                  recommendation_raw=None, source_pages=[], extraction_confidence='low')
    values.update(changes)
    return extraction(**values)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def storage(monkeypatch, tmp_path):
    """No keys, no DB settings, no codex CLI; PDFs live in a temp storage folder."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / 'reports'
    folder.mkdir()
    monkeypatch.setenv('STORAGE_BASE_DIR', str(folder))
    monkeypatch.setattr(core_llm, '_codex_bin', lambda: None)
    monkeypatch.setattr(service, '_supabase_client', None)  # prepare afresh in each test
    monkeypatch.setattr(service, '_analyzing', set())
    return folder


@pytest.fixture
def db(monkeypatch):
    fake = FakeSupabase()
    fake.made = []

    def supabase_client(url, key):
        fake.made.append((url, key))
        return fake

    monkeypatch.setenv('SUPABASE_URL', 'https://example.supabase.test')
    monkeypatch.setenv('SUPABASE_SERVICE_KEY', 'service-key')
    monkeypatch.setattr(core_db, 'supabase_client', supabase_client)
    yield fake
    fake.hold.set()   # never leave a worker thread waiting


@pytest.fixture
def ai(monkeypatch):
    """The AI boundary: LLMClient.parse records each call and returns ``ai.reply``.

    ``errors`` are raised by the next calls, one each; ``error`` by every call.
    """
    state = SimpleNamespace(calls=[], reply=extraction(), gate=None, error=None, errors=[])

    async def parse(self, **kwargs):
        state.calls.append(kwargs)
        if state.gate is not None:
            await state.gate.wait()
        if state.errors:
            raise state.errors.pop(0)
        if state.error is not None:
            raise state.error
        return StructuredResult(parsed=state.reply, input_tokens=1234, output_tokens=567)

    monkeypatch.setattr(LLMClient, 'parse', parse)
    return state


@pytest.fixture
def sleeps(monkeypatch):
    """call_with_retry's back-off, recorded instead of waited."""
    waited = []

    async def sleep(seconds, *args):
        waited.append(seconds)

    monkeypatch.setattr(core_llm.asyncio, 'sleep', sleep)
    return waited


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')  # default model gpt-6-luna (takes images)


@pytest.fixture
def spy(monkeypatch):
    """What the service hands extract_one (the real one still runs)."""
    seen = []
    real_extract = service.extract_one

    async def extract_one(**kwargs):
        seen.append(kwargs)
        return await real_extract(**kwargs)

    monkeypatch.setattr(service, 'extract_one', extract_one)
    return seen


def pages_of(user):
    return user[user.index('<report_pages>\n') + 15:user.index('\n</report_pages>')]


# ── the window ───────────────────────────────────────────────────────────────

def test_window_exposes_the_public_names():
    names = ('analyze_report', 'summaries_for', 'save_comparison', 'ai_slot', 'phase2_llm',
             'delete_summary', 'hold_reports')
    for name in names:
        assert callable(getattr(analysis, name))
        assert getattr(analysis, name) is getattr(service, name)   # bound from .service
    assert asyncio.iscoroutinefunction(analysis.analyze_report)
    assert asyncio.iscoroutinefunction(analysis.delete_summary)
    assert inspect.signature(analysis.analyze_report).parameters['force'].default is False


# ── ported: reuse, 422, 409 ──────────────────────────────────────────────────

async def test_selected_cached_reports_only_and_non_company_rejection(db, ai):
    db.add_summary(1)
    db.add_summary(3)
    db.add_summary(2)
    results = await asyncio.gather(analysis.analyze_report(row(1)), analysis.analyze_report(row(3)))
    assert [summary['report_id'] for summary, _ in results] == [1, 3]
    assert all(reused for _, reused in results)
    # only the selected reports' summaries are read
    assert sorted(q.ids for q in db.of('select')) == [[1], [3]]
    with pytest.raises(HTTPException) as exc:
        await analysis.analyze_report(row(9, report_type='산업'))
    assert exc.value.status_code == 422
    assert exc.value.detail == NOT_COMPANY
    assert not service._analyzing
    assert ai.calls == [] and db.writes() == []


@pytest.mark.parametrize('report_type', ['산업', '섹터', 'IR자료', '전략·시황', '기타', None])
async def test_only_single_company_reports_are_analyzed(report_type):
    # no DB settings, no model key: the 422 comes first
    with pytest.raises(HTTPException) as exc:
        await analysis.analyze_report(row(9, report_type=report_type))
    assert (exc.value.status_code, exc.value.detail) == (422, NOT_COMPANY)
    assert not service._analyzing


async def test_concurrent_request_for_the_same_report_is_409(db):
    db.add_summary(1)
    async with analysis.ai_slot(), analysis.ai_slot():  # both AI slots busy
        first = asyncio.create_task(analysis.analyze_report(row(1)))
        await until(lambda: 1 in service._analyzing)
        with pytest.raises(HTTPException) as exc:
            # refused at once, without waiting for a slot (a timeout here = no 409)
            await asyncio.wait_for(analysis.analyze_report(row(1)), LIMIT_S)
        assert (exc.value.status_code, exc.value.detail) == (409, IN_FLIGHT)
        # a different report is not refused, it waits for a slot
        other = asyncio.create_task(analysis.analyze_report(row(2, report_type='산업')))
        await until(lambda: 2 in service._analyzing)
        assert not first.done() and not other.done()
    summary, reused = await asyncio.wait_for(first, LIMIT_S)
    assert reused is True and summary['report_id'] == 1
    with pytest.raises(HTTPException):
        await asyncio.wait_for(other, LIMIT_S)
    assert not service._analyzing
    # once finished, the same report can be requested again
    assert (await asyncio.wait_for(analysis.analyze_report(row(1)), LIMIT_S))[1] is True


# ── model key only right before the AI call ──────────────────────────────────

async def test_reuse_and_summaries_for_work_without_a_model_key(db, ai):
    saved = db.add_summary(7, financial_details={'metrics': [], 'unsupported_numeric_values': 0})
    summary, reused = await analysis.analyze_report(row(7, file_path='missing.pdf'))
    assert reused is True and summary == saved
    assert analysis.summaries_for([7, 8]) == {7: saved}
    assert ai.calls == [] and db.writes() == []


@pytest.mark.parametrize('force', [False, True])
@pytest.mark.parametrize('model,reason', [
    ('gpt-6-luna', 'OPENAI_API_KEY가 설정되지 않았습니다. .env에 추가 후 분석 다시 시도하세요.'),
    ('claude-haiku-5-5', 'ANTHROPIC_API_KEY가 설정되지 않았습니다. .env에 추가 후 분석 다시 시도하세요.'),
    ('codex:gpt-6-luna', 'codex CLI를 찾을 수 없습니다. 설치 후 `codex login`으로 로그인하세요.'),
])
async def test_missing_model_key_is_not_ready_right_before_the_ai_call(db, ai, monkeypatch,
                                                                       model, reason, force):
    monkeypatch.setenv('LLM_MODEL_PHASE2', model)
    db.add_summary(5, financial_details=None)  # an older summary without details: not reused
    # The PDF does not exist either: NotReady (not 404) shows the key check comes first.
    with pytest.raises(NotReady) as exc:
        await analysis.analyze_report(row(5, file_path='missing.pdf'), force=force)
    assert (exc.value.area, exc.value.reason) == ('분석', reason)
    assert str(exc.value) == f'분석 기능을 지금 쓸 수 없습니다: {reason}'
    # the reuse check ran before (skipped with force)
    assert [q.ids for q in db.of('select')] == ([] if force else [[5]])
    assert ai.calls == [] and db.writes() == []
    assert not service._analyzing


async def test_key_added_to_env_file_counts_on_the_next_request(db, ai, storage, env_file):
    name = write_pdf(storage, 'r.pdf', ['Target 85,000 OP 2026F 1,234', 'Revision OP 1,100 to 1,234'])
    with pytest.raises(NotReady):
        await analysis.analyze_report(row(4, file_path=name))
    env_file.write_text('OPENAI_API_KEY=sk-from-env-file\n', encoding='utf-8')
    summary, reused = await analysis.analyze_report(row(4, file_path=name))
    assert reused is False and summary['report_id'] == 4 and len(ai.calls) == 1


# ── fresh analysis (text path) ───────────────────────────────────────────────

async def test_fresh_analysis_saves_todays_payload_fields(db, ai, storage, key):
    name = write_pdf(storage, 'r.pdf', ['Target 85,000 OP 2026F 1,234', 'Revision OP 1,100 to 1,234'])

    summary, reused = await analysis.analyze_report(row(11, file_path=name))

    assert reused is False
    grounded = FinancialDetails.model_validate({**FD_INPUT, 'metrics': FD_INPUT['metrics'][:1]})
    expected = {
        'financial_details': grounded.model_dump() | {'unsupported_numeric_values': 1},
        'target_price_new': 85000, 'target_price_old': 70000, 'target_price_dir': '상향',
        'recommendation': '매수', 'recommendation_dir': '유지', 'one_line_summary': '목표주가 상향',
        'positive_points': ['수주 증가'], 'risk_points': [], 'target_price_raw': '85,000원',
        'recommendation_raw': 'Buy', 'source_pages': [1, 2], 'extraction_confidence': 'high',
        'report_id': 11, 'input_truncated': False, 'input_pages_used': 2, 'input_total_pages': 2,
        'input_mode': 'text',
        'summary_version': 'llm-summary@1.0', 'llm_model': 'gpt-6-luna',
        'llm_tokens_input': 1234, 'llm_tokens_output': 567,
        'prev_report_id': None, 'prev_match_type': None, 'diff_narrative': None,
        'comparison_details': None,
    }
    assert summary == expected
    assert list(summary) == list(expected)  # same keys, same order
    upserts = db.of('upsert')
    assert [(q.payload, q.on_conflict) for q in upserts] == [(expected, 'report_id')]
    # the first analysis also clears the comparisons citing it, right before the save
    assert [q.kind for q in db.executed] == ['select', 'update', 'upsert']
    (clear,) = db.of('update')
    assert (clear.payload, clear.filters, clear.neqs) == (CLEARED, {'prev_report_id': 11},
                                                          {'report_id': 11})

    (call,) = ai.calls
    assert call['model'] == 'gpt-6-luna'
    assert call['schema'] is ExtractionResult and call['constrained'] is False
    assert 'images' not in call   # the text request is today's, unchanged
    user = call['user']
    metadata = user[user.index('<report_metadata>\n') + 18:user.index('\n</report_metadata>')]
    assert json.loads(metadata) == {'publisher': 'KB', 'stock_codes': ['016360'],
                                    'published_at': '2026-05-11', 'title': 'report 11'}
    pages = pages_of(user)
    assert pages.startswith('--- Page 1 ---\nTarget 85,000 OP 2026F 1,234\n')
    assert '\n--- Page 2 ---\nRevision OP 1,100 to 1,234\n' in pages
    assert not service._analyzing


async def test_settings_flow_into_the_call_and_the_saved_summary(db, ai, storage, monkeypatch, spy):
    monkeypatch.setenv('LLM_MODEL_PHASE2', 'claude-haiku-5-5')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test')
    monkeypatch.setenv('PHASE2_SUMMARY_VERSION', 'llm-summary@test')
    monkeypatch.setenv('PHASE2_PER_REPORT_TIMEOUT_S', '42')
    monkeypatch.setenv('PHASE2_IMAGE_TIMEOUT_S', '77')   # the image path's, not used here
    name = write_pdf(storage, 'r.pdf', ['Target 85,000 OP 2026F 1,234', 'Revision OP 1,100 to 1,234'])
    body = core_pdf.page_texts(storage / name)[0]
    assert body == f'Target 85,000 OP 2026F 1,234\n{FILLER}\n'
    first = f'--- Page 1 ---\n{body}\n'
    monkeypatch.setenv('PHASE2_MAX_INPUT_TOKENS', str(len(first) // 3 + 1))  # page 2 is left out

    summary, reused = await analysis.analyze_report(row(12, file_path=name))

    (seen,) = spy
    assert (seen['model'], seen['timeout_s']) == ('claude-haiku-5-5', 42)
    assert seen['pages_text'] == first
    assert set(seen) == {'client', 'model', 'metadata', 'pages_text', 'timeout_s'}
    assert (summary['input_truncated'], summary['input_pages_used'],
            summary['input_total_pages'], summary['input_mode']) == (True, 1, 2, 'text')
    assert (summary['summary_version'], summary['llm_model']) == ('llm-summary@test',
                                                                  'claude-haiku-5-5')
    # page 2 is not in the text, so its previous value is not supported
    assert summary['financial_details']['metrics'][0]['previous_value'] is None
    assert db.of('select')[0].filters['summary_version'] == 'llm-summary@test'


async def test_summary_without_financial_details_is_analyzed_again(db, ai, storage, key):
    db.add_summary(6, financial_details=None, diff_narrative='old pair', prev_report_id=2,
                   prev_match_type='same_publisher')
    name = write_pdf(storage, 'r.pdf', ['Target 85,000 OP 2026F 1,234'])
    summary, reused = await analysis.analyze_report(row(6, file_path=name))
    assert reused is False and len(ai.calls) == 1
    # re-analysis clears the saved comparison of this report
    assert db.rows[6]['diff_narrative'] is None and db.rows[6]['prev_report_id'] is None


@pytest.mark.parametrize('file_path,status,detail', [
    ('../outside.pdf', 404, 'PDF를 찾을 수 없습니다.'),
    ('notes.txt', 404, 'PDF를 찾을 수 없습니다.'),
    ('missing.pdf', 404, '로컬 PDF 파일이 없습니다. 수집 상태를 확인해 주세요.'),
    ('broken.pdf', 422, NO_TEXT),
    ('empty.pdf', 422, NO_TEXT),
])
async def test_pdf_problems(db, ai, storage, key, file_path, status, detail):
    (storage.parent / 'outside.pdf').write_bytes(b'%PDF-1.4')
    (storage / 'notes.txt').write_text('x', encoding='utf-8')
    (storage / 'broken.pdf').write_bytes(b'not a pdf')
    (storage / 'empty.pdf').write_bytes(b'')
    with pytest.raises(HTTPException) as exc:
        await analysis.analyze_report(row(13, file_path=file_path))
    assert (exc.value.status_code, exc.value.detail) == (status, detail)
    assert ai.calls == [] and db.writes() == []
    assert not service._analyzing


async def test_no_page_fits_the_cap_is_no_text(db, ai, storage, monkeypatch, key):
    monkeypatch.setenv('PHASE2_MAX_INPUT_TOKENS', '1')
    name = write_pdf(storage, 'r.pdf', ['Target 85,000'])
    with pytest.raises(HTTPException) as exc:
        await analysis.analyze_report(row(14, file_path=name))
    assert (exc.value.status_code, exc.value.detail) == (422, NO_TEXT)
    assert ai.calls == [] and db.writes() == []


async def test_ai_failure_is_that_requests_error(db, ai, storage, key):
    saved = db.add_summary(15, financial_details=None)
    name = write_pdf(storage, 'r.pdf', ['Target 85,000'])
    ai.error = RuntimeError('provider rejected the key')
    with pytest.raises(RuntimeError, match='provider rejected'):
        await analysis.analyze_report(row(15, file_path=name))
    assert db.rows[15] == saved and db.writes() == []
    assert not service._analyzing


async def test_refusal_is_an_error_and_nothing_is_saved(db, ai, storage, key):
    name = write_pdf(storage, 'r.pdf', ['Target 85,000'])
    ai.reply = None
    with pytest.raises(RuntimeError, match='거부'):
        await analysis.analyze_report(row(16, file_path=name))
    assert db.writes() == []


async def test_the_empty_image_rule_is_not_applied_to_the_text_path(db, ai, storage, key):
    name = write_pdf(storage, 'r.pdf', ['Target 85,000'])
    ai.reply = empty_answer()
    summary, reused = await analysis.analyze_report(row(18, file_path=name))
    assert reused is False and summary['input_mode'] == 'text'
    assert summary['extraction_confidence'] == 'low' and db.rows[18] == summary


# ── the text-or-image decision ───────────────────────────────────────────────

@pytest.mark.parametrize('pages,mode', [
    ([(0, 300)], 'text'),          # exactly at the Latin limit
    ([(0, 299)], 'page_image'),    # with the page markers' letters it would be 303: raw text decides
    ([(50, 0)], 'text'),           # exactly at the Hangul limit
    ([(49, 0)], 'page_image'),
    ([(49, 299)], 'page_image'),   # both under
    ([(0, 600), (0, 0)], 'text'),        # an average over every page, empty pages count
    ([(0, 599), (0, 0)], 'page_image'),
])
async def test_the_decision_reads_every_pages_raw_text(db, ai, storage, key, pages, mode):
    name = write_counted_pdf(storage, 'r.pdf', pages)
    summary, _ = await analysis.analyze_report(row(19, file_path=name))
    (call,) = ai.calls
    assert summary['input_mode'] == mode
    assert ('images' in call) is (mode == 'page_image')
    assert summary['input_total_pages'] == len(pages)


async def test_the_decision_reads_pages_past_20_and_past_the_token_cap(db, ai, storage, monkeypatch,
                                                                       key):
    # 20 pages without letters, then 5 pages of 1500: an average of 300 a page
    name = write_counted_pdf(storage, 'r.pdf', [(0, 0)] * 20 + [(0, 1500)] * 5)
    monkeypatch.setenv('PHASE2_MAX_INPUT_TOKENS', '60')   # only the first pages are sent
    summary, _ = await analysis.analyze_report(row(20, file_path=name))
    assert summary['input_mode'] == 'text'
    assert (summary['input_truncated'], summary['input_total_pages']) == (True, 25)
    assert summary['input_pages_used'] < 20
    assert 'images' not in ai.calls[0]


# ── the image path ───────────────────────────────────────────────────────────

async def test_image_analysis_request_and_saved_summary(db, ai, storage, key, spy):
    name = write_image_pdf(storage, 'scan.pdf', 3)
    path = storage / name

    summary, reused = await analysis.analyze_report(row(41, file_path=name))

    expected = {
        # no number grounding: both metrics and the previous value stay, 0 unsupported
        'financial_details': (FinancialDetails.model_validate(FD_INPUT).model_dump()
                              | {'unsupported_numeric_values': 0}),
        'target_price_new': 85000, 'target_price_old': 70000, 'target_price_dir': '상향',
        'recommendation': '매수', 'recommendation_dir': '유지', 'one_line_summary': '목표주가 상향',
        'positive_points': ['수주 증가'], 'risk_points': [], 'target_price_raw': '85,000원',
        'recommendation_raw': 'Buy', 'source_pages': [1, 2],
        'extraction_confidence': 'medium',   # high from the model, at most medium from pictures
        'report_id': 41, 'input_truncated': False, 'input_pages_used': 3, 'input_total_pages': 3,
        'input_mode': 'page_image',
        'summary_version': 'llm-summary@1.0', 'llm_model': 'gpt-6-luna',
        'llm_tokens_input': 1234, 'llm_tokens_output': 567,
        'prev_report_id': None, 'prev_match_type': None, 'diff_narrative': None,
        'comparison_details': None,
    }
    assert reused is False
    assert summary == expected and list(summary) == list(expected)
    assert [q.kind for q in db.executed] == ['select', 'update', 'upsert']
    assert db.of('update')[0].filters == {'prev_report_id': 41}
    assert db.of('upsert')[0].payload == expected

    (call,) = ai.calls
    metadata = {**METADATA, 'title': 'report 41'}
    system, user = render_page_image_messages(metadata, [1, 2, 3])
    assert call['user'] == user['content']
    # the system message is the text path's, to the letter
    assert call['system'] == system['content'] == render_extraction_messages(metadata, 'x')[0]['content']
    assert call['schema'] is ExtractionResult and call['constrained'] is False
    assert call['images'] == [core_pdf.render_page_png(path, n) for n in (1, 2, 3)]   # 120 dpi
    (seen,) = spy
    assert seen['timeout_s'] == 360   # PHASE2_IMAGE_TIMEOUT_S default
    assert not service._analyzing


async def test_image_analysis_uses_its_own_time_limit(db, ai, storage, monkeypatch, key, spy):
    monkeypatch.setenv('PHASE2_IMAGE_TIMEOUT_S', '77')
    monkeypatch.setenv('PHASE2_PER_REPORT_TIMEOUT_S', '42')
    await analysis.analyze_report(row(42, file_path=write_image_pdf(storage, 'scan.pdf', 1)))
    await analysis.analyze_report(row(43, file_path=write_pdf(storage, 'text.pdf', ['Target 85,000'])))
    assert [s['timeout_s'] for s in spy] == [77, 42]


async def test_image_analysis_sends_the_first_20_pages(db, ai, storage, key):
    name = write_image_pdf(storage, 'scan.pdf', 25)
    summary, _ = await analysis.analyze_report(row(44, file_path=name))
    (call,) = ai.calls
    assert len(call['images']) == 20
    assert call['user'] == render_page_image_messages({**METADATA, 'title': 'report 44'},
                                                      list(range(1, 21)))[1]['content']
    assert (summary['input_truncated'], summary['input_pages_used'],
            summary['input_total_pages']) == (True, 20, 25)


async def test_undrawable_pages_are_left_out_and_the_rest_keep_their_numbers(db, ai, storage,
                                                                             monkeypatch, key):
    name = write_image_pdf(storage, 'scan.pdf', 4)
    expected_images = [core_pdf.render_page_png(storage / name, n) for n in (1, 3, 4)]
    fail_drawing(monkeypatch, lambda n: n == 2)
    summary, _ = await analysis.analyze_report(row(45, file_path=name))
    (call,) = ai.calls
    assert call['images'] == expected_images
    assert call['user'] == render_page_image_messages({**METADATA, 'title': 'report 45'},
                                                      [1, 3, 4])[1]['content']
    assert (summary['input_truncated'], summary['input_pages_used'],
            summary['input_total_pages']) == (True, 3, 4)


async def test_no_drawable_page_is_no_text_without_the_ai(db, ai, storage, monkeypatch, key):
    saved = db.add_summary(46, financial_details=None)
    name = write_image_pdf(storage, 'scan.pdf', 3)
    drawn = fail_drawing(monkeypatch, lambda n: True)
    with pytest.raises(HTTPException) as exc:
        await analysis.analyze_report(row(46, file_path=name))
    assert (exc.value.status_code, exc.value.detail) == (422, NO_TEXT)
    assert drawn and ai.calls == [] and db.writes() == [] and db.rows[46] == saved
    assert not service._analyzing


async def test_a_model_without_images_is_no_text_without_the_ai(db, ai, storage, monkeypatch, key):
    monkeypatch.setenv('LLM_MODEL_PHASE2', 'gpt-4')   # takes text only
    assert not core_llm.supports_images('gpt-4')
    name = write_image_pdf(storage, 'scan.pdf', 2)
    drawn = fail_drawing(monkeypatch, lambda n: False)
    with pytest.raises(HTTPException) as exc:
        await analysis.analyze_report(row(47, file_path=name))
    assert (exc.value.status_code, exc.value.detail) == (422, NO_TEXT)
    assert drawn == [] and ai.calls == [] and db.writes() == []   # not even drawn


@pytest.mark.parametrize('given,saved', [('high', 'medium'), ('medium', 'medium'), ('low', 'low')])
async def test_image_confidence_is_at_most_medium(db, ai, storage, key, given, saved):
    ai.reply = extraction(extraction_confidence=given)
    summary, _ = await analysis.analyze_report(
        row(48, file_path=write_image_pdf(storage, 'scan.pdf', 1)))
    assert summary['extraction_confidence'] == saved


async def test_an_image_answer_without_financial_details_saves_null(db, ai, storage, key):
    ai.reply = extraction(financial_details=None)
    summary, _ = await analysis.analyze_report(
        row(49, file_path=write_image_pdf(storage, 'scan.pdf', 1)))
    assert summary['financial_details'] is None and db.rows[49]['financial_details'] is None


@pytest.mark.parametrize('financial_details', [None, FD_NO_METRICS])
async def test_an_empty_image_answer_is_422_and_changes_nothing(db, ai, storage, key,
                                                                financial_details):
    saved = db.add_summary(50, financial_details=None)
    citing = db.add_summary(51, prev_report_id=50, prev_match_type='same_publisher',
                            diff_narrative='kept')
    ai.reply = empty_answer(financial_details=financial_details)
    with pytest.raises(HTTPException) as exc:
        await analysis.analyze_report(row(50, file_path=write_image_pdf(storage, 'scan.pdf', 2)))
    assert (exc.value.status_code, exc.value.detail) == (422, EMPTY_IMAGE)
    assert len(ai.calls) == 1 and db.writes() == []
    assert db.rows[50] == saved and db.rows[51] == citing
    assert not service._analyzing


@pytest.mark.parametrize('changes', [
    {'target_price_new': 85000, 'target_price_raw': '85,000원'},
    {'recommendation': '중립'},
    {'financial_details': FD_INPUT},
    {'extraction_confidence': 'medium'},
], ids=['target price', 'rating', 'metrics', 'confidence'])
async def test_an_image_answer_with_anything_in_it_is_saved(db, ai, storage, key, changes):
    ai.reply = empty_answer(**changes)
    summary, _ = await analysis.analyze_report(
        row(52, file_path=write_image_pdf(storage, 'scan.pdf', 1)))
    assert summary['input_mode'] == 'page_image' and db.rows[52] == summary


async def test_an_image_summary_with_details_is_reused_later(db, ai, storage, key):
    name = write_image_pdf(storage, 'scan.pdf', 1)
    first, _ = await analysis.analyze_report(row(53, file_path=name))
    again, reused = await analysis.analyze_report(row(53, file_path=name))
    assert reused is True and again == first and len(ai.calls) == 1


# ── retries and time limits ──────────────────────────────────────────────────

async def test_an_image_timeout_is_not_tried_again(db, ai, storage, key, sleeps):
    saved = db.add_summary(54, financial_details=None)
    ai.error = TimeoutError()
    with pytest.raises(TimeoutError) as exc:
        await analysis.analyze_report(row(54, file_path=write_image_pdf(storage, 'scan.pdf', 1)))
    # neither NotReady nor an HTTP answer: the web app's general 503
    assert not isinstance(exc.value, (HTTPException, NotReady))
    assert len(ai.calls) == 1 and sleeps == []
    assert db.writes() == [] and db.rows[54] == saved
    assert not service._analyzing


async def test_an_image_analysis_tries_other_transient_errors_once_more(db, ai, storage, key,
                                                                        sleeps):
    ai.errors = [TransientLLMError('429')]
    summary, _ = await analysis.analyze_report(
        row(55, file_path=write_image_pdf(storage, 'scan.pdf', 1)))
    assert len(ai.calls) == 2 and sleeps == [5.0]
    assert summary['input_mode'] == 'page_image'


async def test_an_image_analysis_stops_after_a_second_transient_error(db, ai, storage, key, sleeps):
    ai.error = TransientLLMError('503')
    with pytest.raises(TransientLLMError):
        await analysis.analyze_report(row(56, file_path=write_image_pdf(storage, 'scan.pdf', 1)))
    assert len(ai.calls) == 2 and sleeps == [5.0] and db.writes() == []


async def test_a_text_timeout_is_still_tried_again(db, ai, storage, key, sleeps):
    ai.errors = [TimeoutError()]
    summary, _ = await analysis.analyze_report(
        row(57, file_path=write_pdf(storage, 'r.pdf', ['Target 85,000'])))
    assert len(ai.calls) == 2 and sleeps == [5.0]
    assert summary['input_mode'] == 'text'


# ── force: analyze again ─────────────────────────────────────────────────────

async def test_force_analyzes_again_despite_a_saved_summary(db, ai, storage, key):
    db.add_summary(58, input_mode='text')
    name = write_pdf(storage, 'r.pdf', ['Target 85,000'])
    summary, reused = await analysis.analyze_report(row(58, file_path=name), force=True)
    assert reused is False and len(ai.calls) == 1
    assert db.rows[58] == summary and summary['one_line_summary'] == '목표주가 상향'
    assert db.of('select') == []   # the reuse check is skipped


async def test_analyzing_again_switches_the_input_mode_either_way(db, ai, storage, key):
    text = storage / write_pdf(storage, 'text.pdf', ['Target 85,000'])
    image = storage / write_image_pdf(storage, 'image.pdf', 2)
    modes = []
    for source in (text, image, text):
        (storage / 'r.pdf').write_bytes(source.read_bytes())
        summary, _ = await analysis.analyze_report(row(59), force=True)
        modes.append((summary['input_mode'], db.rows[59]['input_mode']))
    assert modes == [('text', 'text'), ('page_image', 'page_image'), ('text', 'text')]


@pytest.mark.parametrize('case', ['not company', 'no key', 'no pdf', 'unreadable', 'ai error',
                                  'no image model', 'undrawable', 'timeout', 'empty image'])
async def test_a_failed_forced_analysis_changes_nothing(db, ai, storage, monkeypatch, key,
                                                        sleeps, case):
    saved = db.add_summary(60, input_mode='text')
    citing = db.add_summary(61, prev_report_id=60, prev_match_type='same_publisher',
                            diff_narrative='kept')
    (storage / 'broken.pdf').write_bytes(b'not a pdf')
    text = write_pdf(storage, 'text.pdf', ['Target 85,000'])
    image = write_image_pdf(storage, 'image.pdf', 2)
    target = row(60, file_path=image)
    if case == 'not company':
        target = row(60, report_type='산업', file_path=text)
    elif case == 'no key':
        monkeypatch.delenv('OPENAI_API_KEY')
    elif case == 'no pdf':
        target = row(60, file_path='missing.pdf')
    elif case == 'unreadable':
        target = row(60, file_path='broken.pdf')
    elif case == 'ai error':
        target, ai.error = row(60, file_path=text), RuntimeError('provider down')
    elif case == 'no image model':
        monkeypatch.setenv('LLM_MODEL_PHASE2', 'gpt-4')
    elif case == 'undrawable':
        fail_drawing(monkeypatch, lambda n: True)
    elif case == 'timeout':
        ai.error = TimeoutError()
    else:
        ai.reply = empty_answer()
    with pytest.raises((HTTPException, NotReady, RuntimeError, TimeoutError)):
        await analysis.analyze_report(target, force=True)
    assert db.writes() == []
    assert db.rows[60] == saved and db.rows[61] == citing
    assert not service._analyzing


async def test_force_without_db_settings_is_not_ready_before_the_ai(ai, storage, key):
    name = write_pdf(storage, 'r.pdf', ['Target 85,000'])
    with pytest.raises(NotReady) as exc:
        await analysis.analyze_report(row(62, file_path=name), force=True)
    assert (exc.value.area, exc.value.reason) == ('분석', DB_MISSING)
    assert ai.calls == []


# ── saving clears the comparisons citing the report ──────────────────────────

async def test_saving_clears_the_comparisons_that_cite_the_report_first(db, ai, storage, key):
    db.add_summary(70, financial_details=None, prev_report_id=69, prev_match_type='same_publisher',
                   diff_narrative='70 after 69')
    db.add_summary(71, prev_report_id=70, prev_match_type='same_publisher',
                   diff_narrative='71 after 70', comparison_details={'metrics': []})
    db.add_summary(72, prev_report_id=70, prev_match_type='cross_publisher',
                   diff_narrative='72 after 70')
    other = db.add_summary(73, prev_report_id=69, prev_match_type='same_publisher',
                           diff_narrative='73 after 69')
    name = write_pdf(storage, 'r.pdf', ['Target 85,000'])

    summary, _ = await analysis.analyze_report(row(70, file_path=name))

    for rid in (71, 72):   # their narratives were written against 70's old result
        assert {k: db.rows[rid].get(k) for k in CLEARED} == CLEARED
        assert db.rows[rid]['one_line_summary'] == f'saved {rid}'   # the analysis stays
    assert db.rows[73] == other
    assert db.rows[70] == summary and summary['prev_report_id'] is None
    assert [q.kind for q in db.writes()] == ['update', 'upsert']


async def test_a_failed_save_keeps_the_clear(db, ai, storage, key):
    db.add_summary(74, prev_report_id=75, prev_match_type='same_publisher', diff_narrative='x')
    db.fail['upsert'] = RuntimeError('db down')
    with pytest.raises(RuntimeError, match='db down'):
        await analysis.analyze_report(row(75, file_path=write_pdf(storage, 'r.pdf', ['T 85,000'])))
    assert db.rows[74]['diff_narrative'] is None   # not put back
    assert 75 not in db.rows and not service._analyzing


# ── delete ───────────────────────────────────────────────────────────────────

async def test_delete_clears_the_citing_comparisons_then_deletes_the_row(db, ai):
    db.add_summary(80, summary_version='llm-summary@0.9')   # any version goes
    db.add_summary(81, prev_report_id=80, prev_match_type='same_publisher', diff_narrative='x')
    other = db.add_summary(82, prev_report_id=79, prev_match_type='same_publisher',
                           diff_narrative='y')

    assert await analysis.delete_summary(80) is None

    assert 80 not in db.rows and db.rows[82] == other
    assert {k: db.rows[81].get(k) for k in CLEARED} == CLEARED
    clear, delete = db.writes()
    assert (clear.kind, clear.payload, clear.filters, clear.neqs) == (
        'update', CLEARED, {'prev_report_id': 80}, {'report_id': 80})
    assert (delete.kind, delete.filters, delete.neqs) == ('delete', {'report_id': 80}, {})
    assert db.of('select') == [] and ai.calls == []
    assert not service._analyzing


async def test_deleting_nothing_or_twice_is_fine(db):
    db.add_summary(83)
    await analysis.delete_summary(83)
    await analysis.delete_summary(83)
    await analysis.delete_summary(84)   # never analyzed
    assert db.rows == {} and [q.kind for q in db.writes()] == ['update', 'delete'] * 3


async def test_delete_needs_no_model_key_and_no_ai_slot(db, monkeypatch):
    monkeypatch.setenv('LLM_MODEL_PHASE2', 'claude-haiku-5-5')   # its key is not set
    db.add_summary(85)
    async with analysis.ai_slot(), analysis.ai_slot():   # both AI slots busy
        await asyncio.wait_for(analysis.delete_summary(85), LIMIT_S)
    assert 85 not in db.rows


async def test_delete_without_db_settings_is_not_ready(monkeypatch):
    monkeypatch.setattr(core_db, 'supabase_client', lambda url, key: FakeSupabase())
    with pytest.raises(NotReady) as exc:
        await analysis.delete_summary(86)
    assert (exc.value.area, exc.value.reason) == ('분석', DB_MISSING)
    assert not service._analyzing


async def test_a_failed_delete_keeps_the_clear_and_unmarks(db):
    db.add_summary(87)
    db.add_summary(88, prev_report_id=87, prev_match_type='same_publisher', diff_narrative='x')
    db.fail['delete'] = RuntimeError('db down')
    with pytest.raises(RuntimeError, match='db down'):
        await analysis.delete_summary(87)
    assert db.rows[88]['diff_narrative'] is None and 87 in db.rows
    assert not service._analyzing


# ── one busy marker: analysis, delete, hold_reports ──────────────────────────

async def test_analysis_and_delete_refuse_each_other(db, ai, storage, key):
    name = write_pdf(storage, 'r.pdf', ['Target 85,000'])
    # an analysis calling the AI: delete and a hold of the same report are refused
    ai.gate = asyncio.Event()
    running = asyncio.create_task(analysis.analyze_report(row(90, file_path=name)))
    await until(lambda: len(ai.calls) == 1)
    with pytest.raises(HTTPException) as exc:
        await asyncio.wait_for(analysis.delete_summary(90), LIMIT_S)
    assert (exc.value.status_code, exc.value.detail) == (409, IN_FLIGHT)
    assert db.writes() == []
    ai.gate.set()
    await asyncio.wait_for(running, LIMIT_S)

    # a delete in progress: analysis (and another delete) of the same report are refused
    db.hold_kind = 'update'
    deleting = asyncio.create_task(analysis.delete_summary(90))
    await until(lambda: db.holding)
    assert 90 in service._analyzing
    for request in (analysis.analyze_report(row(90, file_path=name)),
                    analysis.analyze_report(row(90, file_path=name), force=True),
                    analysis.delete_summary(90)):
        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(request, LIMIT_S)
        assert (exc.value.status_code, exc.value.detail) == (409, IN_FLIGHT)
    db.hold.set()
    await asyncio.wait_for(deleting, LIMIT_S)
    assert 90 not in db.rows and not service._analyzing and len(ai.calls) == 1


async def test_hold_reports_marks_its_reports_for_the_block():
    service._analyzing.add(5)   # someone else's
    async with analysis.hold_reports([1, 2], 'busy'):
        assert service._analyzing == {1, 2, 5}
    assert service._analyzing == {5}   # only its own are unmarked
    with pytest.raises(ValueError):
        async with analysis.hold_reports([1, 2], 'busy'):
            raise ValueError('boom')
    assert service._analyzing == {5}


async def test_hold_reports_refuses_when_one_is_busy_and_marks_nothing():
    service._analyzing.add(2)
    with pytest.raises(HTTPException) as exc:
        async with analysis.hold_reports([1, 2, 3], '비교 중입니다.'):
            raise AssertionError('not entered')
    assert (exc.value.status_code, exc.value.detail) == (409, '비교 중입니다.')
    assert service._analyzing == {2}


async def test_held_reports_refuse_analysis_and_delete(db, ai):
    db.add_summary(91)
    async with analysis.hold_reports([91, 92], '비교 중입니다.'):
        for request in (analysis.analyze_report(row(91)), analysis.analyze_report(row(92)),
                        analysis.delete_summary(91)):
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(request, LIMIT_S)
            assert (exc.value.status_code, exc.value.detail) == (409, IN_FLIGHT)
    assert db.executed == [] and ai.calls == []
    assert (await analysis.analyze_report(row(91)))[1] is True   # free again


async def test_a_running_analysis_or_delete_refuses_a_hold(db, ai, storage, key):
    ai.gate = asyncio.Event()
    running = asyncio.create_task(
        analysis.analyze_report(row(93, file_path=write_pdf(storage, 'r.pdf', ['Target 85,000']))))
    await until(lambda: len(ai.calls) == 1)
    db.hold_kind = 'update'
    deleting = asyncio.create_task(analysis.delete_summary(94))
    await until(lambda: db.holding)
    for ids in ([93], [94], [95, 93]):
        with pytest.raises(HTTPException) as exc:
            async with analysis.hold_reports(ids, '비교 중입니다.'):
                raise AssertionError('not entered')
        assert (exc.value.status_code, exc.value.detail) == (409, '비교 중입니다.')
    assert service._analyzing == {93, 94}   # 95 was not marked
    ai.gate.set()
    db.hold.set()
    await asyncio.wait_for(asyncio.gather(running, deleting), LIMIT_S)
    assert not service._analyzing


# ── AI slot: 2 at a time, shared with compare ────────────────────────────────

async def test_ai_slot_allows_two_calls_at_a_time():
    inside = peak = 0
    entered = []
    release = asyncio.Event()

    async def call(i):
        nonlocal inside, peak
        async with analysis.ai_slot():
            inside += 1
            peak = max(peak, inside)
            entered.append(i)
            await release.wait()
            inside -= 1

    tasks = [asyncio.create_task(call(i)) for i in range(5)]
    await until(lambda: len(entered) == 2)
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(entered) == 2  # the other three wait for a free slot
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), LIMIT_S)
    assert peak == 2 and sorted(entered) == [0, 1, 2, 3, 4]


async def test_ai_slot_is_released_on_error():
    for _ in range(3):
        with pytest.raises(ValueError):
            async with analysis.ai_slot():
                raise ValueError('boom')
    async with asyncio.timeout(LIMIT_S):  # a leaked slot would block here
        async with analysis.ai_slot(), analysis.ai_slot():
            pass  # both slots are free again


async def test_analysis_waits_while_two_ai_calls_hold_the_slots(db, ai, storage, key):
    name = write_pdf(storage, 'r.pdf', ['Target 85,000 OP 2026F 1,234'])
    release = asyncio.Event()
    holding = []

    async def other_ai_call():  # e.g. compare writing a narrative
        async with analysis.ai_slot():
            holding.append(1)
            await release.wait()

    holders = [asyncio.create_task(other_ai_call()) for _ in range(2)]
    await until(lambda: len(holding) == 2)
    task = asyncio.create_task(analysis.analyze_report(row(17, file_path=name)))
    await until(lambda: 17 in service._analyzing)
    for _ in range(20):
        await asyncio.sleep(0)
    # like before, the whole analysis after the 409 check runs inside a slot
    assert not task.done() and ai.calls == [] and db.executed == []
    release.set()
    summary, reused = await asyncio.wait_for(task, LIMIT_S)
    assert reused is False and len(ai.calls) == 1
    await asyncio.wait_for(asyncio.gather(*holders), LIMIT_S)


async def test_image_analysis_also_runs_inside_a_slot(db, ai, storage, key):
    name = write_image_pdf(storage, 'scan.pdf', 1)
    async with analysis.ai_slot(), analysis.ai_slot():
        task = asyncio.create_task(analysis.analyze_report(row(18, file_path=name)))
        await until(lambda: 18 in service._analyzing)
        for _ in range(20):
            await asyncio.sleep(0)
        assert not task.done() and ai.calls == []
    summary, _ = await asyncio.wait_for(task, LIMIT_S)
    assert summary['input_mode'] == 'page_image'


async def test_two_analyses_hold_both_slots(db, ai, storage, key):
    name = write_pdf(storage, 'r.pdf', ['Target 85,000 OP 2026F 1,234'])
    ai.gate = asyncio.Event()
    running = [asyncio.create_task(analysis.analyze_report(row(rid, file_path=name)))
               for rid in (21, 22)]
    await until(lambda: len(ai.calls) == 2)
    entered = []

    async def compare_like_call():
        async with analysis.ai_slot():
            entered.append(1)

    waiting = asyncio.create_task(compare_like_call())
    for _ in range(20):
        await asyncio.sleep(0)
    assert entered == []  # waits while two analyses are calling the AI
    ai.gate.set()
    await asyncio.wait_for(asyncio.gather(*running, waiting), LIMIT_S)
    assert entered == [1]


def test_ai_slot_works_in_each_event_loop():
    # Created lazily for the running loop: contention in a second loop must not
    # fail with "bound to a different event loop".
    async def scenario():
        inside = peak = 0

        async def call():
            nonlocal inside, peak
            async with analysis.ai_slot():
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.01)
                inside -= 1

        await asyncio.gather(*(call() for _ in range(4)))
        return peak

    assert asyncio.run(scenario()) == 2
    assert asyncio.run(scenario()) == 2


# ── summaries_for / save_comparison / DB readiness ───────────────────────────

def test_summaries_for_reads_the_active_version_in_batches_of_100(db, monkeypatch):
    for rid in range(1, 251):
        db.add_summary(rid)
    db.add_summary(999, summary_version='llm-summary@0.9')

    result = analysis.summaries_for(list(range(1, 251)) + [999])

    assert sorted(result) == list(range(1, 251))  # another version is not shown
    assert result[1]['one_line_summary'] == 'saved 1'
    assert [len(q.ids) for q in db.of('select')] == [100, 100, 51]
    assert {q.filters['summary_version'] for q in db.of('select')} == {'llm-summary@1.0'}

    monkeypatch.setenv('PHASE2_SUMMARY_VERSION', 'llm-summary@0.9')
    assert list(analysis.summaries_for([999, 1])) == [999]


def test_summaries_for_nothing_needs_no_db():
    assert analysis.summaries_for([]) == {}


def test_save_comparison_writes_the_four_comparison_fields(db):
    db.add_summary(3, one_line_summary='kept')
    details = {'metrics': [{'previous': 100, 'current': 120}], 'previous_publisher': 'KB',
               'previous_published_at': '2026-01-01'}
    assert analysis.save_comparison(3, 2, 'same_publisher', '전망 상향', details) is None
    (update,) = db.of('update')
    assert update.payload == {'prev_report_id': 2, 'prev_match_type': 'same_publisher',
                              'diff_narrative': '전망 상향', 'comparison_details': details}
    assert update.filters == {'report_id': 3}
    assert db.rows[3]['one_line_summary'] == 'kept'


def test_missing_db_settings_is_not_ready_until_fixed(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(core_db, 'supabase_client', lambda url, key: fake)
    for call in (lambda: analysis.summaries_for([1]),
                 lambda: analysis.save_comparison(3, 2, 'same_publisher', 'x', {})):
        with pytest.raises(NotReady) as exc:
            call()
        assert (exc.value.area, exc.value.reason) == ('분석', DB_MISSING)
    # fixed without a restart: the next call prepares again
    monkeypatch.setenv('SUPABASE_URL', 'https://example.supabase.test')
    monkeypatch.setenv('SUPABASE_SERVICE_KEY', 'service-key')
    assert analysis.summaries_for([1]) == {}


def test_db_preparation_reads_env_file_once(env_file, monkeypatch):
    made = []

    def supabase_client(url, key):
        made.append((url, key))
        return FakeSupabase()

    monkeypatch.setattr(core_db, 'supabase_client', supabase_client)
    env_file.write_text('SUPABASE_URL=https://from-env-file.test\nSUPABASE_SERVICE_KEY=k\n',
                        encoding='utf-8')
    assert analysis.summaries_for([1]) == {}
    analysis.summaries_for([2])
    analysis.save_comparison(2, 1, 'cross_publisher', None, None)
    assert made == [('https://from-env-file.test', 'k')]  # prepared once, then reused
