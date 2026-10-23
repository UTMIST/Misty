"""OAuth writer for Misty-created Docs. Never rewrites a document body."""

from contextlib import contextmanager
from io import BytesIO
from uuid import UUID

from contracts.fetch import FetchResponse, SourcePermission
from contracts.meeting import CreateDoc, GoogleDoc, MeetingWriteError, TaggedDoc
from src.sources.base import SourceError
from src.sources.google import GOOGLE_DOC, _read_permissions

FOLDER = "application/vnd.google-apps.folder"


def _execute(request):
    from googleapiclient.errors import HttpError

    try:
        return request.execute(num_retries=0)
    except HttpError as exc:
        status = exc.resp.status
        raise MeetingWriteError(
            f"google_{status}", uncertain=status >= 500 or status in {408, 429}
        ) from None
    except Exception:
        raise MeetingWriteError("google_transport", uncertain=True) from None


class GoogleMeetingWriter:
    def __init__(
        self,
        *,
        root_folder_id,
        admin_emails,
        client_id="",
        client_secret="",
        refresh_token="",
        timeout=30,
        drive=None,
    ):
        self.root = root_folder_id
        self.admins = {email.strip().lower() for email in admin_emails if email.strip()}
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._timeout = timeout
        self._drive = drive

    @contextmanager
    def _client(self):
        if not self.root or not self.admins:
            raise MeetingWriteError("writer_not_configured")
        if self._drive is not None:
            yield self._drive
            return
        if not all((self._client_id, self._client_secret, self._refresh_token)):
            raise MeetingWriteError("writer_not_configured")
        import google_auth_httplib2
        import httplib2
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = Credentials(
            None,
            refresh_token=self._refresh_token,
            client_id=self._client_id,
            client_secret=self._client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        # Fresh transport per request: httplib2 is not thread safe.
        drive = build(
            "drive",
            "v3",
            cache_discovery=False,
            http=google_auth_httplib2.AuthorizedHttp(
                credentials, http=httplib2.Http(timeout=self._timeout)
            ),
        )
        try:
            yield drive
        finally:
            drive.close()

    def _permissions(self, drive, file_id):
        try:
            return [SourcePermission(**vars(p)) for p in _read_permissions(drive, file_id)]
        except SourceError:
            raise MeetingWriteError("acl_unavailable") from None

    def _safe_acl(self, permissions, commenters=()):
        return bool(permissions) and all(
            p.principal_type in {"user", "group"}
            and p.principal
            and (
                p.principal.lower() in self.admins
                or (
                    p.principal_type == "group"
                    and p.principal.lower() in commenters
                    and p.role == "commenter"
                    and not p.inherited
                )
            )
            for p in permissions
        )

    def _check_folder(self, drive, folder_id):
        meta = _execute(
            drive.files().get(
                fileId=folder_id, supportsAllDrives=True, fields="id,mimeType,trashed"
            )
        )
        if meta.get("trashed") or meta.get("mimeType") != FOLDER:
            raise MeetingWriteError("invalid_destination")
        if not self._safe_acl(self._permissions(drive, folder_id)):
            raise MeetingWriteError("unsafe_destination")

    def _list(self, drive, query):
        token = None
        seen = set()
        files = []
        while True:
            result = _execute(
                drive.files().list(
                    q=query,
                    spaces="drive",
                    corpora="user",
                    pageSize=100,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    pageToken=token,
                    fields="nextPageToken,incompleteSearch,files(id,createdTime,trashed,appProperties)",
                )
            )
            if result.get("incompleteSearch") or not isinstance(result.get("files"), list):
                raise MeetingWriteError("incomplete_search")
            files.extend(result["files"])
            token = result.get("nextPageToken")
            if not token:
                return files
            if token in seen:
                raise MeetingWriteError("repeated_cursor")
            seen.add(token)

    def _tagged(self, drive, file_id):
        meta = _execute(
            drive.files().get(
                fileId=file_id,
                supportsAllDrives=True,
                fields="id,name,mimeType,trashed,appProperties",
            )
        )
        if meta.get("mimeType") != GOOGLE_DOC or not meta.get("appProperties", {}).get(
            "misty_meeting_id"
        ):
            raise MeetingWriteError("not_meeting_doc")
        return meta

    def _revoke(self, drive, file_id, permissions):
        for p in permissions:
            if p.role != "owner" and not p.inherited:
                _execute(
                    drive.permissions().delete(
                        fileId=file_id, permissionId=p.permission_id, supportsAllDrives=True
                    )
                )

    def create(self, body: CreateDoc) -> GoogleDoc:
        from googleapiclient.http import MediaIoBaseUpload

        with self._client() as drive:
            self._check_folder(drive, self.root)  # before any content leaves this process
            folders = self._list(
                drive,
                f"'{self.root}' in parents and trashed = false and "
                f"appProperties has {{ key='misty_voice_channel_id' and value='{body.voice_channel_id}' }}",
            )
            folder_id = (
                folders[0]["id"]
                if folders
                else _execute(
                    drive.files().create(
                        body={
                            "name": body.voice_channel_id,
                            "mimeType": FOLDER,
                            "parents": [self.root],
                            "appProperties": {"misty_voice_channel_id": body.voice_channel_id},
                        },
                        fields="id",
                        supportsAllDrives=True,
                    )
                )["id"]
            )
            self._check_folder(drive, folder_id)
            result = _execute(
                drive.files().create(
                    body={
                        "name": body.title,
                        "mimeType": GOOGLE_DOC,
                        "parents": [folder_id],
                        "appProperties": {
                            "misty_meeting_id": str(body.meeting_id),
                            "misty_attempt_id": str(body.attempt_id),
                        },
                    },
                    media_body=MediaIoBaseUpload(
                        BytesIO(body.html.encode("utf-8")), mimetype="text/html", resumable=False
                    ),
                    fields="id",
                    supportsAllDrives=True,
                )
            )
            file_id = result["id"]
            return GoogleDoc(
                document_id=file_id, url=f"https://docs.google.com/document/d/{file_id}/edit"
            )

    def search(self, meeting_id: UUID) -> list[TaggedDoc]:
        with self._client() as drive:
            files = self._list(
                drive, f"appProperties has {{ key='misty_meeting_id' and value='{meeting_id}' }}"
            )
            try:
                return [
                    TaggedDoc(
                        document_id=f["id"],
                        attempt_id=f["appProperties"]["misty_attempt_id"],
                        created_at=f["createdTime"],
                        trashed=f["trashed"],
                    )
                    for f in files
                ]
            except (KeyError, ValueError, TypeError):
                raise MeetingWriteError("malformed_search") from None

    def share(self, document_id, commenters):
        commenters = {email.strip().lower() for email in commenters}
        if any("@" not in email or any(c.isspace() for c in email) for email in commenters):
            raise MeetingWriteError("invalid_group")
        with self._client() as drive:
            self._tagged(drive, document_id)
            permissions = self._permissions(drive, document_id)
            # Revoke unexpected direct shares before reporting a security failure.
            if not self._safe_acl(permissions, commenters):
                try:
                    self._revoke(drive, document_id, permissions)
                except MeetingWriteError:
                    pass  # inherited access may be irrevocable: caller must delete
                raise MeetingWriteError("unexpected_acl")
            for email in sorted(commenters):
                if not any(
                    p.principal_type == "group" and p.principal == email and p.role == "commenter"
                    for p in permissions
                ):
                    _execute(
                        drive.permissions().create(
                            fileId=document_id,
                            supportsAllDrives=True,
                            body={"type": "group", "role": "commenter", "emailAddress": email},
                            sendNotificationEmail=False,
                            fields="id",
                        )
                    )
            permissions = self._permissions(drive, document_id)
            if not self._safe_acl(permissions, commenters):
                try:
                    self._revoke(drive, document_id, permissions)
                except MeetingWriteError:
                    pass
                raise MeetingWriteError("unexpected_acl")
            actual = {
                p.principal.lower()
                for p in permissions
                if p.principal_type == "group" and p.principal and p.role == "commenter"
            }
            if not commenters.issubset(actual):
                raise MeetingWriteError("sharing_incomplete")
            return permissions

    def fetch(self, document_id):
        with self._client() as drive:
            meta = self._tagged(drive, document_id)
            if meta.get("trashed"):
                raise MeetingWriteError("google_404")
            permissions = self._permissions(drive, document_id)
            content = _execute(drive.files().export(fileId=document_id, mimeType="text/plain"))
            return FetchResponse(
                title=meta.get("name"), content=content.decode("utf-8"), permissions=permissions
            )

    def status(self, document_id):
        with self._client() as drive:
            try:
                meta = self._tagged(drive, document_id)
            except MeetingWriteError as exc:
                if exc.code == "google_404":
                    # Drive 404 cannot distinguish deletion from lost writer access.
                    return "inaccessible"
                raise
            return "trashed" if meta.get("trashed") else "exists"

    def delete(self, document_id):
        with self._client() as drive:
            try:
                self._tagged(drive, document_id)
                try:
                    self._revoke(drive, document_id, self._permissions(drive, document_id))
                except MeetingWriteError:
                    pass  # permanent deletion must still run if revocation failed
                _execute(drive.files().delete(fileId=document_id, supportsAllDrives=True))
            except MeetingWriteError as exc:
                # An inaccessible file cannot count as a successful permanent deletion.
                if exc.code == "google_404":
                    raise MeetingWriteError("delete_unconfirmed") from None
                raise
