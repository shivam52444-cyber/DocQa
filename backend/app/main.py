from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import get_settings
from app.container import build_container
from app.errors import AppError
from app.observability import configure_logging, query_id_var, session_id_var

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    container = await build_container(settings)
    app.state.container = container
    log.info("api_started")
    try:
        yield
    finally:
        await container.close()
        log.info("api_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    docs = None if settings.env == "prod" else "/docs"
    app = FastAPI(title="Document Q&A API", lifespan=lifespan, docs_url=docs, redoc_url=None, openapi_url=None if docs is None else "/openapi.json")

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        tokens = (
            session_id_var.set(request.headers.get("x-session-id", "-")[:36]),
            query_id_var.set(request.headers.get("x-query-id", "-")[:36]),
        )
        start, status = time.perf_counter(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            if request.url.path != "/healthz":
                log.info("http_request", extra={
                    "method": request.method, "path": request.url.path, "status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                })
            session_id_var.reset(tokens[0])
            query_id_var.reset(tokens[1])

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        level = logging.ERROR if exc.status_code >= 500 else logging.INFO
        log.log(level, "app_error", extra={"code": exc.code, "status": exc.status_code, "cause": repr(exc.__cause__) if exc.__cause__ else None})
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": {"code": "invalid_request", "message": "The request was not valid."}}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error")
        return JSONResponse({"error": {"code": "internal_error", "message": "Something went wrong on our side."}}, status_code=500)

    app.include_router(router)
    return app


app = create_app()
