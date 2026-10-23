"""Native Google Docs extraction to markdown.

Drive's text/plain export flattens headings, tables, lists, and link targets
into undifferentiated lines. documents.get returns the document as structural
elements, so this rebuilds the structure the future RAG layer needs to chunk on
section boundaries and cite a section rather than a character offset.
"""

import json

from src.sources.google_extractors.base import ExtractedText, execute
from src.sources.google_extractors.markdown import render_markdown_table

DOCS_READONLY = "https://www.googleapis.com/auth/documents.readonly"

_HEADING_PREFIX = {
    "TITLE": "#",
    "SUBTITLE": "##",
    "HEADING_1": "#",
    "HEADING_2": "##",
    "HEADING_3": "###",
    "HEADING_4": "####",
    "HEADING_5": "#####",
    "HEADING_6": "######",
}


def _run_text(element: dict) -> str:
    """One text run, rendered as a markdown link when it carries one."""
    run = element.get("textRun")
    if not run:
        return ""  # inlineObjectElement, pageBreak, footnoteReference, etc.
    # content carries the paragraph's trailing newline; joining lines is the
    # caller's job, so strip it here or every line doubles up.
    text = (run.get("content") or "").replace("\n", "")
    url = (run.get("textStyle") or {}).get("link", {}).get("url")
    if url and text:
        return f"[{text}]({url})"
    return text


def _paragraph_line(paragraph: dict, heading_offset: int = 0) -> str:
    text = "".join(_run_text(e) for e in paragraph.get("elements") or []).strip()
    if not text:
        return ""
    style = (paragraph.get("paragraphStyle") or {}).get("namedStyleType", "")
    prefix = _HEADING_PREFIX.get(style)
    if prefix:
        prefix = "#" * min(len(prefix) + heading_offset, 6)
        return f"{prefix} {text}"
    if paragraph.get("bullet") is not None:
        return f"- {text}"
    return text


def _cell_text(cell: dict) -> str:
    """A table cell's text. Cells hold structural elements, so this recurses."""
    parts = [
        _paragraph_line(el["paragraph"]) for el in cell.get("content") or [] if "paragraph" in el
    ]
    return " ".join(p for p in parts if p).strip()


def _table_lines(table: dict) -> list[str]:
    """Docs cells hold structural elements, so cell text recurses; rendering is
    shared with the Slides extractor."""
    rows = table.get("tableRows") or []
    rendered = [[_cell_text(cell) for cell in row.get("tableCells") or []] for row in rows]
    return render_markdown_table(rendered)


def _render(elements: list, heading_offset: int = 0) -> list[str]:
    lines: list[str] = []
    for element in elements or []:
        if "paragraph" in element:
            line = _paragraph_line(element["paragraph"], heading_offset)
            if line:
                lines.append(line)
        elif "table" in element:
            lines.extend(_table_lines(element["table"]))
        # tableOfContents and sectionBreak carry no content worth extracting.
    return lines


def _tab_bodies(tabs: list, parent_titles: tuple[str, ...] = ()):
    """Walk top-level and child tabs in the order returned by Google."""
    for tab in tabs:
        title = ((tab.get("tabProperties") or {}).get("title") or "").strip() or "Untitled tab"
        titles = (*parent_titles, title)
        body = ((tab.get("documentTab") or {}).get("body") or {}).get("content") or []
        yield titles, body
        yield from _tab_bodies(tab.get("childTabs") or [], titles)


class DocsExtractor:
    scopes = (DOCS_READONLY,)
    services = ("docs",)

    def extract(self, services: dict, file_id: str, mime: str) -> ExtractedText:
        doc = execute(services["docs"].documents().get(documentId=file_id, includeTabsContent=True))
        tabs = list(_tab_bodies((doc or {}).get("tabs") or []))
        if tabs:
            # Preserve the existing text/hash for ordinary single-tab docs.
            labelled = len(tabs) > 1
            lines = []
            for titles, content in tabs:
                # Reserve H1 for the tab so document headings cannot close it.
                rendered = _render(content, heading_offset=1 if labelled else 0)
                if not rendered:
                    continue  # Children still carry the empty parent's title.
                if labelled:
                    path = " / ".join(json.dumps(title, ensure_ascii=False) for title in titles)
                    lines.append("# Tab: " + path)
                lines.extend(rendered)
            return ExtractedText(text="\n".join(lines))
        # Compatibility for legacy body-shaped responses/test fakes only. With
        # includeTabsContent=True the current API leaves body empty; this cannot
        # recover tabs omitted upstream, and an API error must still propagate.
        body = (doc or {}).get("body") or {}
        return ExtractedText(text="\n".join(_render(body.get("content") or [])))
