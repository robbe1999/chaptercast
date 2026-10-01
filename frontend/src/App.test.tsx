import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import { ApiError } from "./api/client";
import { CONFIG, SUCCEEDED, VOICES, makeApi, makeJob, unauthorized } from "./test/fakes";

const FAST = 1;

describe("App", () => {
  it("shows the demo banner and loads voices", async () => {
    render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    expect(await screen.findByRole("note")).toHaveTextContent(/demo mode/i);
    expect(await screen.findByRole("option", { name: /Aria/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Orion" })).toBeInTheDocument();
  });

  it("hides the demo banner when a real provider is configured", async () => {
    const api = makeApi([], { getConfig: vi.fn(async () => ({ ...CONFIG, provider: "elevenlabs" as const })) });
    render(<App api={api} pollIntervalMs={FAST} />);
    await screen.findByLabelText("Chapter text");
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });

  it("disables submit until there is text, and counts characters", async () => {
    const user = userEvent.setup();
    render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    const button = await screen.findByRole("button", { name: "Generate audio" });
    expect(button).toBeDisabled();

    await user.type(screen.getByLabelText("Chapter text"), "Hello there.");
    expect(button).toBeEnabled();
    expect(screen.getByText(/12 \/ 200 characters/)).toBeInTheDocument();
  });

  it("blocks over-limit text with an actionable message", async () => {
    const user = userEvent.setup();
    render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    await user.click(await screen.findByRole("button", { name: "Use sample text" }));
    expect(screen.getByRole("button", { name: "Generate audio" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(/over the limit/i);
  });

  it("runs the whole flow: submit, progress, playable audio, start over", async () => {
    const user = userEvent.setup();
    const api = makeApi([
      makeJob({ status: "running", progress: { completed_chunks: 1, total_chunks: 2 } }),
      SUCCEEDED(),
    ]);
    render(<App api={api} pollIntervalMs={FAST} />);

    await user.type(await screen.findByLabelText("Chapter text"), "A short chapter.");
    await user.selectOptions(screen.getByLabelText("Voice"), "v2");
    await user.click(screen.getByRole("button", { name: "Generate audio" }));

    expect(api.createJob).toHaveBeenCalledWith(
      { text: "A short chapter.", voiceId: "v2" },
      expect.any(AbortSignal),
    );
    expect(await screen.findByRole("heading", { name: "Your audio is ready" })).toBeInTheDocument();
    expect(screen.getByLabelText("Narrated audio")).toHaveAttribute("src", expect.stringMatching(/^blob:/));
    expect(screen.getByRole("link", { name: /Download \.wav/ })).toHaveAttribute("download", "chaptercast.wav");
    expect(screen.getByText(/about 1:15/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Start over" }));
    expect(await screen.findByLabelText("Chapter text")).toBeInTheDocument();
    expect(api.cancelJob).toHaveBeenCalled();
  });

  it("shows a progress bar with accessible status text while running", async () => {
    const user = userEvent.setup();
    const api = makeApi([makeJob({ status: "running", progress: { completed_chunks: 1, total_chunks: 4 } })], {
      getJob: vi.fn(async () => makeJob({ status: "running", progress: { completed_chunks: 1, total_chunks: 4 } })),
    });
    render(<App api={api} pollIntervalMs={FAST} />);
    await user.type(await screen.findByLabelText("Chapter text"), "Text.");
    await user.click(screen.getByRole("button", { name: "Generate audio" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Narrating section 2 of 4");
    const bar = screen.getByRole("progressbar", { name: "Generation progress" });
    expect(bar).toHaveAttribute("value", "1");
    expect(bar).toHaveAttribute("max", "4");

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("Cancelled.")).toBeInTheDocument();
  });

  it("shows server failures and lets the user try again with the text intact", async () => {
    const user = userEvent.setup();
    const api = makeApi([makeJob({ status: "failed", error: { code: "provider_quota", message: "Quota exhausted." } })]);
    render(<App api={api} pollIntervalMs={FAST} />);
    await user.type(await screen.findByLabelText("Chapter text"), "Keep this text.");
    await user.click(screen.getByRole("button", { name: "Generate audio" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Quota exhausted.");
    expect(screen.getByLabelText("Chapter text")).toHaveValue("Keep this text.");
  });

  it("asks for a token when the server requires one, then loads", async () => {
    const user = userEvent.setup();
    const listVoices = vi
      .fn()
      .mockRejectedValueOnce(unauthorized())
      .mockResolvedValue(VOICES);
    const api = makeApi([], {
      getConfig: vi.fn(async () => ({ ...CONFIG, auth_required: true })),
      listVoices,
    });
    render(<App api={api} pollIntervalMs={FAST} />);

    expect(await screen.findByRole("heading", { name: "Access required" })).toBeInTheDocument();
    expect(api.listVoices).not.toHaveBeenCalled(); // no request without a token

    await user.type(screen.getByLabelText("Access token"), "wrong-token");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/not accepted/i);
    expect(api.token).toBeNull(); // rejected token is discarded

    await user.type(screen.getByLabelText("Access token"), "right-token");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByLabelText("Chapter text")).toBeInTheDocument();
    expect(api.token).toBe("right-token");
  });

  it("shows lockout messages from the server on the gate", async () => {
    const user = userEvent.setup();
    const api = makeApi([], {
      getConfig: vi.fn(async () => ({ ...CONFIG, auth_required: true })),
      listVoices: vi.fn(async () => {
        throw new ApiError(429, "too_many_attempts", "Too many failed attempts. Try again later.");
      }),
    });
    render(<App api={api} pollIntervalMs={FAST} />);
    await user.type(await screen.findByLabelText("Access token"), "whatever-token");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/too many failed attempts/i);
  });

  it("reports a load failure instead of a blank page", async () => {
    const api = makeApi([], {
      getConfig: vi.fn(async () => {
        throw new ApiError(0, "network", "Could not reach the server. Check your connection.");
      }),
    });
    render(<App api={api} pollIntervalMs={FAST} />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/could not reach the server/i));
  });

  it("states plainly that it is unofficial and that text goes to the provider", async () => {
    render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    expect(await screen.findByText(/not affiliated with ElevenLabs/i)).toBeInTheDocument();
  });
});
