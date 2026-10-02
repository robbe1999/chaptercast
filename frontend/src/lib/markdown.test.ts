import { describe, expect, it } from "vitest";
import { ImportError, MAX_IMPORT_BYTES, markdownToNarration, readTextFile } from "./markdown";

describe("markdownToNarration", () => {
  it("turns headings into their own sentences", () => {
    expect(markdownToNarration("# Chapter One\nIt was dark.")).toBe("Chapter One.\n\nIt was dark.");
    expect(markdownToNarration("## Why? ##\nBecause.")).toBe("Why?\n\nBecause.");
    expect(markdownToNarration("Part Two\n========\nText.")).toBe("Part Two.\n\nText.");
    expect(markdownToNarration("Aside\n---\nText.")).toBe("Aside.\n\nText.");
  });

  it("strips emphasis, code and strikethrough but keeps the words", () => {
    expect(markdownToNarration("A **bold**, *quiet* and __loud__ _night_ with `code` and ~~no~~ end.")).toBe(
      "A bold, quiet and loud night with code and no end.",
    );
  });

  it("keeps snake_case and arithmetic intact", () => {
    expect(markdownToNarration("Use my_variable_name and 2 * 3 * 4.")).toBe("Use my_variable_name and 2 * 3 * 4.");
  });

  it("keeps link text and drops URLs, images and autolinks", () => {
    expect(
      markdownToNarration("See [the map](https://x.test/map) ![a cat](cat.png) <https://x.test> [ref][1].\n\n[1]: https://x.test"),
    ).toBe("See the map   ref.");
  });

  it("drops fenced code, HTML, comments and front matter", () => {
    const source = "---\ntitle: T\n---\nBefore.\n\n```js\nconsole.log(1)\n```\n<div>Inside</div><!-- hidden -->\nAfter.";
    expect(markdownToNarration(source)).toBe("Before.\n\nInside\nAfter.");
  });

  it("leaves no tag or comment behind when they are nested to dodge a single pass", () => {
    expect(markdownToNarration("a <<b>script>alert(1)<</b>/script> b")).toBe("a alert(1) b");
    expect(markdownToNarration("x <!<!-- -->-- hidden --> y")).toBe("x  y");
  });

  it("removes list, quote and rule markup", () => {
    expect(markdownToNarration("- one\n* two\n1. three\n> quoted\n\n***\n\nEnd.")).toBe(
      "one\ntwo\nthree\nquoted\n\nEnd.",
    );
  });

  it("unescapes backslash escapes", () => {
    expect(markdownToNarration("Not \\*emphasis\\* here.")).toBe("Not *emphasis* here.");
  });
});

describe("readTextFile", () => {
  const file = (content: string, name: string, type = "") => new File([content], name, { type });

  it("reads text files as-is and converts Markdown", async () => {
    expect(await readTextFile(file("  Plain *text*.  ", "a.txt"))).toBe("Plain *text*.");
    expect(await readTextFile(file("# Title\nBody *text*.", "a.md"))).toBe("Title.\n\nBody text.");
  });

  it("rejects other types and oversized files", async () => {
    await expect(readTextFile(file("x", "book.pdf", "application/pdf"))).rejects.toBeInstanceOf(ImportError);
    const big = new File([new Uint8Array(MAX_IMPORT_BYTES + 1)], "big.txt");
    await expect(readTextFile(big)).rejects.toThrow(/larger than 1 MB/);
  });
});
