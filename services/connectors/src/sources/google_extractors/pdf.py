"""Uploaded-PDF extraction.

Downloads the file's bytes through the same Drive media path used for text/*
uploads and parses locally — so this needs no Google scope beyond Drive and no
new API client. pypdf rather than pymupdf: pymupdf is AGPL.
"""

import io

import pypdf

from src.sources.base import SourceUnsupported
from src.sources.google_extractors.base import ExtractedText, execute
from src.sources.google_extractors.drive_export import DRIVE_READONLY

PDF_MIME = "application/pdf"

NO_TEXT_LAYER_WARNING = (
    "pdf has no extractable text layer (likely a scanned document); no content was captured"
)


class PdfExtractor:
    scopes = (DRIVE_READONLY,)
    services = ("drive",)

    def extract(self, services: dict, file_id: str, mime: str) -> ExtractedText:
        payload = execute(services["drive"].files().get(fileId=file_id, alt="media"))

        # Parsing failures are properties of the FILE, not of the upstream
        # call, so they map to SourceUnsupported (422) rather than
        # SourceUnavailable (502). Kept out of execute(), which exists to
        # normalize Google API failures.
        try:
            reader = pypdf.PdfReader(io.BytesIO(payload))
            if reader.is_encrypted:
                raise SourceUnsupported("pdf is encrypted and cannot be read")
            pages = [(page.extract_text() or "").strip() for page in reader.pages]
        except SourceUnsupported:
            raise  # our own signal — must not be swallowed by the handler below
        except Exception as e:
            # Deliberately broad: pypdf raises a wide range of types on
            # malformed input (PdfReadError, but also KeyError, ValueError,
            # struct.error from deep in the parser). Every one of them means
            # the same thing here — this file cannot be read.
            raise SourceUnsupported(f"pdf could not be parsed: {type(e).__name__}") from e

        lines: list[str] = []
        for number, page_text in enumerate(pages, start=1):
            lines.append(f"## Page {number}")
            if page_text:
                lines.append(page_text)

        # Warn once for the whole document rather than per page: a partially
        # scanned file would otherwise produce a wall of warnings, and the
        # actionable case is a document that captured nothing at all.
        warnings = [] if any(pages) else [NO_TEXT_LAYER_WARNING]
        return ExtractedText(text="\n".join(lines), warnings=warnings)
