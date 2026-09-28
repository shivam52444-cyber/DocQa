"""All SQL lives here. Services never touch asyncpg directly."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence
from uuid import UUID

import asyncpg
import numpy as np

from app.errors import Conflict, RateLimited

log = logging.getLogger(__name__)

_DOC_COLS = (
    "doc_id, session_id, query_id, filename, s3_key, content_type, size_bytes, "
    "status, reject_reason, page_count, created_at"
)


@dataclass(frozen=True)
class DocumentRecord:
    doc_id: UUID
    session_id: UUID
    query_id: UUID
    filename: str
    s3_key: str
    content_type: str
    size_bytes: int
    status: str
    reject_reason: str | None
    page_count: int | None
    created_at: datetime


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: int
    doc_id: UUID
    filename: str
    page: int | None
    content: str
    distance: float


@dataclass(frozen=True)
class RequestRecord:
    session_id: UUID
    query_id: UUID
    question: str
    answer: str
    status: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    chunk_ids: list[int] = field(default_factory=list)
    duration_ms: int | None = None
    action: str = "ask"


def _vec(v: Sequence[float]) -> np.ndarray:
    return np.asarray(v, dtype=np.float32)


class Repository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------ plumbing
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    @asynccontextmanager
    async def _conn(self, conn: asyncpg.Connection | None = None) -> AsyncIterator[asyncpg.Connection]:
        if conn is not None:
            yield conn
        else:
            async with self._pool.acquire() as c:
                yield c

    async def ping(self) -> None:
        async with self._conn() as conn:
            await conn.fetchval("SELECT 1")

    # ------------------------------------------------------------ sessions
    async def ensure_session(self, session_id: UUID) -> None:
        async with self._conn() as conn:
            await conn.execute(
                """INSERT INTO sessions (session_id) VALUES ($1)
                   ON CONFLICT (session_id) DO UPDATE SET last_active_at = now()""",
                session_id,
            )

    async def cleanup_expired_sessions(
        self,
        ttl_hours: int,
        limit: int,
        delete_storage: Callable[[UUID], Awaitable[Any]],
    ) -> int:
        """Deletes sessions older than ttl (chunks/documents/requests cascade).

        Rows are locked with SKIP LOCKED so several workers can run this concurrently.
        A session is only deleted once its S3 objects are gone; otherwise it is retried next cycle.
        """
        async with self.transaction() as conn:
            rows = await conn.fetch(
                """SELECT session_id FROM sessions
                   WHERE created_at < now() - make_interval(hours => $1::int)
                   ORDER BY created_at LIMIT $2 FOR UPDATE SKIP LOCKED""",
                ttl_hours,
                limit,
            )
            deletable: list[UUID] = []
            for row in rows:
                try:
                    await delete_storage(row["session_id"])
                    deletable.append(row["session_id"])
                except Exception:
                    log.exception("session_storage_cleanup_failed", extra={"expired_session": str(row["session_id"])})
            if deletable:
                await conn.execute("DELETE FROM sessions WHERE session_id = ANY($1::uuid[])", deletable)
            return len(deletable)

    # ------------------------------------------------------------ documents
    async def insert_document_with_capacity(
        self,
        conn: asyncpg.Connection,
        *,
        doc_id: UUID,
        session_id: UUID,
        query_id: UUID,
        filename: str,
        s3_key: str,
        content_type: str,
        size_bytes: int,
        max_active: int,
        max_total: int,
    ) -> DocumentRecord:
        # Row lock serialises concurrent uploads of the same session (enforces the limits atomically).
        await conn.execute("SELECT 1 FROM sessions WHERE session_id = $1 FOR UPDATE", session_id)
        counts = await conn.fetchrow(
            """SELECT count(*) FILTER (WHERE status IN ('processing', 'ready')) AS active,
                      count(*) AS total
               FROM documents WHERE session_id = $1""",
            session_id,
        )
        if counts["active"] >= max_active:
            raise Conflict(f"You can keep at most {max_active} documents per session.", code="document_limit")
        if counts["total"] >= max_total:
            raise RateLimited("Upload limit reached for this session. Start a new session to continue.")
        rec = await conn.fetchrow(
            f"""INSERT INTO documents (doc_id, session_id, query_id, filename, s3_key, content_type, size_bytes)
                VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING {_DOC_COLS}""",
            doc_id, session_id, query_id, filename, s3_key, content_type, size_bytes,
        )
        return DocumentRecord(**dict(rec))

    async def list_documents(self, session_id: UUID) -> list[DocumentRecord]:
        async with self._conn() as conn:
            rows = await conn.fetch(
                f"SELECT {_DOC_COLS} FROM documents WHERE session_id = $1 ORDER BY created_at", session_id
            )
        return [DocumentRecord(**dict(r)) for r in rows]

    async def get_document(self, doc_id: UUID) -> DocumentRecord | None:
        async with self._conn() as conn:
            row = await conn.fetchrow(f"SELECT {_DOC_COLS} FROM documents WHERE doc_id = $1", doc_id)
        return DocumentRecord(**dict(row)) if row else None

    async def set_document_status(self, doc_id: UUID, status: str, reject_reason: str | None = None) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE documents SET status = $2, reject_reason = $3, updated_at = now() WHERE doc_id = $1",
                doc_id, status, reject_reason,
            )

    async def has_ready_documents(self, session_id: UUID) -> bool:
        async with self._conn() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM documents WHERE session_id = $1 AND status = 'ready')", session_id
                )
            )

    # ------------------------------------------------------------ chunks
    async def replace_chunks_and_mark_ready(
        self,
        doc: DocumentRecord,
        rows: Sequence[tuple[int, int | None, str, Sequence[float]]],
        page_count: int,
    ) -> None:
        """Atomic and idempotent: chunks and the 'ready' status appear together or not at all."""
        async with self.transaction() as conn:
            await conn.execute("DELETE FROM chunks WHERE doc_id = $1", doc.doc_id)
            await conn.executemany(
                """INSERT INTO chunks (doc_id, session_id, chunk_index, page, content, embedding)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                [(doc.doc_id, doc.session_id, idx, page, text, _vec(vec)) for idx, page, text, vec in rows],
            )
            await conn.execute(
                """UPDATE documents SET status = 'ready', reject_reason = NULL, page_count = $2, updated_at = now()
                   WHERE doc_id = $1""",
                doc.doc_id, page_count,
            )

    async def search_chunks(self, session_id: UUID, embedding: Sequence[float], k: int) -> list[ChunkHit]:
        async with self._conn() as conn:
            rows = await conn.fetch(
                """SELECT c.chunk_id, c.doc_id, d.filename, c.page, c.content,
                          (c.embedding <=> $1) AS distance
                   FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
                   WHERE c.session_id = $2 AND d.status = 'ready'
                   ORDER BY c.embedding <=> $1
                   LIMIT $3""",
                _vec(embedding), session_id, k,
            )
        return [ChunkHit(**dict(r)) for r in rows]

    # ------------------------------------------------------------ requests
    async def get_request(self, session_id: UUID, query_id: UUID, action: str = "ask") -> RequestRecord | None:
        async with self._conn() as conn:
            r = await conn.fetchrow(
                """SELECT session_id, query_id, action, question, answer, status, sources, chunk_ids, duration_ms
                   FROM requests WHERE session_id = $1 AND query_id = $2 AND action = $3""",
                session_id, query_id, action,
            )
        if not r:
            return None
        d = dict(r)
        d["question"], d["answer"] = d["question"] or "", d["answer"] or ""
        return RequestRecord(**d)

    async def save_request(self, rec: RequestRecord) -> None:
        async with self._conn() as conn:
            await conn.execute(
                """INSERT INTO requests (session_id, query_id, action, question, answer, status, sources, chunk_ids, duration_ms)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                   ON CONFLICT (session_id, query_id, action) DO NOTHING""",
                rec.session_id, rec.query_id, rec.action, rec.question, rec.answer, rec.status,
                rec.sources, rec.chunk_ids, rec.duration_ms,
            )

    async def save_request_safe(self, rec: RequestRecord) -> None:
        """Used as a background task: logging must never fail a user request."""
        try:
            await self.save_request(rec)
        except Exception:
            log.exception("save_request_failed")

    async def count_recent_asks(self, session_id: UUID, seconds: int) -> int:
        async with self._conn() as conn:
            return int(
                await conn.fetchval(
                    """SELECT count(*) FROM requests
                       WHERE session_id = $1 AND action = 'ask'
                         AND created_at > now() - make_interval(secs => $2::int)""",
                    session_id, seconds,
                )
            )
