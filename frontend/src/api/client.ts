import type { z } from "zod";
import {
  ConfigSchema,
  ErrorEnvelopeSchema,
  EstimateSchema,
  JobSchema,
  ModelsSchema,
  TranscriptSchema,
  VoicesSchema,
  type Config,
  type Estimate,
  type Job,
  type JobInput,
  type Models,
  type Transcript,
  type Voices,
} from "./schemas";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly retryAfter?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface Api {
  getConfig(signal?: AbortSignal): Promise<Config>;
  listVoices(signal?: AbortSignal): Promise<Voices>;
  listModels(signal?: AbortSignal): Promise<Models>;
  estimate(input: JobInput, signal?: AbortSignal): Promise<Estimate>;
  createJob(input: JobInput, signal?: AbortSignal): Promise<Job>;
  getJob(id: string, signal?: AbortSignal): Promise<Job>;
  cancelJob(id: string): Promise<void>;
  fetchAudio(id: string, signal?: AbortSignal): Promise<Blob>;
  getTranscript(id: string, signal?: AbortSignal): Promise<Transcript>;
  fetchCaptions(id: string, format: "srt" | "vtt"): Promise<Blob>;
  fetchPreview(voiceId: string, signal?: AbortSignal): Promise<Blob>;
  hasToken(): boolean;
  setToken(token: string | null): void;
}

export interface TokenStore {
  get(): string | null;
  set(token: string | null): void;
}

const TOKEN_KEY = "chaptercast.token";

/** Per-tab storage (cleared when the tab closes); falls back to memory if blocked. */
export function sessionTokenStore(): TokenStore {
  let memory: string | null = null;
  return {
    get() {
      try {
        return window.sessionStorage.getItem(TOKEN_KEY) ?? memory;
      } catch {
        return memory;
      }
    },
    set(token) {
      memory = token;
      try {
        if (token === null) window.sessionStorage.removeItem(TOKEN_KEY);
        else window.sessionStorage.setItem(TOKEN_KEY, token);
      } catch {
        /* storage unavailable: memory copy is enough for this tab */
      }
    },
  };
}

export interface ApiOptions {
  fetchImpl?: typeof fetch;
  tokenStore?: TokenStore;
}

export function createApi({ fetchImpl, tokenStore }: ApiOptions = {}): Api {
  const doFetch: typeof fetch = fetchImpl ?? ((...args) => fetch(...args));
  const store = tokenStore ?? sessionTokenStore();

  async function send(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    const token = store.get();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (init.body !== undefined) headers.set("Content-Type", "application/json");

    let response: Response;
    try {
      // Same-origin only: relative paths, no credentials/cookies, no referrer.
      response = await doFetch(path, {
        ...init,
        headers,
        credentials: "same-origin",
        referrerPolicy: "no-referrer",
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new ApiError(0, "network", "Could not reach the server. Check your connection.");
    }
    if (!response.ok) throw await toApiError(response);
    return response;
  }

  async function json<S extends z.ZodTypeAny>(
    schema: S,
    path: string,
    init?: RequestInit,
  ): Promise<z.infer<S>> {
    const response = await send(path, init);
    const parsed = schema.safeParse(await response.json().catch(() => null));
    if (!parsed.success) {
      throw new ApiError(response.status, "bad_response", "The server sent an unexpected response.");
    }
    return parsed.data;
  }

  const blob = async (path: string, signal?: AbortSignal) => (await send(path, { signal })).blob();
  const jobPath = (id: string) => `/api/jobs/${encodeURIComponent(id)}`;

  return {
    getConfig: (signal) => json(ConfigSchema, "/api/config", { signal }),
    listVoices: (signal) => json(VoicesSchema, "/api/voices", { signal }),
    listModels: (signal) => json(ModelsSchema, "/api/models", { signal }),
    estimate: (input, signal) =>
      json(EstimateSchema, "/api/estimate", { method: "POST", body: jobBody(input), signal }),
    createJob: (input, signal) =>
      json(JobSchema, "/api/jobs", { method: "POST", body: jobBody(input), signal }),
    getJob: (id, signal) => json(JobSchema, jobPath(id), { signal }),
    cancelJob: async (id) => {
      await send(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" });
    },
    // Media is fetched with the Authorization header and played from a blob: URL,
    // because <audio src> cannot send headers and tokens must never go in query strings.
    fetchAudio: (id, signal) => blob(`${jobPath(id)}/audio`, signal),
    getTranscript: (id, signal) => json(TranscriptSchema, `${jobPath(id)}/transcript`, { signal }),
    fetchCaptions: (id, format) => blob(`${jobPath(id)}/captions.${format}`),
    fetchPreview: (voiceId, signal) =>
      blob(`/api/voices/${encodeURIComponent(voiceId)}/preview`, signal),
    hasToken: () => store.get() !== null,
    setToken: (token) => store.set(token),
  };
}

function jobBody({ text, voiceId, modelId, voiceSettings }: JobInput): string {
  return JSON.stringify({
    text,
    voice_id: voiceId,
    ...(modelId ? { model_id: modelId } : {}),
    ...(voiceSettings && Object.keys(voiceSettings).length ? { voice_settings: voiceSettings } : {}),
  });
}

async function toApiError(response: Response): Promise<ApiError> {
  const retryAfter = Number(response.headers.get("retry-after"));
  const retry = Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : undefined;
  const parsed = ErrorEnvelopeSchema.safeParse(await response.json().catch(() => null));
  if (parsed.success) {
    const { code, message } = parsed.data.error;
    return new ApiError(response.status, code, message, retry);
  }
  return new ApiError(response.status, "http_error", `Request failed (${response.status}).`, retry);
}
