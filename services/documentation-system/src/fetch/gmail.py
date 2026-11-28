"""Content-free failures and validated responses from the connector Gmail adapter."""

import re
from uuid import UUID

import httpx

from contracts.email import GmailError, GmailInspectResult, GmailListPage

_ALLOWED_CODES = frozenset(
    {
        "gmail_not_configured",
        "gmail_not_found",
        "gmail_400",
        "gmail_401",
        "gmail_403",
        "gmail_408",
        "gmail_429",
        "gmail_500",
        "gmail_502",
        "gmail_503",
        "gmail_504",
        "gmail_transport",
        "gmail_invalid_response",
        "invalid_reply_header",
        "invalid_reply_body",
        "invalid_recipient",
        "self_reply_forbidden",
        "receipt_check_inconclusive",
    }
)
_GMAIL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class HttpGmailClient:
    def __init__(self, base_url: str, api_key: str, *, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = client

    def _call(self, method: str, path: str, body: dict | None = None, *, write: bool = False):
        client = self.client or httpx.Client(timeout=45.0)
        try:
            response = client.request(
                method,
                f"{self.base_url}/google/gmail{path}",
                headers={"X-API-Key": self.api_key},
                json=body,
            )
            if response.status_code >= 300:
                code = "connector_failure"
                uncertain = response.status_code >= 500 or response.status_code in {408, 429}
                try:
                    detail = response.json()["detail"]
                    if isinstance(detail, dict):
                        if detail.get("code") in _ALLOWED_CODES:
                            code = detail["code"]
                        if isinstance(detail.get("uncertain"), bool):
                            uncertain = detail["uncertain"]
                except (KeyError, ValueError, TypeError):
                    pass
                raise GmailError(code, uncertain=uncertain)
            return response.json()
        except GmailError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise GmailError("connector_unavailable", uncertain=write) from None
        finally:
            if self.client is None:
                client.close()

    def list_messages(
        self, *, after_epoch_s: int | None, page_token: str | None, page_size: int
    ) -> GmailListPage:
        payload: dict[str, object] = {"page_size": page_size}
        if after_epoch_s is not None:
            payload["after_epoch_s"] = after_epoch_s
        if page_token is not None:
            payload["page_token"] = page_token
        try:
            return GmailListPage.model_validate(self._call("POST", "/messages/list", payload))
        except ValueError:
            raise GmailError("invalid_list_response") from None

    def inspect_message(self, message_id: str) -> GmailInspectResult:
        if not _GMAIL_ID_RE.match(message_id):
            raise GmailError("invalid_message_id")
        try:
            return GmailInspectResult.model_validate(self._call("POST", f"/messages/{message_id}"))
        except ValueError:
            raise GmailError("invalid_inspect_response") from None

    def send_reply(
        self,
        *,
        thread_id: str,
        to_email: str,
        subject: str,
        body_text: str,
        in_reply_to: str | None,
        receipt_id: UUID,
    ) -> str:
        payload: dict[str, object] = {
            "thread_id": thread_id,
            "to_email": to_email,
            "subject": subject,
            "body_text": body_text,
            "receipt_id": str(receipt_id),
        }
        if in_reply_to is not None:
            payload["in_reply_to"] = in_reply_to
        data = self._call("POST", "/reply", payload, write=True)
        sent_id = data.get("sent_message_id") if isinstance(data, dict) else None
        if not isinstance(sent_id, str) or not _GMAIL_ID_RE.match(sent_id):
            raise GmailError("invalid_reply_response", uncertain=True)
        return sent_id

    def check_receipt(
        self,
        *,
        thread_id: str,
        receipt_id: UUID,
        sent_after_epoch_s: int | None = None,
    ) -> str | None:
        payload: dict[str, object] = {
            "thread_id": thread_id,
            "receipt_id": str(receipt_id),
        }
        if sent_after_epoch_s is not None:
            payload["sent_after_epoch_s"] = sent_after_epoch_s
        data = self._call("POST", "/receipt-check", payload)
        if not isinstance(data, dict) or not isinstance(data.get("sent"), bool):
            raise GmailError("invalid_receipt_check_response")
        if not data["sent"]:
            return None
        sent_id = data.get("sent_message_id")
        if not isinstance(sent_id, str) or not _GMAIL_ID_RE.match(sent_id):
            raise GmailError("invalid_receipt_check_response")
        return sent_id
