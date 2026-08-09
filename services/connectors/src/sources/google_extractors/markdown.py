"""Markdown rendering shared by the Google extractors.

Docs and Slides tables arrive in different API shapes — Docs cells nest full
structural elements, Slides cells hold a TextContent directly — but they render
identically. Traversal stays in each extractor; only the rendering is shared,
so a fix here fixes both.
"""


def _escape_cell(text: str) -> str:
    """A literal `|` in a cell is read as a column separator and splits the row.

    A literal newline is worse: it doesn't just split a column, it splits the
    row itself, destroying the table. Docs cells can never contain one (see
    docs.py's `_cell_text`, which joins paragraphs with " "), but a Slides
    shape's `_text_content` deliberately preserves interior newlines, so a
    multi-line Slides table cell reaches here. Replace with `<br>`, which
    renders in markdown and degrades to readable plain text otherwise.
    """
    return (text or "").replace("|", r"\|").replace("\n", "<br>")


def render_markdown_table(rows: list[list[str]]) -> list[str]:
    """Markdown table lines for `rows`; the first row is the header.

    Every row is padded to the widest row rather than to the header, so a body
    row longer than the header widens the table instead of losing cells.
    """
    if not rows:
        return []
    width = max(len(row) for row in rows)
    if width == 0:
        return []

    def _line(cells: list[str]) -> str:
        padded = [_escape_cell(c) for c in cells] + [""] * (width - len(cells))
        return "| " + " | ".join(padded) + " |"

    header, *body = rows
    separator = "| " + " | ".join("---" for _ in range(width)) + " |"
    return [_line(header), separator, *(_line(cells) for cells in body)]
