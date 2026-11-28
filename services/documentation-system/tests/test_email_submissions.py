import json
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


def test_api_visibility_submitter_isolation_and_pii_free_audit_logs(capsys):
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

    capsys.readouterr()
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
        assert fail_resp.json()["last_error"] == "doc_writer_denied"
        assert fail_resp.json()["failure_notice_status"] == "sent"

    audit_text = capsys.readouterr().out
    assert "/email-submissions/poll" in audit_text
    assert "alice@utmist.ca" not in audit_text
    assert "Private Partnership Budget" not in audit_text
    assert "Room booked" not in audit_text


def test_http_directory_active_member_uses_post_search_without_emails_in_url():
    seen_requests: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        seen_requests.append((request.method, str(request.url.path), body))
        assert request.method == "POST"
        assert request.url.path == "/people/search"
        assert body.get("active_only") is True
        email = body.get("email")
        if email in {"retired@utmist.ca", "outsider@external.example"}:
            return httpx.Response(
                200,
                json={"matches": [], "has_more": False, "needs_disambiguation": False},
            )
        if email == "alias@utoronto.ca":
            return httpx.Response(
                200,
                json={
                    "matches": [
                        {
                            "id": str(ALICE_ID),
                            "display_name": "Alice Chen",
                            "primary_email": "alice@utmist.ca",
                            "active": True,
                        }
                    ],
                    "has_more": False,
                    "needs_disambiguation": False,
                },
            )
        if email == "ambiguous@utmist.ca":
            return httpx.Response(
                200,
                json={
                    "matches": [
                        {
                            "id": str(ALICE_ID),
                            "display_name": "Alice Chen",
                            "primary_email": "alice@utmist.ca",
                            "active": True,
                        },
                        {
                            "id": str(BOB_ID),
                            "display_name": "Bob Li",
                            "primary_email": "bob@utmist.ca",
                            "active": True,
                        },
                    ],
                    "has_more": False,
                    "needs_disambiguation": True,
                },
            )
        if email == "outage@utmist.ca":
            return httpx.Response(503, json={"detail": "down"})
        return httpx.Response(404)

    client = HttpDirectoryClient(
        "http://directory.local",
        "key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.get_active_member_by_verified_email("retired@utmist.ca") is None
    assert client.get_active_member_by_verified_email("outsider@external.example") is None
    member = client.get_active_member_by_verified_email("alias@utoronto.ca")
    assert member is not None and member.person_id == ALICE_ID
    with pytest.raises(DirectoryUnavailable):
        client.get_active_member_by_verified_email("ambiguous@utmist.ca")
    with pytest.raises(DirectoryUnavailable):
        client.get_active_member_by_verified_email("outage@utmist.ca")
    # Confirm no email address ever appeared in a URL path.
    assert all(path == "/people/search" for _, path, _ in seen_requests)


def test_http_gmail_client_normalizes_errors_without_leaking_upstream_text():
    seen_receipt_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages/list"):
            return httpx.Response(
                503,
                json={"detail": {"code": "gmail_not_configured", "uncertain": False}},
            )
        if request.url.path.endswith("/reply"):
            return httpx.Response(502, text="raw upstream body with private address")
        if request.url.path.endswith("/receipt-check"):
            seen_receipt_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                502,
                json={"detail": {"code": "receipt_check_inconclusive", "uncertain": True}},
            )
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

    rid = uuid4()
    with pytest.raises(GmailError) as exc3:
        gclient.check_receipt(thread_id="thr_1", receipt_id=rid, sent_after_epoch_s=1700000000)
    assert exc3.value.code == "receipt_check_inconclusive" and exc3.value.uncertain is True
    assert seen_receipt_payloads == [
        {
            "thread_id": "thr_1",
            "receipt_id": str(rid),
            "sent_after_epoch_s": 1700000000,
        }
    ]


def test_422_validation_errors_strip_submitted_input():
    storage = InMemoryStorageAdapter(build_seed_sources())
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as client:
        resp = client.post(
            f"/email-submissions/{uuid4()}/fail",
            headers=AUTH,
            json={"error_code": "SECRET_MARKER_" + ("x" * 150)},
        )
        assert resp.status_code == 422
        assert all("input" not in err for err in resp.json()["detail"])
        assert "SECRET_MARKER_" not in resp.text


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


def test_email_poll_runner_releases_guard_on_completed_failures_and_resumes_pagination():
    base_env = {
        "DOCS_BASE_URL": "http://docs.local",
        "DOCS_API_KEY": "doc_test_secret",
        "EMAIL_POLL_MONITOR_URL": "https://hc-ping.com/11111111-1111-1111-1111-111111111111",
    }

    def empty_batch(**overrides):
        return {
            "listed": 0,
            "accepted": 0,
            "duplicates": 0,
            "rejected_oversize": 0,
            "ignored_non_inbox": 0,
            "ignored_loop": 0,
            "rejected_unauthenticated": 0,
            "ignored_unknown_sender": 0,
            "directory_deferred": 0,
            "inspect_failed": 0,
            "receipts_attempted": 0,
            "receipts_sent": 0,
            "receipts_suppressed": 0,
            "receipts_failed": 0,
            "expired_cleaned": 0,
            "complete": True,
            "next_page_token": None,
        } | overrides

    # 1. Completed HTTP 502 releases guard so the next run is not locked out.
    events_502: list[str] = []

    def handler_502(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api-keys/self":
            return httpx.Response(200, json={"name": "poller", "scopes": ["emails:write"]})
        if request.url.path.startswith("/email-submissions/poll-runs/"):
            events_502.append(f"{request.method}:guard")
            return httpx.Response(200, json={"run_id": str(uuid4())})
        if request.url.path == "/email-submissions/poll":
            return httpx.Response(502, json={"detail": {"code": "gmail_503", "uncertain": True}})
        return httpx.Response(404)

    mon_502: list[str] = []
    code, summary = run_poll(
        ["--require-monitor"],
        env=base_env,
        http_client=httpx.Client(transport=httpx.MockTransport(handler_502)),
        monitor_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: (mon_502.append(r.url.path), httpx.Response(200, text="OK"))[1]
            )
        ),
    )
    assert code == 2
    assert summary["guard_released"] is True
    assert events_502 == ["POST:guard", "DELETE:guard"]
    assert mon_502[-1].endswith("/fail")

    # 2. Max-batches exhaustion also releases the guard.
    events_max: list[str] = []

    def handler_max(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api-keys/self":
            return httpx.Response(200, json={"name": "poller", "scopes": ["emails:write"]})
        if request.url.path.startswith("/email-submissions/poll-runs/"):
            events_max.append(f"{request.method}:guard")
            return httpx.Response(200, json={"run_id": str(uuid4())})
        if request.url.path == "/email-submissions/poll":
            return httpx.Response(
                200, json=empty_batch(listed=25, complete=False, next_page_token="page2")
            )
        return httpx.Response(404)

    code, summary = run_poll(
        ["--max-batches", "1"],
        env=base_env,
        http_client=httpx.Client(transport=httpx.MockTransport(handler_max)),
        monitor_client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text="OK"))
        ),
    )
    assert code == 2
    assert summary["guard_released"] is True
    assert events_max == ["POST:guard", "DELETE:guard"]

    # 3. Uncertain transport timeout retains the guard (API may still be executing).
    events_to: list[str] = []

    def handler_to(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api-keys/self":
            return httpx.Response(200, json={"name": "poller", "scopes": ["emails:write"]})
        if request.url.path.startswith("/email-submissions/poll-runs/"):
            events_to.append(f"{request.method}:guard")
            return httpx.Response(200, json={"run_id": str(uuid4())})
        if request.url.path == "/email-submissions/poll":
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(404)

    code, summary = run_poll(
        [],
        env=base_env,
        http_client=httpx.Client(transport=httpx.MockTransport(handler_to)),
        monitor_client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text="OK"))
        ),
    )
    assert code == 2
    assert summary["guard_released"] is False
    assert events_to == ["POST:guard"]

    # 4. Guard release failure after a drained run pings /fail and exits nonzero.
    mon_rel: list[str] = []

    def handler_rel_fail(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api-keys/self":
            return httpx.Response(200, json={"name": "poller", "scopes": ["emails:write"]})
        if request.url.path.startswith("/email-submissions/poll-runs/"):
            if request.method == "POST":
                return httpx.Response(200, json={"run_id": str(uuid4())})
            return httpx.Response(500)
        if request.url.path == "/email-submissions/poll":
            return httpx.Response(200, json=empty_batch(listed=1, accepted=1, complete=True))
        return httpx.Response(404)

    code, summary = run_poll(
        ["--require-monitor"],
        env=base_env,
        http_client=httpx.Client(transport=httpx.MockTransport(handler_rel_fail)),
        monitor_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: (mon_rel.append(r.url.path), httpx.Response(200, text="OK"))[1]
            )
        ),
    )
    assert code == 2
    assert summary["guard_released"] is False
    assert "release" in summary["error"]
    assert mon_rel[-1].endswith("/fail")

    # 5. Runner carries page_token across batches until drained.
    seen_tokens: list[str | None] = []

    def handler_paged(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api-keys/self":
            return httpx.Response(200, json={"name": "poller", "scopes": ["emails:write"]})
        if request.url.path.startswith("/email-submissions/poll-runs/"):
            return httpx.Response(200, json={"run_id": str(uuid4())})
        if request.url.path == "/email-submissions/poll":
            tok = request.url.params.get("page_token")
            seen_tokens.append(tok)
            if tok is None:
                return httpx.Response(
                    200, json=empty_batch(listed=125, complete=False, next_page_token="125")
                )
            return httpx.Response(
                200, json=empty_batch(listed=1, accepted=1, complete=True, next_page_token=None)
            )
        return httpx.Response(404)

    code, summary = run_poll(
        [],
        env=base_env,
        http_client=httpx.Client(transport=httpx.MockTransport(handler_paged)),
        monitor_client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text="OK"))
        ),
    )
    assert code == 0
    assert summary["batches"] == 2 and summary["accepted"] == 1 and summary["drained"] is True
    assert seen_tokens == [None, "125"]
