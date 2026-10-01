"""Application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from chaptercast import __version__
from chaptercast.api import router
from chaptercast.config import Settings, get_settings
from chaptercast.deps import Container
from chaptercast.errors import ApiError
from chaptercast.guards import DailyBudget, SlidingWindowLimiter
from chaptercast.jobs import JobManager
from chaptercast.logging_config import configure_logging, request_id_var
from chaptercast.middleware import (
    BodyLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from chaptercast.providers import build_provider
from chaptercast.providers.base import TTSProvider
from chaptercast.storage import AudioStore

log = logging.getLogger(__name__)

_CLEANUP_INTERVAL_SECONDS = 30


class CachedStaticFiles(StaticFiles):
    """Hashed build assets are immutable; the HTML shell must always revalidate."""

    def file_response(
        self, full_path: str | os.PathLike[str], *args: Any, **kwargs: Any
    ) -> Response:
        response = super().file_response(full_path, *args, **kwargs)
        if "/assets/" in str(full_path).replace("\\", "/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response

    async def get_response(self, path: str, scope: Scope) -> Response:
        # Only GET/HEAD reach here; unknown paths under /api never fall through to the SPA.
        return await super().get_response(path, scope)


def _error_body(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": request_id_var.get()}}


async def _cleanup_loop(jobs: JobManager) -> None:
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        try:
            await jobs.purge_expired()
        except Exception:
            log.exception("cleanup failed")


def create_app(settings: Settings | None = None, provider: TTSProvider | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.secret_values())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_provider = provider or build_provider(settings)
        store = AudioStore(settings.data_dir)
        removed = store.purge_orphans()
        budget = DailyBudget(settings.daily_char_budget)
        jobs = JobManager(provider=active_provider, store=store, budget=budget, settings=settings)
        app.state.container = Container(
            settings=settings,
            provider=active_provider,
            store=store,
            budget=budget,
            jobs=jobs,
            job_limiter=SlidingWindowLimiter(settings.jobs_per_minute, 60.0),
            auth_fail_limiter=SlidingWindowLimiter(10, 60.0),
        )
        cleanup = asyncio.create_task(_cleanup_loop(jobs), name="cleanup")
        log.info(
            "startup",
            extra={
                "provider": active_provider.name,
                "auth_required": settings.access_token is not None,
                "orphans_removed": removed,
            },
        )
        try:
            yield
        finally:
            cleanup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup
            await jobs.shutdown()
            await active_provider.aclose()

    app = FastAPI(
        title="ChapterCast",
        version=__version__,
        summary="Turn a chapter of text into a narrated audiobook.",
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )

    # Added innermost-first: RequestContext ends up outermost, so even rejected
    # requests get a request id and security headers.
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(SecurityHeadersMiddleware, trust_proxy=settings.trust_proxy_headers)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
        )
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            _error_body(exc.code, exc.message), status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Report where and why, never the submitted value.
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"Invalid request: {where or 'body'}: {first.get('msg', 'invalid')}."
        return JSONResponse(_error_body("invalid_request", message), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(
            _error_body(code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error", exc_info=exc)
        return JSONResponse(_error_body("internal_error", "Something went wrong."), status_code=500)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        container: Container = request.app.state.container
        ready = container.store.is_writable()
        return JSONResponse(
            {"status": "ready" if ready else "unavailable", "provider": container.provider.name},
            status_code=200 if ready else 503,
        )

    app.include_router(router)

    static_dir: Path | None = settings.static_dir
    if static_dir is not None and static_dir.is_dir():
        app.mount("/", CachedStaticFiles(directory=static_dir, html=True), name="web")
    return app
