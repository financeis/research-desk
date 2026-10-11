"""The analysis AI call: ``extract_one`` → ExtractionResult + token counts.

Two requests, one system message (``prompts._EXTRACTION_SYSTEM``):
- text: the page-marked text in the user message (``pages_text``);
- page images: ``page_images`` = ``[(page number, png), …]`` attached to the user
  message in that order, with one ``--- Page N ---`` marker per image.

Each try has its own ``timeout_s`` limit; a transient error (429 / 5xx /
connection / timeout) is retried once after ``backoff_s`` by
``core.llm.call_with_retry`` (then TransientLLMError). With
``retry_timeouts=False`` (the image path) a timeout is not tried again: the
``asyncio.TimeoutError`` itself is raised, while the other transient errors are
still retried once. The provider is picked by the model name
(``core.llm.LLMClient``). The SDK keeps its default retries.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional, Sequence

from research_desk.core.llm import StructuredResult, call_with_retry

from .prompts import render_extraction_messages, render_page_image_messages
from .schemas import ExtractionResult


class _TimeoutNotRetried(Exception):
    """Carries a timeout past ``call_with_retry`` (which would try it again); the
    ``asyncio.TimeoutError`` is its ``__cause__`` and is raised again outside."""


def _require_parsed(result: StructuredResult):
    if result.parsed is None:
        raise RuntimeError(f"LLM이 응답을 거부했습니다: {result.refusal}")
    return result.parsed


async def extract_one(
    *,
    client,                       # research_desk.core.llm.LLMClient
    model: str,
    metadata: dict[str, Any],
    pages_text: str = '',
    timeout_s: float,
    backoff_s: float = 5.0,
    page_images: Optional[Sequence[tuple[int, bytes]]] = None,
    retry_timeouts: bool = True,
) -> tuple[ExtractionResult, int, int]:
    """ExtractionResult + (input_tokens, output_tokens) 반환.

    ``page_images`` given → the page-image request (``pages_text`` unused); it must
    hold at least one image. Without it the text request is exactly today's (no
    ``images`` argument reaches ``client.parse``).
    """
    images: dict[str, Any] = {}
    if page_images is None:
        messages = render_extraction_messages(metadata, pages_text)
    else:
        if not page_images:
            raise ValueError("page-image extraction needs at least one page image")
        messages = render_page_image_messages(metadata, [number for number, _ in page_images])
        images['images'] = [png for _, png in page_images]
    system, user = messages[0]['content'], messages[1]['content']

    async def call():
        try:
            # ExtractionResult is too large for Claude's grammar compiler — see
            # LLMClient.parse(constrained=False).
            return await asyncio.wait_for(
                client.parse(model=model, system=system, user=user, schema=ExtractionResult,
                             constrained=False, **images),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError as e:
            if retry_timeouts:
                raise
            raise _TimeoutNotRetried() from e

    try:
        result = await call_with_retry(call, backoff_s)
    except _TimeoutNotRetried as e:
        raise e.__cause__ from None
    return _require_parsed(result), result.input_tokens, result.output_tokens
