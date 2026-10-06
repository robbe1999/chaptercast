# Models

ChapterCast offers five ElevenLabs text-to-speech models. What each one can do lives in one place, the model registry in `backend/chaptercast/models.py`, and the values there come from the checks below.

## Verified against the live API on 2026-10-06

Capabilities as reported by `GET /v1/models` for my API key:

| Model | Text to speech | `can_use_style` | `can_use_speaker_boost` | Credits per character (`model_rates.character_cost_multiplier`) | `token_cost_factor` | Max characters per request | Languages | Alpha access |
|---|---|---|---|---|---|---|---|---|
| `eleven_v4` | yes | no | no | 1.0 | 1.0 | 10,000 | 85 | no |
| `eleven_v4_turbo` | yes | no | no | 0.5 | 1.0 | 10,000 | 85 | no |
| `eleven_multilingual_v2` | yes | yes | yes | 1.0 | 1.0 | 10,000 | 29 | no |
| `eleven_flash_v2_5` | yes | no | no | 0.5 | 1.0 | 40,000 | 32 | no |
| `eleven_turbo_v2_5` | yes | no | no | 0.5 | 1.0 | 40,000 | 32 | no |
| `eleven_v3` (not offered in ChapterCast) | yes | no | no | 1.0 | 1.0 | 5,000 | 74 | no |

Real requests to `POST /v1/text-to-speech/{voice_id}/with-timestamps` with `output_format=mp3_44100_128`, one premade voice:

| Check | `eleven_v4` | `eleven_v4_turbo` | `eleven_multilingual_v2` |
|---|---|---|---|
| Plain request, 43 characters (`The lamp was lit. The sea was calm tonight.`) | 200 | 200 | 200 |
| `alignment` returned, characters equal the request text | yes | yes | yes |
| `normalized_alignment` returned | yes, identical to `alignment` | yes, identical to `alignment` | yes, padded with a leading and trailing space (45 characters) |
| Last character end time vs decoded audio length (ffprobe) | 3.600 s vs 3.520 s (80 ms past the end) | 3.520 s vs 3.440 s (80 ms past the end) | 2.879 s vs 2.879 s (exact) |
| `character-cost` response header for 43 characters | 43 | 21 | 43 |
| `previous_text` and `next_text` set (text `Yes.`) | accepted (200) | accepted (200) | not re-tested today |
| Context characters billed | no (`character-cost` 4 for the 4-character text) | no (`character-cost` 2) | |
| Expression tag (`[warm] Quiet.`) | 200, billed 13 (the tag counts) | 200, billed 6 (13 at 0.5) | not tested |
| How the tag appears in the alignment | as its own characters, squeezed into the first 0.08 s, before the first real word | as its own characters, in the first 0.26 s | |
| `voice_settings` with `stability`, `similarity_boost`, `style`, `speed`, `use_speaker_boost` | accepted (200), no field rejected | accepted (200), no field rejected | |
| `tts-latency-ms` response header | about 520 ms (short texts) | 107 to 143 ms | 992 ms |
| Unknown voice id | 404 `voice_not_found`, not billed | | |
| Unknown model id | 400 `model_not_found`, not billed | | |

Every request also returned a top-level `quality_check` field, which was `null` each time.

End to end through ChapterCast (`POST /api/jobs` with the same 43-character text, fresh cache):

| | `eleven_v4` | `eleven_v4_turbo` |
|---|---|---|
| Estimate before generating | 43 characters, 43 credits | 43 characters, 22 credits |
| Job | succeeded, word timings available | succeeded, word timings available |
| Audio length (ffprobe) | 3.600 s | 3.520 s |
| Last word end before clamping | 3.68 s | 3.60 s |
| Read-along words and SRT/WebVTT captions | all 8 words, in order | all 8 words, in order |

## Not verified

- Whether v4 and v4 Turbo actually *use* `previous_text` and `next_text`. The API accepts them, but one short request cannot show a difference in intonation.
- Whether `style` and `use_speaker_boost` have any effect on v4. The API accepts them, `/v1/models` says the models cannot use them, so ChapterCast drops them for these models.
- SSML `<break>` tags on any model. ChapterCast never sends SSML; the registry records "no SSML breaks" for v4 from the documentation only.
- What the older models do with an expression tag (whether they read it aloud). ChapterCast removes tags before sending to them, so this never happens.
- Rounding of credits for half-price models beyond the single observation above (43 characters billed as 21). ChapterCast's estimate rounds up, so it showed 22 for the same text.
- The plain-endpoint fallback and the no-context path against the live API. Every registered model supports timestamps and context, so those paths are tested with fake models only.
- Long texts, many requests, rate limits and concurrency limits for v4 (`concurrency_group` is `standard_eleven_v4`).

## What I learned

The first surprise was that `token_cost_factor` is 1.0 for every model, including the two half-price ones. The number that matches what I am billed is `model_rates.character_cost_multiplier`, and the `character-cost` response header confirmed it: the same 43 characters cost 43 on v4 and 21 on v4 Turbo. The context fields are free, and so are failed requests, but expression tags are billed like any other text.

On my test sentence, v4 took 3.52 s to say what Multilingual v2 said in 2.88 s, a slower and more deliberate delivery for the same voice. Its alignment also runs 80 ms past the end of the decoded audio, where v2's ends exactly on it, so I treat the alignment as accurate to about a tenth of a second rather than to the millisecond. v4 Turbo reported a server latency of around 110 ms against about 1 s for v2, which is the difference the "low latency" label is about.

Expression tags come back in the alignment as real characters with tiny timings before the first spoken word. That makes them easy to remove from the read-along and the captions without disturbing the timings of the words people actually hear.

v4 is not deterministic: the same 43-character request gave 3.52 s of audio in the morning and 3.60 s when I ran it again through ChapterCast. In both runs the alignment ended 80 ms after the audio did, so ChapterCast now clamps word timings to the length of the audio it actually stitched.
