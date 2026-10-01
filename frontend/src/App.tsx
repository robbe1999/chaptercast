import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { ApiError, createApi, type Api } from "./api/client";
import type { Config, Voice } from "./api/schemas";
import { AccessGate } from "./components/AccessGate";
import { AudioResult } from "./components/AudioResult";
import { ProgressPanel } from "./components/ProgressPanel";
import { useJob } from "./hooks/useJob";
import { SAMPLE_TEXT } from "./sample";

type Boot =
  | { stage: "loading" }
  | { stage: "gate"; error: string | null; checking: boolean }
  | { stage: "ready"; config: Config; voices: Voice[] }
  | { stage: "error"; message: string };

const CHARS_PER_SECOND = 15; // typical narration pace, for the length estimate only

interface AppProps {
  api?: Api;
  pollIntervalMs?: number;
}

export default function App({ api: injected, pollIntervalMs }: AppProps) {
  const api = useMemo(() => injected ?? createApi(), [injected]);
  const [boot, setBoot] = useState<Boot>({ stage: "loading" });
  const [text, setText] = useState("");
  const [voiceId, setVoiceId] = useState("");
  const { state, start, cancel, reset } = useJob(api, { pollIntervalMs });

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const config = await api.getConfig(signal);
        if (config.auth_required && !api.hasToken()) {
          setBoot({ stage: "gate", error: null, checking: false });
          return;
        }
        const { voices } = await api.listVoices(signal);
        setVoiceId((current) => current || voices[0]?.voice_id || "");
        setBoot({ stage: "ready", config, voices });
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (error instanceof ApiError && error.status === 401) {
          api.setToken(null);
          setBoot({ stage: "gate", error: "That token was not accepted.", checking: false });
        } else if (error instanceof ApiError && error.status === 429) {
          setBoot({ stage: "gate", error: error.message, checking: false });
        } else {
          setBoot({
            stage: "error",
            message: error instanceof ApiError ? error.message : "Could not load the app.",
          });
        }
      }
    },
    [api],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const submitToken = (token: string) => {
    api.setToken(token);
    setBoot({ stage: "gate", error: null, checking: true });
    void load();
  };

  const busy = state.phase === "submitting" || state.phase === "running";

  return (
    <main className="shell">
      <header>
        <h1>ChapterCast</h1>
        <p className="muted">Paste a chapter. Pick a voice. Get an audiobook.</p>
      </header>

      {boot.stage === "loading" && <p role="status">Loading…</p>}

      {boot.stage === "error" && (
        <p role="alert" className="error">
          {boot.message}
        </p>
      )}

      {boot.stage === "gate" && (
        <AccessGate onSubmit={submitToken} error={boot.error} busy={boot.checking} />
      )}

      {boot.stage === "ready" && (
        <>
          {boot.config.provider === "demo" && (
            <p className="banner" role="note">
              <strong>Demo mode.</strong> No ElevenLabs key is configured, so the audio is synthetic tones
              (one blip per word), not speech. Everything else is the real pipeline.
            </p>
          )}

          {(state.phase === "idle" || state.phase === "failed" || state.phase === "cancelled") && (
            <Composer
              config={boot.config}
              voices={boot.voices}
              text={text}
              voiceId={voiceId}
              onText={setText}
              onVoice={setVoiceId}
              onSubmit={() => void start(text, voiceId)}
              notice={
                state.phase === "failed"
                  ? { kind: "error", message: state.message }
                  : state.phase === "cancelled"
                    ? { kind: "info", message: "Cancelled." }
                    : null
              }
            />
          )}

          {busy && <ProgressPanel job={state.phase === "running" ? state.job : null} onCancel={cancel} />}

          {state.phase === "done" && (
            <AudioResult
              job={state.job}
              audioUrl={state.audioUrl}
              extension={state.extension}
              onReset={reset}
            />
          )}
        </>
      )}

      <footer className="muted small">
        Unofficial demo project, not affiliated with ElevenLabs. Text you submit is sent to the speech
        provider to generate audio.
      </footer>
    </main>
  );
}

interface ComposerProps {
  config: Config;
  voices: Voice[];
  text: string;
  voiceId: string;
  onText: (value: string) => void;
  onVoice: (value: string) => void;
  onSubmit: () => void;
  notice: { kind: "error" | "info"; message: string } | null;
}

function Composer({ config, voices, text, voiceId, onText, onVoice, onSubmit, notice }: ComposerProps) {
  const max = config.max_chars_per_job;
  const length = text.length;
  const tooLong = length > max;
  const empty = text.trim().length === 0;
  const sections = Math.max(1, Math.ceil(length / config.chunk_max_chars));
  const minutes = length / CHARS_PER_SECOND / 60;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!empty && !tooLong && voiceId) onSubmit();
  };

  return (
    <form className="card" onSubmit={submit} aria-labelledby="compose-title">
      <h2 id="compose-title">Your text</h2>

      <label htmlFor="text">Chapter text</label>
      <textarea
        id="text"
        rows={12}
        value={text}
        onChange={(event) => onText(event.target.value)}
        aria-describedby="text-meta"
        aria-invalid={tooLong ? true : undefined}
        placeholder="Paste or type the text to narrate…"
      />
      <div id="text-meta" className="meta">
        <span className={tooLong ? "error" : "muted"} aria-live="polite">
          {length.toLocaleString()} / {max.toLocaleString()} characters
        </span>
        <button type="button" className="link" onClick={() => onText(SAMPLE_TEXT)}>
          Use sample text
        </button>
      </div>
      {tooLong && (
        <p role="alert" className="error">
          That is {(length - max).toLocaleString()} characters over the limit. Trim the text or split it
          into parts.
        </p>
      )}

      <label htmlFor="voice">Voice</label>
      <select id="voice" value={voiceId} onChange={(event) => onVoice(event.target.value)}>
        {voices.map((voice) => (
          <option key={voice.voice_id} value={voice.voice_id}>
            {voice.name}
            {voice.category ? ` (${voice.category})` : ""}
          </option>
        ))}
      </select>

      {!empty && !tooLong && (
        <p className="muted small">
          About {sections} section{sections === 1 ? "" : "s"}, roughly{" "}
          {minutes < 1 ? "under a minute" : `${Math.round(minutes)} min`} of audio. Providers bill per
          character, so check your plan.
        </p>
      )}

      {notice && (
        <p role={notice.kind === "error" ? "alert" : "status"} className={notice.kind}>
          {notice.message}
        </p>
      )}

      <button type="submit" className="primary" disabled={empty || tooLong || !voiceId}>
        Generate audio
      </button>
    </form>
  );
}
