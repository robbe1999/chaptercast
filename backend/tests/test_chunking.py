from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chaptercast.chunking import normalize_text, split_text


def _squash(text: str) -> str:
    return "".join(text.split())


class TestExamples:
    def test_empty_input_gives_no_chunks(self) -> None:
        assert split_text("") == []
        assert split_text("  \n\n \t ") == []

    def test_short_text_is_one_chunk(self) -> None:
        assert split_text("Hello world.") == ["Hello world."]

    def test_splits_on_sentence_boundaries(self) -> None:
        text = "Alpha beta gamma. Delta epsilon zeta. Eta theta iota."
        assert split_text(text, max_chars=30) == [
            "Alpha beta gamma.",
            "Delta epsilon zeta.",
            "Eta theta iota.",
        ]

    def test_paragraph_break_is_kept_when_it_fits(self) -> None:
        assert split_text("First para.\n\nSecond para.", max_chars=100) == [
            "First para.\n\nSecond para."
        ]

    def test_abbreviation_does_not_end_a_sentence(self) -> None:
        chunks = split_text("Dr. Smith arrived. He sat.", max_chars=20)
        assert chunks == ["Dr. Smith arrived.", "He sat."]

    def test_initials_do_not_end_a_sentence(self) -> None:
        assert split_text("J. R. R. Tolkien wrote it.", max_chars=30) == [
            "J. R. R. Tolkien wrote it."
        ]

    def test_closing_quote_stays_with_its_sentence(self) -> None:
        chunks = split_text('She said, "Go now." Then she left.', max_chars=25)
        assert chunks[0].endswith('"Go now."')

    def test_long_sentence_falls_back_to_clauses(self) -> None:
        text = "one two three, four five six, seven eight nine, ten eleven twelve"
        chunks = split_text(text, max_chars=30)
        assert all(len(c) <= 30 for c in chunks)
        assert len(chunks) > 1
        assert chunks[0].endswith(",") or chunks[0].endswith("three,")

    def test_unbreakable_token_is_hard_sliced(self) -> None:
        chunks = split_text("x" * 100, max_chars=30)
        assert [len(c) for c in chunks] == [30, 30, 30, 10]
        assert "".join(chunks) == "x" * 100

    def test_max_chars_floor(self) -> None:
        with pytest.raises(ValueError, match="at least 20"):
            split_text("hi", max_chars=5)


class TestNormalize:
    def test_strips_control_and_bidi_characters(self) -> None:
        assert normalize_text("a‮b\x00c﻿d") == "abcd"

    def test_collapses_whitespace_but_keeps_paragraphs(self) -> None:
        assert normalize_text("a   b\t\tc\r\n\r\n\r\n d  \n e") == "a b c\n\nd e"

    def test_applies_nfc(self) -> None:
        assert normalize_text("é") == "é"


@settings(deadline=None, max_examples=200)
@given(text=st.text(max_size=3000), max_chars=st.integers(min_value=20, max_value=300))
def test_chunks_are_bounded_and_non_empty(text: str, max_chars: int) -> None:
    for chunk in split_text(text, max_chars):
        assert 0 < len(chunk) <= max_chars


@settings(deadline=None, max_examples=200)
@given(text=st.text(max_size=3000), max_chars=st.integers(min_value=20, max_value=300))
def test_no_content_is_lost_or_reordered(text: str, max_chars: int) -> None:
    chunks = split_text(text, max_chars)
    assert _squash("".join(chunks)) == _squash(normalize_text(text))


_word = st.text(alphabet=st.characters(categories=["L", "Nd"]), min_size=1, max_size=14)
_sep = st.sampled_from([" ", " ", " ", ". ", ", ", "! ", "? ", "\n\n", "; "])


@settings(deadline=None, max_examples=200)
@given(
    parts=st.lists(st.tuples(_word, _sep), min_size=1, max_size=400),
    max_chars=st.integers(min_value=20, max_value=250),
)
def test_prose_like_text_round_trips(parts: list[tuple[str, str]], max_chars: int) -> None:
    text = "".join(w + s for w, s in parts)
    chunks = split_text(text, max_chars)
    assert all(0 < len(c) <= max_chars for c in chunks)
    # Compared against the NFC-normalised text: normalisation is intentional
    # (e.g. U+F900 canonically maps to U+8C48) and must be the only change.
    assert _squash("".join(chunks)) == _squash(normalize_text(text))


@settings(deadline=None, max_examples=100)
@given(text=st.text(max_size=1000), max_chars=st.integers(min_value=20, max_value=200))
def test_chunking_is_deterministic(text: str, max_chars: int) -> None:
    assert split_text(text, max_chars) == split_text(text, max_chars)
