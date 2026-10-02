import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type Api } from "../api/client";
import type { Job, JobInput } from "../api/schemas";

export type JobState =
  | { phase: "idle" }
  | { phase: "submitting" }
  | { phase: "running"; job: Job }
  | { phase: "done"; job: Job; audioUrl: string; extension: string }
  | { phase: "failed"; code: string; message: string }
  | { phase: "cancelled" };

export interface UseJobOptions {
  pollIntervalMs?: number;
}

const MAX_POLL_DELAY_MS = 5000;
const MAX_CONSECUTIVE_POLL_FAILURES = 5;

const isAbort = (error: unknown): boolean =>
  error instanceof DOMException && error.name === "AbortError";

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(new DOMException("Aborted", "AbortError"));
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

const extensionFor = (mime: string): string => (mime.includes("mpeg") ? "mp3" : "wav");

/**
 * Drives one narration job: submit, poll with backoff, fetch the audio.
 * Owns the abort controller and the blob URL, so unmounting or starting over
 * can never leak a request, a timer or an object URL.
 */
export function useJob(api: Api, { pollIntervalMs = 700 }: UseJobOptions = {}) {
  const [state, setState] = useState<JobState>({ phase: "idle" });
  const controller = useRef<AbortController | null>(null);
  const jobId = useRef<string | null>(null);
  const audioUrl = useRef<string | null>(null);

  const releaseAudio = useCallback(() => {
    if (audioUrl.current) URL.revokeObjectURL(audioUrl.current);
    audioUrl.current = null;
  }, []);

  /** Best-effort server-side delete so finished audio does not linger. */
  const discardRemote = useCallback(() => {
    const id = jobId.current;
    jobId.current = null;
    if (id) void api.cancelJob(id).catch(() => undefined);
  }, [api]);

  useEffect(
    () => () => {
      controller.current?.abort();
      releaseAudio();
    },
    [releaseAudio],
  );

  const start = useCallback(
    async (input: JobInput) => {
      controller.current?.abort();
      releaseAudio();
      const ctl = new AbortController();
      controller.current = ctl;
      setState({ phase: "submitting" });

      try {
        let job = await api.createJob(input, ctl.signal);
        jobId.current = job.id;
        setState({ phase: "running", job });

        let delay = pollIntervalMs;
        let failures = 0;
        while (job.status === "queued" || job.status === "running") {
          await sleep(delay, ctl.signal);
          try {
            job = await api.getJob(job.id, ctl.signal);
            failures = 0;
            delay = pollIntervalMs;
            setState({ phase: "running", job });
          } catch (error) {
            if (isAbort(error)) throw error;
            const permanent = error instanceof ApiError && [401, 404].includes(error.status);
            if (permanent || ++failures >= MAX_CONSECUTIVE_POLL_FAILURES) throw error;
            delay = Math.min(delay * 2, MAX_POLL_DELAY_MS);
          }
        }

        if (job.status === "succeeded") {
          const blob = await api.fetchAudio(job.id, ctl.signal);
          const url = URL.createObjectURL(blob);
          audioUrl.current = url;
          setState({ phase: "done", job, audioUrl: url, extension: extensionFor(blob.type) });
        } else if (job.status === "failed") {
          setState({
            phase: "failed",
            code: job.error?.code ?? "error",
            message: job.error?.message ?? "Audio generation failed.",
          });
        } else {
          setState({ phase: "cancelled" });
        }
      } catch (error) {
        if (isAbort(error)) return;
        if (error instanceof ApiError) {
          setState({ phase: "failed", code: error.code, message: error.message });
        } else {
          setState({ phase: "failed", code: "error", message: "Something went wrong." });
        }
      }
    },
    [api, pollIntervalMs, releaseAudio],
  );

  const cancel = useCallback(() => {
    controller.current?.abort();
    discardRemote();
    setState({ phase: "cancelled" });
  }, [discardRemote]);

  const reset = useCallback(() => {
    controller.current?.abort();
    discardRemote();
    releaseAudio();
    setState({ phase: "idle" });
  }, [discardRemote, releaseAudio]);

  return { state, start, cancel, reset };
}
