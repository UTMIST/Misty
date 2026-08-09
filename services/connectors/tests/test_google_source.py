import base64

import pytest

from src.sources.base import (
    SourceForbidden,
    SourceNotConfigured,
    SourceNotFound,
    SourceUnavailable,
    SourceUnsupported,
)
from src.sources.google import GoogleSource

DOC_URL = "https://docs.google.com/document/d/abc123/edit"
SHEET_URL = "https://docs.google.com/spreadsheets/d/abc123/edit"
SLIDES_URL = "https://docs.google.com/presentation/d/abc123/edit"
DRIVE_URL = "https://drive.google.com/file/d/abc123/view"


class _FakeRequest:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeFiles:
    def __init__(self, meta, payload=None, get_error=None, export_error=None):
        self._meta, self._payload = meta, payload
        self._get_error, self._export_error = get_error, export_error
        self.export_mime = None

    def get(self, *, fileId, fields=None, alt=None):
        if alt == "media":
            return _FakeRequest(result=self._payload, error=self._export_error)
        return _FakeRequest(result=self._meta, error=self._get_error)

    def export(self, *, fileId, mimeType):
        self.export_mime = mimeType
        return _FakeRequest(result=self._payload, error=self._export_error)


class _FakeService:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


class _FakeDocsService:
    def __init__(self, doc):
        self._doc = doc

    def documents(self):
        return self

    def get(self, *, documentId):
        return self

    def execute(self):
        return self._doc


class _FakeSlidesService:
    def __init__(self, deck):
        self._deck = deck

    def presentations(self):
        return self

    def get(self, *, presentationId):
        return self

    def execute(self):
        return self._deck


class _FakeSheetsService:
    def __init__(self, meta):
        self._meta = meta

    def spreadsheets(self):
        return self

    def get(self, *, spreadsheetId):
        return self

    def values(self):
        return self

    def batchGet(self, *, spreadsheetId, ranges):
        return self

    def execute(self):
        return self._meta


def _source(files, **kwargs):
    return GoogleSource(
        credentials_json_b64="fake",
        max_content_chars=1000,
        services={"drive": _FakeService(files)},
        **kwargs,
    )


def test_google_doc_routes_to_the_native_docs_extractor():
    files = _FakeFiles(
        meta={"name": "Sponsorship Deck", "mimeType": "application/vnd.google-apps.document"}
    )
    docs = _FakeDocsService({"title": "T", "body": {"content": []}})
    source = GoogleSource(
        credentials_json_b64="fake",
        max_content_chars=1000,
        services={"drive": _FakeService(files), "docs": docs},
    )
    result = source.fetch(DOC_URL)
    assert result.title == "Sponsorship Deck"
    assert files.export_mime is None, "Docs must not go through Drive export any more"


def test_slides_routes_to_the_native_slides_extractor():
    files = _FakeFiles(
        meta={"name": "Kickoff", "mimeType": "application/vnd.google-apps.presentation"}
    )
    slides = _FakeSlidesService({"slides": []})
    source = GoogleSource(
        credentials_json_b64="fake",
        max_content_chars=1000,
        services={"drive": _FakeService(files), "slides": slides},
    )
    result = source.fetch(SLIDES_URL)
    assert result.title == "Kickoff"
    assert files.export_mime is None, "Slides must not go through Drive export any more"


def test_sheets_routes_to_the_native_sheets_extractor():
    files = _FakeFiles(
        meta={"name": "Budget", "mimeType": "application/vnd.google-apps.spreadsheet"}
    )
    sheets = _FakeSheetsService({"sheets": []})
    source = GoogleSource(
        credentials_json_b64="fake",
        max_content_chars=1000,
        services={"drive": _FakeService(files), "sheets": sheets},
    )
    result = source.fetch(SHEET_URL)
    assert result.title == "Budget"
    assert files.export_mime is None, "Sheets must not go through Drive export any more"


def test_plain_text_upload_downloads_via_media():
    files = _FakeFiles(meta={"name": "notes.txt", "mimeType": "text/plain"}, payload=b"raw notes")
    result = _source(files).fetch(DRIVE_URL)
    assert result.content == "raw notes"


def test_binary_file_is_unsupported():
    files = _FakeFiles(meta={"name": "logo.png", "mimeType": "image/png"})
    with pytest.raises(SourceUnsupported):
        _source(files).fetch(DRIVE_URL)


def test_unrecognized_url_is_not_found():
    files = _FakeFiles(meta={})
    with pytest.raises(SourceNotFound):
        _source(files).fetch("https://example.com/whatever")


def test_missing_credentials_raise_not_configured():
    source = GoogleSource(credentials_json_b64="", max_content_chars=1000)
    with pytest.raises(SourceNotConfigured):
        source.fetch(DOC_URL)


def test_content_is_bounded_by_max_content_chars_on_the_media_path():
    # text/* uploads stay on the Drive-export fallback (_MEDIA_EXTRACTOR)
    # permanently, so that's the stable home for a generic truncation test.
    files = _FakeFiles(
        meta={"name": "Huge", "mimeType": "text/plain"},
        payload=b"x" * 5000,
    )
    source = GoogleSource(
        credentials_json_b64="fake",
        max_content_chars=100,
        services={"drive": _FakeService(files)},
    )
    result = source.fetch(DRIVE_URL)
    assert len(result.content) == 100


def test_registry_exposes_an_extractor_per_google_editor_type():
    from src.sources.google import EXTRACTORS

    # Exact set (not a subset check) so adding or removing an extractor
    # shows up as a visible diff here instead of silently going untracked.
    assert set(EXTRACTORS) == {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.presentation",
        "application/vnd.google-apps.spreadsheet",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.google-apps.form",
    }


def test_required_scopes_include_drive_readonly():
    from src.sources.google import required_scopes

    assert "https://www.googleapis.com/auth/drive.readonly" in required_scopes()


def test_required_services_include_docs_with_current_registry():
    from src.sources.google import required_services

    # GOOGLE_DOC, GOOGLE_SLIDES, GOOGLE_SHEET, and FORM_MIME route to native
    # extractors, which declare the "docs", "slides", "sheets", and "forms"
    # clients, so the registry's client set is drive + docs + forms + sheets
    # + slides.
    assert required_services() == ("docs", "drive", "forms", "sheets", "slides")


def test_required_scopes_now_include_docs():
    from src.sources.google import required_scopes

    assert "https://www.googleapis.com/auth/documents.readonly" in required_scopes()


def test_required_scopes_include_slides_and_sheets():
    from src.sources.google import required_scopes

    scopes = required_scopes()
    assert "https://www.googleapis.com/auth/presentations.readonly" in scopes
    assert "https://www.googleapis.com/auth/spreadsheets.readonly" in scopes


def test_required_services_now_include_docs():
    from src.sources.google import required_services

    # Registering DocsExtractor is the ONLY change; the docs client now gets
    # built because the extractor declares it, not because google.py was edited.
    assert "docs" in required_services()


def test_required_services_unions_in_a_new_extractor_declared_service():
    from src.sources import google

    class _FakeSlidesExtractor:
        scopes = ()
        services = ("slides",)

        def extract(self, services, file_id, mime):
            raise NotImplementedError

    original = dict(google.EXTRACTORS)
    google.EXTRACTORS["application/vnd.google-apps.presentation"] = _FakeSlidesExtractor()
    try:
        assert "slides" in google.required_services()
    finally:
        google.EXTRACTORS.clear()
        google.EXTRACTORS.update(original)


def test_required_scopes_include_forms():
    from src.sources.google import required_scopes

    assert "https://www.googleapis.com/auth/forms.body.readonly" in required_scopes()


def test_required_services_include_forms():
    from src.sources.google import required_services

    # Registering FormsExtractor is the ONLY change; the forms client gets
    # built because the extractor declares it.
    assert "forms" in required_services()


def test_credentials_are_built_once_but_transports_are_rebuilt_per_fetch():
    # Credentials (decode + JWT exchange) are the expensive, thread-safe part
    # and are memoized. The httplib2 transport underneath each discovery
    # client is NOT thread-safe (mutable per-host connection pool) and must
    # never be shared across concurrent /fetch calls, so it is rebuilt fresh
    # every time — this is what a counting fake for each half proves here.
    files = _FakeFiles(meta={"name": "Doc", "mimeType": "text/plain"}, payload=b"hi")
    source = GoogleSource(credentials_json_b64="fake", max_content_chars=1000)

    creds_calls = {"n": 0}

    def _fake_build_credentials():
        creds_calls["n"] += 1
        return object()

    built_service_dicts = []

    def _fake_build_services(credentials):
        services = {"drive": _FakeService(files)}
        built_service_dicts.append(services)
        return services

    source._build_credentials = _fake_build_credentials
    source._build_services = _fake_build_services

    source.fetch(DRIVE_URL)
    source.fetch(DRIVE_URL)

    assert creds_calls["n"] == 1, "credentials should be built once and memoized"
    assert len(built_service_dicts) == 2, "a distinct transport must be built per fetch"
    assert built_service_dicts[0] is not built_service_dicts[1]


def test_request_timeout_s_reaches_the_http_transport(monkeypatch):
    # Inject a fake httplib2.Http that records the timeout it was constructed
    # with, rather than making a real network/credentials call.
    seen_timeouts = []

    class _FakeHttp:
        def __init__(self, timeout=None):
            seen_timeouts.append(timeout)

    class _FakeAuthorizedHttp:
        def __init__(self, creds, http=None):
            pass

    class _FakeCreds:
        @classmethod
        def from_service_account_info(cls, info, scopes):
            return cls()

    import httplib2
    import google_auth_httplib2
    from google.oauth2 import service_account
    from googleapiclient import discovery

    monkeypatch.setattr(httplib2, "Http", _FakeHttp)
    monkeypatch.setattr(google_auth_httplib2, "AuthorizedHttp", _FakeAuthorizedHttp)
    monkeypatch.setattr(service_account, "Credentials", _FakeCreds)
    monkeypatch.setattr(discovery, "build", lambda *a, **k: object())

    fake_creds_b64 = base64.b64encode(b'{"type": "service_account"}').decode()
    source = GoogleSource(
        credentials_json_b64=fake_creds_b64,
        max_content_chars=1000,
        request_timeout_s=7.5,
    )
    credentials = source._get_credentials()
    source._build_services(credentials)

    assert seen_timeouts, "httplib2.Http was never constructed"
    assert all(t == 7.5 for t in seen_timeouts)


def _http_error(status):
    from googleapiclient.errors import HttpError

    class _Resp:
        def __init__(self, status):
            self.status = status
            self.reason = "fake"

    return HttpError(_Resp(status), b"{}")


@pytest.mark.parametrize(
    "status,expected",
    [(403, SourceForbidden), (404, SourceNotFound), (500, SourceUnavailable)],
)
def test_drive_http_errors_map_to_source_errors(status, expected):
    files = _FakeFiles(meta=None, get_error=_http_error(status))
    with pytest.raises(expected):
        _source(files).fetch(DOC_URL)
