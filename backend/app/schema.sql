-- Idempotent. Run once per deploy with: python -m app.migrate
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS sessions (
    session_id     uuid PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_active_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_created_at_idx ON sessions (created_at);

CREATE TABLE IF NOT EXISTS documents (
    doc_id        uuid PRIMARY KEY,
    session_id    uuid NOT NULL REFERENCES sessions ON DELETE CASCADE,
    query_id      uuid NOT NULL,              -- frontend request id of the upload click
    filename      text NOT NULL,
    s3_key        text NOT NULL,
    content_type  text NOT NULL,
    size_bytes    bigint NOT NULL,
    status        text NOT NULL DEFAULT 'processing'
                  CHECK (status IN ('processing', 'ready', 'rejected', 'failed')),
    reject_reason text,
    page_count    int,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS documents_session_idx ON documents (session_id);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    bigserial PRIMARY KEY,
    doc_id      uuid NOT NULL REFERENCES documents ON DELETE CASCADE,
    session_id  uuid NOT NULL REFERENCES sessions ON DELETE CASCADE,
    chunk_index int NOT NULL,
    page        int,
    content     text NOT NULL,
    embedding   vector({{EMBEDDING_DIM}}) NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_session_idx ON chunks (session_id);
CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (doc_id);

CREATE TABLE IF NOT EXISTS requests (
    id          bigserial PRIMARY KEY,
    session_id  uuid NOT NULL REFERENCES sessions ON DELETE CASCADE,
    query_id    uuid NOT NULL,                -- frontend request id (per click)
    action      text NOT NULL DEFAULT 'ask',
    question    text,
    answer      text,
    status      text NOT NULL,
    sources     jsonb NOT NULL DEFAULT '[]',
    chunk_ids   bigint[] NOT NULL DEFAULT '{}',
    duration_ms int,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, query_id, action)
);
CREATE INDEX IF NOT EXISTS requests_session_created_idx ON requests (session_id, created_at);

-- Postgres-backed job queue (claimed with FOR UPDATE SKIP LOCKED, safe with many workers)
CREATE TABLE IF NOT EXISTS jobs (
    job_id       bigserial PRIMARY KEY,
    kind         text NOT NULL,
    payload      jsonb NOT NULL,
    status       text NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts     int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 3,
    run_after    timestamptz NOT NULL DEFAULT now(),
    locked_until timestamptz,
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs (run_after) WHERE status IN ('queued', 'running');
