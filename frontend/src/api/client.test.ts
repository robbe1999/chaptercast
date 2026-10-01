import { describe, expect, it, vi } from "vitest";
import { ApiError, createApi, type TokenStore } from "./client";

const JOB = {
  id: "a".repeat(32),
  status: "queued",
  voice_id: "v1",
  char_count: 10,
  progress: { completed_chunks: 0, total_chunks: 1 },
  created_at: "2026-10-01T10:00:00Z",
  duration_seconds: null,
  audio_url: null,
  error: null,
};

const reply = (body: unknown, init: ResponseInit = {}) =>
  new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" }, ...init });

function memoryStore(initial: string | null = null): TokenStore {
  let token = initial;
  return { get: () => token, set: (value) => void (token = value) };
}

describe("createApi", () => {
  it("sends the bearer token and only to relative same-origin paths", async () => {
    const fetchImpl = vi.fn(async () => reply({ provider: "demo", voices: [] }));
    const api = createApi({ fetchImpl, tokenStore: memoryStore("secret-token") });
    await api.listVoices();

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/voices");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer secret-token");
    expect(init.credentials).toBe("same-origin");
    expect(init.referrerPolicy).toBe("no-referrer");
  });

  it("omits Authorization when there is no token", async () => {
    const fetchImpl = vi.fn(async () => reply({ provider: "demo", voices: [] }));
    await createApi({ fetchImpl, tokenStore: memoryStore() }).listVoices();
    const init = (fetchImpl.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(new Headers(init.headers).has("Authorization")).toBe(false);
  });

  it("posts jobs as JSON using the snake_case contract", async () => {
    const fetchImpl = vi.fn(async () => reply(JOB, { status: 202 }));
    const job = await createApi({ fetchImpl, tokenStore: memoryStore() }).createJob({
      text: "Hello.",
      voiceId: "v1",
    });
    const init = (fetchImpl.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ text: "Hello.", voice_id: "v1" });
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(job.id).toBe(JOB.id);
  });

  it("turns the server error envelope into an ApiError", async () => {
    const fetchImpl = vi.fn(async () =>
      reply({ error: { code: "rate_limited", message: "Slow down.", request_id: "r" } }, {
        status: 429,
        headers: { "retry-after": "12", "content-type": "application/json" },
      }),
    );
    const error = await createApi({ fetchImpl, tokenStore: memoryStore() })
      .createJob({ text: "x", voiceId: "v" })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 429, code: "rate_limited", message: "Slow down.", retryAfter: 12 });
  });

  it("falls back to a generic error when the body is not the envelope", async () => {
    const fetchImpl = vi.fn(async () => new Response("<html>bad gateway</html>", { status: 502 }));
    const error = await createApi({ fetchImpl, tokenStore: memoryStore() }).getConfig().catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 502, code: "http_error" });
    expect((error as ApiError).message).not.toContain("html");
  });

  it("rejects responses that do not match the schema", async () => {
    const fetchImpl = vi.fn(async () => reply({ unexpected: true }));
    const error = await createApi({ fetchImpl, tokenStore: memoryStore() }).getConfig().catch((e: unknown) => e);
    expect(error).toMatchObject({ code: "bad_response" });
  });

  it("reports network failures without leaking internals", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("Failed to fetch: https://internal.example/secret");
    });
    const error = await createApi({ fetchImpl, tokenStore: memoryStore() }).getConfig().catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 0, code: "network" });
    expect((error as ApiError).message).not.toContain("internal.example");
  });

  it("lets aborts through untouched so callers can ignore them", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new DOMException("Aborted", "AbortError");
    });
    const error = await createApi({ fetchImpl, tokenStore: memoryStore() }).getConfig().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(DOMException);
  });

  it("encodes job ids in paths", async () => {
    const fetchImpl = vi.fn(async () => reply(JOB));
    await createApi({ fetchImpl, tokenStore: memoryStore() }).getJob("../x?y=1");
    expect((fetchImpl.mock.calls[0] as unknown as [string])[0]).toBe("/api/jobs/..%2Fx%3Fy%3D1");
  });

  it("downloads audio as a blob without putting the token in the URL", async () => {
    const fetchImpl = vi.fn(
      async () => new Response("abc", { headers: { "content-type": "audio/mpeg" } }),
    );
    const blob = await createApi({ fetchImpl, tokenStore: memoryStore("tok-123456") }).fetchAudio(JOB.id);
    const [url] = fetchImpl.mock.calls[0] as unknown as [string];
    expect(url).toBe(`/api/jobs/${JOB.id}/audio`);
    expect(url).not.toContain("tok-123456");
    expect(await blob.text()).toBe("abc");
  });

  it("clears and reports the token state", () => {
    const store = memoryStore();
    const api = createApi({ fetchImpl: vi.fn(), tokenStore: store });
    expect(api.hasToken()).toBe(false);
    api.setToken("abc");
    expect(api.hasToken()).toBe(true);
    api.setToken(null);
    expect(api.hasToken()).toBe(false);
  });
});
