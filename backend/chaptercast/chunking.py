"""Sentence-aware text chunking.

A long chapter has to be split into pieces the TTS API accepts. Cutting at an
arbitrary character offset would produce audible glitches mid-word, so we split
on natural boundaries, in this order of preference:

    paragraph  >  sentence  >  clause (, ; :)  >  word  >  hard slice

Guarantees (enforced by property-based tests):

* every chunk is non-empty and at most ``max_chars`` long;
* no content is lost or reordered: ignoring whitespace, the chunks concatenate
  back to the normalised input.
"""

from __future__ import annotations

import re
import unicodedata

_ABBREVIATIONS = frozenset(
    {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "e.g", "i.e", "fig", "inc", "ltd"}
)

# One sentence = lazily up to terminal punctuation (plus closing quotes/brackets)
# that is followed by whitespace or end of text.
_SENTENCE_RE = re.compile(r".+?(?:[.!?…]+[\"'”’)\]]*(?=\s|$)|$)", re.DOTALL)
_CLAUSE_RE = re.compile(r".+?(?:[,;:\u2013\u2014](?=\s)|$)", re.DOTALL)  # also en/em dash
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")
_WHITESPACE_RE = re.compile(r"\s+")

# Bidi override/isolate controls and BOM: never useful in narration text and a
# known source of "invisible text" tricks.
_STRIP_CODEPOINTS = {0xFEFF, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}


def normalize_text(text: str) -> str:
    """NFC-normalise, drop control characters, collapse whitespace.

    Paragraph breaks (blank lines) survive as a single ``\\n\\n``.
    """
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = "".join(
        ch
        for ch in text
        if ch in "\n\t" or (unicodedata.category(ch) != "Cc" and ord(ch) not in _STRIP_CODEPOINTS)
    )
    paragraphs = (_WHITESPACE_RE.sub(" ", p).strip() for p in _PARAGRAPH_SPLIT_RE.split(text))
    return "\n\n".join(p for p in paragraphs if p)


def _ends_with_abbreviation(sentence: str) -> bool:
    tokens = sentence.split()
    if not tokens:
        return False
    token = tokens[-1].lstrip("\"'“‘([")
    if not token.endswith("."):
        return False
    core = token[:-1]
    if core.lower() in _ABBREVIATIONS:
        return True
    # Initials such as "J." in "J. R. R. Tolkien"
    return len(core) == 1 and core.isalpha() and core.isupper()


def _sentences(paragraph: str) -> list[str]:
    raw = [m.group(0).strip() for m in _SENTENCE_RE.finditer(paragraph)]
    merged: list[str] = []
    for sentence in filter(None, raw):
        if merged and _ends_with_abbreviation(merged[-1]):
            merged[-1] = f"{merged[-1]} {sentence}"
        else:
            merged.append(sentence)
    return merged


def _pack(units: list[str], max_chars: int) -> list[str]:
    """Greedily join units with single spaces without exceeding ``max_chars``."""
    packed: list[str] = []
    current = ""
    for unit in units:
        if not current:
            current = unit
        elif len(current) + 1 + len(unit) <= max_chars:
            current = f"{current} {unit}"
        else:
            packed.append(current)
            current = unit
    if current:
        packed.append(current)
    return packed


def _fit(unit: str, max_chars: int, *, try_clauses: bool = True) -> list[str]:
    """Break one over-long sentence into pieces of at most ``max_chars``."""
    if len(unit) <= max_chars:
        return [unit]
    if try_clauses:
        clauses = [m.group(0).strip() for m in _CLAUSE_RE.finditer(unit)]
        clauses = [c for c in clauses if c]
        if len(clauses) > 1:
            pieces = [p for clause in clauses for p in _fit(clause, max_chars, try_clauses=False)]
            return _pack(pieces, max_chars)
    words: list[str] = []
    for word in unit.split():
        if len(word) > max_chars:  # e.g. a very long URL
            words.extend(word[i : i + max_chars] for i in range(0, len(word), max_chars))
        else:
            words.append(word)
    return _pack(words, max_chars)


def split_text(text: str, max_chars: int = 900) -> list[str]:
    """Split ``text`` into narration chunks of at most ``max_chars`` characters."""
    if max_chars < 20:
        raise ValueError("max_chars must be at least 20")
    normalized = normalize_text(text)
    if not normalized:
        return []

    chunks: list[str] = []
    current = ""
    for paragraph in normalized.split("\n\n"):
        first_in_paragraph = True
        for sentence in _sentences(paragraph):
            for piece in _fit(sentence, max_chars):
                joiner = "\n\n" if first_in_paragraph else " "
                candidate = f"{current}{joiner}{piece}" if current else piece
                if len(candidate) <= max_chars:
                    current = candidate
                else:
                    chunks.append(current)
                    current = piece
                first_in_paragraph = False
    if current:
        chunks.append(current)
    return chunks
