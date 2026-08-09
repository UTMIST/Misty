import io

import pytest
from pypdf import PdfWriter

from src.sources.base import SourceUnsupported
from src.sources.google_extractors.pdf import PdfExtractor

PDF_MIME_TYPE = "application/pdf"


class _FakeFiles:
    def __init__(self, payload):
        self._payload = payload

    def get(self, *, fileId, alt=None):
        return self

    def execute(self):
        return self._payload


class _FakeDrive:
    def __init__(self, payload):
        self._files = _FakeFiles(payload)

    def files(self):
        return self._files


def _extract(payload: bytes):
    return PdfExtractor().extract({"drive": _FakeDrive(payload)}, "file123", PDF_MIME_TYPE)


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
