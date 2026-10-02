import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  window.localStorage.clear();
});

// jsdom has no media playback.
window.HTMLMediaElement.prototype.play = vi.fn(async () => undefined);
window.HTMLMediaElement.prototype.pause = vi.fn();

// jsdom does not implement object URLs.
let counter = 0;
URL.createObjectURL = vi.fn(() => `blob:test/${++counter}`);
URL.revokeObjectURL = vi.fn();
