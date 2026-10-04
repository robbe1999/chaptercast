from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chaptercast.providers.base import (
    ProviderAuthError,
    ProviderQuotaError,
    ProviderRejectedError,
)
from tests.conftest import (
    ACCESS_TOKEN,
    SENTINEL_KEY,
    FakeProvider,
    wait_for_job,
)

ClientFactory = Callable[..., TestClient]
TEXT = "The lighthouse stood alone. Its beam swept the water. Ships steered by it."
JOB = {"text": TEXT, "voice_id": "voice1"}
AUTH = {"Authorization": f"Bearer {ACCESS_TOKEN}"}


def error_code(response_json: dict[str, object]) -> str:
    error = response_json["error"]
    assert isinstance(error, dict)
    return str(error["code"])


# --------------------------------------------------------------------- basics
def test_health_and_readiness(make_client: ClientFactory) -> None:
    client = make_client()
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["provider"] == "fake"


def test_public_config(make_client: ClientFactory) -> None:
    body = make_client(max_chars_per_job=1234).get("/api/config").json()
    assert body == {
        "provider": "demo",
        "auth_required": False,
        "max_chars_per_job": 1234,
        "chunk_max_chars": 900,
        "cache_enabled": True,
    }


def test_voices(make_client: ClientFactory) -> None:
    body = make_client().get("/api/voices").json()
    assert body["voices"] == [
        {
            "voice_id": "voice1",
            "name": "Voice One",
            "category": "premade",
            "description": "A test voice",
            "labels": {"accent": "british"},
            "preview_url": "/api/voices/voice1/preview",
        },
        {
            "voice_id": "voice2",
            "name": "Voice Two",
            "category": "premade",
            "description": None,
            "labels": {},
            "preview_url": None,
        },
    ]


# ------------------------------------------------------------------ happy path
def test_full_job_lifecycle(make_client: ClientFactory) -> None:
    client = make_client()
    created = client.post("/api/jobs", json=JOB)
    assert created.status_code == 202
    body = created.json()
    assert created.headers["location"] == f"/api/jobs/{body['id']}"
    assert body["status"] in {"queued", "running", "succeeded"}

    done = wait_for_job(client, body["id"])
    assert done["status"] == "succeeded"
    assert done["progress"]["completed_chunks"] == done["progress"]["total_chunks"] >= 1
    assert done["audio_url"] == f"/api/jobs/{body['id']}/audio"
    assert done["error"] is None

    audio = client.get(done["audio_url"])
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"
    assert audio.headers["content-disposition"].startswith("inline")
    assert audio.content[:4] == b"RIFF"

    ranged = client.get(done["audio_url"], headers={"Range": "bytes=0-9"})
    assert ranged.status_code == 206
    assert len(ranged.content) == 10

    assert client.delete(f"/api/jobs/{body['id']}").status_code == 204
    assert client.get(f"/api/jobs/{body['id']}").status_code == 404


def test_audio_is_not_available_until_the_job_finishes(make_client: ClientFactory) -> None:
    client = make_client(FakeProvider(delay=0.5))
    job_id = client.post("/api/jobs", json=JOB).json()["id"]
    early = client.get(f"/api/jobs/{job_id}/audio")
    assert early.status_code == 409
    assert error_code(early.json()) == "not_ready"
    client.delete(f"/api/jobs/{job_id}")


def test_provider_failures_surface_only_public_messages(make_client: ClientFactory) -> None:
    provider = FakeProvider(fail_when=lambda _t: ProviderRejectedError("raw upstream detail"))
    client = make_client(provider)
    job_id = client.post("/api/jobs", json=JOB).json()["id"]
    done = wait_for_job(client, job_id)
    assert done["status"] == "failed"
    assert done["error"]["code"] == "provider_rejected"
    assert "raw upstream detail" not in str(done)
    assert done["audio_url"] is None


def test_control_characters_are_stripped_before_reaching_the_provider(
    make_client: ClientFactory,
) -> None:
    provider = FakeProvider()
    client = make_client(provider)
    job_id = client.post(
        "/api/jobs", json={"text": "Hello\u0000 wor‮ld.", "voice_id": "voice1"}
    ).json()["id"]
    wait_for_job(client, job_id)
    assert provider.calls[0]["text"] == "Hello world."


# ------------------------------------------------------------------ validation
def test_validation_errors_never_echo_the_input(make_client: ClientFactory) -> None:
    response = make_client().post(
        "/api/jobs", json={"text": "PRIVATE-MANUSCRIPT", "voice_id": "bad id!"}
    )
    assert response.status_code == 422
    assert error_code(response.json()) == "invalid_request"
    assert "PRIVATE-MANUSCRIPT" not in response.text
    assert "bad id!" not in response.text


@pytest.mark.parametrize(
    "payload",
    [{}, {"text": "x"}, {"voice_id": "v"}, {"text": "", "voice_id": "v"}, {**JOB, "extra": 1}],
)
def test_malformed_payloads_are_rejected(
    make_client: ClientFactory, payload: dict[str, object]
) -> None:
    assert make_client().post("/api/jobs", json=payload).status_code == 422


def test_whitespace_only_text_is_rejected(make_client: ClientFactory) -> None:
    response = make_client().post("/api/jobs", json={"text": "  \n\t ", "voice_id": "voice1"})
    assert response.status_code == 422
    assert error_code(response.json()) == "empty_text"


def test_text_over_the_configured_limit_is_rejected(make_client: ClientFactory) -> None:
    response = make_client(max_chars_per_job=100).post(
        "/api/jobs", json={"text": "word " * 100, "voice_id": "voice1"}
    )
    assert response.status_code == 422
    assert error_code(response.json()) == "text_too_long"


@pytest.mark.parametrize("job_id", ["nope", "0" * 32, "../../etc/passwd", "A" * 32])
def test_unknown_and_malformed_job_ids_look_identical(
    make_client: ClientFactory, job_id: str
) -> None:
    client = make_client()
    for path in (f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/audio"):
        response = client.get(path)
        assert response.status_code == 404
        assert error_code(response.json()) == "not_found"
    assert client.delete(f"/api/jobs/{job_id}").status_code == 404


# -------------------------------------------------------------------- limits
def test_per_client_rate_limit(make_client: ClientFactory) -> None:
    client = make_client(jobs_per_minute=2)
    assert client.post("/api/jobs", json=JOB).status_code == 202
    assert client.post("/api/jobs", json=JOB).status_code == 202
    limited = client.post("/api/jobs", json=JOB)
    assert limited.status_code == 429
    assert error_code(limited.json()) == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1


def test_daily_character_budget(make_client: ClientFactory) -> None:
    client = make_client(daily_char_budget=len(TEXT) + 5)
    assert client.post("/api/jobs", json=JOB).status_code == 202
    over = client.post("/api/jobs", json=JOB)
    assert over.status_code == 429
    assert error_code(over.json()) == "daily_budget_exceeded"


def test_busy_server_returns_503_with_retry_after(make_client: ClientFactory) -> None:
    client = make_client(FakeProvider(delay=2.0), max_active_jobs=1)
    first = client.post("/api/jobs", json=JOB).json()["id"]
    busy = client.post("/api/jobs", json=JOB)
    assert busy.status_code == 503
    assert error_code(busy.json()) == "busy"
    assert busy.headers["retry-after"] == "10"
    client.delete(f"/api/jobs/{first}")


def test_oversized_bodies_are_rejected_early(make_client: ClientFactory) -> None:
    client = make_client(max_request_bytes=1024)
    response = client.post("/api/jobs", json={"text": "a" * 5000, "voice_id": "voice1"})
    assert response.status_code == 413
    assert error_code(response.json()) == "payload_too_large"


def test_chunked_bodies_without_a_length_are_rejected(make_client: ClientFactory) -> None:
    response = make_client().post(
        "/api/jobs",
        content=iter([b'{"text": "hi",', b' "voice_id": "voice1"}']),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 411


# ---------------------------------------------------------------------- auth
def test_endpoints_require_the_token_when_one_is_configured(make_client: ClientFactory) -> None:
    client = make_client(access_token=ACCESS_TOKEN)
    for method, path in [
        ("GET", "/api/voices"),
        ("GET", "/api/voices/voice1/preview"),
        ("GET", "/api/models"),
        ("POST", "/api/estimate"),
        ("POST", "/api/jobs"),
        ("GET", f"/api/jobs/{'0' * 32}"),
        ("GET", f"/api/jobs/{'0' * 32}/audio"),
        ("GET", f"/api/jobs/{'0' * 32}/transcript"),
        ("GET", f"/api/jobs/{'0' * 32}/captions.srt"),
        ("DELETE", f"/api/jobs/{'0' * 32}"),
    ]:
        response = client.request(method, path, json=JOB if method == "POST" else None)
        assert response.status_code == 401, path
        assert response.headers["www-authenticate"] == "Bearer"
        assert error_code(response.json()) == "unauthorized"


def test_public_endpoints_stay_public(make_client: ClientFactory) -> None:
    client = make_client(access_token=ACCESS_TOKEN)
    assert client.get("/healthz").status_code == 200
    config = client.get("/api/config").json()
    assert config["auth_required"] is True


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Bearer wrong",
        f"Basic {ACCESS_TOKEN}",
        ACCESS_TOKEN,
        "Bearer " + "x" * 5000,
    ],
)
def test_bad_credentials_are_rejected(make_client: ClientFactory, header: str) -> None:
    response = make_client(access_token=ACCESS_TOKEN).get(
        "/api/voices", headers={"Authorization": header}
    )
    assert response.status_code == 401


def test_the_right_token_works_end_to_end(make_client: ClientFactory) -> None:
    client = make_client(access_token=ACCESS_TOKEN)
    created = client.post("/api/jobs", json=JOB, headers=AUTH)
    assert created.status_code == 202
    done = wait_for_job(client, created.json()["id"], AUTH)
    assert done["status"] == "succeeded"
    assert client.get(done["audio_url"], headers=AUTH).status_code == 200
    assert client.get(done["audio_url"]).status_code == 401


def test_repeated_failures_lock_the_client_out_even_for_the_right_token(
    make_client: ClientFactory,
) -> None:
    client = make_client(access_token=ACCESS_TOKEN)
    for _ in range(10):
        assert (
            client.get("/api/voices", headers={"Authorization": "Bearer nope"}).status_code == 401
        )
    locked = client.get("/api/voices", headers=AUTH)
    assert locked.status_code == 429
    assert error_code(locked.json()) == "too_many_attempts"


def test_forwarded_for_is_ignored_unless_a_proxy_is_trusted(make_client: ClientFactory) -> None:
    client = make_client(access_token=ACCESS_TOKEN)
    for i in range(10):
        client.get(
            "/api/voices", headers={"Authorization": "Bearer x", "X-Forwarded-For": f"10.0.0.{i}"}
        )
    # Spoofed XFF values did not buy ten fresh buckets: the real client is locked out.
    assert (
        client.get("/api/voices", headers={**AUTH, "X-Forwarded-For": "10.9.9.9"}).status_code
        == 429
    )


def test_trusted_proxy_uses_the_last_forwarded_hop(make_client: ClientFactory) -> None:
    client = make_client(access_token=ACCESS_TOKEN, trust_proxy_headers=True)
    for i in range(10):
        client.get(
            "/api/voices",
            headers={"Authorization": "Bearer x", "X-Forwarded-For": f"1.1.1.{i}, 203.0.113.7"},
        )
    blocked = client.get("/api/voices", headers={**AUTH, "X-Forwarded-For": "9.9.9.9, 203.0.113.7"})
    other = client.get("/api/voices", headers={**AUTH, "X-Forwarded-For": "9.9.9.9, 203.0.113.8"})
    assert blocked.status_code == 429
    assert other.status_code == 200


# ----------------------------------------------------------------- hardening
def test_security_headers_are_present(make_client: ClientFactory) -> None:
    response = make_client().get("/api/config")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in response.headers


def test_hsts_only_when_the_request_was_https(make_client: ClientFactory) -> None:
    plain = make_client(trust_proxy_headers=True).get("/healthz")
    secure = make_client(trust_proxy_headers=True).get(
        "/healthz", headers={"X-Forwarded-Proto": "https"}
    )
    untrusted = make_client().get("/healthz", headers={"X-Forwarded-Proto": "https"})
    assert "strict-transport-security" not in plain.headers
    assert "strict-transport-security" in secure.headers
    assert "strict-transport-security" not in untrusted.headers


def test_request_ids_are_echoed_when_sane_and_replaced_when_not(make_client: ClientFactory) -> None:
    client = make_client()
    assert (
        client.get("/healthz", headers={"X-Request-ID": "abc-12345678"}).headers["x-request-id"]
        == "abc-12345678"
    )
    replaced = client.get("/healthz", headers={"X-Request-ID": "bad id\r\n<script>"}).headers[
        "x-request-id"
    ]
    assert len(replaced) == 32 and replaced.isalnum()


def test_error_bodies_carry_the_request_id(make_client: ClientFactory) -> None:
    response = make_client().get(
        f"/api/jobs/{'0' * 32}", headers={"X-Request-ID": "trace-12345678"}
    )
    assert response.json()["error"]["request_id"] == "trace-12345678"


def test_api_docs_are_off_by_default(make_client: ClientFactory) -> None:
    client = make_client()
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_api_docs_can_be_enabled(make_client: ClientFactory) -> None:
    client = make_client(enable_docs=True)
    assert client.get("/openapi.json").status_code == 200
    csp = client.get("/docs").headers["content-security-policy"]
    directives = {d.split()[0]: d.split()[1:] for d in csp.split(";") if d.strip()}
    # Only the docs page may load Swagger UI from its CDN, and only as an exact origin.
    assert "https://cdn.jsdelivr.net" in directives["script-src"]
    assert "https://cdn.jsdelivr.net" not in client.get("/").headers["content-security-policy"]


def test_no_cors_headers_by_default(make_client: ClientFactory) -> None:
    response = make_client().get("/api/config", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_only_configured_origins(make_client: ClientFactory) -> None:
    client = make_client(cors_origins="https://app.example.com")
    allowed = client.get("/api/config", headers={"Origin": "https://app.example.com"})
    denied = client.get("/api/config", headers={"Origin": "https://evil.example.com"})
    assert allowed.headers["access-control-allow-origin"] == "https://app.example.com"
    assert "access-control-allow-origin" not in denied.headers


def test_unhandled_errors_do_not_leak_details(make_client: ClientFactory) -> None:
    class Exploding(FakeProvider):
        async def list_voices(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("kaboom-internal-detail")

    response = make_client(Exploding()).get("/api/voices")
    assert response.status_code == 500
    assert error_code(response.json()) == "internal_error"
    assert "kaboom" not in response.text


def test_upstream_credential_problems_map_to_gateway_errors(make_client: ClientFactory) -> None:
    class AuthFail(FakeProvider):
        async def list_voices(self):  # type: ignore[no-untyped-def]
            raise ProviderAuthError("401 from upstream")

    class QuotaFail(FakeProvider):
        async def list_voices(self):  # type: ignore[no-untyped-def]
            raise ProviderQuotaError("quota")

    auth = make_client(AuthFail()).get("/api/voices")
    quota = make_client(QuotaFail()).get("/api/voices")
    assert (auth.status_code, error_code(auth.json())) == (502, "provider_auth")
    assert (quota.status_code, error_code(quota.json())) == (503, "provider_quota")
    assert "401 from upstream" not in auth.text


# -------------------------------------------------------------- secret hygiene
def test_secrets_never_appear_in_any_response_or_log(
    make_client: ClientFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    client = make_client(
        provider="demo", elevenlabs_api_key=SENTINEL_KEY, access_token=ACCESS_TOKEN
    )
    responses = [
        client.get("/api/config"),
        client.get("/healthz"),
        client.get("/readyz"),
        client.get("/api/voices"),  # 401
        client.get("/api/voices", headers={"Authorization": f"Bearer {SENTINEL_KEY}"}),
        client.post("/api/jobs", json=JOB, headers=AUTH),
        client.get(f"/api/jobs/{'0' * 32}", headers=AUTH),
        client.post("/api/jobs", json={"text": SENTINEL_KEY}, headers=AUTH),  # 422
        client.post("/api/estimate", json={**JOB, "text": SENTINEL_KEY}, headers=AUTH),
        client.get("/api/models", headers=AUTH),
        client.get("/nope"),
    ]
    for response in responses:
        blob = response.text + str(dict(response.headers))
        assert SENTINEL_KEY not in blob
        assert ACCESS_TOKEN not in blob
    out = capsys.readouterr().out
    assert SENTINEL_KEY not in out
    assert ACCESS_TOKEN not in out


# ------------------------------------------------------------ static frontend
def test_frontend_is_served_with_a_sensible_cache_policy(
    make_client: ClientFactory, tmp_path: Path
) -> None:
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>x</title>")
    (static / "assets" / "app-abc123.js").write_text("console.log(1)")
    client = make_client(static_dir=static)

    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-cache"
    assert "default-src 'self'" in index.headers["content-security-policy"]

    asset = client.get("/assets/app-abc123.js")
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_unknown_api_paths_return_json_404_not_the_html_shell(
    make_client: ClientFactory, tmp_path: Path
) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>")
    response = make_client(static_dir=static).get("/api/does-not-exist")
    assert response.status_code == 404
    assert error_code(response.json()) == "not_found"
