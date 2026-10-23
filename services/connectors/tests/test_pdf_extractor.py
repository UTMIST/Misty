import io
import logging

import pytest
from drive_http import DriveHttp, drive_service
from pypdf import PdfWriter
from pypdf.generic import NumberObject, TextStringObject

from src.sources.base import SourceUnsupported
from src.sources.google_extractors.pdf import PdfExtractor

PDF_MIME_TYPE = "application/pdf"


@pytest.fixture
def application_logs():
    # pytest also attaches capture handlers directly to non-propagating
    # loggers. Observe the application's root output separately.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger()
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)


def _extract(payload: bytes):
    drive = drive_service(DriveHttp(payload, PDF_MIME_TYPE))
    return PdfExtractor().extract({"drive": drive}, "file123", PDF_MIME_TYPE)


def _blank_pdf(pages: int = 1, encrypt: str | None = None) -> bytes:
    """A syntactically valid PDF with `pages` blank pages, optionally encrypted."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if encrypt is not None:
        writer.encrypt(user_password=encrypt)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_each_page_gets_its_own_heading():
    result = _extract(_blank_pdf(pages=3))
    assert "## Page 1" in result.text
    assert "## Page 2" in result.text
    assert "## Page 3" in result.text


def test_pages_are_numbered_from_one_in_order():
    text = _extract(_blank_pdf(pages=2)).text
    assert text.index("## Page 1") < text.index("## Page 2")


def test_a_pdf_with_no_text_layer_warns_rather_than_failing():
    # Blank pages have no text layer — the same shape as a scanned document.
    result = _extract(_blank_pdf(pages=2))
    assert len(result.warnings) == 1
    assert "no extractable text layer" in result.warnings[0]
    assert "scanned" in result.warnings[0]


def test_an_encrypted_pdf_is_unsupported():
    with pytest.raises(SourceUnsupported):
        _extract(_blank_pdf(encrypt="hunter2"))


def test_corrupt_bytes_are_unsupported_not_unavailable():
    with pytest.raises(SourceUnsupported):
        _extract(b"this is definitely not a pdf")


def test_recoverable_pdf_does_not_log_document_content(caplog, capsys, application_logs):
    # pypdf recovers this malformed key but includes its value in a warning.
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.root_object[TextStringObject("PRIVATE_DOCUMENT_SENTINEL")] = NumberObject(1)
    buffer = io.BytesIO()
    writer.write(buffer)

    with caplog.at_level(logging.DEBUG):
        result = _extract(buffer.getvalue())
        logging.getLogger("connectors.audit").info("fetch completed")

    assert result.text == "## Page 1"
    assert "no extractable text layer" in result.warnings[0]
    assert "PRIVATE_DOCUMENT_SENTINEL" not in application_logs.getvalue() + capsys.readouterr().err
    assert "fetch completed" in caplog.text


def test_unreadable_pdf_does_not_log_document_content(caplog, capsys, application_logs):
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SourceUnsupported, match="pdf could not be parsed") as exc:
            _extract(b"PRIVATE_DOCUMENT_SENTINEL is not a PDF")

    assert "PRIVATE" not in str(exc.value)
    assert "PRIVA" not in application_logs.getvalue() + capsys.readouterr().err


@pytest.mark.parametrize("level", [logging.WARNING, logging.ERROR, logging.CRITICAL])
def test_pypdf_child_diagnostics_are_silent_even_with_an_explicit_level(
    caplog, capsys, application_logs, level
):
    # A parent logger's level/disabled flag does not block a child with its
    # own level; error diagnostics can contain document bytes too.
    with caplog.at_level(logging.DEBUG, logger="pypdf._page"):
        logging.getLogger("pypdf._page").log(level, "PRIVATE_DOCUMENT_SENTINEL")

    assert "PRIVATE_DOCUMENT_SENTINEL" not in application_logs.getvalue() + capsys.readouterr().err


def test_page_text_appears_under_its_own_heading(monkeypatch):
    # pypdf can create blank pages but not pages containing text, so the
    # page-text assembly is exercised with a stubbed reader. This test covers
    # OUR logic — numbering, heading placement, text under the right heading —
    # not pypdf's parsing, which the other tests exercise for real.
    class _StubPage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _StubReader:
        is_encrypted = False

        def __init__(self, _stream):
            self.pages = [_StubPage("first page words"), _StubPage("second page words")]

    monkeypatch.setattr("pypdf.PdfReader", _StubReader)
    text = _extract(_blank_pdf(pages=2)).text
    assert "## Page 1\nfirst page words" in text
    assert "## Page 2\nsecond page words" in text


def test_a_partially_scanned_pdf_does_not_warn(monkeypatch):
    # Some pages have text, some don't. The warning is for a document that
    # captured NOTHING; per-page warnings would be noise.
    class _StubPage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _StubReader:
        is_encrypted = False

        def __init__(self, _stream):
            self.pages = [_StubPage(""), _StubPage("this page has text")]

    monkeypatch.setattr("pypdf.PdfReader", _StubReader)
    result = _extract(_blank_pdf(pages=2))
    assert result.warnings == []
    assert "this page has text" in result.text


def test_extractor_declares_drive_scope_and_service():
    assert PdfExtractor().scopes == ("https://www.googleapis.com/auth/drive.readonly",)
    assert PdfExtractor().services == ("drive",)
