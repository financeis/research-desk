"""Report analysis: analyze one selected report or delete its result, read summaries, save
comparisons, and the web server's busy-report marker.

``analyze_report(row, force=False)`` gets a row the reports feature has already read (its
404). Then, in this order — a step that stops ends the request, nothing after it happens:

1. the report is busy (being analyzed or deleted, or held by ``hold_reports``) → 409, at
   once, without waiting for an AI slot;
2. inside an AI slot (like before, the rest of the analysis runs in one):
   not 단일종목 → 422;
3. a saved summary (active version) with financial_details → returned as is
   (``reused=True``). ``force`` skips only this reuse; the DB is still prepared here, so a
   missing DB setting stops the request before the AI as it does without ``force``;
4. model key / codex CLI (``phase2_llm``) → ``NotReady("분석", …)``;
5. PDF in the storage folder (404); the text of every page, read once (422 when the file
   cannot be opened or has no pages); one decision for the whole document
   (``logic.needs_page_images``):
   - text path (as before): page-marked text within the token cap (422 when empty), the
     AI call, arithmetic target-price direction, number grounding;
   - image path: a model that cannot take images → 422 without the AI; pages
     1..``PAGE_IMAGE_MAX_PAGES`` drawn (undrawable pages left out, none drawn → 422); the AI
     call with the pictures (``PHASE2_IMAGE_TIMEOUT_S``, a timeout is not tried again); an
     empty answer → 422; arithmetic target-price direction; confidence at most medium; no
     grounding (``unsupported_numeric_values`` 0);
6. save: first the comparisons of other reports that cite this one as the earlier report
   are cleared, then this report's summary is upserted (its own comparison fields NULL,
   ``input_mode`` 'text' or 'page_image') → ``(payload, False)``.

A failure changes nothing: AI error, timeout, empty image answer, model without images,
undrawable PDF, PDF or key problem. Only a save failing after the clear leaves the clear.

``delete_summary(rid)``: busy → 409; marked busy while it clears the comparisons citing the
report, then deletes its row (any version; nothing to delete is fine). No AI slot, no
model key, any report type.

``hold_reports(ids, busy_detail)``: marks reports busy for a block (compare's narrative);
409 ``busy_detail`` when one already is, and then none is marked.

So reuse, delete, summaries_for, 409 and 422 never need a model key.

``ai_slot()`` limits the web server to 2 AI calls at once, analysis and compare
together. The Supabase REST client is prepared on first use (re-reading
``.env``); a failed preparation is tried again on the next call.
"""
from __future__ import annotations

import asyncio
import threading
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Mapping, Optional

from fastapi import HTTPException

from research_desk.core import db, pdf
from research_desk.core import settings as core_settings
from research_desk.core.llm import LLMClient, api_key_env, supports_images
from research_desk.core.settings import NotReady

from . import store
from .financials import ground_metrics
from .llm import extract_one
from .logic import (cap_image_confidence, is_empty_image_result, needs_page_images,
                    normalize_target_price_dir)
from .pdf_text import build_pages_text
from .settings import AnalysisSettings, load_settings, summary_version

AREA = "분석"

IN_FLIGHT = "이 보고서는 분석 중입니다. 잠시 후 새로고침해 주세요."
NOT_SINGLE_COMPANY = "금융 정보 분석은 단일종목 보고서를 선택해 주세요."
NO_TEXT = "PDF에서 읽을 수 있는 텍스트가 없습니다."
EMPTY_IMAGE_RESULT = "그림으로도 리포트 내용을 읽지 못했습니다."
DB_NOT_CONFIGURED = "DB 접속 설정(SUPABASE_URL, SUPABASE_SERVICE_KEY)이 없습니다"
CODEX_MISSING = "codex CLI를 찾을 수 없습니다. 설치 후 `codex login`으로 로그인하세요."

# The local web server is one process; at most this many AI calls at once.
AI_CONCURRENCY = 2
SUMMARY_BATCH = 100
METADATA_KEYS = ("publisher", "stock_codes", "published_at", "title")

# Image path: pages 1..this many are drawn and sent (adjustable).
PAGE_IMAGE_MAX_PAGES = 20
INPUT_TEXT = "text"
INPUT_PAGE_IMAGE = "page_image"


def _key_missing(env: str) -> str:
    return f"{env}가 설정되지 않았습니다. .env에 추가 후 분석 다시 시도하세요."


# ── AI slot ──────────────────────────────────────────────────────────────────

# One semaphore per running event loop, made on first use inside that loop: an
# asyncio.Semaphore cannot be shared across loops (the server has one loop).
_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary())
_slots_lock = threading.Lock()


def _slot_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _slots_lock:
        semaphore = _slots.get(loop)
        if semaphore is None:
            semaphore = _slots[loop] = asyncio.Semaphore(AI_CONCURRENCY)
        return semaphore


@asynccontextmanager
async def ai_slot() -> AsyncIterator[None]:
    """Hold one of the web server's 2 AI-call slots (shared by analysis and compare)."""
    async with _slot_semaphore():
        yield


# ── busy reports ─────────────────────────────────────────────────────────────

# Reports busy in this web server: being analyzed or deleted, or held by a comparison
# narrative. Only touched on the event loop; a check and its marking have no await
# between them.
_analyzing: set[int] = set()


@asynccontextmanager
async def hold_reports(ids: Iterable[int], busy_detail: str) -> AsyncIterator[None]:
    """Mark ``ids`` busy for the block; unmarked when it ends, whatever the outcome.

    One of them busy already → ``HTTPException(409, busy_detail)`` and none is marked.
    On the way out only the ids marked here are unmarked.
    """
    ids = list(dict.fromkeys(ids))
    if any(rid in _analyzing for rid in ids):
        raise HTTPException(409, busy_detail)
    _analyzing.update(ids)
    try:
        yield
    finally:
        _analyzing.difference_update(ids)


# ── model connection ─────────────────────────────────────────────────────────

def phase2_llm() -> tuple[LLMClient, str, int]:
    """``(AI client, model name, per-call timeout in seconds)`` for an AI call now.

    Re-reads ``.env`` first, so a key added there counts without a restart.
    Raises NotReady("분석", …) when the model's API key or the codex CLI is
    missing. The client keeps the SDK's default retries; use it as
    ``async with client:`` so it is closed afterwards.
    """
    core_settings.load_env()
    cfg = load_settings()
    client = LLMClient(openai_api_key=core_settings.openai_api_key(),
                       anthropic_api_key=core_settings.anthropic_api_key())
    try:
        client.require_key(cfg.llm_model)
    except RuntimeError:
        env = api_key_env(cfg.llm_model)
        if env is None:
            raise NotReady(AREA, CODEX_MISSING) from None
        raise NotReady(AREA, _key_missing(env)) from None
    return client, cfg.llm_model, cfg.per_report_timeout_s


# ── report_summaries ─────────────────────────────────────────────────────────

_supabase_client: Optional[Any] = None
_supabase_lock = threading.Lock()


def _supabase():
    """The Supabase REST client, prepared on first use (NotReady without DB settings)."""
    global _supabase_client
    with _supabase_lock:
        if _supabase_client is None:
            core_settings.load_env()
            url, key = core_settings.supabase_url(), core_settings.supabase_service_key()
            if not (url and key):
                raise NotReady(AREA, DB_NOT_CONFIGURED)
            _supabase_client = db.supabase_client(url, key)
        return _supabase_client


def summaries_for(ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Saved summaries of the active version, ``{report_id: summary}``, read 100 ids at a time."""
    ids = list(ids)
    if not ids:
        return {}
    sb = _supabase()
    version = summary_version()
    result: dict[int, dict[str, Any]] = {}
    for i in range(0, len(ids), SUMMARY_BATCH):
        result.update(store.fetch_summaries(sb, ids[i:i + SUMMARY_BATCH], version))
    return result


def save_comparison(report_id: int, prev_report_id: Optional[int], match_type: str,
                    narrative: Optional[str], comparison_details: Optional[dict] = None) -> None:
    """Store a comparison on the later report's summary (only the four comparison fields)."""
    store.update_diff(_supabase(), report_id, prev_report_id, match_type, narrative,
                      comparison_details)


def _save_summary(payload: dict[str, Any]) -> None:
    """Clear the comparisons citing the report, then upsert its summary (worker thread)."""
    sb = _supabase()
    store.clear_comparisons_citing(sb, payload['report_id'])
    store.upsert_summary(sb, payload)


def _delete_summary(rid: int) -> None:
    """Clear the comparisons citing the report, then delete its row (worker thread)."""
    sb = _supabase()
    store.clear_comparisons_citing(sb, rid)
    store.delete_summary_row(sb, rid)


# ── analysis ─────────────────────────────────────────────────────────────────

async def analyze_report(row: Mapping[str, Any],
                         force: bool = False) -> tuple[dict[str, Any], bool]:
    """Analyze one report row → ``(saved summary, reused)``. See the module docstring."""
    async with hold_reports([row['id']], IN_FLIGHT):
        async with ai_slot():
            return await _analyze(row, force)


async def delete_summary(rid: int) -> None:
    """Delete one report's analysis result. See the module docstring."""
    async with hold_reports([rid], IN_FLIGHT):
        await asyncio.to_thread(_delete_summary, rid)


async def _analyze(row: Mapping[str, Any], force: bool) -> tuple[dict[str, Any], bool]:
    rid = row['id']
    if row.get('report_type') != '단일종목':
        raise HTTPException(422, NOT_SINGLE_COMPANY)
    if force:
        # The reuse is skipped, but the DB is still prepared here: a missing DB setting
        # stops the request before the AI, as it does without force.
        await asyncio.to_thread(_supabase)
    else:
        existing = (await asyncio.to_thread(summaries_for, [rid])).get(rid)
        if existing and existing.get('financial_details'):
            return existing, True
    llm, model, timeout_s = phase2_llm()
    cfg = load_settings()
    try:
        path = pdf.resolve_in_storage(core_settings.storage_base_dir(), row['file_path'])
    except pdf.PDFNotFound as e:
        raise HTTPException(404, e.message) from None
    pages = await asyncio.to_thread(pdf.page_texts, path)
    if not pages:
        raise HTTPException(422, NO_TEXT)
    metadata = {k: row.get(k) for k in METADATA_KEYS}
    if needs_page_images(pages):
        got = await _from_page_images(llm, model, cfg, path, pages, metadata)
    else:
        got = await _from_text(llm, model, timeout_s, cfg, path, pages, metadata)
    payload = got.fields
    payload.update(report_id=rid, input_truncated=got.truncated,
                   input_pages_used=got.pages_used, input_total_pages=got.total_pages,
                   input_mode=got.mode, summary_version=cfg.summary_version, llm_model=model,
                   llm_tokens_input=got.tokens_in, llm_tokens_output=got.tokens_out,
                   prev_report_id=None, prev_match_type=None, diff_narrative=None,
                   comparison_details=None)
    await asyncio.to_thread(_save_summary, payload)
    return payload, False


@dataclass(frozen=True)
class _Extracted:
    fields: dict[str, Any]   # ExtractionResult.model_dump() after post-processing
    truncated: bool          # fewer pages sent than the PDF has
    pages_used: int          # pages sent (text pages, or pictures)
    total_pages: int         # pages in the PDF
    mode: str                # INPUT_TEXT or INPUT_PAGE_IMAGE
    tokens_in: int
    tokens_out: int


async def _from_text(llm: LLMClient, model: str, timeout_s: int, cfg: AnalysisSettings,
                     path: Path, pages: list[str], metadata: dict[str, Any]) -> _Extracted:
    text = build_pages_text(pages, cfg.max_input_tokens, path.name)
    if not text.text:
        raise HTTPException(422, NO_TEXT)
    async with llm as client:
        result, tokens_in, tokens_out = await extract_one(
            client=client, model=model, metadata=metadata,
            pages_text=text.text, timeout_s=timeout_s,
        )
    result = normalize_target_price_dir(result)
    fields: dict[str, Any] = result.model_dump()
    if result.financial_details:
        grounded, omitted = ground_metrics(result.financial_details, text.text)
        fields['financial_details'] = (grounded.model_dump()
                                       | {'unsupported_numeric_values': omitted})
    return _Extracted(fields, text.input_truncated, text.pages_used, text.total_pages,
                      INPUT_TEXT, tokens_in, tokens_out)


async def _from_page_images(llm: LLMClient, model: str, cfg: AnalysisSettings, path: Path,
                            pages: list[str], metadata: dict[str, Any]) -> _Extracted:
    if not supports_images(model):
        raise HTTPException(422, NO_TEXT)
    drawn = await asyncio.to_thread(pdf.render_pages_fitting, path, PAGE_IMAGE_MAX_PAGES)
    if not drawn:
        raise HTTPException(422, NO_TEXT)
    async with llm as client:
        result, tokens_in, tokens_out = await extract_one(
            client=client, model=model, metadata=metadata, page_images=drawn,
            timeout_s=cfg.image_timeout_s, retry_timeouts=False,
        )
    if is_empty_image_result(result):   # the answer as the model gave it
        raise HTTPException(422, EMPTY_IMAGE_RESULT)
    result = cap_image_confidence(normalize_target_price_dir(result))
    fields: dict[str, Any] = result.model_dump()
    if result.financial_details:
        # No text to ground the numbers in: the model's metrics as given.
        fields['financial_details'] = (result.financial_details.model_dump()
                                       | {'unsupported_numeric_values': 0})
    return _Extracted(fields, len(drawn) < len(pages), len(drawn), len(pages),
                      INPUT_PAGE_IMAGE, tokens_in, tokens_out)
