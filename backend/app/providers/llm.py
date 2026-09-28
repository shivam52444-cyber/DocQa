"""LLM adapters. Llama (OpenAI-compatible endpoint) first, OpenAI as fallback."""
from __future__ import annotations

import logging
from typing import Any, Sequence

from openai import AsyncOpenAI, OpenAIError

from app.errors import UpstreamError
from app.observability import add_trace_metadata
from app.providers.base import LLM

log = logging.getLogger(__name__)


class OpenAICompatibleLLM:
    """Works with OpenAI itself and with any OpenAI-compatible host serving Llama."""

    def __init__(self, name: str, client: AsyncOpenAI, model: str) -> None:
        self.name = name
        self._client = client
        self._model = model

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 800,
        json_mode: bool = False,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except OpenAIError as exc:
            raise UpstreamError(f"{self.name} request failed: {type(exc).__name__}") from exc
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        if not text:
            raise UpstreamError(f"{self.name} returned an empty response")
        return text


class FallbackLLM:
    """Tries providers in order; the first success wins."""

    name = "fallback"

    def __init__(self, providers: Sequence[LLM]) -> None:
        if not providers:
            raise ValueError("at least one LLM provider is required")
        self._providers = list(providers)

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        last: Exception | None = None
        for provider in self._providers:
            try:
                result = await provider.complete(messages, **kwargs)
                add_trace_metadata(llm_provider=provider.name)
                return result
            except UpstreamError as exc:
                last = exc
                log.warning("llm_provider_failed", extra={"provider": provider.name, "error": exc.message})
                add_trace_metadata(**{f"llm_{provider.name}_failed": True})
        raise UpstreamError("The AI service is temporarily unavailable. Please try again.") from last
