from __future__ import annotations

import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chaptercast.chunking import normalize_text, split_chunks
from chaptercast.tags import MAX_TAG_CHARS, find_tags, has_tags, is_tag, strip_tags, tag_mask


@pytest.mark.parametrize(
    "token", ["[warm]", "[whispered]", "[softly whispered]", "[sighs]", "[don't]", "[Warm]"]
)
def test_tags(token: str) -> None:
    assert is_tag(token)


@pytest.mark.parametrize(
    "token",
    [
        "[1]",  # footnote
        "[]",
        "[ warm]",
        "[warm ]",
        "[two  spaces]",
        "[see note 4]",  # digits: ordinary text
        "[<script>]",
        "[" + "a" * MAX_TAG_CHARS + "]",  # too long to be an instruction
        "warm",
        "[warm",
    ],
)
def test_not_tags(token: str) -> None:
    assert not is_tag(token)


def test_find_strip_and_mask() -> None:
    text = "[warm] It was [softly whispered] quiet [1]."
    assert [m.group(0) for m in find_tags(text)] == ["[warm]", "[softly whispered]"]
    assert has_tags(text) and not has_tags("No tags [1] here.")
    assert normalize_text(strip_tags(text)) == "It was quiet [1]."
    mask = tag_mask("[hi] yo")
    assert mask == [True] * 4 + [False] * 3


# ------------------------------------------------------------------- chunking
def test_a_tag_stays_with_the_words_it_modifies() -> None:
    text = "First sentence goes here. [warm] [whispered] The quiet part follows now."
    for size in range(40, 80):
        chunks = [c.text for c in split_chunks(text, size)]
        holder = next(c for c in chunks if "[warm]" in c)
        assert "[warm] [whispered] The" in holder, (size, chunks)


def test_a_tag_at_the_end_of_a_paragraph_moves_to_the_next_chunk() -> None:
    text = "One short paragraph here. [excited]\n\nThe next paragraph starts here."
    chunks = split_chunks(text, 45)
    assert [c.text for c in chunks] == [
        "One short paragraph here.",
        "[excited] The next paragraph starts here.",
    ]
    assert all(c.starts_paragraph for c in chunks)


def test_a_chunk_of_only_tags_is_merged_forward() -> None:
    chunks = split_chunks("Some words in a sentence here.\n\n[warm]\n\nThen more words.", 40)
    assert [c.text for c in chunks] == ["Some words in a sentence here.", "[warm] Then more words."]


def test_a_trailing_tag_at_the_very_end_stays_put() -> None:
    assert [c.text for c in split_chunks("Goodbye now. [sighs]", 40)] == ["Goodbye now. [sighs]"]


def test_a_tag_is_never_hard_sliced_even_when_glued_to_a_huge_word() -> None:
    chunks = [c.text for c in split_chunks("[warm] " + "x" * 120, 50)]
    assert chunks[0].startswith("[warm]")
    assert all(len(c) <= 50 for c in chunks)
    assert "".join(chunks).replace(" ", "") == "[warm]" + "x" * 120


def test_private_use_characters_cannot_spoof_the_internal_glue() -> None:
    assert normalize_text("ab") == "ab"


_TAG_WORDS = st.sampled_from(["warm", "whispered", "excited", "softly whispered", "sighs"])
_WORDS = st.text(alphabet="abcdefghij", min_size=1, max_size=12)
_PUNCT = st.sampled_from(["", ",", ".", "!", "?"])


@st.composite
def tagged_text(draw: st.DrawFn) -> str:
    paragraphs = []
    for _ in range(draw(st.integers(1, 4))):
        tokens = []
        for _ in range(draw(st.integers(1, 30))):
            if draw(st.booleans()) and draw(st.booleans()):
                tokens.append(f"[{draw(_TAG_WORDS)}]")
            else:
                tokens.append(draw(_WORDS) + draw(_PUNCT))
        paragraphs.append(" ".join(tokens))
    return "\n\n".join(paragraphs)


@settings(deadline=None, max_examples=300)
@given(text=tagged_text(), max_chars=st.integers(min_value=MAX_TAG_CHARS, max_value=200))
def test_tags_are_atomic_ordered_and_bounded(text: str, max_chars: int) -> None:
    chunks = split_chunks(text, max_chars)
    expected = [m.group(0) for m in find_tags(normalize_text(text))]
    found = [m.group(0) for c in chunks for m in find_tags(c.text)]
    assert found == expected  # no tag split, lost, duplicated or reordered
    for chunk in chunks:
        assert 0 < len(chunk.text) <= max_chars
        # Brackets only ever appear as parts of whole tags in this generator.
        assert chunk.text.count("[") == chunk.text.count("]") == len(list(find_tags(chunk.text)))
    joined = "".join("".join(c.text.split()) for c in chunks)
    assert joined == "".join(normalize_text(text).split())


@settings(deadline=None, max_examples=300)
@given(text=tagged_text(), max_chars=st.integers(min_value=40, max_value=200))
def test_a_chunk_only_ends_with_a_tag_when_moving_it_would_not_fit(
    text: str, max_chars: int
) -> None:
    chunks = [c.text for c in split_chunks(text, max_chars)]
    for current, following in itertools.pairwise(chunks):
        last_word = current.split()[-1]
        if is_tag(last_word):
            tail = [m for m in find_tags(current) if not current[m.end() :].strip()]
            assert len(tail[-1].group(0)) + 1 + len(following) > max_chars
