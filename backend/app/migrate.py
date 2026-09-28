"""One-off schema migration: `python -m app.migrate` (run as an ECS one-off task before deploys)."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import asyncpg

from app.config import get_settings
from app.observability import configure_logging

log = logging.getLogger("migrate")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    sql = (Path(__file__).with_name("schema.sql")).read_text()
    sql = sql.replace("{{EMBEDDING_DIM}}", str(settings.embedding_dim))
    conn = await asyncpg.connect(settings.database_url.get_secret_value())
    try:
        await conn.execute("SELECT pg_advisory_lock(727001)")  # serialise concurrent migrations
        await conn.execute(sql)
        log.info("migration_applied")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
