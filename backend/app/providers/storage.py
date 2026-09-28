"""S3 adapter. boto3 is synchronous, so calls run in worker threads."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.errors import UpstreamError

log = logging.getLogger(__name__)


class S3ObjectStore:
    def __init__(self, bucket: str, region: str, endpoint_url: str | None = None) -> None:
        cfg = Config(
            region_name=region,
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
            max_pool_connections=32,
        )
        self._bucket = bucket
        self._s3 = boto3.client("s3", config=cfg, endpoint_url=endpoint_url)

    @staticmethod
    async def _run(fn: Callable[[], Any]) -> Any:
        try:
            return await asyncio.to_thread(fn)
        except (BotoCoreError, ClientError) as exc:
            raise UpstreamError("File storage is temporarily unavailable.") from exc

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await self._run(
            lambda: self._s3.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type, ServerSideEncryption="AES256"
            )
        )

    async def get(self, key: str) -> bytes:
        def _get() -> bytes:
            return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()

        return await self._run(_get)

    async def delete(self, key: str) -> None:
        await self._run(lambda: self._s3.delete_object(Bucket=self._bucket, Key=key))

    async def delete_prefix(self, prefix: str) -> int:
        def _delete() -> int:
            total = 0
            for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=self._bucket, Prefix=prefix):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    self._s3.delete_objects(Bucket=self._bucket, Delete={"Objects": keys, "Quiet": True})
                    total += len(keys)
            return total

        return await self._run(_delete)
