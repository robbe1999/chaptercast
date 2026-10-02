import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JobInput } from "../api/schemas";
import { makeApi, makeEstimate } from "../test/fakes";
import { useEstimate } from "./useEstimate";

const input = (text: string): JobInput => ({ text, voiceId: "v1", modelId: "m1" });

describe("useEstimate", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("debounces: only the last input after a pause is estimated", async () => {
    const api = makeApi([]);
    const { result, rerender } = renderHook(({ value }) => useEstimate(api, value, 400), {
      initialProps: { value: input("a") as JobInput | null },
    });
    rerender({ value: input("ab") });
    rerender({ value: input("abc") });
    expect(api.estimate).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(api.estimate).toHaveBeenCalledTimes(1);
    expect(api.estimate).toHaveBeenCalledWith(input("abc"), expect.any(AbortSignal));
    expect(result.current).toEqual(makeEstimate());
  });

  it("aborts an in-flight estimate when the input changes", async () => {
    const signals: AbortSignal[] = [];
    const api = makeApi([], {
      estimate: vi.fn((_input: JobInput, signal?: AbortSignal) => {
        if (signal) signals.push(signal);
        return new Promise<never>(() => undefined);
      }),
    });
    const { rerender } = renderHook(({ value }) => useEstimate(api, value, 10), {
      initialProps: { value: input("first") as JobInput | null },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    rerender({ value: input("second") });
    expect(signals[0]?.aborted).toBe(true);
  });

  it("is null without input and hides itself on errors", async () => {
    const api = makeApi([], { estimate: vi.fn(async () => Promise.reject(new Error("boom"))) });
    const { result, rerender } = renderHook(({ value }) => useEstimate(api, value, 10), {
      initialProps: { value: null as JobInput | null },
    });
    expect(result.current).toBeNull();
    rerender({ value: input("x") });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(result.current).toBeNull();
  });
});
