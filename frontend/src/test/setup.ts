import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

// jsdom does not implement object URLs.
let counter = 0;
URL.createObjectURL = vi.fn(() => `blob:test/${++counter}`);
URL.revokeObjectURL = vi.fn();
