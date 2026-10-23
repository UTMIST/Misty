from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from contracts.discovery import DiscoveryPage, DiscoveryRequest
from src.api.app import create_app
from src.api.deps import get_discovery_source
from src.sources.base import SourceForbidden, SourceNotConfigured, SourceNotFound, SourceUnavailable
from src.sources.google import EXTRACTORS, GOOGLE_DOC, GoogleSource, _read_permissions


class Drive:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def files(self):
        return self

    def list(self, **kwargs):
        self.calls.append(kwargs)
        value = self.pages.pop(0)
        return SimpleNamespace(execute=lambda: value)


def source(drive):
    return GoogleSource(
        credentials_json_b64="",
        max_content_chars=100,
        max_file_bytes=100,
        services={"drive": drive},
    )


def test_direct_shares_and_nested_listing_are_bounded_and_paginated():
    drive = Drive(
        [
            {
                "files": [
                    {"id": "direct", "mimeType": GOOGLE_DOC},
                    {"id": "folder", "mimeType": "application/vnd.google-apps.folder"},
                ],
                "nextPageToken": "next",
            },
            {"files": [], "nextPageToken": "last"},
            {"files": [{"id": "child", "mimeType": "text/plain"}]},
        ]
    )
    google = source(drive)
    first = google.discover(DiscoveryRequest(page_size=2))
    assert first.items[0].source_id == "gdocs"
    assert first.items[1].kind == "folder"
    assert first.next_page_token == "next"
    google.discover(DiscoveryRequest(page_token="next", page_size=2))
    child = google.discover(DiscoveryRequest(folder_id="folder"))
    assert child.items[0].source_id == "gdrive"
    assert drive.calls[0]["q"] == "trashed = false and (sharedWithMe = true)"
    assert drive.calls[1]["pageToken"] == "next"
    assert drive.calls[2]["q"] == "trashed = false and ('folder' in parents)"
    assert all(call["corpora"] == "user" for call in drive.calls)
    assert all("name" not in call["fields"] for call in drive.calls)


def test_formats_follow_registry_and_exclusions_are_explicit():
    entries = [{"id": f"f{i}", "mimeType": mime} for i, mime in enumerate(EXTRACTORS)]
    entries += [
        {"id": "plain", "mimeType": "text/csv"},
        {"id": "image", "mimeType": "image/png"},
        {"id": "shortcut", "mimeType": "application/vnd.google-apps.shortcut"},
        {"id": "shared", "mimeType": GOOGLE_DOC, "driveId": "drive"},
    ]
    page = source(Drive([{"files": entries, "incompleteSearch": True}])).discover(
        DiscoveryRequest()
    )
    assert all(item.kind == "file" for item in page.items[:-3])
    assert [item.kind for item in page.items[-3:]] == ["unsupported", "shortcut", "shared_drive"]
    assert page.incomplete


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"files": None},
        {"files": [{}]},
        {"files": [{"id": "bad'", "mimeType": GOOGLE_DOC}]},
        {"files": [], "incompleteSearch": "false"},
        {"files": [], "nextPageToken": "same"},
    ],
)
def test_malformed_listing_fails_closed(body):
    with pytest.raises(SourceUnavailable):
        source(Drive([body])).discover(DiscoveryRequest(page_token="same"))


@pytest.mark.parametrize(
    "error,code",
    [
        (SourceForbidden, 403),
        (SourceNotFound, 404),
        (SourceNotConfigured, 503),
        (SourceUnavailable, 502),
    ],
)
def test_route_errors_are_sanitized(error, code):
    class Fake:
        def discover(self, body):
            raise error("secret upstream body")

    app = create_app()
    app.dependency_overrides[get_discovery_source] = lambda: Fake()
    with TestClient(app) as client:
        response = client.post(
            "/discover/google", json={}, headers={"X-API-Key": "dev-api-key-change-me"}
        )
    assert response.status_code == code
    assert "secret" not in response.text


def test_auth_and_validation_before_provider():
    calls = []

    class Fake:
        def discover(self, body):
            calls.append(body)
            return DiscoveryPage(items=[])

    app = create_app()
    app.dependency_overrides[get_discovery_source] = lambda: Fake()
    with TestClient(app) as client:
        assert client.post("/discover/google", json={}).status_code == 401
        assert (
            client.post(
                "/discover/google",
                json={"folder_id": "x' or true"},
                headers={"X-API-Key": "dev-api-key-change-me"},
            ).status_code
            == 422
        )
    assert calls == []


def test_incomplete_or_looping_acl_never_returns_partial_permissions():
    for response in ({}, {"permissions": [], "nextPageToken": "repeat"}):
        calls = []

        def listing(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(execute=lambda: response)

        drive = SimpleNamespace(permissions=lambda: SimpleNamespace(list=listing))
        with pytest.raises(SourceUnavailable):
            _read_permissions(drive, "file")
        assert len(calls) <= 2


def test_fetch_scope_cannot_enumerate():
    from platform_auth import InMemoryKeyStore
    from src.api.deps import get_key_store
    from src.api.hashing import generate_key

    key, prefix, hashed = generate_key()
    store = InMemoryKeyStore()
    store.add(name="fetch-only", prefix=prefix, key_hash=hashed, scopes=["fetch"])
    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: store
    app.dependency_overrides[get_discovery_source] = lambda: source(Drive([]))
    with TestClient(app) as client:
        response = client.post("/discover/google", json={}, headers={"X-API-Key": key})
    assert response.status_code == 403
