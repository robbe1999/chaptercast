import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { SUCCEEDED, makeApi, makeJob } from "../test/fakes";
import { useJob } from "./useJob";

const FAST = { pollIntervalMs: 1 };

describe("useJob", () => {
  it("polls until the job succeeds, then exposes a playable blob URL", async () => {
    const api = makeApi([
      makeJob({ status: "queued" }),
      makeJob({ status: "running", progress: { completed_chunks: 1, total_chunks: 2 } }),
      SUCCEEDED(),
    ]);
    const { result } = renderHook(() => useJob(api, FAST));

    act(() => void result.current.start("Hello.", "v1"));
    expect(result.current.state.phase).toBe("submitting");

    await waitFor(() => expect(result.current.state.phase).toBe("done"));
    const state = result.current.state;
    if (state.phase !== "done") throw new Error("unreachable");
    expect(state.audioUrl).toMatch(/^blob:/);
    expect(state.extension).toBe("wav");
    expect(api.getJob).toHaveBeenCalledTimes(2);
    expect(api.fetchAudio).toHaveBeenCalledTimes(1);
  });

  it("reports server-side failures with the public message", async () => {
    const api = makeApi([
      makeJob({ status: "running" }),
      makeJob({ status: "failed", error: { code: "provider_quota", message: "Quota exhausted." } }),
    ]);
    const { result } = renderHook(() => useJob(api, FAST));
    act(() => void result.current.start("Hello.", "v1"));

    await waitFor(() => expect(result.current.state.phase).toBe("failed"));
    expect(result.current.state).toMatchObject({ code: "provider_quota", message: "Quota exhausted." });
  });

  it("surfaces submit errors such as rate limits", async () => {
    const api = makeApi([], {
      createJob: vi.fn(async () => {
        throw new ApiError(429, "rate_limited", "Too many jobs. Slow down.");
      }),
    });
    const { result } = renderHook(() => useJob(api, FAST));
    act(() => void result.current.start("Hello.", "v1"));
    await waitFor(() => expect(result.current.state.phase).toBe("failed"));
    expect(result.current.state).toMatchObject({ code: "rate_limited" });
  });

  it("retries transient poll errors with backoff and recovers", async () => {
    const getJob = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(0, "network", "offline"))
      .mockRejectedValueOnce(new ApiError(503, "busy", "busy"))
      .mockResolvedValue(SUCCEEDED());
    const api = makeApi([makeJob({ status: "running" })], { getJob });
    const { result } = renderHook(() => useJob(api, FAST));
    act(() => void result.current.start("Hello.", "v1"));
    await waitFor(() => expect(result.current.state.phase).toBe("done"));
    expect(getJob).toHaveBeenCalledTimes(3);
  });

  it("gives up after repeated poll failures", async () => {
    const getJob = vi.fn().mockRejectedValue(new ApiError(503, "busy", "still busy"));
    const api = makeApi([makeJob({ status: "running" })], { getJob });
    const { result } = renderHook(() => useJob(api, FAST));
    act(() => void result.current.start("Hello.", "v1"));
    await waitFor(() => expect(result.current.state.phase).toBe("failed"), { timeout: 3000 });
    expect(getJob).toHaveBeenCalledTimes(5);
  });

  it("stops immediately on a permanent poll error", async () => {
    const getJob = vi.fn().mockRejectedValue(new ApiError(404, "not_found", "Job not found."));
    const api = makeApi([makeJob({ status: "running" })], { getJob });
    const { result } = renderHook(() => useJob(api, FAST));
    act(() => void result.current.start("Hello.", "v1"));
    await waitFor(() => expect(result.current.state.phase).toBe("failed"));
    expect(getJob).toHaveBeenCalledTimes(1);
  });

  it("cancel aborts polling and deletes the job on the server", async () => {
    const api = makeApi([makeJob({ status: "running" })], {
      getJob: vi.fn(async () => makeJob({ status: "running" })),
    });
    const { result } = renderHook(() => useJob(api, FAST));
    act(() => void result.current.start("Hello.", "v1"));
    await waitFor(() => expect(result.current.state.phase).toBe("running"));

    act(() => result.current.cancel());
    expect(result.current.state.phase).toBe("cancelled");
    expect(api.cancelJob).toHaveBeenCalledWith("a".repeat(32));

    const calls = vi.mocked(api.getJob).mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(vi.mocked(api.getJob).mock.calls.length).toBe(calls); // no polling after cancel
  });

  it("reset revokes the blob URL and deletes the audio server-side", async () => {
    const api = makeApi([SUCCEEDED()]);
    const { result } = renderHook(() => useJob(api, FAST));
    act(() => void result.current.start("Hello.", "v1"));
    await waitFor(() => expect(result.current.state.phase).toBe("done"));
    const url = (result.current.state as { audioUrl: string }).audioUrl;

    act(() => result.current.reset());
    expect(result.current.state.phase).toBe("idle");
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(url);
    expect(api.cancelJob).toHaveBeenCalledTimes(1);
  });

  it("unmounting revokes the blob URL and stops polling", async () => {
    const api = makeApi([SUCCEEDED()]);
    const { result, unmount } = renderHook(() => useJob(api, FAST));
    act(() => void result.current.start("Hello.", "v1"));
    await waitFor(() => expect(result.current.state.phase).toBe("done"));
    const url = (result.current.state as { audioUrl: string }).audioUrl;
    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(url);
  });

  it("picks the mp3 extension from the audio type", async () => {
    const api = makeApi([SUCCEEDED()], {
      fetchAudio: vi.fn(async () => new Blob(["x"], { type: "audio/mpeg" })),
    });
    const { result } = renderHook(() => useJob(api, FAST));
    act(() => void result.current.start("Hello.", "v1"));
    await waitFor(() => expect(result.current.state.phase).toBe("done"));
    expect(result.current.state).toMatchObject({ extension: "mp3" });
  });
});
