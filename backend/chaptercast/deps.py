"""Shared application state and request-level security helpers."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Request

from chaptercast.cache import ClipCache
from chaptercast.config import Settings
from chaptercast.errors import ApiError
from chaptercast.guards import DailyBudget, SlidingWindowLimiter
from chaptercast.jobs import JobManager
from chaptercast.providers.base import TTSProvider
from chaptercast.storage import AudioStore


@dataclass
class Container:
    settings: Settings
    provider: TTSProvider
    store: AudioStore
    budget: DailyBudget
    cache: ClipCache
    jobs: JobManager
    job_limiter: SlidingWindowLimiter
    auth_fail_limiter: SlidingWindowLimiter
    read_limiter: SlidingWindowLimiter


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def client_ip(request: Request, *, trust_proxy: bool) -> str:
    """Best-effort client identity for rate limiting.

    ``X-Forwarded-For`` is client-controllable, so it is only consulted when the
    operator says a trusted proxy sits in front. We then take the *last* entry,
    the one appended by that proxy, not the first, which the client can forge.
    """
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        last = forwarded.split(",")[-1].strip()
        if last:
            return last[:64]
    return request.client.host if request.client else "unknown"


async def require_access(request: Request) -> None:
    """Bearer-token gate. A no-op when no access token is configured (local use)."""
    container = get_container(request)
    token = container.settings.access_token
    if token is None:
        return

    ip = client_ip(request, trust_proxy=container.settings.trust_proxy_headers)
    # Checked *before* comparing, so a blocked client gets no guessing oracle.
    wait = container.auth_fail_limiter.retry_after(ip)
    if wait > 0:
        raise ApiError(
            429, "too_many_attempts", "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(int(wait) + 1)},
        )  # fmt: skip

    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    expected = token.get_secret_value().encode()
    if scheme.lower() == "bearer" and secrets.compare_digest(supplied.strip().encode(), expected):
        return
    container.auth_fail_limiter.hit(ip)
    raise ApiError(
        401, "unauthorized", "A valid access token is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )  # fmt: skip
