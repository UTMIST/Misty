"""Bounded media reads through the real SDK and auth wrapper, without network."""

import httpx
import pytest
from drive_http import DriveHttp
from google.oauth2.credentials import Credentials

from src.sources.base import SourceForbidden, SourceNotFound, SourceUnavailable, SourceUnsupported
from src.sources.google import GoogleSource
from src.sources.google_extractors.base import execute_bytes
from src.sources.google_extractors.docx import DOCX_MIME
from src.sources.google_extractors.pdf import PDF_MIME
from src.sources.google_http import BoundedDriveHttp

URL = "https://drive.google.com/file/d/file123/view"


class TrackingStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.bytes_read = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.bytes_read += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


def _source(monkeypatch, handler, *, mime="text/plain", size=None, limit=8, credentials=None):
    metadata = {"name": "Uploaded file", "mimeType": mime}
    if size is not None:
        metadata["size"] = size
    # The old unbounded path would consume this whole payload via httplib2.
    http = DriveHttp(b"x" * 64, mime, metadata=metadata)
    monkeypatch.setattr("httplib2.Http", lambda **kwargs: http)
    real_client = httpx.Client
    monkeypatch.setattr(
        "httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    source = GoogleSource(
        credentials_json_b64="fake",
        max_content_chars=1000,
        max_file_bytes=limit,
        request_timeout_s=7.5,
    )
    monkeypatch.setattr(
        source, "_get_credentials", lambda: credentials or Credentials("test-token")
    )
    return source, http


@pytest.mark.parametrize("mime", [PDF_MIME, DOCX_MIME, "text/plain"])
@pytest.mark.parametrize("size", [None, "1", "8"])
def test_missing_or_stale_metadata_cannot_bypass_media_limit(monkeypatch, mime, size):
    stream = TrackingStream([b"12345678", b"9", b"must not be read"])

    def handler(request):
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.headers["accept-encoding"] == "identity"
        assert request.extensions["timeout"]["read"] == 7.5
        assert request.url.params["alt"] == "media"
        assert request.url.params["supportsAllDrives"] == "true"
        return httpx.Response(200, stream=stream)

    source, http = _source(monkeypatch, handler, mime=mime, size=size)
    with pytest.raises(SourceUnsupported, match="MAX_FILE_BYTES"):
        source.fetch(URL)

    assert stream.bytes_read == 9
    assert stream.closed
    assert len(http.requests) == 2  # metadata and complete ACL, then bounded media


@pytest.mark.parametrize("payload", [b"", b"1234567", b"12345678"])
def test_downloads_up_to_the_limit_are_accepted(monkeypatch, payload):
    stream = TrackingStream([payload])
    source, _ = _source(monkeypatch, lambda request: httpx.Response(200, stream=stream))

    assert source.fetch(URL).content == payload.decode()
    assert stream.closed


def test_oversized_content_length_rejects_without_reading_the_body(monkeypatch):
    stream = TrackingStream([b"must not be read"])
    source, _ = _source(
        monkeypatch,
        lambda request: httpx.Response(200, headers={"Content-Length": "100"}, stream=stream),
    )

    with pytest.raises(SourceUnsupported, match="MAX_FILE_BYTES"):
        source.fetch(URL)
    assert stream.bytes_read == 0
    assert stream.closed


def test_oversized_metadata_still_rejects_before_the_media_request(monkeypatch):
    def handler(request):
        pytest.fail("oversized metadata must reject before a media request")

    source, _ = _source(monkeypatch, handler, size="9")
    with pytest.raises(SourceUnsupported, match="MAX_FILE_BYTES"):
        source.fetch(URL)


def test_ignored_range_and_understated_content_length_cannot_bypass_limit(monkeypatch):
    stream = TrackingStream([b"x" * 64, b"must not be read"])

    def handler(request):
        assert request.headers["range"] == "bytes=0-7"
        return httpx.Response(200, headers={"Content-Length": "1"}, stream=stream)

    source, _ = _source(monkeypatch, handler)
    drive = source._build_services(source._get_credentials())["drive"]
    request = drive.files().get_media(fileId="file123", supportsAllDrives=True)
    request.headers["Range"] = "bytes=0-7"

    with pytest.raises(SourceUnsupported, match="MAX_FILE_BYTES"):
        execute_bytes(request)
    assert stream.bytes_read == 64
    assert stream.closed


@pytest.mark.parametrize(
    "status,error_type",
    [
        (403, SourceForbidden),
        (404, SourceNotFound),
        (429, SourceUnavailable),
        (500, SourceUnavailable),
    ],
)
def test_http_errors_keep_their_classification_without_reading_bodies(
    monkeypatch, status, error_type
):
    stream = TrackingStream([b"PRIVATE upstream detail"])
    source, _ = _source(monkeypatch, lambda request: httpx.Response(status, stream=stream))

    with pytest.raises(error_type) as exc:
        source.fetch(URL)
    assert "PRIVATE" not in str(exc.value)
    assert stream.bytes_read == 0
    assert stream.closed


@pytest.mark.parametrize("encoding", ["gzip", "br"])
def test_encoded_responses_are_rejected_before_decompression(monkeypatch, encoding):
    stream = TrackingStream([b"compressed data"])
    source, _ = _source(
        monkeypatch,
        lambda request: httpx.Response(200, headers={"Content-Encoding": encoding}, stream=stream),
    )

    with pytest.raises(SourceUnavailable, match="encoded"):
        source.fetch(URL)
    assert stream.bytes_read == 0
    assert stream.closed


def test_partial_response_is_not_silently_indexed(monkeypatch):
    stream = TrackingStream([b"part"])
    source, _ = _source(monkeypatch, lambda request: httpx.Response(206, stream=stream))

    with pytest.raises(SourceUnavailable, match="incomplete"):
        source.fetch(URL)
    assert stream.bytes_read == 0
    assert stream.closed


def test_read_failure_closes_the_response_and_is_unavailable(monkeypatch):
    class FailingStream(TrackingStream):
        def __iter__(self):
            yield b"part"
            raise httpx.ReadError("PRIVATE transport detail")

    stream = FailingStream([])
    source, _ = _source(monkeypatch, lambda request: httpx.Response(200, stream=stream))

    with pytest.raises(SourceUnavailable) as exc:
        source.fetch(URL)
    assert "PRIVATE" not in str(exc.value)
    assert stream.closed


@pytest.mark.parametrize("cross_origin", [False, True])
def test_redirect_bodies_are_not_read_and_cross_origin_auth_is_stripped(monkeypatch, cross_origin):
    redirect = TrackingStream([b"unbounded redirect body must not be read"])
    media = TrackingStream([b"content"])
    target = (
        "https://download.example.test/file" if cross_origin else "https://www.googleapis.com/media"
    )
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": target}, stream=redirect)
        assert str(request.url) == target
        assert ("authorization" not in request.headers) == cross_origin
        return httpx.Response(200, stream=media)

    source, _ = _source(monkeypatch, handler)
    assert source.fetch(URL).content == "content"
    assert len(calls) == 2
    assert redirect.bytes_read == 0
    assert redirect.closed and media.closed


def test_oversized_redirect_target_is_also_bounded(monkeypatch):
    stream = TrackingStream([b"123456789", b"must not be read"])

    def handler(request):
        if request.url.path != "/media":
            return httpx.Response(302, headers={"Location": "/media"})
        return httpx.Response(200, stream=stream)

    source, _ = _source(monkeypatch, handler)
    with pytest.raises(SourceUnsupported, match="MAX_FILE_BYTES"):
        source.fetch(URL)
    assert stream.bytes_read == 9
    assert stream.closed


def test_redirect_loop_is_bounded_and_all_responses_are_closed(monkeypatch):
    streams = []

    def handler(request):
        stream = TrackingStream([b"must not be read"])
        streams.append(stream)
        return httpx.Response(302, headers={"Location": str(request.url)}, stream=stream)

    source, _ = _source(monkeypatch, handler)
    with pytest.raises(SourceUnavailable, match="redirect limit"):
        source.fetch(URL)
    assert len(streams) == 6  # httplib2 allows five redirects
    assert all(stream.closed and stream.bytes_read == 0 for stream in streams)


def test_redirect_to_plain_http_is_rejected(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://example.test/file"})

    source, _ = _source(monkeypatch, handler)
    with pytest.raises(SourceUnavailable, match="insecure"):
        source.fetch(URL)
    assert len(calls) == 1


def test_unauthorized_response_still_refreshes_credentials(monkeypatch):
    class RefreshingCredentials(Credentials):
        def refresh(self, request):
            self.token = "refreshed-token"

    tokens = []
    streams = []

    def handler(request):
        token = request.headers["authorization"]
        tokens.append(token)
        stream = TrackingStream([b"content"])
        streams.append(stream)
        return httpx.Response(200 if token == "Bearer refreshed-token" else 401, stream=stream)

    source, _ = _source(monkeypatch, handler, credentials=RefreshingCredentials("expired-token"))
    assert source.fetch(URL).content == "content"
    assert tokens == ["Bearer expired-token", "Bearer refreshed-token"]
    assert streams[0].bytes_read == 0
    assert all(stream.closed for stream in streams)


def test_native_exports_and_transport_close_are_delegated():
    class SdkHttp:
        closed = False

        def request(self, uri, **kwargs):
            assert uri.endswith("/export?alt=media")
            return "response", b"native export larger than media limit"

        def close(self):
            self.closed = True

    sdk_http = SdkHttp()
    transport = BoundedDriveHttp(sdk_http, max_file_bytes=1, timeout=7.5)
    result, data = transport.request(
        "https://www.googleapis.com/drive/v3/files/file123/export?alt=media"
    )
    assert result == "response"
    assert data == b"native export larger than media limit"
    transport.close()
    assert sdk_http.closed
