"""Expression tags such as ``[warm]`` or ``[softly whispered]``.

Models with audio-tag support (Eleven v4) read a tag as an instruction, not as
words. A tag is a bracketed run of letters with single spaces between words, at
most ``MAX_TAG_CHARS`` long. Anything else in brackets (``[1]``, ``[see note]``
with digits, long asides) is ordinary text.

The length cap keeps every tag shorter than the smallest chunk size, so the
chunker can always keep a tag in one piece.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

MAX_TAG_CHARS = 20
_CANDIDATE_RE = re.compile(r"\[[A-Za-z][A-Za-z'-]*(?: [A-Za-z][A-Za-z'-]*)*\]")


def find_tags(text: str) -> Iterator[re.Match[str]]:
    """Every tag in ``text``, in order."""
    return (m for m in _CANDIDATE_RE.finditer(text) if len(m.group(0)) <= MAX_TAG_CHARS)


def is_tag(token: str) -> bool:
    return len(token) <= MAX_TAG_CHARS and _CANDIDATE_RE.fullmatch(token) is not None


def has_tags(text: str) -> bool:
    return next(find_tags(text), None) is not None


def strip_tags(text: str) -> str:
    """Remove tags for models that would otherwise read them aloud.

    Each tag becomes a space; the chunker's normalisation collapses the extra
    whitespace, so "[warm] It was quiet." becomes "It was quiet.".
    """
    out: list[str] = []
    last = 0
    for match in find_tags(text):
        out.append(text[last : match.start()])
        out.append(" ")
        last = match.end()
    out.append(text[last:])
    return "".join(out)


def tag_mask(text: str) -> list[bool]:
    """For each character of ``text``, whether it belongs to a tag."""
    mask = [False] * len(text)
    for match in find_tags(text):
        for i in range(match.start(), match.end()):
            mask[i] = True
    return mask
