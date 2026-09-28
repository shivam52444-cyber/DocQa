"""Background pipeline: download -> parse -> chunk -> guardrail -> embed -> store."""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import asyncpg

from app.config import Settings
from app.db.repository import DocumentRecord, Repository
from app.errors import NonRetryableIngestError
from app.observability import add_trace_metadata, query_id_var, record_rejection, session_id_var, timed, trace
from app.providers.base import Embedder, ObjectStore
from app.services.chunking import chunk_pages
from app.services.guardrail import Guardrail
from app.services.parsing import extract_pages

log = logging.getLogger(__name__)

FLAGGED_MESSAGE = "Malicious content detected: this document looks like it contains instructions or content that could harm the assistant, so it was not processed."


class IngestionService:
    def __init__(
        self, repo: Repository, store: ObjectStore, embedder: Embedder, guardrail: Guardrail, settings: Settings
    ) -> None:
        self._repo, self._store, self._embedder, self._guardrail, self._s = repo, store, embedder, guardrail, settings

    @trace("ingest_document")
    async def handle(self, payload: dict) -> None:
        doc_id = UUID(payload["doc_id"])
        doc = await self._repo.get_document(doc_id)
        if doc is None:  # session expired or was cleaned up
            log.info("document_gone", extra={"doc_id": str(doc_id)})
            return
        if doc.status != "processing":  # duplicate delivery / retry after success
            return

        tokens = (session_id_var.set(str(doc.session_id)), query_id_var.set(str(doc.query_id)))
        add_trace_metadata(session_id=str(doc.session_id), query_id=str(doc.query_id), doc_id=str(doc_id), size_bytes=doc.size_bytes)
        try:
            await self._process(doc)
        except NonRetryableIngestError as exc:
            record_rejection(exc.stage, exc.category or "unreadable", doc_id=str(doc_id))
            await self._repo.set_document_status(doc_id, "rejected", exc.message)
            await self._discard_file(doc)
        except asyncpg.ForeignKeyViolationError:
            log.info("session_deleted_during_ingestion", extra={"doc_id": str(doc_id)})
        finally:
            session_id_var.reset(tokens[0])
            query_id_var.reset(tokens[1])

    async def handle_dead(self, payload: dict, error: Exception) -> None:
        """All retries exhausted: surface a clear status to the user."""
        await self._repo.set_document_status(
            UUID(payload["doc_id"]), "failed", "Processing failed after several attempts. Please upload the file again."
        )

    async def _process(self, doc: DocumentRecord) -> None:
        s = self._s
        with timed("download"):
            data = await self._store.get(doc.s3_key)
        with timed("parse"):
            pages = await asyncio.to_thread(extract_pages, data, doc.content_type, s.max_pages)
        chunks = chunk_pages(pages, s.chunk_size_chars, s.chunk_overlap_chars)
        if not chunks:
            raise NonRetryableIngestError("No readable text found (the file may be empty or scanned).", stage="parse")
        if len(chunks) > s.max_chunks_per_doc:
            raise NonRetryableIngestError("This document is too large to process.", stage="parse")
        add_trace_metadata(pages=len(pages), chunks=len(chunks))

        with timed("guardrail", chunks=len(chunks)):
            verdicts = await self._guardrail.check([c.text for c in chunks], "document")
        flagged = [(c, v) for c, v in zip(chunks, verdicts) if not v.safe]
        if flagged:
            categories = sorted({v.category or "unknown" for _, v in flagged})
            log.warning(
                "document_flagged",
                extra={"flagged_chunks": len(flagged), "chunk_indexes": [c.index for c, _ in flagged][:20], "categories": categories},
            )
            raise NonRetryableIngestError(FLAGGED_MESSAGE, stage="guardrail", category=categories[0])

        with timed("embed", chunks=len(chunks)):
            vectors = await self._embedder.embed([c.text for c in chunks])
        with timed("store", chunks=len(chunks)):
            await self._repo.replace_chunks_and_mark_ready(
                doc, [(c.index, c.page, c.text, v) for c, v in zip(chunks, vectors)], page_count=len(pages)
            )

    async def _discard_file(self, doc: DocumentRecord) -> None:
        try:
            await self._store.delete(doc.s3_key)
        except Exception:
            log.warning("discard_file_failed", extra={"doc_id": str(doc.doc_id)}, exc_info=True)
