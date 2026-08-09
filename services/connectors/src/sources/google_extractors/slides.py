"""Native Google Slides extraction to markdown.

Drive's text/plain export concatenates every slide into one blob and drops
speaker notes entirely — usually where a deck's real explanation lives, since
the slide says "Q3 Roadmap" and the notes say what the roadmap is.
presentations.get returns slides, their placeholder roles, and their notes
pages in a single call.
"""

from src.sources.google_extractors.base import ExtractedText, execute
from src.sources.google_extractors.markdown import render_markdown_table

SLIDES_READONLY = "https://www.googleapis.com/auth/presentations.readonly"

_TITLE_PLACEHOLDERS = frozenset({"TITLE", "CENTERED_TITLE"})

# Placeholder types that are obviously not a slide title. Excluded only from
# the fallback pass — a slide whose only text shapes are e.g. a slide number
# and a footer has no title candidate at all, and should fall through to a
# bare "## Slide N" rather than promoting one of these.
_NON_TITLE_PLACEHOLDERS = frozenset({"SLIDE_NUMBER", "FOOTER", "SLIDE_IMAGE"})


def _text_content(holder: dict) -> str:
    """All text in a shape or table cell, edges trimmed.

    Newline handling is deliberately the OPPOSITE of the Docs extractor. There,
    each paragraph is its own structural element, so runs are stripped and the
    caller joins lines. Here a single shape holds every paragraph of a bullet
    list in one blob with the breaks inside textRun.content — stripping them
    would collapse a five-bullet slide into one run-on line. So interior
    newlines are preserved and only the edges trimmed.
    """
    elements = ((holder or {}).get("text") or {}).get("textElements") or []
    return "".join((e.get("textRun") or {}).get("content") or "" for e in elements).strip()


def _placeholder_type(shape: dict) -> str:
    return ((shape or {}).get("placeholder") or {}).get("type") or ""


def _table_rows(table: dict) -> list[list[str]]:
    """Slides cells hold a TextContent directly — no recursion, unlike Docs.

    A merged cell appears once at its top-left position and the positions it
    covers are absent, so rows can be ragged; render_markdown_table pads them.
    """
    return [
        [_text_content(cell) for cell in row.get("tableCells") or []]
        for row in table.get("tableRows") or []
    ]


def _notes_text(slide: dict) -> str:
    """Speaker notes, found by object id rather than by position."""
    notes_page = (slide.get("slideProperties") or {}).get("notesPage") or {}
    notes_id = (notes_page.get("notesProperties") or {}).get("speakerNotesObjectId")
    if not notes_id:
        return ""
    for element in notes_page.get("pageElements") or []:
        if element.get("objectId") == notes_id:
            return _text_content(element.get("shape") or {})
    return ""


def _title_element(elements: list) -> dict | None:
    """The page element supplying the slide's title, or None.

    Real decks are often built by dropping text boxes onto a slide rather than
    using the layout's title field, so `shape.placeholder` may be absent on
    every slide. Primary path: any TITLE/CENTERED_TITLE placeholder anywhere
    on the slide, regardless of its position in pageElements. Fallback, only
    when no placeholder exists: the first non-empty, single-line text shape in
    `pageElements` order — never a table, and never a shape whose placeholder
    type is in `_NON_TITLE_PLACEHOLDERS`. `pageElements` order is creation/
    z-order, not guaranteed reading order, but it's the best ordering signal
    the API gives.

    A multi-line shape is never promoted, even if it's the only candidate: a
    five-bullet body box becoming "## Slide N: <first bullet>" would make the
    heading a lie and scatter the remaining bullets as unattributed body
    lines. Matched by element identity (not objectId) so the chosen element
    can be excluded from the body without relying on ids being unique.
    """
    for element in elements:
        shape = element.get("shape")
        if shape and _placeholder_type(shape) in _TITLE_PLACEHOLDERS and _text_content(shape):
            return element
    for element in elements:
        shape = element.get("shape")
        if not shape or _placeholder_type(shape) in _NON_TITLE_PLACEHOLDERS:
            continue
        text = _text_content(shape)
        if text and "\n" not in text:
            return element
    return None


def _slide_lines(slide: dict, number: int) -> list[str]:
    elements = slide.get("pageElements") or []
    title_element = _title_element(elements)
    title = _text_content(title_element["shape"]) if title_element is not None else ""

    body: list[str] = []
    for element in elements:
        if element is title_element:
            continue
        if "table" in element:
            body.extend(render_markdown_table(_table_rows(element["table"])))
            continue
        shape = element.get("shape")
        if not shape:
            continue  # image, video, line, sheetsChart, speakerSpotlight
        text = _text_content(shape)
        if not text:
            continue
        body.append(text)

    lines = [f"## Slide {number}: {title}" if title else f"## Slide {number}", *body]
    notes = _notes_text(slide)
    if notes:
        lines.append(f"**Speaker notes:** {notes}")
    return lines


class SlidesExtractor:
    scopes = (SLIDES_READONLY,)
    services = ("slides",)

    def extract(self, services: dict, file_id: str, mime: str) -> ExtractedText:
        deck = execute(services["slides"].presentations().get(presentationId=file_id))
        lines: list[str] = []
        for number, slide in enumerate((deck or {}).get("slides") or [], start=1):
            lines.extend(_slide_lines(slide, number))
        return ExtractedText(text="\n".join(lines))
