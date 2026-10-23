"""Exercise the real SDK request model against a fully offline HTTP transport."""

import io
from urllib.parse import parse_qs, urlsplit

import pytest
from docx import Document
from drive_http import DriveHttp, drive_service
from pypdf import PdfWriter

from src.sources.base import SourceForbidden, SourceNotFound, SourceUnavailable, SourceUnsupported
from src.sources.google import GOOGLE_DOC, GoogleSource
from src.sources.google_extractors.docx import DOCX_MIME, DocxExtractor
from src.sources.google_extractors.drive_export import DriveExportExtractor
from src.sources.google_extractors.pdf import PDF_MIME, PdfExtractor


def _pdf_bytes():
    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buffer)
    return buffer.getvalue()


def _docx_bytes():
    buffer = io.BytesIO()
    doc = Document()
    doc.add_paragraph("Uploaded document content")
    doc.save(buffer)
    return buffer.getvalue()


UPLOAD_CASES = [
    pytest.param(_pdf_bytes, PDF_MIME, PdfExtractor(), "## Page 1", id="pdf"),
    pytest.param(_docx_bytes, DOCX_MIME, DocxExtractor(), "Uploaded document content", id="docx"),
    pytest.param(
        lambda: b"Uploaded text content",
        "text/plain",
        DriveExportExtractor(export_mime=None),
        "Uploaded text content",
        id="text",
    ),
]
EXTRACTOR_CASES = UPLOAD_CASES + [
    pytest.param(
        lambda: b"Exported native content",
        GOOGLE_DOC,
        DriveExportExtractor(export_mime="text/plain"),
        "Exported native content",
        id="native-export",
    ),
]


@pytest.mark.parametrize("payload_factory,mime,extractor,expected", EXTRACTOR_CASES)
def test_extractors_download_bytes_through_the_real_sdk(payload_factory, mime, extractor, expected):
    http = DriveHttp(payload_factory(), mime)

    result = extractor.extract({"drive": drive_service(http)}, "file123", mime)

    assert expected in result.text
    assert len(http.requests) == 1
    request = urlsplit(http.requests[0])
    query = parse_qs(request.query)
    if mime == GOOGLE_DOC:
        assert request.path == "/drive/v3/files/file123/export"
        assert query["mimeType"] == ["text/plain"]
        assert "supportsAllDrives" not in query  # files.export has no such parameter
    else:
        assert request.path == "/drive/v3/files/file123"
        assert query["alt"] == ["media"]
        assert query["supportsAllDrives"] == ["true"]


@pytest.mark.parametrize("payload_factory,mime,extractor,expected", EXTRACTOR_CASES)
@pytest.mark.parametrize("payload", [{"private": "document metadata"}, "document text", None])
def test_non_bytes_payload_is_an_upstream_failure(
    payload_factory, mime, extractor, expected, payload
):
    http = DriveHttp(payload, mime)

    with pytest.raises(SourceUnavailable, match="non-bytes") as exc:
        extractor.extract({"drive": drive_service(http)}, "file123", mime)

    assert "document" not in str(exc.value)


@pytest.mark.parametrize("payload_factory,mime,extractor,expected", EXTRACTOR_CASES)
@pytest.mark.parametrize(
    "status,error_type",
    [(403, SourceForbidden), (404, SourceNotFound), (500, SourceUnavailable)],
)
def test_download_http_errors_keep_their_domain_classification(
    payload_factory, mime, extractor, expected, status, error_type
):
    http = DriveHttp(b'{"error": {"message": "upstream detail"}}', mime, status=status)

    with pytest.raises(error_type) as exc:
        extractor.extract({"drive": drive_service(http)}, "file123", mime)

    assert "upstream detail" not in str(exc.value)


@pytest.mark.parametrize("payload_factory,mime,extractor,expected", EXTRACTOR_CASES)
def test_download_transport_errors_remain_unavailable(payload_factory, mime, extractor, expected):
    http = DriveHttp(b"", mime, error=TimeoutError("private transport detail"))

    with pytest.raises(SourceUnavailable) as exc:
        extractor.extract({"drive": drive_service(http)}, "file123", mime)

    assert "private transport detail" not in str(exc.value)


@pytest.mark.parametrize(
    "mime,extractor", [(PDF_MIME, PdfExtractor()), (DOCX_MIME, DocxExtractor())]
)
def test_malformed_file_bytes_are_still_unsupported(mime, extractor):
    http = DriveHttp(b"not a valid document", mime)

    with pytest.raises(SourceUnsupported, match="could not be parsed"):
        extractor.extract({"drive": drive_service(http)}, "file123", mime)


@pytest.mark.parametrize("export_mime", [None, "text/plain"])
@pytest.mark.parametrize("payload,expected", [(b"", ""), (b"a\xffb", "a\ufffdb")])
def test_text_decoding_and_warnings_are_preserved(export_mime, payload, expected):
    http = DriveHttp(payload, "text/plain")
    extractor = DriveExportExtractor(export_mime=export_mime, warning="lossy export")

    result = extractor.extract({"drive": drive_service(http)}, "file123", "text/plain")

    assert result.text == expected
    assert result.warnings == ["lossy export"]


@pytest.mark.parametrize("payload_factory,mime,extractor,expected", UPLOAD_CASES)
def test_source_supports_shared_drives_and_reads_acl_before_media(
    payload_factory, mime, extractor, expected
):
    payload = payload_factory()
    http = DriveHttp(
        payload,
        mime,
        metadata={"name": "Shared file", "mimeType": mime, "size": str(len(payload))},
        permissions={
            "permissions": [
                {"id": "p1", "type": "group", "emailAddress": "team@example.com", "role": "reader"}
            ]
        },
    )
    source = GoogleSource(
        credentials_json_b64="",
        max_content_chars=1000,
        max_file_bytes=len(payload),
        services={"drive": drive_service(http)},
    )

    result = source.fetch("https://drive.google.com/file/d/file123/view")

    assert expected in result.content
    assert result.title == "Shared file"
    assert result.permissions[0].principal == "team@example.com"
    assert len(http.requests) == 3
    metadata, permissions, media = [urlsplit(uri) for uri in http.requests]
    assert parse_qs(metadata.query)["fields"] == ["name,mimeType,size"]
    assert permissions.path.endswith("/permissions")
    assert parse_qs(media.query)["alt"] == ["media"]
    for request in (metadata, permissions, media):
        assert parse_qs(request.query)["supportsAllDrives"] == ["true"]
