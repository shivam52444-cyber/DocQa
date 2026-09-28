"""Turns raw bytes into cleaned per-page text. CPU-bound: call via asyncio.to_thread."""
from __future__ import annotations

import io
import logging
import re
from typing import NamedTuple

from pypdf import PdfReader

from app.errors import NonRetryableIngestError

log = logging.getLogger(__name__)

_SPACES = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")


class PageText(NamedTuple):
    page: int | None  # None for plain text files
    text: str


def _clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")  # NUL breaks Postgres text
    return _BLANKS.sub("\n\n", _SPACES.sub(" ", text)).strip()


def extract_pages(data: bytes, content_type: str, max_pages: int) -> list[PageText]:
    if content_type == "application/pdf":
        return _from_pdf(data, max_pages)
    return _from_text(data)


def _from_text(data: bytes) -> list[PageText]:
    text = _clean(data.decode("utf-8", errors="replace"))
    return [PageText(None, text)] if text else []


def _from_pdf(data: bytes, max_pages: int) -> list[PageText]:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise NonRetryableIngestError("Password-protected PDFs are not supported.", stage="parse")
        total = len(reader.pages)
    except NonRetryableIngestError:
        raise
    except Exception as exc:
        raise NonRetryableIngestError("Could not read this PDF (it may be corrupted).", stage="parse") from exc
    if total > max_pages:
        raise NonRetryableIngestError(f"PDF has more than {max_pages} pages.", stage="parse")

    pages: list[PageText] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = _clean(page.extract_text() or "")
        except Exception:
            log.warning("pdf_page_extract_failed", extra={"page": number})
            continue
        if text:
            pages.append(PageText(number, text))
    return pages
