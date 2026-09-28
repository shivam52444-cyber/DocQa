"""Central configuration. Every value comes from environment variables (injected by ECS)."""
from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "prod"
    log_level: str = "INFO"
    # Optional shared secret between Streamlit and the backend (defence in depth).
    internal_api_token: SecretStr | None = None

    # Database (RDS PostgreSQL + pgvector). Use ?sslmode=require in the DSN.
    database_url: SecretStr
    db_pool_min: int = 2
    db_pool_max: int = 10

    # S3 (credentials come from the ECS task role, never from env)
    s3_bucket: str
    aws_region: str = "us-east-1"
    s3_endpoint_url: str | None = None  # only for local MinIO/LocalStack

    # LLMs: Llama (any OpenAI-compatible endpoint) primary, OpenAI secondary
    llama_base_url: str
    llama_api_key: SecretStr
    llama_model: str
    openai_api_key: SecretStr
    openai_model: str = "gpt-4o-mini"
    guard_llama_model: str | None = None  # defaults to llama_model
    guard_openai_model: str | None = None  # defaults to openai_model
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    answer_max_tokens: int = 800

    # Embeddings (API based; never mix embedding models inside one index)
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    embedding_batch_size: int = 64

    # Guardrail
    guard_batch_size: int = 8
    guard_concurrency: int = 4
    guard_max_chars: int = 4000
    output_guardrail_enabled: bool = True

    # RAG
    top_k: int = 5
    chunk_size_chars: int = 3000
    chunk_overlap_chars: int = 400

    # Limits
    max_file_mb: int = 20
    max_pages: int = 300
    max_chunks_per_doc: int = 1500
    max_active_documents: int = 5
    max_uploads_per_session: int = 15  # includes rejected files (stops guardrail-cost abuse)
    max_asks_per_hour: int = 60

    # Lifecycle / background work
    session_ttl_hours: int = 24
    cleanup_interval_seconds: int = 600
    cleanup_batch_size: int = 50
    worker_concurrency: int = 2
    worker_poll_seconds: float = 2.0
    job_lease_seconds: int = 300
    job_max_attempts: int = 3

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
