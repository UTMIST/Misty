import random

import pytest

from src.chunking import (
    CHUNKER_VERSION,
    ChunkSettings,
    chunk_text,
    estimate_tokens,
)
from src.content import content_hash

# Small budgets keep fixtures readable: 20 tokens ~ 80 chars, 4 tokens ~ 16 chars.
SMALL = ChunkSettings(target_tokens=20, overlap_tokens=4)


def _check(source, result, count_tokens=estimate_tokens):
    """Invariants every chunking must satisfy, whatever the input."""
    settings = result.settings
    chunks = result.chunks
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert c.text == source[c.start : c.end]
        assert c.text and c.text == c.text.strip()
        assert c.token_count == count_tokens(c.text) <= settings.target_tokens
        assert c.chunk_hash == content_hash(c.text)
        assert c.section_start <= c.start < c.end <= c.section_end
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start > prev.start
        assert nxt.end > prev.end
        assert nxt.overlap_prev == max(0, prev.end - nxt.start)
        if nxt.overlap_prev:
            assert count_tokens(source[nxt.start : prev.end]) <= settings.overlap_tokens
    # Nothing but whitespace may fall outside every chunk.
    covered_to = 0
    for c in chunks:
        assert source[covered_to : c.start].strip() == "" or c.start < covered_to
        covered_to = max(covered_to, c.end)
    assert source[covered_to:].strip() == ""
    # Deterministic: same input and settings, same output.
    assert chunk_text(source, settings, count_tokens) == result


def _prose(sentences):
    return " ".join(f"Sentence number {i} says something useful." for i in range(sentences))


# ---- settings -------------------------------------------------------------


def test_default_settings_match_the_208_decision():
    assert ChunkSettings() == ChunkSettings(target_tokens=500, overlap_tokens=50)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_tokens": 0},
        {"target_tokens": -5},
        {"target_tokens": 100, "overlap_tokens": 100},
        {"target_tokens": 100, "overlap_tokens": 150},
        {"overlap_tokens": -1},
        {"target_tokens": 1.5},
        {"target_tokens": True},
    ],
)
def test_invalid_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ChunkSettings(**kwargs)


def test_zero_overlap_is_allowed_and_produces_no_overlap():
    text = _prose(30)
    result = chunk_text(text, ChunkSettings(target_tokens=20, overlap_tokens=0))
    _check(text, result)
    assert all(c.overlap_prev == 0 for c in result.chunks)


def test_estimate_tokens_is_four_chars_rounded_up():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


# ---- empty and short input ------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\n\t \r\n"])
def test_empty_or_whitespace_only_yields_no_chunks(text):
    result = chunk_text(text)
    assert result.chunks == ()
    assert result.chunker_version == CHUNKER_VERSION
    assert result.counting == "chars/4"


def test_short_text_is_one_chunk_with_edge_boundaries():
    result = chunk_text("  A single link: https://example.com  \n")
    _check("  A single link: https://example.com  \n", result)
    (chunk,) = result.chunks
    assert chunk.text == "A single link: https://example.com"
    assert (chunk.start, chunk.start_boundary, chunk.end_boundary) == (2, "start", "end")
    assert chunk.heading_path == ()
    assert chunk.locator is None


def test_exactly_at_budget_is_one_chunk_and_one_over_splits():
    at_budget = "word " * 15 + "abcde"  # 80 chars = 20 tokens
    assert estimate_tokens(at_budget) == 20
    assert len(chunk_text(at_budget, SMALL).chunks) == 1
    over = at_budget + "x"
    result = chunk_text(over, SMALL)
    _check(over, result)
    assert len(result.chunks) == 2


# ---- long text, overlap, hard cuts -----------------------------------------


def test_long_prose_splits_on_sentences_with_overlap():
    text = _prose(200)
    result = chunk_text(text)
    _check(text, result)
    assert len(result.chunks) > 3
    assert all(c.end_boundary == "sentence" for c in result.chunks[:-1])
    assert all(c.overlap_prev > 0 for c in result.chunks[1:])


def test_unbroken_token_is_hard_cut_within_budget():
    text = "https://example.com/" + "a1b2" * 2500
    result = chunk_text(text)
    _check(text, result)
    assert len(result.chunks) > 1
    assert all(c.end_boundary == "hard" for c in result.chunks[:-1])


def test_hard_cut_does_not_split_a_zwj_emoji_sequence():
    person = "\U0001f469‍\U0001f4bb"  # woman technologist: 3 code points
    text = person * 200
    result = chunk_text(text, SMALL)
    _check(text, result)
    for c in result.chunks:
        assert c.text.startswith("\U0001f469")
        assert c.text.endswith("\U0001f4bb")


@pytest.mark.parametrize(
    "unit",
    [
        "\U0001f1e8\U0001f1e6",  # flag: regional-indicator pair
        "1\ufe0f\u20e3",  # keycap: enclosing mark has combining class 0
        "\u0915\u093e",  # Devanagari ka + vowel sign aa (spacing mark)
        "\u0e01\u0e33",  # Thai
        "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",  # England
        "\u1100\u1161\u11a8",  # Hangul L V T jamo
    ],
)
@pytest.mark.parametrize("prefix", range(1, 5))
def test_hard_cut_does_not_split_a_grapheme_cluster(unit, prefix):
    text = "x" * prefix + unit * 100
    result = chunk_text(text, ChunkSettings(target_tokens=3, overlap_tokens=0))
    _check(text, result)
    boundaries = {prefix + k * len(unit) for k in range(101)}
    for c in result.chunks:
        assert c.end <= prefix or c.end in boundaries


# ---- offsets ----------------------------------------------------------------


def test_repeated_paragraphs_get_distinct_correct_offsets():
    para = "The same paragraph appears again and again in this document."
    text = "\n\n".join([para] * 6)
    result = chunk_text(text, ChunkSettings(target_tokens=20, overlap_tokens=0))
    _check(text, result)
    starts = [c.start for c in result.chunks if c.text == para]
    assert len(starts) == 6
    assert len(set(starts)) == 6


@pytest.mark.parametrize(
    "text",
    [
        "Café crème brûlée. " * 20,  # combining accents
        "文档内容。" * 60,  # CJK, no spaces
        "Emoji \U0001f600 here. " * 30,
        "Line one.\r\n\r\nLine two.\r\n" * 20,  # CRLF
    ],
)
def test_unicode_and_crlf_offsets_are_exact(text):
    _check(text, chunk_text(text, SMALL))


def test_entity_like_text_is_never_decoded():
    text = "Cell one&#10;Cell two&#10;Cell three " * 10
    result = chunk_text(text, SMALL)
    _check(text, result)
    assert "&#10;" in result.chunks[0].text


def test_counting_label_is_explicit_for_anonymous_counters():
    import functools

    with pytest.raises(ValueError, match="counting="):
        chunk_text("hi", count_tokens=lambda t: len(t.split()))
    with pytest.raises(ValueError, match="counting="):
        chunk_text("hi", count_tokens=functools.partial(len))
    result = chunk_text("hi", count_tokens=lambda t: len(t), counting="chars")
    assert result.counting == "chars"


def test_counter_that_cannot_fit_one_character_is_rejected():
    def utf8_bytes(text):
        return len(text.encode())

    with pytest.raises(ValueError, match="target_tokens"):
        chunk_text("é" * 10, ChunkSettings(target_tokens=1, overlap_tokens=0), utf8_bytes)


def test_custom_token_counter_is_honored():
    def words(text):
        return len(text.split())

    text = _prose(20)
    result = chunk_text(text, ChunkSettings(target_tokens=12, overlap_tokens=3), words)
    _check(text, result, words)
    assert result.counting == "words"


# ---- structure and metadata ------------------------------------------------


def test_heading_path_and_sections_follow_nesting():
    text = (
        "# Guide\n\nIntro.\n\n## Setup\n\nInstall it.\n\n### Linux\n\nUse apt.\n\n## Usage\n\nRun."
    )
    result = chunk_text(text, ChunkSettings(target_tokens=5, overlap_tokens=1))
    _check(text, result)
    linux = next(c for c in result.chunks if "apt" in c.text)
    assert linux.heading_path == ("Guide", "Setup", "Linux")
    usage = next(c for c in result.chunks if "Run." in c.text)
    assert usage.heading_path == ("Guide", "Usage")
    assert usage.section_end == len(text)


def test_small_sections_pack_into_one_chunk():
    text = "## A\n\none\n\n## B\n\ntwo\n\n## C\n\nthree"
    (chunk,) = chunk_text(text).chunks
    # Spans three top-level sections, so no single heading encloses it.
    assert chunk.heading_path == ()
    assert (chunk.section_start, chunk.section_end) == (0, len(text))


def test_packed_chunk_reports_the_section_enclosing_all_of_it():
    text = "# Guide\n\n## Setup\n\n### Linux\n\napt.\n\n## Usage\n\nRun it.\n\n# Appendix\n\nMore."
    result = chunk_text(text, ChunkSettings(target_tokens=15, overlap_tokens=0))
    _check(text, result)
    first = result.chunks[0]
    assert "## Usage" in first.text
    assert first.heading_path == ("Guide",)
    assert first.section_end == text.index("# Appendix")


def test_packed_slides_keep_the_starting_slide_locator():
    text = "## Slide 1: A\nOne.\n\n## Slide 2: B\nTwo."
    (chunk,) = chunk_text(text).chunks
    assert chunk.heading_path == ()
    assert chunk.locator == ("slide", 1)


def test_chunk_starting_mid_section_stops_at_next_heading():
    text = "# Intro\n\n" + _prose(12) + "\n\n## Next\n\nShort."
    result = chunk_text(text, SMALL)
    _check(text, result)
    for c in result.chunks:
        if c.start_boundary not in ("start", "heading"):
            assert "## Next" not in c.text


def test_overlap_never_reaches_back_across_a_heading():
    text = "## One\n\n" + _prose(6) + "\n\n## Two\n\n" + _prose(6)
    result = chunk_text(text, SMALL)
    _check(text, result)
    two = text.index("## Two")
    for c in result.chunks:
        if c.start >= two:
            assert c.heading_path == ("Two",)


def test_overlap_never_starts_inside_a_heading_line():
    heading = "## A very long heading title that is over the budget by itself for sure yes"
    text = heading + "\n\n" + "Body sentence. " * 8
    result = chunk_text(text, SMALL)
    _check(text, result)
    for c in result.chunks[1:]:
        assert not 0 < c.start <= len(heading), c.text


@pytest.mark.parametrize(
    "prose",
    [
        "Contact Dr. Smith about it",
        "We met U. of T. staff today",
        "Bring snacks, e.g. chips and fruit",
        "The Jan. 5 meeting moved",
        "Version 2. then version three",
    ],
)
def test_abbreviations_and_initials_are_not_sentence_ends(prose):
    text = (prose + ". ") * 6
    result = chunk_text(text, ChunkSettings(target_tokens=10, overlap_tokens=0))
    _check(text, result)
    for c in result.chunks:
        if c.end_boundary == "sentence":
            assert c.text.endswith(prose + "."), c.text


def test_slides_shape_gets_slide_locator_and_keeps_br_cells():
    text = (
        "## Slide 1: Welcome\nUTMIST overview\n\n"
        "## Slide 3: Budget\n| Item | Cost |\n| --- | --- |\n| Food<br>Drinks | $120 |\n\n"
        "### Speaker notes\nMention sponsors."
    )
    result = chunk_text(text, ChunkSettings(target_tokens=12, overlap_tokens=2))
    _check(text, result)
    notes = next(c for c in result.chunks if "sponsors" in c.text)
    assert notes.locator == ("slide", 3)
    assert any("Food<br>Drinks" in c.text for c in result.chunks)


def test_pdf_shape_gets_page_locator():
    text = "## Page 1\n" + _prose(4) + "\n\n## Page 2\n" + _prose(4)
    result = chunk_text(text, SMALL)
    _check(text, result)
    assert result.chunks[0].locator == ("page", 1)
    assert result.chunks[-1].locator == ("page", 2)


def test_sheets_csv_is_not_cut_inside_a_quoted_multiline_cell():
    rows = "".join(f'r{i},"first line\nsecond line",{i}\n' for i in range(20))
    text = "## Roster\n" + rows
    result = chunk_text(text, SMALL)
    _check(text, result)
    for c in result.chunks:
        if c.end_boundary != "end":
            assert c.text.count('"') % 2 == 0, c.text
    assert result.chunks[0].heading_path == ("Roster",)


def _starts_inside_quote(text, pos):
    return text.count('"', 0, pos) % 2 == 1


def test_csv_cell_with_blank_lines_and_bullets_is_not_cut_through():
    cell = '"' + "line one of cell\n\n- bullet a\n- bullet b\n" * 3 + '"'
    text = "## Tab\nname,notes\nalice," + cell + ",1\nbob,ok,2\n"
    result = chunk_text(text, SMALL)
    _check(text, result)
    # The cell exceeds the budget, so sentence/word cuts inside it are allowed,
    # but no chunk may start at a paragraph, list, or line boundary inside it.
    for c in result.chunks:
        if _starts_inside_quote(text, c.start):
            assert c.start_boundary in ("sentence", "word", "hard"), c


def test_rows_after_an_oversized_quoted_cell_are_not_cut_mid_cell():
    big = '"' + "word " * 60 + '"'
    rows = "".join(f'r{i},"x\ny",{i}\n' for i in range(10))
    text = f"## Tab\na,{big},1\n" + rows
    result = chunk_text(text, SMALL)
    _check(text, result)
    after = text.index("r0,")
    for c in result.chunks:
        if c.start >= after:
            assert not _starts_inside_quote(text, c.start), c.text


def test_unbalanced_quote_does_not_disable_cuts_past_its_section():
    text = '## A\n\nA 12" pipe.\n\n## B\n\n' + "".join(f"Line {i} of B\n" for i in range(30))
    result = chunk_text(text, SMALL)
    _check(text, result)
    b = [c for c in result.chunks if c.heading_path == ("B",)]
    assert {c.end_boundary for c in b[:-1]} == {"line"}


def test_unbalanced_quote_at_scale_stays_fast():
    # Regression: line cuts once re-counted quotes from the span start, O(n^2).
    import time

    text = 'He said "hi\n' + "".join(f"line {i} text here\n" for i in range(50_000))
    t = time.perf_counter()
    _check(text, chunk_text(text))
    assert time.perf_counter() - t < 5


def test_hash_lines_in_fenced_code_are_not_headings():
    text = "# Setup\n\nRun:\n\n```bash\n# install deps\npip install x\n```\n\nDone."
    (chunk,) = chunk_text(text).chunks
    assert chunk.heading_path == ("Setup",)
    assert chunk.section_end == len(text)


def test_unclosed_fence_does_not_hide_later_headings():
    text = "# A\n\n```\nstray fence\n\n## B\n\n" + _prose(6)
    result = chunk_text(text, SMALL)
    _check(text, result)
    assert result.chunks[-1].heading_path == ("A", "B")


@pytest.mark.parametrize(
    "line, title",
    [
        ("# C#", "C#"),
        ("## F# notes", "F# notes"),
        ("## Notes ##", "Notes"),
        ("### Issue #12", "Issue #12"),
    ],
)
def test_heading_titles_keep_meaningful_hashes(line, title):
    (chunk,) = chunk_text(line + "\n\nBody.").chunks
    assert chunk.heading_path == (title,)


def test_forms_shape_keeps_each_question_with_its_options():
    # The Forms extractor's shape: title, description, then "- question" items with
    # an indented "  Options:" line, and "## section" headers between them.
    questions = "".join(
        f"- Question {i} about your preferences?\n  Options: Alpha, Beta, Gamma\n" for i in range(8)
    )
    text = "# Exec Application\nRolling basis.\n" + questions + "## Role Preferences\n" + questions
    result = chunk_text(text, ChunkSettings(target_tokens=30, overlap_tokens=5))
    _check(text, result)
    assert len(result.chunks) > 2
    for c in result.chunks:
        assert not c.text.startswith("Options:"), c.text


def test_docs_table_rows_split_on_lines():
    header = "# Roles\n\n| Role | Owner |\n| --- | --- |\n"
    text = header + "".join(f"| Role {i} | Person {i} |\n" for i in range(30))
    result = chunk_text(text, SMALL)
    _check(text, result)
    assert {c.end_boundary for c in result.chunks[:-1]} <= {"line", "paragraph"}


# ---- seeded fuzz ------------------------------------------------------------

_FRAGMENTS = (
    "# H{}\n",
    "## Slide {}: t\n",
    "- item {} here.\n",
    "Sentence {} is here. ",
    "\n\n",
    'r{},"a\nb",x\n',
    "\U0001f469‍\U0001f4bb",
    "文。",
    "x" * 300,
    "\r\n",
    '"',
    "&#10;",
)


@pytest.mark.parametrize("seed", range(200))
def test_invariants_hold_on_random_structured_input(seed):
    # Seeded, so a failure reproduces exactly; the seed is in the test id.
    rng = random.Random(seed)
    text = "".join(rng.choice(_FRAGMENTS).format(i) for i in range(rng.randint(0, 300)))
    target = rng.randint(1, 120)
    settings = ChunkSettings(target_tokens=target, overlap_tokens=rng.randint(0, target - 1))
    _check(text, chunk_text(text, settings))
