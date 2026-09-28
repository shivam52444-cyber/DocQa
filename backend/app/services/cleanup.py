"""Periodic housekeeping (runs inside worker containers; safe to run in several at once)."""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from app.config import Settings
from app.db.repository import Repository
from app.jobs.queue import PgJobQueue
from app.observability import timed
from app.providers.base import ObjectStore

log = logging.getLogger(__name__)


class CleanupService:
    def __init__(self, repo: Repository, store: ObjectStore, queue: PgJobQueue, settings: Settings) -> None:
        self._repo, self._store, self._queue, self._s = repo, store, queue, settings

    async def _delete_session_storage(self, session_id: UUID) -> None:
        await self._store.delete_prefix(f"sessions/{session_id}/")

    async def run_once(self) -> None:
        s = self._s
        deleted_total = 0
        with timed("cleanup_sessions"):
            for _ in range(20):  # bounded drain of the backlog
                deleted = await self._repo.cleanup_expired_sessions(
                    s.session_ttl_hours, s.cleanup_batch_size, self._delete_session_storage
                )
                deleted_total += deleted
                if deleted < s.cleanup_batch_size:
                    break
        for job in await self._queue.reap_dead():  # worker crashed on the last attempt
            doc_id = job.payload.get("doc_id")
            if doc_id:
                await self._repo.set_document_status(UUID(doc_id), "failed", "Processing failed. Please upload the file again.")
        purged = await self._queue.purge_finished(24)
        log.info("cleanup_done", extra={"sessions_deleted": deleted_total, "jobs_purged": purged})

    async def loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("cleanup_failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._s.cleanup_interval_seconds)
            except asyncio.TimeoutError:
                pass
