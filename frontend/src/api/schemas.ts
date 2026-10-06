import { z } from "zod";

// These mirror the backend contract in docs/openapi.json. Every response is
// validated at runtime, so a server/client mismatch fails loudly in one place
// instead of surfacing as `undefined is not a function` deep in a component.

export const ProviderSchema = z.enum(["elevenlabs", "demo"]);

export const ConfigSchema = z.object({
  provider: ProviderSchema,
  auth_required: z.boolean(),
  max_chars_per_job: z.number().int().positive(),
  chunk_max_chars: z.number().int().positive(),
  cache_enabled: z.boolean(),
});

export const VoiceSchema = z.object({
  voice_id: z.string(),
  name: z.string(),
  category: z.string().nullable(),
  description: z.string().nullable(),
  labels: z.record(z.string()),
  preview_url: z.string().nullable(),
});

export const VoicesSchema = z.object({
  provider: ProviderSchema,
  voices: z.array(VoiceSchema),
});

// Unknown keys are dropped rather than rejected (zod's default), so the server can add
// a capability without breaking older clients.
export const CapabilitiesSchema = z.object({
  timestamps: z.boolean(),
  context_stitching: z.boolean(),
  style: z.boolean(),
  speaker_boost: z.boolean(),
  audio_tags: z.boolean(),
  ssml_breaks: z.boolean(),
});

export const ModelSchema = z.object({
  model_id: z.string(),
  label: z.string(),
  description: z.string(),
  cost_multiplier: z.number().positive(),
  max_chars_per_request: z.number().int().positive(),
  latency_class: z.string(), // "standard" | "low"; kept open for future classes
  capabilities: CapabilitiesSchema,
});

export const ModelsSchema = z.object({
  provider: ProviderSchema,
  default_model_id: z.string(),
  models: z.array(ModelSchema).min(1),
});

export const EstimateSchema = z.object({
  characters: z.number().int(),
  chunks: z.number().int(),
  cached_chunks: z.number().int(),
  billable_characters: z.number().int(),
  cost_multiplier: z.number(),
  estimated_credits: z.number().int(),
  max_chars_per_job: z.number().int(),
  within_limit: z.boolean(),
  daily_budget_remaining: z.number().int(),
  tags_ignored: z.boolean(),
});

export const TranscriptWordSchema = z.object({
  text: z.string(),
  start: z.number(),
  end: z.number(),
  paragraph: z.number().int(),
});

export const TranscriptSchema = z.object({
  duration_seconds: z.number().nullable(),
  words: z.array(TranscriptWordSchema),
});

export const JobStatusSchema = z.enum(["queued", "running", "succeeded", "failed", "cancelled"]);

export const JobSchema = z.object({
  id: z.string(),
  status: JobStatusSchema,
  voice_id: z.string(),
  model_id: z.string(),
  char_count: z.number().int(),
  progress: z.object({
    completed_chunks: z.number().int(),
    total_chunks: z.number().int(),
  }),
  cached_chunks: z.number().int(),
  billed_characters: z.number().int(),
  created_at: z.string(),
  duration_seconds: z.number().nullable(),
  audio_url: z.string().nullable(),
  word_timings: z.boolean(),
  transcript_url: z.string().nullable(),
  captions: z.object({ srt: z.string(), vtt: z.string() }).nullable(),
  error: z.object({ code: z.string(), message: z.string() }).nullable(),
});

export const ErrorEnvelopeSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    request_id: z.string().optional(),
  }),
});

export type Provider = z.infer<typeof ProviderSchema>;
export type Config = z.infer<typeof ConfigSchema>;
export type Voice = z.infer<typeof VoiceSchema>;
export type Voices = z.infer<typeof VoicesSchema>;
export type Job = z.infer<typeof JobSchema>;
export type Model = z.infer<typeof ModelSchema>;
export type Capabilities = z.infer<typeof CapabilitiesSchema>;
export type Models = z.infer<typeof ModelsSchema>;
export type Estimate = z.infer<typeof EstimateSchema>;
export type TranscriptWord = z.infer<typeof TranscriptWordSchema>;
export type Transcript = z.infer<typeof TranscriptSchema>;

/** Optional voice tuning; omitted fields use the voice's stored defaults. */
export interface VoiceSettings {
  stability?: number;
  similarity_boost?: number;
  style?: number;
  speed?: number;
  use_speaker_boost?: boolean;
}

export interface JobInput {
  text: string;
  voiceId: string;
  modelId?: string;
  voiceSettings?: VoiceSettings;
}
export type JobStatus = z.infer<typeof JobStatusSchema>;
