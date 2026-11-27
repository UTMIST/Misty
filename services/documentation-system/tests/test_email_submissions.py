import json
import logging
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from contracts.directory import DirectoryUnavailable
from contracts.email import GmailError
from src.api.app import create_app
from src.api.deps import get_directory, get_gmail_client, get_storage
from src.api.hashing import generate_key
from src.api.routers.email_submissions import get_email_service
from src.config import Settings
from src.directory.http_client import HttpDirectoryClient
from src.email_poll import run_poll
from src.email_submissions import EmailSubmissions
from src.fetch.gmail import HttpGmailClient
from src.storage.in_memory import InMemoryStorageAdapter
from tests.conftest import build_seed_sources
from tests.email_submission_cases import (
    ALICE_ID,
    BOB_ID,
    Clock,
    EmailSubmissionCases,
    FakeDirectory,
    FakeGmailClient,
    MAILBOX,
    make_candidate,
)

AUTH = {"X-API-Key": "dev-api-key-change-me"}


class TestEmailSubmissionsInMemory(EmailSubmissionCases):
    @pytest.fixture
    def adapter(self):
        return InMemoryStorageAdapter(build_seed_sources())


def _issue_key(storage, name: str, scopes: list[str]) -> str:
    plain, prefix, key_hash = generate_key()
    storage.create_api_key(name=name, prefix=prefix, key_hash=key_hash, scopes=scopes, actor="test")
    return plain


def test_api_visibility_submitter_isolation_and_pii_free_audit_logs(caplog):
    storage = InMemoryStorageAdapter(build_seed_sources())
    clock = Clock()
    gmail = FakeGmailClient()
    directory = FakeDirectory()
    settings = Settings(_env_file=None, email_mailbox_identity=MAILBOX)
    svc = EmailSubmissions(storage, gmail, directory, settings, now=clock)

    gmail.add(
        make_candidate(
            "msg_alice",
            thread_id="thr_a",
            sender="alice@utmist.ca",
            outer_message_id="<alice@utmist.ca>",
            subject="Fwd: Private Partnership Budget",
        )
    )
    gmail.add(
        make_candidate(
            "msg_bob",
            thread_id="thr_b",
            sender="bob@utmist.ca",
            outer_message_id="<bob@utmist.ca>",
            subject="Fwd: Bob Notes",
        )
    )

    poller_key = _issue_key(storage, "email-poller", ["emails:write"])
    member_key = _issue_key(storage, "discord-bot", ["emails:read", "act-as-user"])
    bare_read_key = _issue_key(storage, "bare-reader", ["emails:read"])
    admin_key = _issue_key(storage, "ops-admin", ["emails:admin"])

    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_gmail_client] = lambda: gmail
    app.dependency_overrides[get_directory] = lambda: directory
    app.dependency_overrides[get_email_service] = lambda: svc

    caplog.set_level(logging.INFO, logger="documentation_system.audit")
    with TestClient(app) as client:
        # Guard lifecycle
        run_id = str(uuid4())
        assert (
            client.post(
                f"/email-submissions/poll-runs/{run_id}",
                headers={"X-API-Key": poller_key},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/email-submissions/poll-runs/{uuid4()}",
                headers={"X-API-Key": poller_key},
            ).status_code
            == 409
        )
        assert (
            client.delete(
                f"/email-submissions/poll-runs/{run_id}",
                headers={"X-API-Key": poller_key},
            ).status_code
            == 200
        )

        # Poll intake
        poll_resp = client.post("/email-submissions/poll", headers={"X-API-Key": poller_key})
        assert poll_resp.status_code == 200
        assert poll_resp.json()["accepted"] == 2

        # Bare emails:read without X-On-Behalf-Of is rejected closed (403)
        assert (
            client.get("/email-submissions", headers={"X-API-Key": bare_read_key}).status_code
            == 403
        )
        # Admin without literal act-as-user cannot pass X-On-Behalf-Of
        assert (
            client.get(
                "/email-submissions",
                headers=AUTH | {"X-On-Behalf-Of": str(ALICE_ID)},
            ).status_code
            == 403
        )

        # Alice sees ONLY her own submission; Bob's UUID returns 404 (not 403)
        alice_list = client.get(
            "/email-submissions",
            headers={"X-API-Key": member_key, "X-On-Behalf-Of": str(ALICE_ID)},
        )
        assert alice_list.status_code == 200
        alice_items = alice_list.json()
        assert len(alice_items) == 1
        alice_sub_id = alice_items[0]["submission_id"]
        assert "pending_payload" not in alice_items[0]
        assert "verified_sender_email" not in alice_items[0]

        bob_items = client.get(
            "/email-submissions",
            headers={"X-API-Key": member_key, "X-On-Behalf-Of": str(BOB_ID)},
        ).json()
        bob_sub_id = bob_items[0]["submission_id"]

        assert (
            client.get(
                f"/email-submissions/{bob_sub_id}",
                headers={"X-API-Key": member_key, "X-On-Behalf-Of": str(ALICE_ID)},
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/email-submissions/{alice_sub_id}",
                headers={"X-API-Key": member_key, "X-On-Behalf-Of": str(ALICE_ID)},
            ).status_code
            == 200
        )

        # Operator emails:admin sees all submissions (still content-free) and can mark failed
        all_items = client.get("/email-submissions", headers={"X-API-Key": admin_key}).json()
        assert len(all_items) == 2

        fail_resp = client.post(
            f"/email-submissions/{alice_sub_id}/fail",
            headers={"X-API-Key": admin_key},
            json={"error_code": "doc_writer_denied"},
        )
        assert fail_resp.status_code == 200
        assert fail_resp.json()["status"] == "failed"
        assert fail_resp.json()["failure_notice_status"] == "sent"

    audit_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "alice@utmist.ca" not in audit_text
    assert "Private Partnership Budget" not in audit_text
    assert "Room booked" not in audit_text


def test_http_directory_active_member_enforces_active_flag_and_alias_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.decode("ascii")
        if raw_path == "/people/by-email/retired%40utmist.ca":
            return httpx.Response(
                200,
                json={
                    "id": str(uuid4()),
                    "display_name": "Retired Member",
                    "primary_email": "retired@utmist.ca",
                    "active": False,
                },
            )
        if raw_path == "/people/by-identifier/email/retired%40utmist.ca":
            return httpx.Response(404, json={"detail": "not found"})
        if raw_path == "/people/by-email/alias%40utoronto.ca":
            return httpx.Response(404, json={"detail": "not found"})
        if raw_path == "/people/by-identifier/email/alias%40utoronto.ca":
            return httpx.Response(
                200,
                json={
                    "id": str(ALICE_ID),
                    "display_name": "Alice Chen",
                    "primary_email": "alice@utmist.ca",
                    "active": True,
                },
            )
        if raw_path == "/people/by-email/outage%40utmist.ca":
            return httpx.Response(503, json={"detail": "down"})
        return httpx.Response(404)

    client = HttpDirectoryClient(
        "http://directory.local",
        "key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.get_active_member_by_verified_email("retired@utmist.ca") is None
    member = client.get_active_member_by_verified_email("alias@utoronto.ca")
    assert member is not None and member.person_id == ALICE_ID
    with pytest.raises(DirectoryUnavailable):
        client.get_active_member_by_verified_email("outage@utmist.ca")


def test_http_gmail_client_normalizes_errors_without_leaking_upstream_text():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages/list"):
            return httpx.Response(
                503,
                json={"detail": {"code": "gmail_not_configured", "uncertain": False}},
            )
        if request.url.path.endswith("/reply"):
            return httpx.Response(502, text="raw upstream body with private address")
        return httpx.Response(404)

    gclient = HttpGmailClient(
        "http://connectors.local",
        "key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GmailError) as exc1:
        gclient.list_messages(after_epoch_s=0, page_token=None, page_size=10)
    assert exc1.value.code == "gmail_not_configured" and exc1.value.uncertain is False

    with pytest.raises(GmailError) as exc2:
        gclient.send_reply(
            thread_id="thr_1",
            to_email="alice@utmist.ca",
            subject="Re: Hi",
            body_text="Receipt",
            in_reply_to=None,
            receipt_id=uuid4(),
        )
    assert exc2.value.uncertain is True
    assert "private address" not in str(exc2.value)


def test_email_poll_runner_checks_scopes_acquires_guard_and_pings_monitor():
    events: list[str] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api-keys/self":
            return httpx.Response(200, json={"name": "poller", "scopes": ["emails:write"]})
        if request.url.path.startswith("/email-submissions/poll-runs/"):
            events.append(f"{request.method}:guard")
            return httpx.Response(200, json={"run_id": str(uuid4())})
        if request.url.path == "/email-submissions/poll":
            events.append("poll")
            return httpx.Response(
                200,
                json={
                    "listed": 2,
                    "accepted": 1,
                    "duplicates": 0,
                    "rejected_oversize": 0,
                    "ignored_non_inbox": 0,
                    "ignored_loop": 1,
                    "rejected_unauthenticated": 0,
                    "ignored_unknown_sender": 0,
                    "directory_deferred": 0,
                    "inspect_failed": 0,
                    "receipts_attempted": 1,
                    "receipts_sent": 1,
                    "receipts_suppressed": 0,
                    "receipts_failed": 0,
                    "expired_cleaned": 0,
                    "complete": True,
                },
            )
        return httpx.Response(404)

    def monitor_handler(request: httpx.Request) -> httpx.Response:
        events.append(f"monitor:{request.url.path}")
        return httpx.Response(200, text="OK")

    code, summary = run_poll(
        ["--require-monitor"],
        env={
            "DOCS_BASE_URL": "http://docs.local",
            "DOCS_API_KEY": "doc_test_secret",
            "EMAIL_POLL_MONITOR_URL": "https://hc-ping.com/11111111-1111-1111-1111-111111111111",
        },
        http_client=httpx.Client(transport=httpx.MockTransport(api_handler)),
        monitor_client=httpx.Client(transport=httpx.MockTransport(monitor_handler)),
    )
    assert code == 0
    assert summary["outcome"] == "completed"
    assert summary["accepted"] == 1
    assert summary["guard_released"] is True
    assert events == [
        "POST:guard",
        "monitor:/11111111-1111-1111-1111-111111111111/start",
        "poll",
        "DELETE:guard",
        "monitor:/11111111-1111-1111-1111-111111111111",
    ]
    assert "doc_test_secret" not in json.dumps(summary)
    assert "hc-ping.com" not in json.dumps(summary)
