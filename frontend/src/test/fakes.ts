import { vi } from "vitest";
import { ApiError, type Api } from "../api/client";
import type { Config, Job, Voices } from "../api/schemas";

export const CONFIG: Config = {
  provider: "demo",
  auth_required: false,
  max_chars_per_job: 200,
  chunk_max_chars: 50,
};

export const VOICES: Voices = {
  provider: "demo",
  voices: [
    { voice_id: "v1", name: "Aria", category: "demo", description: null },
    { voice_id: "v2", name: "Orion", category: null, description: null },
  ],
};

export function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: "a".repeat(32),
    status: "queued",
    voice_id: "v1",
    char_count: 40,
    progress: { completed_chunks: 0, total_chunks: 2 },
    created_at: "2026-10-01T10:00:00Z",
    duration_seconds: null,
    audio_url: null,
    error: null,
    ...overrides,
  };
}

/** Jobs returned by createJob and then successive getJob calls. */
export function makeApi(sequence: Job[], overrides: Partial<Api> = {}): Api & { token: string | null } {
  const queue = [...sequence];
  const state = { token: null as string | null };
  const api = {
    get token() {
      return state.token;
    },
    getConfig: vi.fn(async () => CONFIG),
    listVoices: vi.fn(async () => VOICES),
    createJob: vi.fn(async () => queue.shift() ?? makeJob()),
    getJob: vi.fn(async () => queue.shift() ?? queue[queue.length - 1] ?? makeJob()),
    cancelJob: vi.fn(async () => undefined),
    fetchAudio: vi.fn(async () => new Blob(["RIFF"], { type: "audio/wav" })),
    hasToken: vi.fn(() => state.token !== null),
    setToken: vi.fn((token: string | null) => {
      state.token = token;
    }),
    ...overrides,
  };
  return api as Api & { token: string | null };
}

export const unauthorized = () => new ApiError(401, "unauthorized", "A valid access token is required.");

export const SUCCEEDED = (id = "a".repeat(32)) =>
  makeJob({
    id,
    status: "succeeded",
    progress: { completed_chunks: 2, total_chunks: 2 },
    duration_seconds: 75,
    audio_url: `/api/jobs/${id}/audio`,
  });
