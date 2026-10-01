"""Pure-ASGI middleware: request context + access log, security headers, body limits."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chaptercast.logging_config import request_id_var

log = logging.getLogger("chaptercast.access")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

_STRICT_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
# Swagger UI (only when CHAPTERCAST_ENABLE_DOCS=true) needs a CDN and inline script.
_DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: "
    "https://fastapi.tiangolo.com; frame-ancestors 'none'"
)
_DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/openapi.json"})


class RequestContextMiddleware:
    """Assigns a request id, echoes it back, and writes one access-log line.

    The log line holds method, path, status and duration only: no query string,
    no headers, no body, no client address.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = Headers(scope=scope).get("x-request-id", "")
        request_id = supplied if _REQUEST_ID_RE.fullmatch(supplied) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            log.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            request_id_var.reset(token)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, trust_proxy: bool = False) -> None:
        self.app = app
        self._trust_proxy = trust_proxy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_headers = Headers(scope=scope)
        is_https = scope.get("scheme") == "https" or (
            self._trust_proxy and request_headers.get("x-forwarded-proto") == "https"
        )
        csp = _DOCS_CSP if scope["path"] in _DOCS_PATHS else _STRICT_CSP

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", csp)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
                if is_https:
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
                    )
                if scope["path"].startswith("/api/"):
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodyLimitMiddleware:
    """Rejects oversized bodies up front (413) and unsized chunked bodies (411)."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        length = headers.get("content-length")
        problem: tuple[int, str, str] | None = None
        if length is not None:
            if not length.isdigit():
                problem = (400, "bad_request", "Invalid Content-Length header.")
            elif int(length) > self._max:
                problem = (413, "payload_too_large", f"Request body exceeds {self._max} bytes.")
        elif "chunked" in headers.get("transfer-encoding", "").lower():
            problem = (411, "length_required", "A Content-Length header is required.")

        if problem is not None:
            status, code, message = problem
            body = {"error": {"code": code, "message": message, "request_id": request_id_var.get()}}
            await JSONResponse(body, status_code=status)(scope, receive, send)
            return
        await self.app(scope, receive, send)
