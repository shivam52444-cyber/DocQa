"""Composition root: the ONLY place that knows which concrete implementations are used.

Swap Llama host, embedding vendor, queue (SQS), vector store or guardrail by editing this file.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import asyncpg
from openai import AsyncOpenAI

from app.config import Settings
from app.db.pool import create_pool
from app.db.repository import Repository
from app.jobs.queue import PgJobQueue
from app.observability import wrap_openai_client
from app.providers.base import LLM, Embedder, ObjectStore
from app.providers.embeddings import OpenAIEmbedder
from app.providers.llm import FallbackLLM, OpenAICompatibleLLM
from app.providers.storage import S3ObjectStore
from app.services.cleanup import CleanupService
from app.services.guardrail import Guardrail, LLMGuardrail
from app.services.ingestion import IngestionService
from app.services.rag import RagService
from app.services.uploads import UploadService

log = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    pool: asyncpg.Pool
    repo: Repository
    store: ObjectStore
    embedder: Embedder
    llm: LLM
    guardrail: Guardrail
    queue: PgJobQueue
    uploads: UploadService
    ingestion: IngestionService
    rag: RagService
    cleanup: CleanupService
    _closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    async def close(self) -> None:
        for closer in reversed(self._closers):
            try:
                await closer()
            except Exception:
                log.exception("shutdown_step_failed")


async def build_container(settings: Settings) -> Container:
    pool = await create_pool(settings)
    repo = Repository(pool)
    queue = PgJobQueue(pool, max_attempts=settings.job_max_attempts, lease_seconds=settings.job_lease_seconds)
    store = S3ObjectStore(settings.s3_bucket, settings.aws_region, settings.s3_endpoint_url)

    def client(api_key: str, base_url: str | None = None) -> AsyncOpenAI:
        return wrap_openai_client(  # adds LangSmith latency/token tracing when enabled
            AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries)
        )

    llama_client = client(settings.llama_api_key.get_secret_value(), settings.llama_base_url)
    openai_client = client(settings.openai_api_key.get_secret_value())

    def chain(llama_model: str, openai_model: str) -> FallbackLLM:
        return FallbackLLM(
            [OpenAICompatibleLLM("llama", llama_client, llama_model), OpenAICompatibleLLM("openai", openai_client, openai_model)]
        )

    llm = chain(settings.llama_model, settings.openai_model)
    guard_llm = chain(settings.guard_llama_model or settings.llama_model, settings.guard_openai_model or settings.openai_model)
    guardrail = LLMGuardrail(
        guard_llm, batch_size=settings.guard_batch_size, concurrency=settings.guard_concurrency, max_chars=settings.guard_max_chars
    )
    embedder = OpenAIEmbedder(openai_client, settings.embedding_model, settings.embedding_dim, settings.embedding_batch_size)

    return Container(
        settings=settings, pool=pool, repo=repo, store=store, embedder=embedder, llm=llm, guardrail=guardrail, queue=queue,
        uploads=UploadService(repo, store, queue, settings),
        ingestion=IngestionService(repo, store, embedder, guardrail, settings),
        rag=RagService(repo, embedder, llm, guardrail, settings),
        cleanup=CleanupService(repo, store, queue, settings),
        _closers=[pool.close, llama_client.close, openai_client.close],
    )
