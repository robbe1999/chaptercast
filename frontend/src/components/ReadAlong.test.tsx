import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TRANSCRIPT } from "../test/fakes";
import { ReadAlong, activeWordIndex } from "./ReadAlong";

const words = TRANSCRIPT.words;

describe("activeWordIndex", () => {
  it.each([
    [-1, -1],
    [0, 0],
    [0.55, 0], // in the gap after "Hello": still the last word that started
    [0.6, 1],
    [2.5, 3],
    [99, 3],
  ])("at %s s is word %s", (time, expected) => {
    expect(activeWordIndex(words, time)).toBe(expected);
  });

  it("handles an empty transcript", () => {
    expect(activeWordIndex([], 1)).toBe(-1);
  });
});

describe("ReadAlong", () => {
  it("renders paragraphs and highlights the spoken word", () => {
    const { container } = render(<ReadAlong words={words} time={1.6} onSeek={() => undefined} />);
    expect(container.querySelectorAll("p")).toHaveLength(2);
    const current = container.querySelector('[aria-current="true"]');
    expect(current).toHaveTextContent("Next");
    expect(container.querySelectorAll(".word.active")).toHaveLength(1);
  });

  it("seeks to a word when it is clicked", async () => {
    const onSeek = vi.fn();
    render(<ReadAlong words={words} time={0} onSeek={onSeek} />);
    await userEvent.click(screen.getByText("part."));
    expect(onSeek).toHaveBeenCalledWith(2.1);
  });

  it("ignores clicks between words", async () => {
    const onSeek = vi.fn();
    render(<ReadAlong words={words} time={0} onSeek={onSeek} />);
    await userEvent.click(screen.getByLabelText("Transcript"));
    expect(onSeek).not.toHaveBeenCalled();
  });
});
