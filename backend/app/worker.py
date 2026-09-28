"""Background worker: `python -m app.worker`. Run any number of these containers.

Runs (1) N concurrent job loops and (2) the 24h session cleanup loop.
On SIGTERM it stops claiming jobs and finishes the current ones; anything cut off is
re-queued automatically when its lease expires.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.config import Settings, get_settings
from app.container import build_container
from app.jobs.queue import Job, PgJobQueue
from app.observability import configure_logging, job_id_var

log = logging.getLogger("worker")


@dataclass(frozen=True)
class JobHandler:
    run: Callable[[dict[str, Any]], Awaitable[None]]
    on_dead: Callable[[dict[str, Any], Exception], Awaitable[None]]


class JobRunner:
    def __init__(self, queue: PgJobQueue, handlers: dict[str, JobHandler], settings: Settings) -> None:
        self._queue, self._handlers, self._s = queue, handlers, settings

    async def run(self, stop: asyncio.Event, slot: int) -> None:
        while not stop.is_set():
            try:
                job = await self._queue.claim(list(self._handlers))
            except Exception:
                log.exception("claim_failed", extra={"slot": slot})
                await self._sleep(stop, 5)
                continue
            if job is None:
                await self._sleep(stop, self._s.worker_poll_seconds)
                continue
            await self._process(job)

    async def _process(self, job: Job) -> None:
        token = job_id_var.set(str(job.job_id))
        handler = self._handlers[job.kind]
        heartbeat = asyncio.create_task(self._heartbeat(job.job_id))
        try:
            await handler.run(job.payload)
            await self._queue.complete(job.job_id)
            log.info("job_done", extra={"kind": job.kind, "attempt": job.attempts})
        except Exception as exc:
            log.exception("job_failed", extra={"kind": job.kind, "attempt": job.attempts})
            try:
                if await self._queue.fail(job, f"{type(exc).__name__}: {exc}"):
                    await handler.on_dead(job.payload, exc)
            except Exception:
                log.exception("job_failure_handling_failed")
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            job_id_var.reset(token)

    async def _heartbeat(self, job_id: int) -> None:
        interval = max(5, self._queue.lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._queue.extend_lease(job_id)
            except Exception:
                log.warning("lease_extend_failed", exc_info=True)

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    container = await build_container(settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    handlers = {"ingest_document": JobHandler(container.ingestion.handle, container.ingestion.handle_dead)}
    runner = JobRunner(container.queue, handlers, settings)
    tasks = [asyncio.create_task(runner.run(stop, i)) for i in range(settings.worker_concurrency)]
    tasks.append(asyncio.create_task(container.cleanup.loop(stop)))
    log.info("worker_started", extra={"concurrency": settings.worker_concurrency})

    await stop.wait()
    log.info("worker_stopping")
    await asyncio.gather(*tasks, return_exceptions=True)
    await container.close()


if __name__ == "__main__":
    asyncio.run(main())
