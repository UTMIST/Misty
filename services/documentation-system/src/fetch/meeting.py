"""Content-free failures and validated responses from the connector writer."""

import httpx
from pydantic import TypeAdapter

from contracts.fetcher import FetchResult, SourcePermission
from contracts.meeting import TaggedDoc, WriterError


class HttpMeetingWriter:
    def __init__(self, base_url, api_key, *, client=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = client

    def _call(self, method, path, body=None):
        client = self.client or httpx.Client(timeout=45)
        try:
            response = client.request(
                method,
                f"{self.base_url}/google/docs{path}",
                headers={"X-API-Key": self.api_key},
                json=body,
            )
            if response.status_code >= 300:
                # Allowlisted codes only; never log arbitrary upstream text.
                code = "connector_failure"
                uncertain = response.status_code >= 500 or response.status_code in {408, 429}
                try:
                    detail = response.json()["detail"]
                    if detail["code"] in {
                        "unexpected_acl",
                        "google_404",
                        "writer_not_configured",
                        "unsafe_destination",
                        "delete_unconfirmed",
                    }:
                        code = detail["code"]
                    uncertain = uncertain or detail.get("uncertain", False) is True
                except (KeyError, ValueError, TypeError):
                    pass
                raise WriterError(code, uncertain=uncertain)
            return response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            raise WriterError("connector_unavailable", uncertain=True) from None
        finally:
            if self.client is None:
                client.close()

    def create(self, **body):
        data = self._call(
            "POST",
            "",
            {k: str(v) if k in {"meeting_id", "attempt_id"} else v for k, v in body.items()},
        )
        file_id = data.get("document_id")
        import re

        if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", file_id):
            raise WriterError("invalid_create_response", uncertain=True)
        return file_id

    def search(self, meeting_id):
        try:
            return TypeAdapter(list[TaggedDoc]).validate_python(
                self._call("POST", "/search", {"meeting_id": str(meeting_id)})
            )
        except ValueError:
            raise WriterError("invalid_search_response") from None

    def share(self, document_id, commenters):
        try:
            return TypeAdapter(list[SourcePermission]).validate_python(
                self._call("PUT", f"/{document_id}/sharing", {"commenters": commenters})
            )
        except ValueError:
            raise WriterError("invalid_sharing_response") from None

    def fetch(self, document_id):
        try:
            result = FetchResult.model_validate(self._call("POST", f"/{document_id}/fetch"))
            if result.permissions is None:
                raise ValueError("missing ACL")
            return result.model_copy(
                update={"content_snapshot": (result.content or "")[:4000] or None}
            )
        except ValueError:
            raise WriterError("invalid_fetch_response") from None

    def status(self, document_id):
        state = self._call("GET", f"/{document_id}/status").get("status")
        if state not in {"exists", "trashed", "deleted", "inaccessible"}:
            raise WriterError("invalid_status_response")
        return state

    def delete(self, document_id):
        self._call("DELETE", f"/{document_id}")
