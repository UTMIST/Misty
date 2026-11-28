"""httplib2-compatible transport with bounded reads for Drive file media."""

from urllib.parse import parse_qs, urlsplit

import httplib2
import httpx

from src.sources.base import SourceUnavailable, SourceUnsupported


class BoundedDriveHttp:
    def __init__(self, http, *, max_file_bytes: int, timeout: float):
        self._http = http
        self._max_file_bytes = max_file_bytes
        self.timeout = timeout

    def close(self):
        self._http.close()

    def request(
        self,
        uri,
        method="GET",
        body=None,
        headers=None,
        redirections=httplib2.DEFAULT_MAX_REDIRECTS,
        connection_type=None,
        **kwargs,
    ):
        url = urlsplit(uri)
        if (
            method != "GET"
            or parse_qs(url.query).get("alt") != ["media"]
            or url.path.endswith("/export")
        ):
            # Metadata, native exports, and credential refresh retain the SDK
            # transport. Only uploaded file bytes are subject to this limit.
            return self._http.request(
                uri,
                method=method,
                body=body,
                headers=headers,
                redirections=redirections,
                connection_type=connection_type,
                **kwargs,
            )

        # AuthorizedHttp already attached credentials. HTTPX strips them on
        # cross-origin redirects. Ask for identity and read raw bytes so a
        # compressed response cannot expand before we check the byte count.
        media_headers = httpx.Headers(headers)
        media_headers["Accept-Encoding"] = "identity"
        with httpx.Client(timeout=self.timeout) as client:
            request = client.build_request(method, uri, headers=media_headers)
            for _ in range(redirections + 1):
                if request.url.scheme != "https":
                    raise SourceUnavailable("google media redirected to an insecure URL")
                response = client.send(request, stream=True, follow_redirects=False)
                try:
                    if response.has_redirect_location:
                        # Automatic redirect following reads the redirect body
                        # in full. Close it unread and follow next_request.
                        request = response.next_request
                        continue
                    result = httplib2.Response(
                        {**response.headers, "status": str(response.status_code)}
                    )
                    if response.status_code >= 300:
                        # Error classification needs only the status, and 401
                        # must reach AuthorizedHttp's credential-refresh path.
                        return result, b""
                    if response.status_code != 200:
                        raise SourceUnavailable("google returned an incomplete media response")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise SourceUnavailable("google returned encoded file media")
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > self._max_file_bytes:
                        self._too_large()
                    data = bytearray()
                    # The default HTTPX transport reads bounded network chunks.
                    # Check each one BEFORE retaining it; neither metadata nor
                    # a server honoring Range/Content-Length is needed for the cap.
                    for chunk in response.iter_raw():
                        if len(data) + len(chunk) > self._max_file_bytes:
                            self._too_large()
                        data.extend(chunk)
                    return result, bytes(data)
                finally:
                    response.close()
        raise SourceUnavailable("google media redirect limit exceeded")

    def _too_large(self):
        raise SourceUnsupported(
            f"file exceeds MAX_FILE_BYTES limit of {self._max_file_bytes} bytes"
        )


class GmailRawTooLarge(Exception):
    """Raised before buffering when a Gmail format=raw response exceeds MAX_EMAIL_BYTES."""


class GmailHttp:
    """No-retry, bounded-response httplib2-compatible transport for Gmail API calls.

    httplib2.Http._conn_request retries even POST requests when a response is
    lost (BadStatusLine/ResponseNotReady), which can send a duplicate receipt.
    This transport uses httpx with retries=0 and streams every response with a
    hard byte ceiling so raw email fetches cannot buffer unbounded bodies.
    """

    _DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
    _RAW_ENVELOPE_OVERHEAD_BYTES = 65_536

    def __init__(
        self,
        *,
        max_email_bytes: int,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout = timeout
        self._max_raw_response_bytes = (
            (max_email_bytes + 2) // 3
        ) * 4 + self._RAW_ENVELOPE_OVERHEAD_BYTES
        self._max_default_response_bytes = max(
            self._DEFAULT_MAX_RESPONSE_BYTES, self._max_raw_response_bytes
        )
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport if transport is not None else httpx.HTTPTransport(retries=0),
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        uri,
        method="GET",
        body=None,
        headers=None,
        redirections=httplib2.DEFAULT_MAX_REDIRECTS,
        connection_type=None,
        **kwargs,
    ):
        method_upper = str(method or "GET").upper()
        url = urlsplit(uri)
        is_raw_fetch = method_upper == "GET" and "raw" in parse_qs(url.query).get("format", [])
        byte_limit = (
            self._max_raw_response_bytes if is_raw_fetch else self._max_default_response_bytes
        )

        req_headers = httpx.Headers({k: v for k, v in (headers or {}).items() if v is not None})
        if is_raw_fetch:
            req_headers["Accept-Encoding"] = "identity"

        content = body.encode("utf-8") if isinstance(body, str) else body
        max_hops = (redirections + 1) if method_upper == "GET" else 1
        request = self._client.build_request(
            method_upper, uri, headers=req_headers, content=content
        )

        for _ in range(max_hops):
            if request.url.scheme != "https":
                raise httpx.UnsupportedProtocol("gmail transport requires https")
            response = self._client.send(request, stream=True, follow_redirects=False)
            try:
                if method_upper == "GET" and response.has_redirect_location:
                    request = response.next_request
                    continue

                resp_headers = {
                    k: v for k, v in response.headers.items() if k.lower() != "content-encoding"
                }
                resp_headers["status"] = str(response.status_code)
                result = httplib2.Response(resp_headers)

                if is_raw_fetch and 200 <= response.status_code < 300:
                    encoding = response.headers.get("content-encoding", "identity").lower()
                    length = response.headers.get("content-length", "")
                    if encoding == "identity" and length.isdigit() and int(length) > byte_limit:
                        raise GmailRawTooLarge()

                data = bytearray()
                # iter_bytes() yields decoded chunks incrementally so neither
                # uncompressed nor compressed payloads can expand past the cap.
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > byte_limit:
                        if is_raw_fetch and 200 <= response.status_code < 300:
                            raise GmailRawTooLarge()
                        raise httpx.ReadError("gmail response exceeded byte limit")
                    data.extend(chunk)
                return result, bytes(data)
            finally:
                response.close()

        raise httpx.TooManyRedirects("gmail redirect limit exceeded")
