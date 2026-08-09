"""Native Google Sheets extraction.

Drive's CSV export returns only the first tab. spreadsheets.get lists every
tab and a single values.batchGet fetches them all.
"""

import csv
import io

from src.sources.google_extractors.base import ExtractedText, execute

SHEETS_READONLY = "https://www.googleapis.com/auth/spreadsheets.readonly"

# Per-tab row cap. A module constant rather than config — make it tunable when
# something actually needs tuning.
MAX_ROWS_PER_TAB = 2000


def _a1_quote(title: str) -> str:
    """Quote a tab title for use in an A1-notation range.

    Unquoted multi-word titles are NOT the problem — Google normalizes bare
    spaces fine (confirmed against a live spreadsheet: "Form Responses 1"
    round-trips identically quoted or not). The real ambiguity is narrower:
    an apostrophe in the title would terminate a quoted string early (so it
    must be doubled), and a title that looks like a cell reference (e.g.
    "Q1", "A1") or contains "!" (the sheet/range separator) is ambiguous
    unquoted. Google's own guidance is that it is always safe to quote, and
    quoting is a no-op for titles that already work unquoted, so every title
    is quoted unconditionally rather than special-casing which ones need it.
    """
    return "'" + title.replace("'", "''") + "'"


def _csv_lines(rows: list[list]) -> list[str]:
    """Rows as CSV lines.

    csv.writer rather than ",".join: a cell containing a comma or a newline
    would otherwise silently corrupt the row, and "$1,200" is exactly what a
    budget sheet is full of.
    """
    if not rows:
        return []
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in rows:
        writer.writerow(["" if cell is None else str(cell) for cell in row])
    return buffer.getvalue().rstrip("\n").split("\n")


class SheetsExtractor:
    scopes = (SHEETS_READONLY,)
    services = ("sheets",)

    def extract(self, services: dict, file_id: str, mime: str) -> ExtractedText:
        spreadsheets = services["sheets"].spreadsheets()

        # No includeGridData: tab names only. gridProperties.rowCount is the
        # ALLOCATED grid (1000 by default even for a five-row sheet), so it is
        # deliberately not used to pre-filter — that would skip tabs holding
        # almost nothing.
        meta = execute(spreadsheets.get(spreadsheetId=file_id))
        properties = [(s.get("properties") or {}) for s in (meta or {}).get("sheets") or []]
        titles = [
            p["title"]
            for p in sorted(properties, key=lambda p: p.get("index", 0))
            if p.get("title")
        ]
        if not titles:
            return ExtractedText(text="")

        # One request for every tab. valueRenderOption stays at its default
        # FORMATTED_VALUE — what a human sees, not raw floats or formula source.
        # Ranges are quoted (see _a1_quote); the raw, unquoted title is still
        # what gets used for the "## <title>" heading below.
        ranges = [_a1_quote(title) for title in titles]
        response = execute(spreadsheets.values().batchGet(spreadsheetId=file_id, ranges=ranges))
        value_ranges = (response or {}).get("valueRanges") or []

        lines: list[str] = []
        warnings: list[str] = []
        if len(value_ranges) != len(titles):
            warnings.append(
                f"batchGet returned {len(value_ranges)} valueRanges for {len(titles)} tabs; "
                "some tabs may be missing from the output"
            )
        for title, value_range in zip(titles, value_ranges):
            rows = (value_range or {}).get("values") or []
            if not rows:
                continue  # an empty tab is not lost information — no warning
            if len(rows) > MAX_ROWS_PER_TAB:
                warnings.append(
                    f"sheet {title!r} truncated to {MAX_ROWS_PER_TAB} of {len(rows)} rows"
                )
                rows = rows[:MAX_ROWS_PER_TAB]
            lines.append(f"## {title}")
            lines.extend(_csv_lines(rows))

        return ExtractedText(text="\n".join(lines), warnings=warnings)
