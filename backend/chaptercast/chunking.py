"""Sentence-aware text chunking.

A long chapter has to be split into pieces the TTS API accepts. Cutting at an
arbitrary character offset would produce audible glitches mid-word, so we split
on natural boundaries, in this order of preference:

    paragraph  >  sentence  >  clause (, ; :)  >  word  >  hard slice

Guarantees (enforced by property-based tests):

* every chunk is non-empty and at most ``max_chars`` long;
* no content is lost or reordered: ignoring whitespace, the chunks concatenate
  back to the normalised input;
* expression tags (``[warm]``, see tags.py) are atomic: never split across
  chunks, kept in order, and kept with the words they modify.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from chaptercast.tags import MAX_TAG_CHARS, find_tags

_ABBREVIATIONS = frozenset(
    {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "e.g", "i.e", "fig", "inc", "ltd"}
)

# One sentence = lazily up to terminal punctuation (plus closing quotes/brackets)
# that is followed by whitespace or end of text.
_SENTENCE_RE = re.compile(r".+?(?:[.!?…]+[\"'”’)\]]*(?=\s|$)|$)", re.DOTALL)
_CLAUSE_RE = re.compile(r".+?(?:[,;:\u2013\u2014](?=\s)|$)", re.DOTALL)  # also en/em dash
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")
_WHITESPACE_RE = re.compile(r"\s+")
# Private-use characters: normalize_text removes these from input, so they are free
# to mark "these belong together" while splitting. _INNER joins the words inside a
# tag (never cut); _LINK joins a tag to the word it modifies (cut only if too long).
_INNER = "\ue000"
_LINK = "\ue001"

# Bidi override/isolate controls and BOM: never useful in narration text and a
# known source of "invisible text" tricks.
_STRIP_CODEPOINTS = {0xFEFF, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}


def normalize_text(text: str) -> str:
    """NFC-normalise, drop control and private-use characters, collapse whitespace.

    Paragraph breaks (blank lines) survive as a single ``\\n\\n``.
    """
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = "".join(
        ch
        for ch in text
        if ch in "\n\t"
        or (unicodedata.category(ch) not in {"Cc", "Co"} and ord(ch) not in _STRIP_CODEPOINTS)
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
        if len(word) <= max_chars:
            words.append(word)
            continue
        # Too long: first unlink tags from their word (a tag itself always fits),
        # then hard-slice whatever is still too long, e.g. a very long URL.
        for part in word.split(_LINK):
            if len(part) <= max_chars:
                words.append(part)
            else:
                words.extend(part[i : i + max_chars] for i in range(0, len(part), max_chars))
    return _pack(words, max_chars)


def _protect_tags(text: str) -> str:
    """Glue each tag into one unbreakable word, together with the word it modifies.

    Spaces inside a tag become ``_INNER`` and the space after it ``_LINK``; no
    splitting step treats either as whitespace. ``_restore`` turns them back.
    """
    out: list[str] = []
    last = 0
    for match in find_tags(text):
        out.append(text[last : match.start()])
        out.append(match.group(0).replace(" ", _INNER))
        last = match.end()
        if text[last : last + 1] == " " and last + 1 < len(text) and not text[last + 1].isspace():
            out.append(_LINK)
            last += 1
    out.append(text[last:])
    return "".join(out)


def _restore(text: str) -> str:
    return text.replace(_INNER, " ").replace(_LINK, " ")


def _move_trailing_tags(chunks: list[Chunk], max_chars: int) -> list[Chunk]:
    """A tag at the very end of a chunk modifies what follows: move it to the next chunk.

    As many trailing tags as fit are moved, last ones first, so a chunk only ends
    with a tag when the next chunk has no room for it. Chunks are processed right
    to left, so every chunk has reached its final length before tags move into it.
    """
    result = list(chunks)
    for i in range(len(result) - 2, -1, -1):
        body, tags = _split_trailing_tags(result[i].text)
        following = result[i + 1]
        moving = 0
        while moving < len(tags) and (
            len(" ".join(tags[-(moving + 1) :])) + 1 + len(following.text) <= max_chars
        ):
            moving += 1
        if not moving:
            continue
        kept = " ".join([body, *tags[:-moving]]).strip()
        starts = following.starts_paragraph or (not kept and result[i].starts_paragraph)
        result[i + 1] = Chunk(f"{' '.join(tags[-moving:])} {following.text}", starts)
        if kept:
            result[i] = Chunk(kept, result[i].starts_paragraph)
        else:  # the chunk was nothing but tags
            del result[i]
    return result


def _split_trailing_tags(text: str) -> tuple[str, list[str]]:
    """``("body", ["[tag]", "[tag]"])`` for the tags at the end of ``text``, if any."""
    end = len(text)
    tags: list[str] = []
    for match in reversed(list(find_tags(text))):
        if text[match.end() : end].strip():
            break
        tags.insert(0, match.group(0))
        end = match.start()
    return text[:end].rstrip(), tags


@dataclass(frozen=True)
class Chunk:
    text: str
    # True when the chunk begins a new paragraph. Paragraph breaks *inside* a chunk
    # stay visible as "\n\n"; this flag preserves the ones that fall on a boundary.
    starts_paragraph: bool


def split_chunks(text: str, max_chars: int = 900) -> list[Chunk]:
    """Split ``text`` into narration chunks of at most ``max_chars`` characters."""
    if max_chars < MAX_TAG_CHARS:
        raise ValueError(f"max_chars must be at least {MAX_TAG_CHARS}")
    normalized = _protect_tags(normalize_text(text))
    if not normalized:
        return []

    chunks: list[Chunk] = []
    current = ""
    current_starts_paragraph = True
    for paragraph in normalized.split("\n\n"):
        first_in_paragraph = True
        for sentence in _sentences(paragraph):
            for piece in _fit(sentence, max_chars):
                joiner = "\n\n" if first_in_paragraph else " "
                candidate = f"{current}{joiner}{piece}" if current else piece
                if len(candidate) <= max_chars:
                    current = candidate
                else:
                    chunks.append(Chunk(current, current_starts_paragraph))
                    current = piece
                    current_starts_paragraph = first_in_paragraph
                first_in_paragraph = False
    if current:
        chunks.append(Chunk(current, current_starts_paragraph))
    restored = [Chunk(_restore(c.text), c.starts_paragraph) for c in chunks]
    return _move_trailing_tags(restored, max_chars)


def split_text(text: str, max_chars: int = 900) -> list[str]:
    """Like :func:`split_chunks`, returning only the chunk texts."""
    return [chunk.text for chunk in split_chunks(text, max_chars)]
