"""Postgres-backed job queue. Safe with many worker containers (SKIP LOCKED + leases).

To move to SQS later, implement the same methods and swap it in container.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

import asyncpg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    job_id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


class PgJobQueue:
    def __init__(self, pool: asyncpg.Pool, *, max_attempts: int, lease_seconds: int) -> None:
        self._pool = pool
        self._max_attempts = max_attempts
        self._lease = lease_seconds

    @property
    def lease_seconds(self) -> int:
        return self._lease

    async def enqueue(
        self, kind: str, payload: dict[str, Any], *, conn: asyncpg.Connection | None = None
    ) -> None:
        """Pass `conn` to enqueue inside the caller's transaction (no lost or orphan jobs)."""
        sql = "INSERT INTO jobs (kind, payload, max_attempts) VALUES ($1, $2, $3)"
        if conn is not None:
            await conn.execute(sql, kind, payload, self._max_attempts)
        else:
            async with self._pool.acquire() as c:
                await c.execute(sql, kind, payload, self._max_attempts)

    async def claim(self, kinds: Sequence[str]) -> Job | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE jobs SET status = 'running', attempts = attempts + 1,
                          locked_until = now() + make_interval(secs => $2::int), updated_at = now()
                   WHERE job_id = (
                       SELECT job_id FROM jobs
                       WHERE kind = ANY($1::text[]) AND run_after <= now() AND attempts < max_attempts
                         AND (status = 'queued' OR (status = 'running' AND locked_until < now()))
                       ORDER BY run_after, job_id
                       FOR UPDATE SKIP LOCKED LIMIT 1)
                   RETURNING job_id, kind, payload, attempts, max_attempts""",
                list(kinds), self._lease,
            )
        return Job(**dict(row)) if row else None

    async def extend_lease(self, job_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET locked_until = now() + make_interval(secs => $2::int) WHERE job_id = $1 AND status = 'running'",
                job_id, self._lease,
            )

    async def complete(self, job_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET status = 'done', locked_until = NULL, updated_at = now() WHERE job_id = $1", job_id
            )

    async def fail(self, job: Job, error: str) -> bool:
        """Schedules a retry with exponential backoff. Returns True if attempts are exhausted."""
        permanent = job.attempts >= job.max_attempts
        async with self._pool.acquire() as conn:
            if permanent:
                await conn.execute(
                    "UPDATE jobs SET status = 'failed', locked_until = NULL, last_error = $2, updated_at = now() WHERE job_id = $1",
                    job.job_id, error[:1000],
                )
            else:
                delay = min(300, 15 * 2 ** (job.attempts - 1))
                await conn.execute(
                    """UPDATE jobs SET status = 'queued', locked_until = NULL, last_error = $2,
                              run_after = now() + make_interval(secs => $3::int), updated_at = now()
                       WHERE job_id = $1""",
                    job.job_id, error[:1000], delay,
                )
        return permanent

    async def reap_dead(self) -> list[Job]:
        """Jobs whose worker died on the final attempt (lease expired, no attempts left)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """UPDATE jobs SET status = 'failed', last_error = 'lease expired on final attempt', updated_at = now()
                   WHERE status = 'running' AND locked_until < now() AND attempts >= max_attempts
                   RETURNING job_id, kind, payload, attempts, max_attempts"""
            )
        return [Job(**dict(r)) for r in rows]

    async def purge_finished(self, older_than_hours: int = 24) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """DELETE FROM jobs WHERE status IN ('done', 'failed')
                   AND updated_at < now() - make_interval(hours => $1::int)""",
                older_than_hours,
            )
        return int(result.split()[-1])
