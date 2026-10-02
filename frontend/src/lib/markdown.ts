/**
 * Turn Markdown into plain narration text.
 *
 * The goal is not a faithful renderer but text that sounds right when read
 * aloud: no "hash hash", no asterisks, no URLs, code blocks dropped, and
 * headings turned into their own sentence so the narrator pauses after them.
 */
export function markdownToNarration(source: string): string {
  // Escaped characters are hidden behind private-use placeholders first, so that
  // "\*not emphasis\*" survives the emphasis rules, and restored at the end.
  let text = source
    .replace(/\r\n?/g, "\n")
    .replace(/\\([\\`*_{}[\]()#+\-.!>~])/g, (_, ch: string) => `\uE000${ch.charCodeAt(0)}\uE001`);

  text = text.replace(/^---\n[\s\S]*?\n---\n/, ""); // YAML front matter
  text = text.replace(/^(```|~~~)[^\n]*\n[\s\S]*?^\1[^\n]*$/gm, ""); // fenced code
  text = text.replace(/<!--[\s\S]*?-->/g, ""); // HTML comments
  text = text.replace(/^\s{0,3}\[[^\]]+\]:\s*\S+.*$/gm, ""); // reference link definitions
  // Setext headings before rules: "Title\n---" is a heading, a lone "---" is a rule.
  text = text.replace(/^([^\n]*\S[^\n]*)\n(=+|-+)[ \t]*$/gm, (_, h: string) => asHeading(h));
  text = text.replace(/^\s{0,3}([-*_])(\s*\1){2,}\s*$/gm, "\n"); // horizontal rules

  text = text.replace(/^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$/gm, (_, h: string) => asHeading(h));

  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, ""); // images
  text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1"); // inline links keep their text
  text = text.replace(/\[([^\]]+)\]\[[^\]]*\]/g, "$1"); // reference links
  text = text.replace(/<https?:\/\/[^>]+>/g, ""); // autolinks
  text = text.replace(/<\/?[a-zA-Z][^>]*>/g, ""); // inline HTML tags

  text = text.replace(/^\s{0,3}>\s?/gm, ""); // blockquotes
  text = text.replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, ""); // list markers

  text = text.replace(/`([^`]+)`/g, "$1"); // inline code
  text = text.replace(/(\*\*|__)(?=\S)([\s\S]*?\S)\1/g, "$2"); // bold
  text = text.replace(/(^|[^\w*])\*(?=\S)([^*\n]*?\S)\*(?!\w)/g, "$1$2"); // *italic*
  text = text.replace(/(^|[^\w])_(?=\S)([^_\n]*?\S)_(?!\w)/g, "$1$2"); // _italic_ (not snake_case)
  text = text.replace(/~~(.+?)~~/g, "$1"); // strikethrough
  text = text.replace(/\uE000(\d+)\uE001/g, (_, code: string) => String.fromCharCode(Number(code)));

  return text
    .split("\n")
    .map((line) => line.trimEnd())
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** A heading becomes its own sentence, so the narrator pauses after it. */
function asHeading(heading: string): string {
  const text = heading.trim();
  return /[.!?:…]$/.test(text) ? `\n${text}\n\n` : `\n${text}.\n\n`;
}

export const IMPORT_ACCEPT = ".txt,.md,.markdown,text/plain,text/markdown";
export const MAX_IMPORT_BYTES = 1024 * 1024;

export class ImportError extends Error {}

/** Read a dropped or picked file as narration text. */
export async function readTextFile(file: File): Promise<string> {
  const name = file.name.toLowerCase();
  const markdown = name.endsWith(".md") || name.endsWith(".markdown");
  if (!markdown && !name.endsWith(".txt") && file.type !== "text/plain") {
    throw new ImportError("Only .txt and .md files can be imported.");
  }
  if (file.size > MAX_IMPORT_BYTES) {
    throw new ImportError("That file is larger than 1 MB. Import one chapter at a time.");
  }
  const text = await readAsText(file);
  return markdown ? markdownToNarration(text) : text.trim();
}

function readAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new ImportError("That file could not be read."));
    reader.readAsText(file);
  });
}
