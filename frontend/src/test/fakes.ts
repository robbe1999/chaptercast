import { vi } from "vitest";
import { ApiError, type Api } from "../api/client";
import type {
  Capabilities,
  Config,
  Estimate,
  Job,
  Model,
  Models,
  Transcript,
  Voices,
} from "../api/schemas";

export const CONFIG: Config = {
  provider: "demo",
  auth_required: false,
  max_chars_per_job: 200,
  chunk_max_chars: 50,
  cache_enabled: true,
};

export const VOICES: Voices = {
  provider: "demo",
  voices: [
    {
      voice_id: "v1",
      name: "Aria",
      category: "demo",
      description: null,
      labels: { accent: "british", use_case: "narrative_story" },
      preview_url: "/api/voices/v1/preview",
    },
    { voice_id: "v2", name: "Orion", category: null, description: "Calm", labels: {}, preview_url: null },
  ],
};

export const ALL_CAPABILITIES: Capabilities = {
  timestamps: true,
  context_stitching: true,
  style: true,
  speaker_boost: true,
  audio_tags: false,
  ssml_breaks: false,
};

export function makeModel(overrides: Partial<Model> = {}): Model {
  return {
    model_id: "m1",
    label: "Standard",
    description: "Full price",
    cost_multiplier: 1,
    max_chars_per_request: 10_000,
    latency_class: "standard",
    capabilities: ALL_CAPABILITIES,
    ...overrides,
  };
}

export const MODELS: Models = {
  provider: "demo",
  default_model_id: "m1",
  models: [
    makeModel({ model_id: "m1", label: "Standard", description: "Full price" }),
    makeModel({
      model_id: "m2",
      label: "Fast",
      description: "Half price",
      cost_multiplier: 0.5,
      latency_class: "low",
      capabilities: { ...ALL_CAPABILITIES, style: false, speaker_boost: false },
    }),
    makeModel({
      model_id: "m4",
      label: "Expressive",
      description: "Understands tags",
      capabilities: { ...ALL_CAPABILITIES, style: false, speaker_boost: false, audio_tags: true },
    }),
  ],
};

const NO_STYLE = { ...ALL_CAPABILITIES, style: false, speaker_boost: false };

/** The five registered ElevenLabs models, shaped as GET /api/models returns them. */
export const FIVE_MODELS: Models = {
  provider: "elevenlabs",
  default_model_id: "eleven_multilingual_v2",
  models: [
    makeModel({ model_id: "eleven_multilingual_v2", label: "Eleven Multilingual v2", description: "Stable." }),
    makeModel({
      model_id: "eleven_flash_v2_5",
      label: "Eleven Flash v2.5",
      description: "Fast.",
      cost_multiplier: 0.5,
      latency_class: "low",
      capabilities: NO_STYLE,
    }),
    makeModel({
      model_id: "eleven_turbo_v2_5",
      label: "Eleven Turbo v2.5",
      description: "Turbo.",
      cost_multiplier: 0.5,
      latency_class: "low",
      capabilities: NO_STYLE,
    }),
    makeModel({
      model_id: "eleven_v4",
      label: "Eleven v4",
      description: "Expressive with audio tags.",
      capabilities: { ...NO_STYLE, audio_tags: true },
    }),
    makeModel({
      model_id: "eleven_v4_turbo",
      label: "Eleven v4 Turbo",
      description: "v4 with audio tags at low latency.",
      cost_multiplier: 0.5,
      latency_class: "low",
      capabilities: { ...NO_STYLE, audio_tags: true },
    }),
  ],
};

export function makeEstimate(overrides: Partial<Estimate> = {}): Estimate {
  return {
    characters: 40,
    chunks: 2,
    cached_chunks: 0,
    billable_characters: 40,
    cost_multiplier: 1,
    estimated_credits: 40,
    max_chars_per_job: 200,
    within_limit: true,
    daily_budget_remaining: 25_000,
    tags_ignored: false,
    ...overrides,
  };
}

export const TRANSCRIPT: Transcript = {
  duration_seconds: 3,
  words: [
    { text: "Hello", start: 0, end: 0.5, paragraph: 0 },
    { text: "there.", start: 0.6, end: 1.2, paragraph: 0 },
    { text: "Next", start: 1.5, end: 2, paragraph: 1 },
    { text: "part.", start: 2.1, end: 2.8, paragraph: 1 },
  ],
};

export function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: "a".repeat(32),
    status: "queued",
    voice_id: "v1",
    model_id: "m1",
    char_count: 40,
    progress: { completed_chunks: 0, total_chunks: 2 },
    cached_chunks: 0,
    billed_characters: 0,
    created_at: "2026-10-01T10:00:00Z",
    duration_seconds: null,
    audio_url: null,
    word_timings: false,
    transcript_url: null,
    captions: null,
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
    listModels: vi.fn(async () => MODELS),
    estimate: vi.fn(async () => makeEstimate()),
    getTranscript: vi.fn(async () => TRANSCRIPT),
    fetchCaptions: vi.fn(async () => new Blob(["WEBVTT"], { type: "text/vtt" })),
    fetchPreview: vi.fn(async () => new Blob(["ID3"], { type: "audio/mpeg" })),
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
    billed_characters: 40,
    audio_url: `/api/jobs/${id}/audio`,
    word_timings: true,
    transcript_url: `/api/jobs/${id}/transcript`,
    captions: { srt: `/api/jobs/${id}/captions.srt`, vtt: `/api/jobs/${id}/captions.vtt` },
  });
