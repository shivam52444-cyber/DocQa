from __future__ import annotations

import json

import asyncpg
from pgvector.asyncpg import register_vector

from app.config import Settings


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await register_vector(conn)


async def create_pool(settings: Settings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=settings.database_url.get_secret_value(),
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        command_timeout=30,
        max_inactive_connection_lifetime=300,
        init=_init_connection,
    )
