import type { Job } from "../api/schemas";

interface Props {
  job: Job;
  audioUrl: string;
  extension: string;
  onReset: () => void;
}

export function formatDuration(seconds: number | null): string | null {
  if (seconds === null || !Number.isFinite(seconds)) return null;
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

export function AudioResult({ job, audioUrl, extension, onReset }: Props) {
  const duration = formatDuration(job.duration_seconds);
  return (
    <section className="card" aria-labelledby="result-title">
      <h2 id="result-title">Your audio is ready</h2>
      <audio controls src={audioUrl} aria-label="Narrated audio" />
      <p className="muted">
        {job.char_count.toLocaleString()} characters
        {duration ? ` · about ${duration}` : ""} · {job.progress.total_chunks} section
        {job.progress.total_chunks === 1 ? "" : "s"}
      </p>
      <div className="row">
        <a className="button primary" href={audioUrl} download={`chaptercast.${extension}`}>
          Download .{extension}
        </a>
        <button type="button" onClick={onReset}>
          Start over
        </button>
      </div>
      <p className="muted small">Audio is deleted from the server when you start over, or after an hour.</p>
    </section>
  );
}
