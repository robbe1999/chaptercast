import { useEffect, useState } from "react";
import type { Api } from "../api/client";
import type { Estimate, JobInput } from "../api/schemas";

/**
 * Live, debounced cost estimate for what is in the composer.
 *
 * Every keystroke cancels the previous timer and in-flight request, so at most
 * one estimate is outstanding and a slow response can never overwrite a newer
 * one. Errors simply hide the estimate: it is advisory, never blocking.
 */
export function useEstimate(api: Api, input: JobInput | null, delayMs = 400): Estimate | null {
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const key = input ? JSON.stringify(input) : null;

  useEffect(() => {
    if (key === null) {
      setEstimate(null);
      return;
    }
    const request = JSON.parse(key) as JobInput;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api.estimate(request, controller.signal).then(setEstimate, () => {
        if (!controller.signal.aborted) setEstimate(null);
      });
    }, delayMs);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [api, key, delayMs]);

  return key === null ? null : estimate;
}
