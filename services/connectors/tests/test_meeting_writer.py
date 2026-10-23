from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from contracts.meeting import CreateDoc, MeetingWriteError
from src.api.app import create_app
from src.api.deps import get_meeting_writer
from src.sources.google import GOOGLE_DOC
from src.sources.meeting_google import FOLDER, GoogleMeetingWriter

ADMIN = {"id": "owner", "type": "user", "emailAddress": "infra@example.org", "role": "owner"}
AUTH = {"X-API-Key": "dev-api-key-change-me"}


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self, **kwargs):
        return self.value() if callable(self.value) else self.value


class Drive:
    def __init__(self):
        self.created = []
        self.perms = {"root": [ADMIN], "folder": [ADMIN], "doc": [ADMIN]}
        self.deleted = []
        self.revoked = []
        self.pages = None

    def files(self):
        return Files(self)

    def permissions(self):
        return Permissions(self)


class Files:
    def __init__(self, drive):
        self.d = drive

    def get(self, *, fileId, **kwargs):
        return Request(
            {
                "id": fileId,
                "name": "Record",
                "mimeType": GOOGLE_DOC if fileId == "doc" else FOLDER,
                "appProperties": {"misty_meeting_id": "id"},
                "trashed": False,
            }
        )

    def list(self, **kwargs):
        if self.d.pages:
            return Request(self.d.pages[kwargs.get("pageToken")])
        return Request({"files": []})

    def create(self, **kwargs):
        self.d.created.append(kwargs)
        return Request({"id": "folder" if kwargs["body"]["mimeType"] == FOLDER else "doc"})

    def export(self, **kwargs):
        return Request(b"Full transcript")

    def delete(self, *, fileId, **kwargs):
        self.d.deleted.append(fileId)
        return Request({})


class Permissions:
    def __init__(self, drive):
        self.d = drive

    def list(self, *, fileId, **kwargs):
        return Request({"permissions": list(self.d.perms[fileId])})

    def create(self, *, fileId, body, **kwargs):
        self.d.perms[fileId].append(body | {"id": "team"})
        return Request({"id": "team"})

    def delete(self, *, fileId, permissionId, **kwargs):
        self.d.revoked.append(permissionId)
        self.d.perms[fileId] = [p for p in self.d.perms[fileId] if p["id"] != permissionId]
        return Request({})


def writer(drive):
    return GoogleMeetingWriter(
        root_folder_id="root", admin_emails=["infra@example.org"], drive=drive
    )


def body():
    return CreateDoc(
        meeting_id=uuid4(),
        attempt_id=uuid4(),
        voice_channel_id="123",
        title="Title",
        html="<h1>Transcript</h1>",
    )


def test_single_native_conversion_contains_html_and_tags_and_fetches_as_owner():
    drive = Drive()
    w = writer(drive)
    request = body()
    assert w.create(request).document_id == "doc"
    folder, document = drive.created
    assert document["body"]["mimeType"] == GOOGLE_DOC
    assert document["body"]["parents"] == ["folder"]
    assert document["body"]["appProperties"] == {
        "misty_meeting_id": str(request.meeting_id),
        "misty_attempt_id": str(request.attempt_id),
    }
    assert document["media_body"].getbytes(0, 100) == request.html.encode()
    assert len(drive.perms["doc"]) == 1
    w.share("doc", ["team@example.org"])
    w.share("doc", ["team@example.org"])
    assert len(drive.perms["doc"]) == 2
    assert w.fetch("doc").content == "Full transcript"
    assert w.status("doc") == "exists"
    w.delete("doc")
    assert drive.deleted == ["doc"] and "team" in drive.revoked


@pytest.mark.parametrize("folder", ["root", "folder"])
def test_unsafe_inheritance_fails_before_uploading_content(folder):
    drive = Drive()
    drive.perms[folder] = [
        ADMIN,
        {
            "id": "broad",
            "type": "anyone",
            "role": "reader",
            "permissionDetails": [{"inherited": True, "inheritedFrom": "parent"}],
        },
    ]
    with pytest.raises(MeetingWriteError, match="unsafe_destination"):
        writer(drive).create(body())
    assert not any("media_body" in request for request in drive.created)


def test_unexpected_principal_is_revoked_and_reported():
    drive = Drive()
    drive.perms["doc"].append(
        {"id": "public", "type": "domain", "domain": "example.org", "role": "reader"}
    )
    with pytest.raises(MeetingWriteError, match="unexpected_acl"):
        writer(drive).share("doc", [])
    assert "public" in drive.revoked


def test_search_scans_all_pages_including_trashed_and_other_folders():
    drive = Drive()
    attempt = str(uuid4())

    def file(file_id, trashed):
        return {
            "id": file_id,
            "createdTime": datetime.now(timezone.utc).isoformat(),
            "trashed": trashed,
            "appProperties": {"misty_attempt_id": attempt},
        }

    drive.pages = {
        None: {"files": [file("older", True)], "nextPageToken": "next"},
        "next": {"files": [file("newer", False)]},
    }
    result = writer(drive).search(uuid4())
    assert [r.document_id for r in result] == ["older", "newer"]
    assert result[0].trashed


def test_route_auth_and_validation_precede_writer():
    drive = Drive()
    app = create_app()
    app.dependency_overrides[get_meeting_writer] = lambda: writer(drive)
    with TestClient(app) as client:
        assert client.post("/google/docs", json=body().model_dump(mode="json")).status_code == 401
        malformed = body().model_dump(mode="json") | {"actor": "claimed"}
        assert client.post("/google/docs", json=malformed, headers=AUTH).status_code == 422
        assert not drive.created
        assert (
            client.post(
                "/google/docs", json=body().model_dump(mode="json"), headers=AUTH
            ).status_code
            == 200
        )


@pytest.mark.parametrize("status,uncertain", [(403, False), (400, False), (503, True), (429, True)])
def test_google_denied_and_ambiguous_writes_are_normalized(status, uncertain):
    from googleapiclient.errors import HttpError
    import httplib2
    from src.sources.meeting_google import _execute

    class Failure:
        def execute(self, **kwargs):
            assert kwargs == {"num_retries": 0}
            raise HttpError(httplib2.Response({"status": str(status)}), b"private provider body")

    with pytest.raises(MeetingWriteError) as error:
        _execute(Failure())
    assert error.value.uncertain is uncertain
    assert "private provider body" not in str(error.value)
