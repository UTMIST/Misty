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
