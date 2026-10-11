"""PDF files: confinement to the storage folder, page text, page count, page images (one page, or
several within the AI picture limits).

Errors are plain exceptions; the features turn them into HTTP responses.

PyMuPDF must not be used from several threads at once, and this module is called from several:
the tagger reads two rows' PDFs in worker threads, and the web app renders review pages and reads
report text in its thread pool. So every function here that opens a document holds one
process-wide lock (``_PYMUPDF_LOCK``) from opening the document to closing it: one call at a time
is inside PyMuPDF, the others wait their turn. Only the PDF work waits; the AI calls around it
are not serialized.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from pathlib import Path
from typing import Optional, Union

import pymupdf

logger = logging.getLogger(__name__)

# MuPDF prints some document errors (e.g. "MuPDF error: format error: ...") straight to the
# process's stdout, which would corrupt a command's one-JSON-object stdout. Turn its own
# printing off; the errors still surface as exceptions or empty results.
pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)

StrPath = Union[str, os.PathLike]

NOT_IN_STORAGE = "PDF를 찾을 수 없습니다."
FILE_MISSING = "로컬 PDF 파일이 없습니다. 수집 상태를 확인해 주세요."

# One lock for the whole process, held across open → read → close by every PyMuPDF user below.
# Reentrant so a future helper that calls another function here from inside it cannot deadlock.
_PYMUPDF_LOCK = threading.RLock()

# What one page picture for an AI may be, under the strictest provider's limits (Anthropic: at
# most 8000 px a side and 5 MiB of base64 text per image) with a margin: the same values as the
# tagger's page-1 picture. The base64 text is 4/3 of the PNG bytes.
PAGE_IMAGE_MAX_SIDE_PX = 7000
PAGE_IMAGE_MAX_BASE64_BYTES = 4_500_000
# All the pictures of one request together, in base64 text: well under Anthropic's 32 MB request
# limit, leaving room for the prompt and the JSON around the pictures. Read when drawing.
PAGE_IMAGES_MAX_TOTAL_BASE64_BYTES = 20_000_000
# Smaller drawings tried before giving up: for one page over the one-picture limits, and for all
# the pages together over the total.
_FIT_ATTEMPTS = 4
# Each smaller drawing aims a bit under the limit: PNG size only roughly follows the dpi.
_FIT_MARGIN = 0.9

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PDFNotFound(Exception):
    """The path leaves the storage folder, is not a ``.pdf``, or the file is missing.

    ``message`` (also ``str()``) is the user-facing Korean text.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class PageNotFound(LookupError):
    """The page number is not in the document."""


def resolve_in_storage(base: StrPath, relative: StrPath) -> Path:
    """The resolved path of ``relative`` if it is a ``.pdf`` file inside ``base``.

    Raises PDFNotFound(NOT_IN_STORAGE) for a path outside ``base`` (``../``, an
    absolute path elsewhere) or a non-PDF name, and PDFNotFound(FILE_MISSING)
    when the file does not exist.
    """
    root = Path(base).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path.suffix.lower() != ".pdf":
        raise PDFNotFound(NOT_IN_STORAGE)
    if not path.is_file():
        raise PDFNotFound(FILE_MISSING)
    return path


def page_texts(path: StrPath, max_pages: Optional[int] = None) -> list[str]:
    """Text of each page in order, at most ``max_pages`` pages.

    A file that cannot be opened (missing, broken, empty) gives ``[]``; a page
    whose text cannot be read gives ``""`` for that page.
    """
    with _PYMUPDF_LOCK:
        try:
            doc = pymupdf.open(path)
        except Exception as exc:
            logger.debug("PDF open failed: %s (%s)", path, exc)
            return []
        try:
            count = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
            texts: list[str] = []
            for i in range(count):
                try:
                    texts.append(doc[i].get_text("text") or "")
                except Exception:
                    texts.append("")
            return texts
        finally:
            doc.close()


def page_count(path: StrPath) -> int:
    """Number of pages; an error opening the file propagates."""
    with _PYMUPDF_LOCK, pymupdf.open(path) as doc:
        return doc.page_count


def _draw_page(doc, number: int, dpi: float) -> bytes:
    """PNG of page ``number`` (1-based) of the open ``doc`` at ``dpi``; call with the lock held."""
    zoom = dpi / 72
    pixmap = doc[number - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    png = pixmap.tobytes("png")
    del pixmap   # free MuPDF's pixmap while the lock is still held
    return png


def render_page_png(path: StrPath, page: int, dpi: float = 120) -> bytes:
    """PNG of page ``page`` (1-based) at ``dpi``; PageNotFound if out of range."""
    with _PYMUPDF_LOCK, pymupdf.open(path) as doc:
        if page < 1 or page > doc.page_count:
            raise PageNotFound(f"page {page} is not in the document ({doc.page_count} pages)")
        return _draw_page(doc, page, dpi)


# ── page pictures within the AI picture limits ───────────────────────────────

def _base64_len(png: bytes) -> int:
    return 4 * math.ceil(len(png) / 3)


def _png_size(png: bytes) -> Optional[tuple[int, int]]:
    """(width, height) from a PNG's IHDR chunk; None for bytes that are not a PNG."""
    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE) or png[12:16] != b"IHDR":
        return None
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def _fit_scale(png: bytes) -> float:
    """1.0 when ``png`` is within the one-picture limits, else the factor to shrink the dpi by."""
    scale = 1.0
    size = _png_size(png)
    if size is not None and max(size) > PAGE_IMAGE_MAX_SIDE_PX:
        scale = PAGE_IMAGE_MAX_SIDE_PX / max(size)
    encoded = _base64_len(png)
    if encoded > PAGE_IMAGE_MAX_BASE64_BYTES:
        # PNG bytes grow roughly with the pixel count, i.e. with the dpi squared.
        scale = min(scale, math.sqrt(PAGE_IMAGE_MAX_BASE64_BYTES / encoded))
    return scale


def _draw_fitting(doc, number: int, dpi: float) -> Optional[tuple[bytes, float]]:
    """(PNG, the dpi it was drawn at) of page ``number``, drawn again smaller while over the
    one-picture limits; the same steps as the tagger's page-1 picture.

    None when the drawing fails, or the page is still over the limits after _FIT_ATTEMPTS
    smaller drawings.
    """
    for _ in range(1 + _FIT_ATTEMPTS):
        try:
            png = _draw_page(doc, number, dpi)
        except Exception as exc:
            logger.debug("page %d could not be drawn at %.0f dpi (%s)", number, dpi, exc)
            return None
        scale = _fit_scale(png)
        if scale >= 1.0:
            return png, dpi
        dpi *= scale * _FIT_MARGIN
    logger.debug("page %d is over the picture limits even at %.0f dpi", number, dpi)
    return None


def _total_base64(drawn: list[tuple[int, bytes, float]]) -> int:
    return sum(_base64_len(png) for _, png, _ in drawn)


def render_pages_fitting(path: StrPath, max_pages: int, dpi: float = 120) -> list[tuple[int, bytes]]:
    """PNGs of pages 1..``max_pages`` for an AI to read, as ``[(page number, png), …]`` in page order.

    - Each page is drawn at ``dpi``. A page over the one-picture limits (PAGE_IMAGE_MAX_SIDE_PX,
      PAGE_IMAGE_MAX_BASE64_BYTES) is drawn again smaller, like the tagger's page 1.
    - A page whose drawing fails, or that is still over the limits after _FIT_ATTEMPTS smaller
      drawings, is left out. The page numbers are 1-based and real: the others keep theirs.
    - When the pictures together are over PAGE_IMAGES_MAX_TOTAL_BASE64_BYTES, every page is drawn
      again at its own dpi times one common factor, up to _FIT_ATTEMPTS times. If they are still
      over, pages are left out from the end until the rest fit.

    A file that cannot be opened (missing, broken, empty) or whose pages cannot be read
    (password-locked, no pages) gives ``[]``: never an exception for a PDF problem. The document is
    opened once, and the lock held for the whole call.
    """
    with _PYMUPDF_LOCK:
        try:
            doc = pymupdf.open(path)
        except Exception as exc:
            logger.debug("PDF open failed: %s (%s)", path, exc)
            return []
        try:
            try:
                count = min(max_pages, doc.page_count)
            except Exception as exc:
                logger.debug("PDF page count failed: %s (%s)", path, exc)
                return []

            drawn: list[tuple[int, bytes, float]] = []
            for number in range(1, count + 1):
                fitted = _draw_fitting(doc, number, dpi)
                if fitted is not None:
                    drawn.append((number, *fitted))

            for _ in range(_FIT_ATTEMPTS):
                total = _total_base64(drawn)
                if total <= PAGE_IMAGES_MAX_TOTAL_BASE64_BYTES:
                    break
                # Same reasoning as one page: the bytes follow the dpi squared.
                factor = math.sqrt(PAGE_IMAGES_MAX_TOTAL_BASE64_BYTES / total) * _FIT_MARGIN
                redrawn = []
                for number, _, page_dpi in drawn:
                    fitted = _draw_fitting(doc, number, page_dpi * factor)
                    if fitted is not None:
                        redrawn.append((number, *fitted))
                drawn = redrawn

            while drawn and _total_base64(drawn) > PAGE_IMAGES_MAX_TOTAL_BASE64_BYTES:
                dropped = drawn.pop()
                logger.debug("page %d left out: the pictures together are over the limit", dropped[0])
            return [(number, png) for number, png, _ in drawn]
        finally:
            doc.close()
