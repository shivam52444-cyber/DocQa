"""Embedding adapter (API based, nothing is downloaded into the container)."""
from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from openai import AsyncOpenAI, OpenAIError

from app.errors import UpstreamError

log = logging.getLogger(__name__)


class OpenAIEmbedder:
    def __init__(self, client: AsyncOpenAI, model: str, dim: int, batch_size: int = 64, concurrency: int = 4) -> None:
        self._client = client
        self._model = model
        self.dim = dim
        self._batch_size = batch_size
        self._sem = asyncio.Semaphore(concurrency)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        batches = [list(texts[i : i + self._batch_size]) for i in range(0, len(texts), self._batch_size)]
        results = await asyncio.gather(*(self._embed_batch(b) for b in batches))
        vectors = [v for batch in results for v in batch]
        if len(vectors) != len(texts) or any(len(v) != self.dim for v in vectors):
            raise UpstreamError("Embedding service returned unexpected dimensions")
        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        kwargs: dict = {"model": self._model, "input": batch}
        if self._model.startswith("text-embedding-3"):
            kwargs["dimensions"] = self.dim
        async with self._sem:
            try:
                resp = await self._client.embeddings.create(**kwargs)
            except OpenAIError as exc:
                raise UpstreamError(f"embedding request failed: {type(exc).__name__}") from exc
        return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
