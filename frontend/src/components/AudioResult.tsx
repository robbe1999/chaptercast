import { useEffect, useRef, useState } from "react";
import type { Api } from "../api/client";
import type { Job, TranscriptWord } from "../api/schemas";
import { usePlaybackTime } from "../hooks/usePlaybackTime";
import { downloadBlob } from "../lib/download";
import { ReadAlong } from "./ReadAlong";

interface Props {
  api: Api;
  job: Job;
  audioUrl: string;
  extension: string;
  cacheEnabled: boolean;
  onReset: () => void;
}

const SPEEDS = [0.75, 1, 1.25, 1.5, 2];

export function formatDuration(seconds: number | null): string | null {
  if (seconds === null || !Number.isFinite(seconds)) return null;
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

export function AudioResult({ api, job, audioUrl, extension, cacheEnabled, onReset }: Props) {
  const audio = useRef<HTMLAudioElement>(null);
  const time = usePlaybackTime(audio);
  const [words, setWords] = useState<TranscriptWord[] | null>(null);
  const [rate, setRate] = useState(1);
  const [notice, setNotice] = useState<string | null>(null);
  const duration = formatDuration(job.duration_seconds);
  const sections = job.progress.total_chunks;

  useEffect(() => {
    if (!job.transcript_url) return;
    const controller = new AbortController();
    api.getTranscript(job.id, controller.signal).then(
      (transcript) => setWords(transcript.words),
      () => undefined, // the read-along is a bonus; the audio still works without it
    );
    return () => controller.abort();
  }, [api, job.id, job.transcript_url]);

  useEffect(() => {
    if (audio.current) audio.current.playbackRate = rate;
  }, [rate]);

  const seek = (seconds: number) => {
    const element = audio.current;
    if (!element) return;
    element.currentTime = seconds;
    void element.play()?.catch(() => undefined);
  };

  const saveCaptions = async (format: "srt" | "vtt") => {
    setNotice(null);
    try {
      downloadBlob(await api.fetchCaptions(job.id, format), `chaptercast.${format}`);
    } catch {
      setNotice("The captions could not be downloaded. Try again.");
    }
  };

  return (
    <section className="card" aria-labelledby="result-title">
      <h2 id="result-title">Your audio is ready</h2>
      <audio ref={audio} controls src={audioUrl} aria-label="Narrated audio" />

      <div className="row between">
        <p className="muted small">
          {job.char_count.toLocaleString()} characters
          {duration ? ` · about ${duration}` : ""} · {sections} section{sections === 1 ? "" : "s"}
          {job.cached_chunks > 0 &&
            ` · ${job.cached_chunks} reused, ${job.billed_characters.toLocaleString()} characters billed`}
        </p>
        <label className="inline small">
          Speed{" "}
          <select value={rate} onChange={(event) => setRate(Number(event.target.value))}>
            {SPEEDS.map((speed) => (
              <option key={speed} value={speed}>
                {speed}×
              </option>
            ))}
          </select>
        </label>
      </div>

      {words && words.length > 0 && (
        <>
          <h3 className="small-heading">Read along</h3>
          <ReadAlong words={words} time={time} onSeek={seek} />
        </>
      )}

      <div className="row">
        <a className="button primary" href={audioUrl} download={`chaptercast.${extension}`}>
          Download .{extension}
        </a>
        {job.captions && (
          <>
            <button type="button" onClick={() => void saveCaptions("srt")}>
              Captions .srt
            </button>
            <button type="button" onClick={() => void saveCaptions("vtt")}>
              Captions .vtt
            </button>
          </>
        )}
        <button type="button" onClick={onReset}>
          Start over
        </button>
      </div>
      {notice && (
        <p role="alert" className="error small">
          {notice}
        </p>
      )}
      <p className="muted small">
        Audio is deleted from the server when you start over, or after an hour.
        {cacheEnabled &&
          " Individual sections stay cached for up to a day, so re-generating an edited chapter only pays for what changed."}
      </p>
    </section>
  );
}
