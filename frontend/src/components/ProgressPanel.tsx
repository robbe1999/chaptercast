import type { Job } from "../api/schemas";

interface Props {
  job: Job | null;
  onCancel: () => void;
}

export function ProgressPanel({ job, onCancel }: Props) {
  const total = job?.progress.total_chunks ?? 0;
  const done = job?.progress.completed_chunks ?? 0;
  const working = job?.status === "running" && total > 0;

  const label =
    job === null
      ? "Submitting…"
      : job.status === "queued"
        ? "Waiting for a free slot…"
        : `Narrating section ${Math.min(done + 1, total)} of ${total}`;

  return (
    <section className="panel narrow" aria-labelledby="progress-title">
      <h2 id="progress-title">Generating audio</h2>
      {/* A native <progress> needs no inline style, which the strict CSP forbids. */}
      <progress
        aria-label="Generation progress"
        max={working ? total : undefined}
        value={working ? done : undefined}
      />
      <p role="status" aria-live="polite" className="muted">
        {label}
      </p>
      <button type="button" onClick={onCancel}>
        Cancel
      </button>
    </section>
  );
}
