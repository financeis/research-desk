"""Deterministic rules around an extraction (no DB, no network).

- ``normalize_target_price_dir``: target price direction by arithmetic.
- Page-image analysis: ``needs_page_images`` decides, from every page's text,
  whether the pages go to the AI as images instead of text;
  ``is_empty_image_result`` spots an image answer with nothing in it;
  ``cap_image_confidence`` keeps an image answer's confidence at medium or below.
"""
from __future__ import annotations

import re

from .schemas import ExtractionResult

# Page-image decision thresholds (per-page averages over the whole document).
# Measured: PDFs whose body text is missing had Hangul 0-38 and Latin about 50
# or less per page; normal reports had Hangul 76 or more. Adjustable.
PAGE_IMAGE_MAX_HANGUL_PER_PAGE = 50
PAGE_IMAGE_MAX_LATIN_PER_PAGE = 300

_HANGUL = re.compile(r'[가-힣]')
_LATIN = re.compile(r'[A-Za-z]')


def needs_page_images(page_texts: list[str]) -> bool:
    """True when the whole document has essentially no body text (image path).

    Hangul average = total ``[가-힣]`` count ÷ page count, Latin average = total
    ``[A-Za-z]`` count ÷ page count, over ALL pages given (empty pages count).
    Images only when Hangul average < 50 AND Latin average < 300; exactly at a
    limit is the text path. Compared as totals against limit × pages, which is
    the same test without float rounding.

    No pages → False. The service rejects a PDF without pages (422) before
    deciding, so this case is never an image request.
    """
    pages = len(page_texts)
    if pages == 0:
        return False
    hangul = sum(len(_HANGUL.findall(text or '')) for text in page_texts)
    latin = sum(len(_LATIN.findall(text or '')) for text in page_texts)
    return (hangul < PAGE_IMAGE_MAX_HANGUL_PER_PAGE * pages
            and latin < PAGE_IMAGE_MAX_LATIN_PER_PAGE * pages)


def is_empty_image_result(r: ExtractionResult) -> bool:
    """An image answer with nothing in it: no target price, rating ``N/A``,
    no metrics (no financial details counts as none) and confidence ``low``.

    Judge the answer as the model gave it, before ``cap_image_confidence``.
    """
    metrics = len(r.financial_details.metrics) if r.financial_details is not None else 0
    return (r.target_price_new is None
            and r.recommendation == 'N/A'
            and metrics == 0
            and r.extraction_confidence == 'low')


def cap_image_confidence(r: ExtractionResult) -> ExtractionResult:
    """Image answers are at most ``medium``: ``high`` → ``medium``; others unchanged."""
    if r.extraction_confidence != 'high':
        return r
    return r.model_copy(update={'extraction_confidence': 'medium'})


def normalize_target_price_dir(r: ExtractionResult) -> ExtractionResult:
    """deterministic 산수로 target_price_dir 덮어씀 (LLM 판단보다 산수 우선).

    old/new 둘 다 int면 부호 비교, 한쪽만이면 LLM 판단 유지,
    둘 다 None이면 'N/A' 강제.
    """
    new, old = r.target_price_new, r.target_price_old
    if new is not None and old is not None:
        if new > old:    forced = '상향'
        elif new < old:  forced = '하향'
        else:            forced = '불변'
    elif new is None and old is None:
        forced = 'N/A'
    else:
        # 한쪽만 있음 → LLM 판단 유지 (신규/N/A 등)
        return r
    if r.target_price_dir == forced:
        return r
    return r.model_copy(update={'target_price_dir': forced})
