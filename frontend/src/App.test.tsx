import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import { ApiError } from "./api/client";
import {
  CONFIG,
  FIVE_MODELS,
  SUCCEEDED,
  VOICES,
  makeApi,
  makeEstimate,
  makeJob,
  unauthorized,
} from "./test/fakes";

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
      { text: "A short chapter.", voiceId: "v2", modelId: "m1" },
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

  // ------------------------------------------------------------ narration options
  it("sends the chosen model and, only when opted in, the voice settings", async () => {
    const user = userEvent.setup();
    const api = makeApi([SUCCEEDED()]);
    render(<App api={api} pollIntervalMs={FAST} estimateDelayMs={0} />);

    await user.type(await screen.findByLabelText("Chapter text"), "Hello.");
    expect(screen.getByRole("option", { name: "Fast · 0.5x credits" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Model"), "m2");
    expect(screen.queryByLabelText("Stability")).not.toBeInTheDocument();

    await user.click(screen.getByLabelText("Fine-tune the voice"));
    expect(screen.getByLabelText("Style exaggeration")).toBeDisabled(); // m2 has no style support
    fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "1.1" } });
    await user.click(screen.getByRole("button", { name: "Generate audio" }));

    expect(api.createJob).toHaveBeenCalledWith(
      {
        text: "Hello.",
        voiceId: "v1",
        modelId: "m2",
        voiceSettings: { stability: 0.5, similarity_boost: 0.75, speed: 1.1 },
      },
      expect.any(AbortSignal),
    );
  });

  it("remembers voice, model and settings for next time", async () => {
    const user = userEvent.setup();
    const first = render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    await user.selectOptions(await screen.findByLabelText("Voice"), "v2");
    await user.selectOptions(screen.getByLabelText("Model"), "m2");
    first.unmount();

    render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    expect(await screen.findByLabelText("Voice")).toHaveValue("v2");
    expect(screen.getByLabelText("Model")).toHaveValue("m2");
  });

  it("ignores remembered choices the server no longer offers", async () => {
    window.localStorage.setItem("chaptercast.prefs", JSON.stringify({ voiceId: "gone", modelId: "gone" }));
    render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    expect(await screen.findByLabelText("Voice")).toHaveValue("v1");
    expect(screen.getByLabelText("Model")).toHaveValue("m1");
  });

  // -------------------------------------------------------------------- estimate
  it("shows the live cost estimate, including sections reused from the cache", async () => {
    const user = userEvent.setup();
    const api = makeApi([], {
      estimate: vi.fn(async () =>
        makeEstimate({ chunks: 3, cached_chunks: 2, billable_characters: 12, estimated_credits: 6 }),
      ),
    });
    render(<App api={api} pollIntervalMs={FAST} estimateDelayMs={0} />);
    await user.type(await screen.findByLabelText("Chapter text"), "Some text.");
    expect(await screen.findByText(/≈ 6 credits/)).toBeInTheDocument();
    expect(screen.getByText(/3 sections \(2 already generated\)/)).toBeInTheDocument();
    expect(screen.getByText(/25,000 characters left/)).toBeInTheDocument();
  });

  it("says when a re-generation is free and warns when the budget is short", async () => {
    const estimate = vi
      .fn()
      .mockResolvedValueOnce(makeEstimate({ cached_chunks: 2, billable_characters: 0, estimated_credits: 0 }))
      .mockResolvedValue(makeEstimate({ billable_characters: 500, daily_budget_remaining: 100 }));
    render(<App api={makeApi([], { estimate })} pollIntervalMs={FAST} estimateDelayMs={0} />);
    const textarea = await screen.findByLabelText("Chapter text");
    fireEvent.change(textarea, { target: { value: "Cached." } });
    expect(await screen.findByText("Free to generate")).toBeInTheDocument();
    fireEvent.change(textarea, { target: { value: "Something new." } });
    expect(await screen.findByText(/only 100 are left/)).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------- import
  it("imports a Markdown file as narration text", async () => {
    const user = userEvent.setup();
    render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    await screen.findByLabelText("Chapter text");
    const file = new File(["# Chapter 1\nIt was **dark**."], "chapter.md", { type: "text/markdown" });
    await user.upload(screen.getByLabelText("Import a text or Markdown file"), file);
    await waitFor(() => expect(screen.getByLabelText("Chapter text")).toHaveValue("Chapter 1.\n\nIt was dark."));
  });

  it("explains why a file cannot be imported", async () => {
    render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    const textarea = await screen.findByLabelText("Chapter text");
    const file = new File(["%PDF"], "book.pdf", { type: "application/pdf" });
    fireEvent.drop(textarea, { dataTransfer: { files: [file] } });
    expect(await screen.findByRole("alert")).toHaveTextContent(/only .txt and .md/i);
  });

  // --------------------------------------------------------------------- preview
  it("plays a free voice preview and describes the voice", async () => {
    const user = userEvent.setup();
    const api = makeApi([]);
    render(<App api={api} pollIntervalMs={FAST} />);
    expect(await screen.findByText("british · narrative story")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Preview Aria" }));
    expect(api.fetchPreview).toHaveBeenCalledWith("v1", expect.any(AbortSignal));
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
    await user.click(await screen.findByRole("button", { name: "Stop voice preview" }));
    expect(screen.getByRole("button", { name: "Preview Aria" })).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Voice"), "v2");
    expect(screen.getByRole("button", { name: "Preview Orion" })).toBeDisabled(); // no sample
    expect(screen.getByText("Calm")).toBeInTheDocument();
  });

  // ------------------------------------------------------------------ read-along
  it("shows a read-along transcript and downloads captions", async () => {
    const user = userEvent.setup();
    const api = makeApi([SUCCEEDED()]);
    render(<App api={api} pollIntervalMs={FAST} />);
    await user.type(await screen.findByLabelText("Chapter text"), "Hello there. Next part.");
    await user.click(screen.getByRole("button", { name: "Generate audio" }));

    expect(await screen.findByRole("heading", { name: "Read along" })).toBeInTheDocument();
    expect(screen.getByLabelText("Transcript")).toHaveTextContent("Hello there. Next part.");
    const save = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    await user.click(screen.getByRole("button", { name: "Captions .srt" }));
    expect(api.fetchCaptions).toHaveBeenCalledWith(SUCCEEDED().id, "srt");
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    save.mockRestore();

    await user.click(screen.getByText("Next"));
    expect((screen.getByLabelText("Narrated audio") as HTMLAudioElement).currentTime).toBe(1.5);
  });

  it("works without a transcript when the provider gave no timings", async () => {
    const user = userEvent.setup();
    const api = makeApi([{ ...SUCCEEDED(), transcript_url: null, captions: null }]);
    render(<App api={api} pollIntervalMs={FAST} />);
    await user.type(await screen.findByLabelText("Chapter text"), "Hello.");
    await user.click(screen.getByRole("button", { name: "Generate audio" }));
    expect(await screen.findByRole("heading", { name: "Your audio is ready" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Transcript")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /captions/i })).not.toBeInTheDocument();
    expect(api.getTranscript).not.toHaveBeenCalled();
  });

  // ------------------------------------------------------------- model registry
  it("lists the models with their cost and capability badges", async () => {
    const user = userEvent.setup();
    render(<App api={makeApi([], { listModels: vi.fn(async () => FIVE_MODELS) })} pollIntervalMs={FAST} />);
    const picker = await screen.findByLabelText("Model");
    expect(Array.from((picker as HTMLSelectElement).options).map((o) => o.textContent)).toEqual([
      "Eleven Multilingual v2 · 1.0x credits",
      "Eleven Flash v2.5 · 0.5x credits",
      "Eleven Turbo v2.5 · 0.5x credits",
      "Eleven v4 · 1.0x credits",
      "Eleven v4 Turbo · 0.5x credits",
    ]);
    expect(screen.queryByRole("list", { name: "Model capabilities" })).not.toBeInTheDocument();

    await user.selectOptions(picker, "eleven_v4_turbo");
    const badges = screen.getByRole("list", { name: "Model capabilities" });
    expect(within(badges).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Audio tags",
      "Low latency",
    ]);
    expect(screen.getByText("v4 with audio tags at low latency.")).toBeInTheDocument();
  });

  it("disables the controls a model cannot use and says why", async () => {
    const user = userEvent.setup();
    render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    await user.click(await screen.findByLabelText("Fine-tune the voice"));
    expect(screen.getByLabelText("Speaker boost")).toBeEnabled();

    await user.selectOptions(screen.getByLabelText("Model"), "m2");
    expect(screen.getByLabelText("Style exaggeration")).toBeDisabled();
    expect(screen.getByLabelText("Speaker boost")).toBeDisabled();
    expect(screen.getByLabelText("Speaker boost").closest("label")).toHaveAttribute(
      "title",
      "Fast does not support speaker boost.",
    );
    expect(screen.getByLabelText("Style exaggeration").closest(".slider")).toHaveAttribute(
      "title",
      "Fast does not support style exaggeration.",
    );
  });

  it("sends speaker boost only to turn it off, and only where supported", async () => {
    const user = userEvent.setup();
    const api = makeApi([SUCCEEDED()]);
    render(<App api={api} pollIntervalMs={FAST} />);
    await user.type(await screen.findByLabelText("Chapter text"), "Hello.");
    await user.click(screen.getByLabelText("Fine-tune the voice"));
    await user.click(screen.getByLabelText("Speaker boost"));
    await user.click(screen.getByRole("button", { name: "Generate audio" }));
    expect(api.createJob).toHaveBeenCalledWith(
      expect.objectContaining({
        voiceSettings: { stability: 0.5, similarity_boost: 0.75, speed: 1, style: 0, use_speaker_boost: false },
      }),
      expect.any(AbortSignal),
    );
  });

  // ---------------------------------------------------------------- audio tags
  it("offers expression tags only for models that understand them", async () => {
    const user = userEvent.setup();
    render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    await screen.findByLabelText("Chapter text");
    expect(screen.queryByRole("group", { name: /expression tags/ })).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Model"), "m4");
    const helper = screen.getByRole("group", { name: /understands expression tags/ });
    expect(within(helper).getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual([
      "Insert [warm] tag",
      "Insert [whispered] tag",
    ]);
  });

  it("inserts a tag at the cursor and puts the cursor after it", async () => {
    const user = userEvent.setup();
    render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    await user.selectOptions(await screen.findByLabelText("Model"), "m4");
    const textarea = screen.getByLabelText<HTMLTextAreaElement>("Chapter text");
    fireEvent.change(textarea, { target: { value: "Hello world." } });
    textarea.setSelectionRange(6, 6); // before "world"

    await user.click(screen.getByRole("button", { name: "Insert [warm] tag" }));
    expect(textarea).toHaveValue("Hello [warm] world.");
    await waitFor(() => expect(textarea).toHaveFocus());
    expect(textarea.selectionStart).toBe("Hello [warm] ".length);
  });

  it("can insert a tag with the keyboard alone", async () => {
    const user = userEvent.setup();
    render(<App api={makeApi([])} pollIntervalMs={FAST} />);
    await user.selectOptions(await screen.findByLabelText("Model"), "m4");
    const textarea = screen.getByLabelText<HTMLTextAreaElement>("Chapter text");
    await user.click(textarea);
    await user.keyboard("Quiet.");
    textarea.setSelectionRange(0, 0);
    screen.getByRole("button", { name: "Insert [whispered] tag" }).focus();
    await user.keyboard("{Enter}");
    expect(textarea).toHaveValue("[whispered] Quiet.");
  });

  it("explains when a model will ignore tags", async () => {
    const user = userEvent.setup();
    const api = makeApi([], { estimate: vi.fn(async () => makeEstimate({ tags_ignored: true })) });
    render(<App api={api} pollIntervalMs={FAST} estimateDelayMs={0} />);
    await user.type(await screen.findByLabelText("Chapter text"), "[[warm] Hello.");
    expect(await screen.findByText(/Tags are ignored by this model/)).toBeInTheDocument();
  });

  // ------------------------------------------------------------ result and errors
  it("says plainly when the audio has no word timings", async () => {
    const user = userEvent.setup();
    const api = makeApi([{ ...SUCCEEDED(), word_timings: false, transcript_url: null, captions: null }]);
    render(<App api={api} pollIntervalMs={FAST} />);
    await user.type(await screen.findByLabelText("Chapter text"), "Hello.");
    await user.click(screen.getByRole("button", { name: "Generate audio" }));
    expect(await screen.findByText(/did not return word timings/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Transcript")).not.toBeInTheDocument();
  });

  it("shows the server's message when a model is rejected", async () => {
    const user = userEvent.setup();
    const api = makeApi([], {
      createJob: vi.fn(async () => {
        throw new ApiError(422, "unknown_model", "That model is not available on this server.");
      }),
    });
    render(<App api={api} pollIntervalMs={FAST} />);
    await user.type(await screen.findByLabelText("Chapter text"), "Hello.");
    await user.click(screen.getByRole("button", { name: "Generate audio" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That model is not available on this server.");
  });
});
