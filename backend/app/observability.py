"""Structured JSON logging, per-stage timing and optional LangSmith tracing.

LangSmith is optional: if the package is missing or tracing is disabled, everything is a no-op.
Enable with LANGSMITH_TRACING=true, LANGSMITH_API_KEY, LANGSMITH_PROJECT.
Set LANGSMITH_HIDE_INPUTS=true and LANGSMITH_HIDE_OUTPUTS=true so document text and
questions are never sent to LangSmith (only timings, counts and metadata are).
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

try:  # optional dependency
    from langsmith import traceable as _traceable
    from langsmith.run_helpers import get_current_run_tree as _get_run_tree
    from langsmith.wrappers import wrap_openai as _wrap_openai
except ImportError:  # pragma: no cover
    _traceable = _get_run_tree = _wrap_openai = None  # type: ignore[assignment]

F = TypeVar("F", bound=Callable[..., Any])
log = logging.getLogger(__name__)

session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="-")
query_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("query_id", default="-")
job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="-")

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "session_id": session_id_var.get(),
            "query_id": query_id_var.get(),
            "job_id": job_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "botocore", "urllib3", "boto3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------- tracing
def _drop(_: Any) -> dict:
    return {}


def trace(name: str, run_type: str = "chain") -> Callable[[F], F]:
    """LangSmith span. Inputs/outputs are dropped on purpose (they contain user documents)."""

    def decorator(fn: F) -> F:
        if _traceable is None:
            return fn
        try:
            return _traceable(name=name, run_type=run_type, process_inputs=_drop, process_outputs=_drop)(fn)  # type: ignore[return-value]
        except TypeError:  # older langsmith without process_* hooks
            return _traceable(name=name, run_type=run_type)(fn)  # type: ignore[return-value]

    return decorator


def add_trace_metadata(**metadata: Any) -> None:
    if _get_run_tree is None:
        return
    try:
        run = _get_run_tree()
        if run is not None:
            run.add_metadata(metadata)
    except Exception:  # tracing must never break the request
        log.debug("trace_metadata_failed", exc_info=True)


def add_trace_tags(*tags: str) -> None:
    if _get_run_tree is None:
        return
    try:
        run = _get_run_tree()
        if run is not None:
            run.add_tags(list(tags))
    except Exception:
        log.debug("trace_tags_failed", exc_info=True)


def wrap_openai_client(client: Any) -> Any:
    """Adds token/latency tracing for every OpenAI-compatible call (no-op if disabled)."""
    if _wrap_openai is None:
        return client
    try:
        return _wrap_openai(client)
    except Exception:
        log.warning("langsmith_wrap_failed", exc_info=True)
        return client


# ---------------------------------------------------------------- timing / rejections
@contextmanager
def timed(stage: str, **fields: Any) -> Iterator[None]:
    """Logs how long a stage took and attaches the duration to the current trace."""
    start = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        ms = round((time.perf_counter() - start) * 1000, 1)
        log.info("stage_completed", extra={"stage": stage, "duration_ms": ms, "outcome": outcome, **fields})
        add_trace_metadata(**{f"{stage}_ms": ms})


def record_rejection(stage: str, category: str, **fields: Any) -> None:
    """Single place to mark a rejection: structured log + LangSmith tags/metadata."""
    log.warning("rejection", extra={"stage": stage, "category": category, **fields})
    add_trace_tags("rejected", f"rejected:{stage}")
    add_trace_metadata(rejected=True, rejection_stage=stage, rejection_category=category, **fields)
