"""Page-aware chunking so every chunk can be cited with its page number."""
from __future__ import annotations

from typing import NamedTuple, Sequence

from app.services.parsing import PageText


class Chunk(NamedTuple):
    index: int
    page: int | None
    text: str


def split_text(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    pieces: list[str] = []
    start, n = 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:  # prefer to cut at a paragraph / sentence boundary
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > size * 0.5:
                end = start + cut + 1
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return pieces


def chunk_pages(pages: Sequence[PageText], size: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for page in pages:
        for piece in split_text(page.text, size, overlap):
            chunks.append(Chunk(len(chunks), page.page, piece))
    return chunks
