"""Synchronous part of an upload: validate, store in S3, register document + job atomically."""
from __future__ import annotations

import logging
import os
import re
from uuid import UUID, uuid4

from fastapi import UploadFile

from app.config import Settings
from app.db.repository import DocumentRecord, Repository
from app.errors import BadRequest, PayloadTooLarge
from app.jobs.queue import PgJobQueue
from app.observability import add_trace_metadata, timed, trace
from app.providers.base import ObjectStore

log = logging.getLogger(__name__)

_ALLOWED = {".pdf": "application/pdf", ".txt": "text/plain"}  # decided by extension + magic bytes, never client MIME
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_filename(name: str | None) -> str:
    base = os.path.basename((name or "").replace("\\", "/"))
    base = _CONTROL.sub("", base).strip()[:200]
    if not base:
        raise BadRequest("The file has no name.")
    return base


class UploadService:
    def __init__(self, repo: Repository, store: ObjectStore, queue: PgJobQueue, settings: Settings) -> None:
        self._repo, self._store, self._queue, self._s = repo, store, queue, settings

    @trace("upload")
    async def submit(self, session_id: UUID, query_id: UUID, upload: UploadFile) -> DocumentRecord:
        filename = clean_filename(upload.filename)
        ext = os.path.splitext(filename)[1].lower()
        content_type = _ALLOWED.get(ext)
        if content_type is None:
            raise BadRequest("Only PDF and TXT files are supported.", code="unsupported_type")

        data = await self._read_limited(upload)
        self._validate_signature(ext, data)
        add_trace_metadata(session_id=str(session_id), query_id=str(query_id), size_bytes=len(data))

        doc_id = uuid4()
        key = f"sessions/{session_id}/{doc_id}"  # user filename never touches the object key
        with timed("s3_upload", size_bytes=len(data)):
            await self._store.put(key, data, content_type)
        try:
            async with self._repo.transaction() as conn:  # document row + job are committed together
                doc = await self._repo.insert_document_with_capacity(
                    conn,
                    doc_id=doc_id, session_id=session_id, query_id=query_id, filename=filename, s3_key=key,
                    content_type=content_type, size_bytes=len(data),
                    max_active=self._s.max_active_documents, max_total=self._s.max_uploads_per_session,
                )
                await self._queue.enqueue("ingest_document", {"doc_id": str(doc_id)}, conn=conn)
        except Exception:
            try:
                await self._store.delete(key)
            except Exception:
                log.warning("orphan_cleanup_failed", extra={"key": key}, exc_info=True)
            raise
        return doc

    async def _read_limited(self, upload: UploadFile) -> bytes:
        limit = self._s.max_file_bytes
        buf = bytearray()
        while chunk := await upload.read(1024 * 1024):
            buf.extend(chunk)
            if len(buf) > limit:
                raise PayloadTooLarge(f"File is larger than {self._s.max_file_mb} MB.")
        if not buf:
            raise BadRequest("The file is empty.")
        return bytes(buf)

    @staticmethod
    def _validate_signature(ext: str, data: bytes) -> None:
        if ext == ".pdf" and not data.startswith(b"%PDF-"):
            raise BadRequest("This file is not a valid PDF.", code="invalid_file")
        if ext == ".txt" and b"\x00" in data[:8192]:
            raise BadRequest("This file is not a plain text file.", code="invalid_file")
