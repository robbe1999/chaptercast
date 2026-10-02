# Security model

This is a small service that holds one valuable secret (a paid API key) and spends real money on request. The model below says what is protected, from whom, how, and where each control is verified.

## Assets

1. **The ElevenLabs API key.** Compromise means arbitrary spend and possible account abuse.
2. **Credits.** Even without the key, an open endpoint can burn them.
3. **User text, generated audio and cached sections.** May be private manuscripts.
4. **The access token**, if one is configured.

## Trust boundaries

```
Browser  ──(untrusted input)──▶  API  ──(key attached here only)──▶  api.elevenlabs.io
                                  │
                                  └──▶ private audio directory
```

Everything from the browser is untrusted. The key exists only inside the API process and is attached only to requests to the configured ElevenLabs host.

## Threats and controls

| # | Threat | Control | Verified by |
|---|---|---|---|
| 1 | Key committed to git | `.env` and key files gitignored; `.env.example` holds blanks; local scanner; gitleaks over full history; pre-commit hooks | `test_repository_contains_no_secrets`, `test_env_example_has_no_real_values`, CI `secrets` job |
| 2 | Key shipped to the browser | No `VITE_`-style variables; frontend only calls its own origin; CI greps the built bundle for credential strings | CI `frontend` job; `client.test.ts` |
| 3 | Key leaked via logs or errors | `SecretStr`; JSON logger redacts known secrets and key-shaped strings on the final serialised line, including tracebacks; pydantic `hide_input_in_errors`; public error messages are separate from log detail | `test_logging_redaction.py`, `test_secrets_never_appear_in_any_response_or_log`, `test_secrets_never_appear_in_repr_or_dump` |
| 4 | Key sent to an attacker's host (bad config, redirect) | Base URL must be `https` on `elevenlabs.io`; redirects never followed | `test_provider_base_url_must_be_an_https_elevenlabs_host`, `test_redirects_are_never_followed` |
| 5 | Path or URL injection through ids | Voice ids validated against `^[A-Za-z0-9_-]{1,64}$` and percent-encoded; job ids are server-generated 128-bit hex; storage names derive only from those; model id and output format are pattern-restricted | `test_voice_ids_cannot_escape_the_url_path`, `test_rejects_ids_that_are_not_server_generated` |
| 6 | Credit exhaustion by an anonymous caller | Optional bearer token; per-job and daily character budgets; per-client rate limit; active and stored job caps; request body limits | `test_per_client_rate_limit`, `test_daily_character_budget`, `test_busy_server_returns_503_with_retry_after`, `test_oversized_bodies_are_rejected_early` |
| 7 | Token guessing | Constant-time comparison; after 10 failures per client per minute the client is locked out *before* comparison, so a correct guess during lockout is also refused (no oracle); tokens must be 16+ characters | `test_repeated_failures_lock_the_client_out_even_for_the_right_token` |
| 8 | Rate-limit evasion via `X-Forwarded-For` | Header ignored unless `TRUST_PROXY_HEADERS`; when trusted, the last hop (the one the proxy appended) is used, not the client-controlled first | `test_forwarded_for_is_ignored_unless_a_proxy_is_trusted`, `test_trusted_proxy_uses_the_last_forwarded_hop` |
| 9 | Job enumeration | Unknown and malformed ids return the same `404`; ids are unguessable | `test_unknown_and_malformed_job_ids_look_identical` |
| 10 | XSS, clickjacking, MIME sniffing | Strict CSP (`script-src 'self'`, `style-src 'self'`, no inline), `frame-ancestors 'none'`, `nosniff`, `no-referrer`; the UI uses no inline styles or scripts; HSTS when the request was HTTPS | `test_security_headers_are_present`, `test_hsts_only_when_the_request_was_https` |
| 11 | CSRF | No cookies and no ambient credentials: auth is an explicit `Authorization` header, so a cross-site page cannot ride a session. CORS is off by default and rejects wildcards | `test_no_cors_headers_by_default`, `test_cors_allows_only_configured_origins` |
| 12 | Information disclosure in errors | One error shape; validation errors name the field but never echo the value; unexpected errors return a generic message plus a request id | `test_validation_errors_never_echo_the_input`, `test_unhandled_errors_do_not_leak_details` |
| 13 | Hidden or confusing text (bidi overrides, control characters) | Stripped during normalisation | `TestNormalize` |
| 14 | Vulnerable dependencies | Pinned `requirements.lock` and `package-lock.json`; `pip-audit` and `npm audit` in CI; weekly scheduled run; Dependabot; CodeQL | CI `security` workflow |
| 15 | CI compromise | Workflows run with `contents: read`; only CodeQL gets `security-events: write`; no secrets are needed to build or test | Workflow files |
| 16 | Container breakout or persistence | Non-root user, read-only root filesystem, tmpfs for scratch, all capabilities dropped, `no-new-privileges`, PID and memory limits, published on localhost only | `docker-compose.yml`; CI smoke test runs with the same flags |
| 17 | Private audio left on disk | Directory `0700`, files `0600`, atomic writes, deletion on start-over, TTL expiry, orphan purge at boot | `TestStore`, `test_finished_jobs_expire_after_the_ttl` |
| 18 | Key sent to, or server tricked into fetching from, a third party via voice previews (SSRF) | Separate HTTP client with no default headers; only `https` URLs on an allowlisted CDN host and default port, only URLs the ElevenLabs API itself returned; no redirects; 2 MB cap; per-client rate limit | `test_previews_are_fetched_without_the_api_key_and_cached`, `test_previews_from_unexpected_urls_are_never_fetched`, `test_preview_redirects_and_non_audio_bytes_are_rejected` |
| 19 | Third-party content served with a dangerous type | The preview's content type is derived from its magic bytes (MP3, WAV, Ogg) and set by the server; anything else is refused. The CDN's own header is ignored | `test_preview_audio_is_identified_by_its_bytes` |
| 20 | Cached audio outliving the user's "delete" | Cache file names are SHA-256 digests, so they reveal no text; entries expire (`CACHE_TTL_SECONDS`, default 1 day) and are size-bounded; `CACHE_MAX_MB=0` disables caching; the UI states the retention | `test_file_names_reveal_nothing_about_the_text`, `test_entries_expire`, `test_the_cache_can_be_disabled` |
| 21 | Cache poisoning or path traversal through keys | Keys are computed by the server, never accepted from clients, and must match `^[0-9a-f]{64}$` before touching the filesystem; a corrupt entry is a miss | `test_bad_keys_and_oversized_clips_are_ignored`, `test_corrupt_entries_are_misses_not_errors` |
| 22 | Markup injection through captions | WebVTT cue text is HTML-escaped; `-->` is neutralised in SRT | `test_webvtt_format_escapes_markup`, `test_srt_format` |
| 23 | Spend on unexpected models | Users can choose only models on the operator's allowlist that the provider actually lists; unknown ids are rejected with `422` | `test_elevenlabs_models_are_limited_to_the_allowlist` |

## Secret handling policy

- The key is supplied through the environment at run time. It is never baked into an image, written to a file the app owns, or placed in frontend build variables.
- For anything beyond local use, inject it from the platform's secret store rather than a `.env` file.
- Use a dedicated ElevenLabs key for this app and set a spending limit on the account. That is the one control that holds even if every other layer fails.

### If a key leaks

1. Revoke it in the ElevenLabs dashboard immediately, then create a new one.
2. Remove it from git history (`git filter-repo`) and force-push. Removing it in a later commit is not enough, and the key must still be treated as burned.
3. Check usage for the exposure window.
4. Run `make scan` and confirm gitleaks is clean before re-deploying.

## Deployment checklist

- [ ] Serve only over HTTPS (terminate TLS at a reverse proxy). The container binds to `127.0.0.1` by default.
- [ ] Set `CHAPTERCAST_ACCESS_TOKEN` to a random 32+ character value (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
- [ ] Set `CHAPTERCAST_TRUST_PROXY_HEADERS=true` **only** if a proxy you control sets `X-Forwarded-For` and `X-Forwarded-Proto`.
- [ ] Keep `CHAPTERCAST_DAILY_CHAR_BUDGET` at a value you are happy to lose in a day.
- [ ] Set a spend limit on the ElevenLabs account.
- [ ] Leave `CHAPTERCAST_ENABLE_DOCS` off in production.
- [ ] Review `CHAPTERCAST_ALLOWED_MODELS`, and decide on cache retention (`CHAPTERCAST_CACHE_TTL_SECONDS`, or `CHAPTERCAST_CACHE_MAX_MB=0` to disable it).

## Known limitations

- Access control is a single shared token. There are no per-user identities, quotas or audit trail.
- Rate limits and the daily budget are in-process. With multiple replicas each has its own counters (see the scaling path).
- A job id is a capability: anyone holding it (and the token, if configured) can read that job's audio.
- The clip cache is shared across all users of one server. An estimate can reveal that identical text was recently generated with the same voice and settings. That is acceptable behind one shared token; a multi-tenant deployment would add a per-user salt to the cache key, or disable the cache.
- The access token lives in `sessionStorage`, so any script running on the page could read it. The strict CSP (no inline script, same-origin scripts only) is the mitigation; there is no cookie-based alternative that avoids the CSRF trade-off.
- Text is processed by a third party. Treat anything submitted as shared with the provider.
- The `blob:` audio approach buffers the file client side.
- Security headers and the CSP are verified by tests and by `curl` against a running server, not by a browser-based end-to-end suite.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
