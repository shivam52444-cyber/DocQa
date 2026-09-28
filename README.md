# Document Q&A (Streamlit + FastAPI + worker on ECS)

```
Browser -> Frontend ALB -> Streamlit (private) -> Backend ALB (internal) -> FastAPI (public subnet)
                                                                              |-> S3 (gateway endpoint)
                                                                              |-> RDS pgvector (private)
                                                                              `-> jobs table <- Worker containers
```

## Containers (same backend image, different command)
| Service  | Image     | Command                                | Port | Health check      |
|----------|-----------|----------------------------------------|------|-------------------|
| frontend | frontend/ | (default) streamlit                    | 8501 | /_stcore/health   |
| api      | backend/  | (default) uvicorn                      | 8000 | /healthz          |
| worker   | backend/  | `python -m app.worker`                 | none | ECS task health   |
| migrate  | backend/  | `python -m app.migrate` (one-off task) | none | -                 |

* Run `migrate` before every deploy (idempotent). It creates the pgvector extension and tables.
* Scale `api` and `worker` independently; all state is in RDS/S3. Any number of each is safe.
* Worker task definition: `stopTimeout` 120s. Workers and API need outbound internet (LLM APIs): public subnet + public IP, security group with no inbound rules for the worker.
* Task role for api + worker: S3 read/write/delete on `sessions/*` of your bucket. Secrets via ECS `secrets:` from Secrets Manager.
* Add an S3 lifecycle rule (expire `sessions/` after 2 days) as a safety net behind the 24h cleanup.
* Streamlit ALB: enable stickiness, idle timeout >= 120s. Backend ALB: internal, idle timeout >= 60s.

## Request flow
* Upload: API validates -> S3 -> (document row + job in ONE transaction) -> 202. Worker: parse, chunk, guardrail every chunk, embed, store. Any flagged chunk rejects the whole file with a "malicious content" reason.
* Ask: guardrail(question) -> embed -> top-k in this session -> Llama (OpenAI fallback) -> guardrail(answer).
* Every call carries `X-Session-Id` (per tab) and `X-Query-Id` (per call). Replaying the same query id returns the stored answer.
* 24h TTL: worker cleanup loop deletes sessions older than 24h (chunks, documents, requests cascade) and their S3 prefix.

## Swapping things
Everything concrete is wired in `backend/app/container.py`: LLM hosts (`providers/llm.py`), embeddings, S3, job queue (`jobs/queue.py`, replaceable by SQS), guardrail (`services/guardrail.py`).
Never mix embedding models in one index: changing `EMBEDDING_MODEL`/`EMBEDDING_DIM` needs a fresh schema and re-ingestion.
