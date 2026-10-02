import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type FormEvent } from "react";
import { ApiError, createApi, type Api } from "./api/client";
import type { Config, Estimate, JobInput, Model, Voice, VoiceSettings } from "./api/schemas";
import { AccessGate } from "./components/AccessGate";
import { AudioResult } from "./components/AudioResult";
import { EstimateLine } from "./components/EstimateLine";
import { DEFAULT_SETTINGS, NarrationSettings, type TunedSettings } from "./components/NarrationSettings";
import { ProgressPanel } from "./components/ProgressPanel";
import { VoicePicker } from "./components/VoicePicker";
import { useEstimate } from "./hooks/useEstimate";
import { useJob } from "./hooks/useJob";
import { IMPORT_ACCEPT, ImportError, readTextFile } from "./lib/markdown";
import { loadPrefs, savePrefs } from "./lib/prefs";
import { SAMPLE_TEXT } from "./sample";

type Boot =
  | { stage: "loading" }
  | { stage: "gate"; error: string | null; checking: boolean }
  | { stage: "ready"; config: Config; voices: Voice[]; models: Model[]; defaultModelId: string }
  | { stage: "error"; message: string };

const CHARS_PER_SECOND = 15; // typical narration pace, for the length estimate only

interface AppProps {
  api?: Api;
  pollIntervalMs?: number;
  estimateDelayMs?: number;
}

/** The voice settings to send: none unless the user opted in, and no style where unsupported. */
export function settingsFor(custom: boolean, tuned: TunedSettings, model: Model | undefined): VoiceSettings | undefined {
  if (!custom) return undefined;
  const { style, ...rest } = tuned;
  return model?.supports_style ? { ...rest, style } : rest;
}

export default function App({ api: injected, pollIntervalMs, estimateDelayMs }: AppProps) {
  const api = useMemo(() => injected ?? createApi(), [injected]);
  const prefs = useMemo(loadPrefs, []);
  const [boot, setBoot] = useState<Boot>({ stage: "loading" });
  const [text, setText] = useState("");
  const [voiceId, setVoiceId] = useState(prefs.voiceId ?? "");
  const [modelId, setModelId] = useState(prefs.modelId ?? "");
  const [custom, setCustom] = useState(prefs.customSettings ?? false);
  const [tuned, setTuned] = useState<TunedSettings>({ ...DEFAULT_SETTINGS, ...prefs.voiceSettings });
  const { state, start, cancel, reset } = useJob(api, { pollIntervalMs });

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const config = await api.getConfig(signal);
        if (config.auth_required && !api.hasToken()) {
          setBoot({ stage: "gate", error: null, checking: false });
          return;
        }
        const [{ voices }, catalog] = await Promise.all([api.listVoices(signal), api.listModels(signal)]);
        // Remembered choices win only if the server still offers them.
        setVoiceId((current) =>
          voices.some((v) => v.voice_id === current) ? current : (voices[0]?.voice_id ?? ""),
        );
        setModelId((current) =>
          catalog.models.some((m) => m.model_id === current) ? current : catalog.default_model_id,
        );
        setBoot({
          stage: "ready",
          config,
          voices,
          models: catalog.models,
          defaultModelId: catalog.default_model_id,
        });
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

  useEffect(() => {
    if (boot.stage === "ready") savePrefs({ voiceId, modelId, customSettings: custom, voiceSettings: tuned });
  }, [boot.stage, voiceId, modelId, custom, tuned]);

  const submitToken = (token: string) => {
    api.setToken(token);
    setBoot({ stage: "gate", error: null, checking: true });
    void load();
  };

  const ready = boot.stage === "ready" ? boot : null;
  const model = ready?.models.find((m) => m.model_id === modelId);
  const composing = state.phase === "idle" || state.phase === "failed" || state.phase === "cancelled";
  const input: JobInput | null =
    ready && composing && voiceId && text.trim()
      ? { text, voiceId, modelId: model?.model_id, voiceSettings: settingsFor(custom, tuned, model) }
      : null;
  const estimate = useEstimate(api, input, estimateDelayMs);
  const busy = state.phase === "submitting" || state.phase === "running";

  return (
    <main className="shell">
      <header>
        <h1>ChapterCast</h1>
        <p className="muted">Paste a chapter. Pick a voice. Get an audiobook you can read along with.</p>
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

      {ready && (
        <>
          {ready.config.provider === "demo" && (
            <p className="banner" role="note">
              <strong>Demo mode.</strong> No ElevenLabs key is configured, so the audio is synthetic tones
              (one blip per word), not speech. Everything else is the real pipeline.
            </p>
          )}

          {composing && (
            <Composer
              api={api}
              config={ready.config}
              voices={ready.voices}
              models={ready.models}
              text={text}
              voiceId={voiceId}
              modelId={model?.model_id ?? ready.defaultModelId}
              custom={custom}
              tuned={tuned}
              estimate={estimate}
              onText={setText}
              onVoice={setVoiceId}
              onModel={setModelId}
              onCustom={setCustom}
              onTuned={setTuned}
              onSubmit={() => input && void start(input)}
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
              api={api}
              job={state.job}
              audioUrl={state.audioUrl}
              extension={state.extension}
              cacheEnabled={ready.config.cache_enabled}
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
  api: Api;
  config: Config;
  voices: Voice[];
  models: Model[];
  text: string;
  voiceId: string;
  modelId: string;
  custom: boolean;
  tuned: TunedSettings;
  estimate: Estimate | null;
  onText: (value: string) => void;
  onVoice: (value: string) => void;
  onModel: (value: string) => void;
  onCustom: (value: boolean) => void;
  onTuned: (value: TunedSettings) => void;
  onSubmit: () => void;
  notice: { kind: "error" | "info"; message: string } | null;
}

function Composer(props: ComposerProps) {
  const { api, config, voices, models, text, voiceId, estimate, onText, onSubmit, notice } = props;
  const [importError, setImportError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const max = config.max_chars_per_job;
  const length = text.length;
  const tooLong = length > max;
  const empty = text.trim().length === 0;
  const minutes = length / CHARS_PER_SECOND / 60;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!empty && !tooLong && voiceId) onSubmit();
  };

  const importFile = async (file: File | undefined) => {
    if (!file) return;
    setImportError(null);
    try {
      onText(await readTextFile(file));
    } catch (error) {
      setImportError(error instanceof ImportError ? error.message : "That file could not be read.");
    }
  };

  const drop = (event: DragEvent<HTMLTextAreaElement>) => {
    event.preventDefault();
    setDragging(false);
    void importFile(event.dataTransfer.files[0]);
  };

  return (
    <form className="card" onSubmit={submit} aria-labelledby="compose-title">
      <h2 id="compose-title">Your text</h2>

      <label htmlFor="text">Chapter text</label>
      <textarea
        id="text"
        rows={12}
        value={text}
        className={dragging ? "dropping" : undefined}
        onChange={(event) => onText(event.target.value)}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={drop}
        aria-describedby="text-meta"
        aria-invalid={tooLong ? true : undefined}
        placeholder="Paste or type the text to narrate, or drop a .txt or .md file here…"
      />
      <div id="text-meta" className="meta">
        <span className={tooLong ? "error" : "muted"} aria-live="polite">
          {length.toLocaleString()} / {max.toLocaleString()} characters
        </span>
        <span className="row">
          <button type="button" className="link" onClick={() => fileInput.current?.click()}>
            Import .txt or .md
          </button>
          <button type="button" className="link" onClick={() => onText(SAMPLE_TEXT)}>
            Use sample text
          </button>
        </span>
      </div>
      <input
        ref={fileInput}
        type="file"
        accept={IMPORT_ACCEPT}
        className="visually-hidden"
        tabIndex={-1}
        aria-label="Import a text or Markdown file"
        onChange={(event) => {
          void importFile(event.target.files?.[0]);
          event.target.value = ""; // allow re-importing the same file
        }}
      />
      {importError && (
        <p role="alert" className="error">
          {importError}
        </p>
      )}
      {tooLong && (
        <p role="alert" className="error">
          That is {(length - max).toLocaleString()} characters over the limit. Trim the text or split it
          into parts.
        </p>
      )}

      <VoicePicker api={api} voices={voices} value={voiceId} onChange={props.onVoice} />

      <NarrationSettings
        models={models}
        modelId={props.modelId}
        onModel={props.onModel}
        custom={props.custom}
        onCustom={props.onCustom}
        settings={props.tuned}
        onSettings={props.onTuned}
      />

      {!empty && !tooLong && <EstimateLine estimate={estimate} minutes={minutes} />}

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
