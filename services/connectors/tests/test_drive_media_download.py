"""Exercise the real SDK request model against a fully offline HTTP transport."""

import io
import json
from urllib.parse import parse_qs, urlsplit

import httplib2
import pytest
from docx import Document
from googleapiclient.discovery import build
from pypdf import PdfWriter

from src.sources.google_extractors.docx import DOCX_MIME, DocxExtractor
from src.sources.google_extractors.drive_export import DriveExportExtractor
from src.sources.google_extractors.pdf import PDF_MIME, PdfExtractor


class _DriveHttp:
    def __init__(self, payload, mime):
        self.payload = payload
        self.mime = mime
        self.requests = []

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        assert method == "GET"
        assert urlsplit(uri).path == "/drive/v3/files/file123"
        self.requests.append(uri)
        if parse_qs(urlsplit(uri).query).get("alt") == ["media"]:
            return httplib2.Response({"status": "200", "content-type": self.mime}), self.payload
        # files.get(alt="media") uses the SDK's JSON model, which overwrites
        # alt with json. A fake files.get returning bytes conceals this bug.
        return (
            httplib2.Response({"status": "200", "content-type": "application/json"}),
            json.dumps({"id": "file123", "mimeType": self.mime}).encode(),
        )


@pytest.mark.parametrize("kind", ["pdf", "docx", "text"])
def test_uploaded_file_extractors_download_bytes_through_the_real_sdk(kind):
    buffer = io.BytesIO()
    if kind == "pdf":
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(buffer)
        payload, mime, extractor, expected = (
            buffer.getvalue(),
            PDF_MIME,
            PdfExtractor(),
            "## Page 1",
        )
    elif kind == "docx":
        doc = Document()
        doc.add_paragraph("Uploaded document content")
        doc.save(buffer)
        payload, mime, extractor, expected = (
            buffer.getvalue(),
            DOCX_MIME,
            DocxExtractor(),
            "Uploaded document content",
        )
    else:
        payload, mime, extractor, expected = (
            b"Uploaded text content",
            "text/plain",
            DriveExportExtractor(export_mime=None),
            "Uploaded text content",
        )
    http = _DriveHttp(payload, mime)
    # Static discovery uses the SDK's bundled schema, never a network lookup.
    drive = build("drive", "v3", http=http, static_discovery=True, cache_discovery=False)

    result = extractor.extract({"drive": drive}, "file123", mime)

    assert expected in result.text
    assert len(http.requests) == 1
    assert parse_qs(urlsplit(http.requests[0]).query)["alt"] == ["media"]
