"""Shared offline transport for tests using the SDK's bundled Drive schema."""

import json
from urllib.parse import parse_qs, urlsplit

import httplib2
from googleapiclient.discovery import build


class DriveHttp:
    def __init__(self, payload, mime, *, status=200, error=None, metadata=None, permissions=None):
        self.payload = payload
        self.mime = mime
        self.status = status
        self.error = error
        self.metadata = metadata if metadata is not None else {"mimeType": mime}
        self.permissions = permissions if permissions is not None else {"permissions": []}
        self.requests = []

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        assert method == "GET"
        path = urlsplit(uri).path
        assert path in {
            "/drive/v3/files/file123",
            "/drive/v3/files/file123/export",
            "/drive/v3/files/file123/permissions",
        }
        self.requests.append(uri)
        if path.endswith("/export") or parse_qs(urlsplit(uri).query).get("alt") == ["media"]:
            if self.error is not None:
                raise self.error
            return (
                httplib2.Response({"status": str(self.status), "content-type": self.mime}),
                self.payload,
            )
        # files.get(alt="media") uses the SDK's JSON model, which overwrites
        # alt with json. A fake files.get returning bytes conceals this bug.
        metadata = self.permissions if path.endswith("/permissions") else self.metadata
        return (
            httplib2.Response({"status": "200", "content-type": "application/json"}),
            json.dumps(metadata).encode(),
        )


def drive_service(http):
    # Static discovery uses the SDK's bundled schema, never a network lookup.
    return build("drive", "v3", http=http, static_discovery=True, cache_discovery=False)
