from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chaptercast.providers.base import Alignment
from chaptercast.transcript import (
    Cue,
    Segment,
    Word,
    build_cues,
    build_words,
    to_srt,
    to_webvtt,
)
from tests.conftest import even_alignment


def segment(text: str, duration: float = 1.0, starts_paragraph: bool = False) -> Segment:
    return Segment(even_alignment(text, duration), duration, starts_paragraph)


def test_words_are_grouped_and_offset_by_previous_clips() -> None:
    words = build_words([segment("Hello world.", 1.2), segment("Second clip.", 2.0)])
    assert [w.text for w in words] == ["Hello", "world.", "Second", "clip."]
    assert words[0].start == 0.0
    assert words[0].end == pytest.approx(0.5)  # 5 of 12 chars of a 1.2 s clip
    assert words[2].start == pytest.approx(1.2)  # second clip starts after the first
    assert words[3].end == pytest.approx(3.2)


def test_paragraphs_come_from_blank_lines_and_from_flagged_clips() -> None:
    words = build_words(
        [segment("One.\n\nTwo."), segment("Three.", starts_paragraph=True), segment("Four.")]
    )
    assert [(w.text, w.paragraph) for w in words] == [
        ("One.", 0),
        ("Two.", 1),
        ("Three.", 2),
        ("Four.", 2),
    ]


def test_leading_flag_and_single_newlines_do_not_start_paragraphs() -> None:
    words = build_words([segment("Line one\nline two", starts_paragraph=True)])
    assert {w.paragraph for w in words} == {0}


def test_a_clip_boundary_ends_a_word_even_without_whitespace() -> None:
    words = build_words([segment("abc"), segment("def")])
    assert [w.text for w in words] == ["abc", "def"]


def test_word_end_is_never_before_its_start() -> None:
    weird = Alignment(("a", "b"), (0.5, 0.6), (0.1, 0.2))  # provider glitch: ends < starts
    (word,) = build_words([Segment(weird, 1.0)])
    assert word.end >= word.start


@settings(deadline=None, max_examples=150)
@given(
    texts=st.lists(
        st.text(alphabet=st.sampled_from("ab .\n"), min_size=1, max_size=40),
        min_size=1,
        max_size=6,
    ),
)
def test_words_cover_all_non_space_text_in_order(texts: list[str]) -> None:
    words = build_words([segment(t) for t in texts])
    assert "".join(w.text for w in words) == "".join("".join(t.split()) for t in texts)
    starts = [w.start for w in words]
    assert starts == sorted(starts)


# ------------------------------------------------------------------------ captions
def words_from(text: str, seconds_per_word: float = 0.4, paragraph: int = 0) -> list[Word]:
    return [
        Word(w, round(i * seconds_per_word, 3), round((i + 1) * seconds_per_word, 3), paragraph)
        for i, w in enumerate(text.split())
    ]


def test_cues_prefer_sentence_ends() -> None:
    words = words_from("This is the first sentence here. And this is the second one here.")
    cues = build_cues(words)
    assert [c.text for c in cues] == [
        "This is the first sentence here.",
        "And this is the second one here.",
    ]
    assert cues[0].end <= cues[1].start


def test_cues_respect_length_and_duration_limits() -> None:
    long_text = " ".join(["word"] * 200)
    for cue in build_cues(words_from(long_text, 0.2)):
        lines = cue.text.split("\n")
        assert len(lines) <= 2
        assert len(cue.text.replace("\n", " ")) <= 84
        assert cue.end - cue.start <= 6.0 + 1e-9
    slow = build_cues(words_from("a b c d e f g h", 2.0))
    assert all(c.end - c.start <= 6.0 for c in slow)


def test_long_cues_wrap_onto_two_balanced_lines() -> None:
    (cue,) = build_cues(words_from("one two three four five six seven eight nine ten eleven"))
    top, bottom = cue.text.split("\n")
    assert abs(len(top) - len(bottom)) <= 6


def test_a_new_paragraph_starts_a_new_cue() -> None:
    words = [*words_from("short one", paragraph=0), Word("next", 1.0, 1.2, 1)]
    words.append(Word("para", 1.2, 1.4, 1))
    assert [c.text for c in build_cues(words)] == ["short one", "next para"]


def test_srt_format() -> None:
    srt = to_srt([Cue(0.0, 1.5, "Hello."), Cue(3661.25, 3662.0, "A --> B")])
    assert srt == (
        "1\n00:00:00,000 --> 00:00:01,500\nHello.\n\n2\n01:01:01,250 --> 01:01:02,000\nA -> B\n"
    )


def test_webvtt_format_escapes_markup() -> None:
    vtt = to_webvtt([Cue(0.0, 1.0, "<b>bold</b> & co")])
    assert vtt.startswith("WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\n")
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; co" in vtt
    assert "<b>" not in vtt


def test_empty_inputs() -> None:
    assert build_words([]) == []
    assert build_cues([]) == []
    assert to_webvtt([]) == "WEBVTT\n"
    assert to_srt([]) == ""
