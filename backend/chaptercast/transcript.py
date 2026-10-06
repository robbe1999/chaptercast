"""Word timings for the whole chapter, and caption files built from them.

Each chunk is synthesised separately and comes back with per-character timings
relative to *its own* clip. To get chapter-level timings we walk the clips in
playback order, shift every timing by the duration of the audio before it, and
group characters into words. Captions are then cut from the word list,
preferring sentence ends, with the usual readability limits (two lines of at
most 42 characters, at most ~6 seconds on screen).

Expression tags such as ``[warm]`` come back in the alignment as characters with
short timings before the first spoken word (seen on the live API). They are
instructions, not speech, so they are treated like whitespace: they never become
words in the read-along or the captions, and the real words keep their timings.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from chaptercast.providers.base import Alignment
from chaptercast.tags import tag_mask

_SENTENCE_END = (".", "!", "?", "…", '."', '!"', '?"', ".”", "!”", "?”")
_LINE_CHARS = 42
_CUE_CHARS = 2 * _LINE_CHARS
_CUE_SECONDS = 6.0
_MIN_SENTENCE_CUE_CHARS = 24


@dataclass(frozen=True)
class Segment:
    """One stitched clip: its alignment, its real duration, and whether it opens a paragraph."""

    alignment: Alignment
    duration: float
    starts_paragraph: bool = False


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    paragraph: int


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def build_words(segments: Sequence[Segment]) -> list[Word]:
    words: list[Word] = []
    paragraph = 0
    offset = 0.0
    for segment in segments:
        # A clip that opens a paragraph behaves as if preceded by a blank line.
        newlines = 2 if segment.starts_paragraph else 0
        current: list[str] = []
        start = end = 0.0
        a = segment.alignment
        single = all(len(c) == 1 for c in a.characters)  # what the live API returns
        in_tag = tag_mask("".join(a.characters)) if single else [False] * len(a.characters)
        for index, (char, char_start, char_end) in enumerate(
            zip(a.characters, a.starts, a.ends, strict=True)
        ):
            if char.isspace() or in_tag[index]:
                if current:
                    words.append(Word("".join(current), round(start, 3), round(end, 3), paragraph))
                    current = []
                newlines += char == "\n"
                continue
            if not current:
                if newlines >= 2 and words:
                    paragraph += 1
                newlines = 0
                start = offset + char_start
            end = max(start, offset + char_end)
            current.append(char)
        if current:  # a clip boundary is always a word boundary
            words.append(Word("".join(current), round(start, 3), round(end, 3), paragraph))
        offset += segment.duration
    return words


def build_cues(words: Sequence[Word]) -> list[Cue]:
    cues: list[Cue] = []
    current: list[Word] = []

    def flush() -> None:
        if current:
            text = _wrap(" ".join(w.text for w in current))
            cues.append(Cue(current[0].start, current[-1].end, text))
            current.clear()

    for word in words:
        if current:
            length = sum(len(w.text) + 1 for w in current) + len(word.text)
            if (
                word.paragraph != current[-1].paragraph
                or length > _CUE_CHARS
                or word.end - current[0].start > _CUE_SECONDS
            ):
                flush()
        current.append(word)
        if word.text.endswith(_SENTENCE_END) and len(" ".join(w.text for w in current)) >= (
            _MIN_SENTENCE_CUE_CHARS
        ):
            flush()
    flush()

    # Never let a cue end before it starts or overlap the next one.
    fixed: list[Cue] = []
    for i, cue in enumerate(cues):
        end = max(cue.end, cue.start + 0.01)
        if i + 1 < len(cues):
            end = min(end, cues[i + 1].start) if cues[i + 1].start > cue.start else end
        fixed.append(Cue(cue.start, end, cue.text))
    return fixed


def _wrap(text: str) -> str:
    """Split into at most two lines at the space nearest the middle."""
    if len(text) <= _LINE_CHARS:
        return text
    middle = len(text) // 2
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    if not spaces:
        return text
    cut = min(spaces, key=lambda i: abs(i - middle))
    return f"{text[:cut]}\n{text[cut + 1 :]}"


def _timestamp(seconds: float, separator: str) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def to_webvtt(cues: Sequence[Cue]) -> str:
    blocks = ["WEBVTT", ""]
    for index, cue in enumerate(cues, start=1):
        # WebVTT cue text is HTML-ish: escape it so narration can never inject markup.
        text = cue.text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        blocks += [
            str(index),
            f"{_timestamp(cue.start, '.')} --> {_timestamp(cue.end, '.')}",
            text,
            "",
        ]
    return "\n".join(blocks)


def to_srt(cues: Sequence[Cue]) -> str:
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        blocks += [
            str(index),
            f"{_timestamp(cue.start, ',')} --> {_timestamp(cue.end, ',')}",
            cue.text.replace("-->", "->"),  # the only sequence that can break an SRT parser
            "",
        ]
    return "\n".join(blocks)
