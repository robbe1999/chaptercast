import { useCallback, useEffect, useRef, useState } from "react";
import type { Api } from "../api/client";
import type { Voice } from "../api/schemas";

interface Props {
  api: Api;
  voices: Voice[];
  value: string;
  onChange: (voiceId: string) => void;
}

type Preview = { state: "idle" } | { state: "loading" | "playing"; voiceId: string };

export function describeVoice(voice: Voice | undefined): string {
  if (!voice) return "";
  const labels = Object.values(voice.labels).map((label) => label.replace(/_/g, " "));
  return labels.length ? labels.join(" · ") : (voice.description ?? "");
}

/** Voice dropdown plus a free preview, so nobody spends credits to find out what a voice sounds like. */
export function VoicePicker({ api, voices, value, onChange }: Props) {
  const [preview, setPreview] = useState<Preview>({ state: "idle" });
  const [error, setError] = useState<string | null>(null);
  const player = useRef<{ audio: HTMLAudioElement; url: string } | null>(null);
  const request = useRef<AbortController | null>(null);
  const voice = voices.find((v) => v.voice_id === value);

  const stop = useCallback(() => {
    request.current?.abort();
    request.current = null;
    if (player.current) {
      player.current.audio.pause();
      URL.revokeObjectURL(player.current.url);
      player.current = null;
    }
    setPreview({ state: "idle" });
  }, []);

  useEffect(() => stop, [stop]);
  useEffect(() => stop(), [value, stop]); // switching voice stops the old sample

  const play = async () => {
    if (!voice?.preview_url) return;
    stop();
    setError(null);
    const controller = new AbortController();
    request.current = controller;
    setPreview({ state: "loading", voiceId: voice.voice_id });
    try {
      const blob = await api.fetchPreview(voice.voice_id, controller.signal);
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      player.current = { audio, url };
      audio.addEventListener("ended", stop);
      setPreview({ state: "playing", voiceId: voice.voice_id });
      await audio.play();
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      stop();
      setError("The preview could not be played.");
    }
  };

  const active = preview.state !== "idle";
  const description = describeVoice(voice);

  return (
    <div className="field">
      <label htmlFor="voice">Voice</label>
      <div className="row nowrap">
        <select
          id="voice"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          aria-describedby={description ? "voice-description" : undefined}
        >
          {voices.map((v) => (
            <option key={v.voice_id} value={v.voice_id}>
              {v.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={active ? stop : () => void play()}
          disabled={!voice?.preview_url}
          aria-label={active ? "Stop voice preview" : `Preview ${voice?.name ?? "voice"}`}
        >
          {preview.state === "loading" ? "Loading…" : active ? "■ Stop" : "▶ Preview"}
        </button>
      </div>
      {description && (
        <p id="voice-description" className="muted small">
          {description}
        </p>
      )}
      {error && (
        <p role="alert" className="error small">
          {error}
        </p>
      )}
    </div>
  );
}
