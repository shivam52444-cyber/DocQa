"""API request/response models."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    doc_id: UUID
    filename: str
    status: str  # processing | ready | rejected | failed
    reject_reason: str | None = None
    page_count: int | None = None
    created_at: datetime


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be blank")
        return v


class SourceOut(BaseModel):
    doc_id: UUID
    filename: str
    page: int | None = None
    snippet: str


AskStatus = Literal["ok", "blocked_input", "blocked_output", "no_documents"]


class AskOut(BaseModel):
    query_id: UUID
    status: AskStatus
    answer: str
    sources: list[SourceOut] = []
