"""Pure splitter: stored document text in, ordered chunks with source offsets out.

Implements the #208 decision recorded in docs/ARCHITECTURE.md ("Retrieval layer"):
~500-token chunks, ~50-token overlap, boundaries first and size second. No I/O and
no knowledge of URLs, connectors, storage, embeddings, or access — the caller (#175)
supplies `doc_content.content_text` and decides what to do with the chunks.

Offsets are Python string indices (Unicode code points), half-open `[start, end)`,
into the exact text passed in. Every chunk satisfies
`chunk.text == text[chunk.start:chunk.end]`: nothing is normalized, decoded, or
prefixed, so a chunk can always be traced back to the stored content.
"""

import re
from bisect import bisect_right
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil

from src.content import content_hash

# Bump on ANY change that can alter output for the same input and settings, so the
# indexing pipeline can tell which stored chunks were produced by an older splitter.
CHUNKER_VERSION = 1

# The #208 spike's proxy for a model token. An estimate, not a tokenizer: 500
# estimated tokens is ~2,000 characters, far inside /embed's 32,000-character cap.
CHARS_PER_TOKEN = 4

# Boundary levels, coarsest first. A span is split at a finer level only when it is
# still over budget after splitting at every coarser one.
LEVELS = ("heading", "list", "paragraph", "line", "sentence", "word", "hard")
_RANK = {level: i for i, level in enumerate(LEVELS)}

# A chunk that overflows prefers to close at a coarser boundary if one exists in the
# last (1 - this) of the budget, so chunks end at headings/paragraphs, not mid-list.
_LOOKBACK_FRACTION = 0.8

_HEADING = re.compile(r"(?m)^(#{1,6})[ \t]+([^\r\n]*)")
# A closing run of #s counts only after whitespace (or alone), so "# C#" keeps its #.
_CLOSING_HASHES = re.compile(r"(?:^|[ \t])#+[ \t]*$")
_FENCE = re.compile(r"(?m)^ {0,3}(`{3,}|~{3,})")
_LIST_ITEM = re.compile(r"(?m)^(?:[-*+]|\d+[.)])[ \t]")
_PARAGRAPH_BREAK = re.compile(r"\n(?:[ \t]*\r?\n)+")
_LINE_BREAK = re.compile(r"\n")
_SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]*\s+|[。！？]")
_WHITESPACE = re.compile(r"\s+")
# The exact heading shapes the Slides and PDF extractors emit (connectors service).
_LOCATOR = re.compile(r"^(Slide|Page) (\d+)(?::|$)")


def estimate_tokens(text: str) -> int:
    """Estimated token count: one token per CHARS_PER_TOKEN code points, rounded up."""
    return ceil(len(text) / CHARS_PER_TOKEN)


@dataclass(frozen=True)
class ChunkSettings:
    target_tokens: int = 500
    overlap_tokens: int = 50

    def __post_init__(self) -> None:
        for name in ("target_tokens", "overlap_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an int, got {value!r}")
        if self.target_tokens <= 0:
            raise ValueError(f"target_tokens must be positive, got {self.target_tokens}")
        if not 0 <= self.overlap_tokens < self.target_tokens:
            raise ValueError(
                f"overlap_tokens must be >= 0 and < target_tokens "
                f"({self.target_tokens}), got {self.overlap_tokens}"
            )


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    start: int
    end: int
    text: str
    token_count: int
    # Headings enclosing the whole chunk, outermost first; () if none does.
    heading_path: tuple[str, ...]
    # Offsets of the innermost section enclosing the whole chunk — the "parent" a
    # retriever can expand a precise chunk into.
    section_start: int
    section_end: int
    # ("slide", 4) / ("page", 2) when `start` is in a "## Slide N" / "## Page N" section.
    locator: tuple[str, int] | None
    # The boundary level the chunk starts/ends on: one of LEVELS, or "start"/"end"
    # at the edges of the text.
    start_boundary: str
    end_boundary: str
    # Leading characters repeated from the previous chunk's tail.
    overlap_prev: int
    chunk_hash: str


@dataclass(frozen=True)
class ChunkResult:
    chunks: tuple[Chunk, ...]
    chunker_version: int
    settings: ChunkSettings
    counting: str


@dataclass(frozen=True)
class _Heading:
    start: int
    title_end: int  # end of the heading line, excluding its newline
    end: int  # end of this heading's section
    path: tuple[str, ...]
    locator: tuple[str, int] | None
    parent: int | None  # index of the enclosing heading


# (start, end, boundary level that precedes start)
_Piece = tuple[int, int, str]


def chunk_text(
    text: str,
    settings: ChunkSettings = ChunkSettings(),
    count_tokens: Callable[[str], int] = estimate_tokens,
    counting: str | None = None,
) -> ChunkResult:
    """Split `text` into ordered, overlapping chunks no larger than
    `settings.target_tokens` as measured by `count_tokens`.

    `count_tokens` must not decrease as text grows, and must count any single
    character within budget. `counting` labels it on the result (stored as
    provenance); it defaults to "chars/4" or the counter's function name, and is
    required when that name says nothing (a lambda or functools.partial).

    Deterministic: the output is a pure function of the arguments. Empty or
    whitespace-only text yields no chunks.
    """
    if counting is None:
        counting = "chars/4" if count_tokens is estimate_tokens else _name_of(count_tokens)
    if not counting or counting.startswith("<") or counting == "partial":
        raise ValueError(f"pass counting= to label the token counter (got {counting!r})")
    if not text.strip():
        return ChunkResult((), CHUNKER_VERSION, settings, counting)
    return ChunkResult(
        _Chunker(text, settings, count_tokens).run(), CHUNKER_VERSION, settings, counting
    )


def _name_of(fn: Callable) -> str:
    return getattr(fn, "__name__", type(fn).__name__)


class _Chunker:
    def __init__(self, text: str, settings: ChunkSettings, count_tokens: Callable[[str], int]):
        self.text = text
        self.settings = settings
        self.count_tokens = count_tokens
        self.headings = _scan_headings(text)
        self.heading_starts = [h.start for h in self.headings]
        self.quoted = _scan_quoted(text, self.heading_starts)
        self.quoted_opens = [q[0] for q in self.quoted]

    # ---- sizing -------------------------------------------------------------

    def _trim(self, start: int, end: int) -> tuple[int, int]:
        """Shrink [start, end) past leading/trailing whitespace without editing text."""
        while start < end and self.text[start].isspace():
            start += 1
        while end > start and self.text[end - 1].isspace():
            end -= 1
        return start, end

    def _tokens(self, start: int, end: int) -> int:
        start, end = self._trim(start, end)
        return self.count_tokens(self.text[start:end])

    def _fits(self, start: int, end: int) -> bool:
        return self._tokens(start, end) <= self.settings.target_tokens

    def _in_quote(self, pos: int) -> bool:
        """True if a unit starting at `pos` would begin inside a quoted span."""
        idx = bisect_right(self.quoted_opens, pos - 1) - 1
        return idx >= 0 and pos <= self.quoted[idx][1]

    # ---- pass 1: split into boundary-respecting pieces that each fit ---------

    def _split(self, start: int, end: int, level: int, label: str) -> list[_Piece]:
        if self._fits(start, end):
            return [(start, end, label)]
        if LEVELS[level] == "hard":
            return self._hard_split(start, end, label)
        cuts = self._cut_points(LEVELS[level], start, end)
        if not cuts:
            return self._split(start, end, level + 1, label)
        bounds = [start, *cuts, end]
        pieces: list[_Piece] = []
        for i in range(len(bounds) - 1):
            # A sub-unit holds no further cut of this level, so finer levels only.
            sub_label = label if i == 0 else LEVELS[level]
            pieces.extend(self._split(bounds[i], bounds[i + 1], level + 1, sub_label))
        return pieces

    def _cut_points(self, level: str, start: int, end: int) -> list[int]:
        """Offsets strictly inside (start, end) where a new unit of `level` begins."""
        text = self.text
        if level == "heading":
            positions = self.heading_starts
        elif level == "list":
            positions = [m.start() for m in _LIST_ITEM.finditer(text, start, end)]
        elif level == "paragraph":
            positions = [m.end() for m in _PARAGRAPH_BREAK.finditer(text, start, end)]
        elif level == "line":
            positions = [m.end() for m in _LINE_BREAK.finditer(text, start, end)]
        elif level == "sentence":
            positions = _sentence_ends(text, start, end)
        else:  # word
            positions = [m.end() for m in _WHITESPACE.finditer(text, start, end)]
        if level in ("list", "paragraph", "line"):
            # A quoted CSV cell (Sheets) may hold lines, blank lines, or bullets;
            # structural cuts must not go through it. Sentence/word cuts still may,
            # so an oversized cell can be split.
            positions = [p for p in positions if not self._in_quote(p)]
        return [p for p in positions if start < p < end]

    def _hard_split(self, start: int, end: int, label: str) -> list[_Piece]:
        """Character cuts for a span with no usable boundary (e.g. a long URL)."""
        pieces: list[_Piece] = []
        pos = start
        while pos < end:
            length = self._longest_fitting(pos, end)
            if not self._fits(pos, pos + length):
                raise ValueError(
                    f"count_tokens counts {self.text[pos]!r} over target_tokens "
                    f"({self.settings.target_tokens}); no chunk can fit it"
                )
            # Don't split a user-perceived character (see _joins_previous).
            while length > 1 and _joins_previous(self.text, pos, pos + length, end):
                length -= 1
            pieces.append((pos, pos + length, label if pos == start else "hard"))
            pos += length
        return pieces

    def _longest_fitting(self, pos: int, end: int) -> int:
        """Largest n >= 1 with text[pos:pos+n] within budget. Exponential then binary
        search, so a 1M-character blob costs O(log n) counts per piece, not O(n)."""
        limit = end - pos
        good, probe = 1, 2
        while probe <= limit and self._fits(pos, pos + probe):
            good, probe = probe, probe * 2
        bad = min(probe, limit + 1)
        while bad - good > 1:
            mid = (good + bad) // 2
            if self._fits(pos, pos + mid):
                good = mid
            else:
                bad = mid
        return good

    # ---- pass 2: pack pieces into chunks, with overlap -------------------------

    def run(self) -> tuple[Chunk, ...]:
        pieces = self._split(0, len(self.text), 0, "start")
        chunks: list[Chunk] = []
        prev: tuple[int, int] | None = None
        i = 0
        while i < len(pieces):
            overlap = None
            if prev is not None and pieces[i][2] != "heading":
                overlap = self._overlap_start(*prev, pieces)
                if overlap is not None and not self._fits(overlap[0], pieces[i][1]):
                    overlap = None  # the first piece always wins over overlap
            start = overlap[0] if overlap else pieces[i][0]
            start_boundary = overlap[1] if overlap else pieces[i][2]

            # Whole small sections may pack together, but a chunk that begins
            # mid-section ends at the next heading rather than absorb another section.
            packs_sections = start_boundary in ("start", "heading")
            j = i + 1
            while j < len(pieces) and self._fits(start, pieces[j][1]):
                if pieces[j][2] == "heading" and not packs_sections:
                    break
                j += 1
            if j < len(pieces) and pieces[j][2] != "heading":
                j = self._close_before_section(pieces, start, i, j)

            ts, te = self._trim(start, pieces[j - 1][1])
            # Whitespace-only new pieces would make a chunk of nothing but overlap.
            new_s, new_e = self._trim(pieces[i][0], pieces[j - 1][1])
            if new_s < new_e:
                chunks.append(
                    self._make_chunk(
                        ordinal=len(chunks),
                        start=ts,
                        end=te,
                        start_boundary=start_boundary,
                        end_boundary=pieces[j][2] if j < len(pieces) else "end",
                        overlap_prev=max(0, prev[1] - ts) if prev else 0,
                    )
                )
                prev = (ts, te)
            i = j
        return tuple(chunks)

    def _close_before_section(self, pieces: list[_Piece], start: int, i: int, j: int) -> int:
        """Pieces i..j-1 fit; pieces[j] doesn't and isn't a heading. Pick a better
        close: before the latest heading inside the chunk, so a packed chunk never
        ends partway into a section it absorbed; else at a coarser boundary than
        pieces[j]'s if one starts in the last stretch of the budget (latest wins ties)."""
        for k in range(j - 1, i, -1):
            if pieces[k][2] == "heading":
                return k
        best, best_rank = j, _RANK[pieces[j][2]]
        floor = _LOOKBACK_FRACTION * self.settings.target_tokens
        for k in range(i + 1, j):
            rank = _RANK[pieces[k][2]]
            if rank <= best_rank and rank < _RANK[pieces[j][2]]:
                if self._tokens(start, pieces[k][0]) >= floor:
                    best, best_rank = k, rank
        return best

    def _overlap_start(
        self, prev_start: int, prev_end: int, pieces: list[_Piece]
    ) -> tuple[int, str] | None:
        """Where the next chunk starts inside the previous one's tail: the earliest
        boundary whose tail fits the overlap budget, preferring whole trailing pieces,
        then sentence ends, then words. Never starts inside an open ASCII double quote
        (a quoted CSV cell) or a heading line, and never reaches back across a heading."""
        if self.settings.overlap_tokens == 0:
            return None
        text = self.text
        tiers = (
            [(p[0], p[2]) for p in pieces if prev_start < p[0] < prev_end],
            [(pos, "sentence") for pos in _sentence_ends(text, prev_start, prev_end)],
            [(m.end(), "word") for m in _WHITESPACE.finditer(text, prev_start, prev_end)],
        )
        found = None
        for tier in tiers:
            candidates = [
                (pos, label)
                for pos, label in tier
                if pos < prev_end and not self._in_quote(pos) and not self._in_heading_line(pos)
            ]
            # Tail size only shrinks as the start moves right, so binary search.
            lo, hi = 0, len(candidates)
            while lo < hi:
                mid = (lo + hi) // 2
                if self._tokens(candidates[mid][0], prev_end) <= self.settings.overlap_tokens:
                    hi = mid
                else:
                    lo = mid + 1
            if lo < len(candidates):
                found = candidates[lo]
                break
        if found is None:
            return None
        start, label = found
        for h in self.heading_starts:
            if prev_start < h < prev_end and h > start:
                start, label = h, "heading"
        return start, label

    def _make_chunk(self, *, ordinal: int, start: int, end: int, **boundaries) -> Chunk:
        at_start = self._heading_at(start)
        section = self._section_of(start, end)
        chunk = self.text[start:end]
        first_heading = self.heading_starts[0] if self.headings else len(self.text)
        if section is not None:
            section_start, section_end = section.start, section.end
        elif end <= first_heading:  # the preamble is its own section
            section_start, section_end = 0, first_heading
        else:  # spans top-level sections (or the preamble and one): the whole text
            section_start, section_end = 0, len(self.text)
        return Chunk(
            ordinal=ordinal,
            start=start,
            end=end,
            text=chunk,
            token_count=self.count_tokens(chunk),
            heading_path=section.path if section else (),
            section_start=section_start,
            section_end=section_end,
            locator=at_start.locator if at_start else None,
            chunk_hash=content_hash(chunk),
            **boundaries,
        )

    def _heading_at(self, pos: int) -> _Heading | None:
        """The innermost heading enclosing `pos`: the last one starting at or before
        it, since every section runs at least until the next heading."""
        idx = bisect_right(self.heading_starts, pos) - 1
        return self.headings[idx] if idx >= 0 else None

    def _section_of(self, start: int, end: int) -> _Heading | None:
        """The innermost heading whose section holds all of [start, end). A chunk
        that packed several small sections reports their common parent, not the
        first one, so parent expansion never returns less than the chunk."""
        heading = self._heading_at(start)
        while heading is not None and heading.end < end:
            heading = None if heading.parent is None else self.headings[heading.parent]
        return heading

    def _in_heading_line(self, pos: int) -> bool:
        """True if `pos` falls after the start of a heading line, within its title."""
        idx = bisect_right(self.heading_starts, pos - 1) - 1
        return idx >= 0 and pos <= self.headings[idx].title_end


def _scan_headings(text: str) -> list[_Heading]:
    """Markdown ATX headings with their section extents, paths, and locators.
    Lines inside fenced code blocks (e.g. a `# comment` in bash) are not headings."""
    fences = _scan_fences(text)
    fence_opens = [f[0] for f in fences]
    raw = []
    for m in _HEADING.finditer(text):
        idx = bisect_right(fence_opens, m.start()) - 1
        if idx >= 0 and m.start() < fences[idx][1]:
            continue
        title = _CLOSING_HASHES.sub("", m.group(2)).strip()
        if title:
            raw.append((m.start(), m.end(), len(m.group(1)), title))

    headings: list[_Heading] = []
    # (level, title, locator, index into headings)
    stack: list[tuple[int, str, tuple[str, int] | None, int]] = []
    for idx, (start, title_end, level, title) in enumerate(raw):
        end = len(text)
        for later_start, _, later_level, _ in raw[idx + 1 :]:
            if later_level <= level:
                end = later_start
                break
        while stack and stack[-1][0] >= level:
            stack.pop()
        match = _LOCATOR.match(title)
        own = (match.group(1).lower(), int(match.group(2))) if match else None
        # Inherit the nearest enclosing slide/page, so a "### Notes" under
        # "## Slide 4" still cites slide 4.
        locator = own or next((loc for _, _, loc, _ in reversed(stack) if loc), None)
        parent = stack[-1][3] if stack else None
        stack.append((level, title, locator, idx))
        headings.append(
            _Heading(
                start=start,
                title_end=title_end,
                end=end,
                path=tuple(t for _, t, _, _ in stack),
                locator=locator,
                parent=parent,
            )
        )
    return headings


def _scan_fences(text: str) -> list[tuple[int, int]]:
    """[open, close) spans of fenced code blocks. A fence closes at the next fence
    line of the same character and at least the same length. An unclosed fence is
    ignored rather than swallowing every heading after it."""
    spans = []
    opening = None  # (start, fence)
    for m in _FENCE.finditer(text):
        fence = m.group(1)
        if opening is None:
            opening = (m.start(), fence)
        elif fence[0] == opening[1][0] and len(fence) >= len(opening[1]):
            spans.append((opening[0], m.end()))
            opening = None
    return spans


def _scan_quoted(text: str, heading_starts: list[int]) -> list[tuple[int, int]]:
    """(open, close) offsets of ASCII double-quote pairs, matched left to right
    within each section. A quote left open at the next heading is treated as
    prose (e.g. `12" pipe`), so it cannot disable structural cuts beyond its
    section. A CSV `""` escape pairs off harmlessly: no line starts between them."""
    bounds = [*heading_starts, len(text)]
    spans = []
    open_at = None
    b = 0
    for m in re.finditer('"', text):
        pos = m.start()
        while pos >= bounds[b]:
            open_at = None  # unclosed at a section boundary: drop it
            b += 1
        if open_at is None:
            open_at = pos
        else:
            spans.append((open_at, pos))
            open_at = None
    return spans


def _joins_previous(text: str, start: int, i: int, end: int) -> bool:
    """True if cutting before text[i] would split a user-perceived character.
    `start` is a known character boundary at or before i (the current piece's
    start), bounding the look-back for regional-indicator pairs."""
    if i >= end:
        return False
    ch, prev = text[i], text[i - 1]
    if _is_regional_indicator(ch):
        # Flags are RI pairs: ch joins prev iff an odd number of RIs precede it.
        run = 0
        while i - run - 1 >= start and _is_regional_indicator(text[i - run - 1]):
            run += 1
        return run % 2 == 1
    return (
        unicodedata.category(ch).startswith("M")  # Mn/Mc/Me, incl. keycap U+20E3
        or ch in "\u0e33\u0eb3"  # Thai/Lao SARA AM: letters, but spacing marks in UAX #29
        or ch in "\u200d\ufe0e\ufe0f"  # ZWJ, text/emoji variation selectors
        or "\U0001f3fb" <= ch <= "\U0001f3ff"  # skin-tone modifiers
        or "\U000e0020" <= ch <= "\U000e007f"  # tag sequences (subdivision flags)
        or "\u1160" <= ch <= "\u11ff"  # Hangul medial vowel / final consonant jamo
        or "\ud7b0" <= ch <= "\ud7ff"
        or prev == "\u200d"
        or (prev == "\r" and ch == "\n")
    )


def _is_regional_indicator(ch: str) -> bool:
    return "\U0001f1e6" <= ch <= "\U0001f1ff"


# A "." after one of these (or after a single letter, an initial) usually isn't a
# sentence end. Kept short: a missed sentence end only falls through to words.
_ABBREVIATIONS = frozenset(
    "dr mr mrs ms prof st jr sr vs etc e.g i.e approx dept fig inc ltd "
    "jan feb mar apr jun jul aug sep sept oct nov dec".split()
)
_WORD_BEFORE = re.compile(r"([A-Za-z.]+)$")


def _sentence_ends(text: str, start: int, end: int) -> list[int]:
    """Offsets just past a sentence end in [start, end), skipping likely
    abbreviations and initials, and any end followed by a lowercase word."""
    ends = []
    for m in _SENTENCE_END.finditer(text, start, end):
        pos = m.end()
        if pos < len(text) and text[pos].islower():
            continue
        if m.group(0)[0] == ".":
            word = _WORD_BEFORE.search(text, max(0, m.start() - 12), m.start())
            if word and (len(word.group(1)) == 1 or word.group(1).lower() in _ABBREVIATIONS):
                continue
        ends.append(pos)
    return ends
