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

  const hasTranscript = words !== null && words.length > 0;

  return (
    <section className={hasTranscript ? "workspace" : "workspace single"} aria-labelledby="result-title">
      {hasTranscript && (
        <div className="panel reader">
          <div className="panel-head">
            <h3>Read along</h3>
            <span className="muted small">Click any word to jump there</span>
          </div>
          <ReadAlong words={words} time={time} onSeek={seek} />
        </div>
      )}

      <aside className="panel sidebar">
        <h2 id="result-title">Your audio is ready</h2>
        <audio ref={audio} controls src={audioUrl} aria-label="Narrated audio" />
        {!job.word_timings && (
          <p className="muted small">
            This model did not return word timings, so there is no read-along or captions for this
            audio.
          </p>
        )}

        <dl className="stats">
          <div>
            <dt>Length</dt>
            <dd>{duration ? `about ${duration}` : "unknown"}</dd>
          </div>
          <div>
            <dt>Characters</dt>
            <dd>{job.char_count.toLocaleString()}</dd>
          </div>
          <div>
            <dt>Sections</dt>
            <dd>
              {sections}
              {job.cached_chunks > 0 && <span className="muted"> ({job.cached_chunks} reused)</span>}
            </dd>
          </div>
          <div>
            <dt>Billed</dt>
            <dd>{job.billed_characters.toLocaleString()} chars</dd>
          </div>
        </dl>

        <label className="inline small">
          Playback speed
          <select value={rate} onChange={(event) => setRate(Number(event.target.value))}>
            {SPEEDS.map((speed) => (
              <option key={speed} value={speed}>
                {speed}×
              </option>
            ))}
          </select>
        </label>

        <div className="sidebar-foot">
          <a className="button primary block" href={audioUrl} download={`chaptercast.${extension}`}>
            Download .{extension}
          </a>
          {job.captions && (
            <div className="row tight">
              <button type="button" className="grow" onClick={() => void saveCaptions("srt")}>
                Captions .srt
              </button>
              <button type="button" className="grow" onClick={() => void saveCaptions("vtt")}>
                Captions .vtt
              </button>
            </div>
          )}
          <button type="button" className="ghost block" onClick={onReset}>
            Start over
          </button>
          {notice && (
            <p role="alert" className="error small">
              {notice}
            </p>
          )}
          <p className="muted small">
            Audio is deleted from the server when you start over, or after an hour.
            {cacheEnabled &&
              " Sections stay cached for up to a day, so re-generating an edited chapter only pays for what changed."}
          </p>
        </div>
      </aside>
    </section>
  );
}
