"""Thin HTTP client for the backend. The UI never talks to anything else."""
from __future__ import annotations

import uuid
from typing import Any

import httpx


class BackendError(Exception):
    def __init__(self, message: str, *, code: str = "error", status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class BackendClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 90.0) -> None:
        headers = {"User-Agent": "docqa-frontend"}
        if token:
            headers["X-Internal-Token"] = token
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=5.0),
            transport=httpx.HTTPTransport(retries=2),  # retries connection failures only
        )

    def _call(self, method: str, path: str, session_id: str, **kwargs: Any) -> Any:
        headers = {"X-Session-Id": session_id, "X-Query-Id": str(uuid.uuid4())}  # fresh id per call/click
        try:
            resp = self._http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise BackendError("The server took too long to respond. Please try again.", code="timeout") from exc
        except httpx.HTTPError as exc:
            raise BackendError("Could not reach the server. Please try again.", code="unreachable") from exc

        if resp.is_success:
            return resp.json()
        try:
            err = resp.json()["error"]
            raise BackendError(err["message"], code=err["code"], status=resp.status_code)
        except (ValueError, KeyError, TypeError):
            raise BackendError(f"Unexpected server error ({resp.status_code}).", code="http_error", status=resp.status_code)

    def upload(self, session_id: str, filename: str, data: bytes, content_type: str) -> dict:
        return self._call("POST", "/v1/documents", session_id, files={"file": (filename, data, content_type)})

    def list_documents(self, session_id: str) -> list[dict]:
        return self._call("GET", "/v1/documents", session_id)

    def ask(self, session_id: str, question: str) -> dict:
        return self._call("POST", "/v1/ask", session_id, json={"question": question})
