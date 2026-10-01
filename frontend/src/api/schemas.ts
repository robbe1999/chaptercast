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
});

export const VoiceSchema = z.object({
  voice_id: z.string(),
  name: z.string(),
  category: z.string().nullable(),
  description: z.string().nullable(),
});

export const VoicesSchema = z.object({
  provider: ProviderSchema,
  voices: z.array(VoiceSchema),
});

export const JobStatusSchema = z.enum(["queued", "running", "succeeded", "failed", "cancelled"]);

export const JobSchema = z.object({
  id: z.string(),
  status: JobStatusSchema,
  voice_id: z.string(),
  char_count: z.number().int(),
  progress: z.object({
    completed_chunks: z.number().int(),
    total_chunks: z.number().int(),
  }),
  created_at: z.string(),
  duration_seconds: z.number().nullable(),
  audio_url: z.string().nullable(),
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
export type JobStatus = z.infer<typeof JobStatusSchema>;
