import json

import httpx
import pytest

from contracts.login import VerificationUnavailable
from src.verification.http_client import HttpVerificationClient


def client(handler):
    return HttpVerificationClient(
        "http://verification.test",
        "private-test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_private_request_contract_and_secret_unwrap():
    calls = []

    def handle(request):
        assert request.headers["X-API-Key"] == "private-test-key"
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("request-code"):
            return httpx.Response(202, json={"status": "sent"})
        return httpx.Response(200, json={"verified": True, "subject": "opaque", "email": "a@b.com"})

    upstream = client(handle)
    upstream.request_code("opaque", "a@b.com")
    assert upstream.confirm_code("opaque", "123456") == "a@b.com"
    assert calls == [
        ("/verification/request-code", {"subject": "opaque", "email": "a@b.com"}),
        ("/verification/confirm-code", {"subject": "opaque", "code": "123456"}),
    ]
    upstream.close()


@pytest.mark.parametrize("status", [400, 404, 410, 429])
def test_invalid_code_status_is_not_an_identity(status):
    upstream = client(lambda _: httpx.Response(status, text="private-response"))
    assert upstream.confirm_code("opaque", "123456") is None
    upstream.close()


@pytest.mark.parametrize(
    "status,data",
    [
        (200, {"verified": True, "subject": "other", "email": "a@b.com"}),
        (200, {"verified": "true", "subject": "opaque", "email": "a@b.com"}),
        (200, None),
        (401, {"detail": "private-response"}),
        (500, {"detail": "private-response"}),
    ],
)
def test_malformed_or_failed_verification_is_sanitized(status, data):
    upstream = client(lambda _: httpx.Response(status, json=data))
    with pytest.raises(VerificationUnavailable) as exc:
        upstream.confirm_code("opaque", "123456")
    assert "private-response" not in str(exc.value)
    upstream.close()


def test_transport_failures_do_not_leak_request_data():
    def broken(request):
        raise httpx.ConnectError("private-request-data", request=request)

    upstream = client(broken)
    with pytest.raises(VerificationUnavailable) as exc:
        upstream.request_code("opaque", "a@b.com")
    assert "private-request-data" not in str(exc.value)
    upstream.close()


@pytest.mark.parametrize(
    "header,body",
    [
        (None, {"detail": "rate_limited"}),
        ("0", {"detail": "rate_limited"}),
        ("61", {"detail": "rate_limited"}),
        ("-1", {"detail": "rate_limited"}),
        ("1.5", {"detail": "rate_limited"}),
        ("NaN", {"detail": "rate_limited"}),
        ("9" * 100, {"detail": "rate_limited"}),
        ("1", {"detail": "different_failure"}),
        ("1", None),
    ],
)
def test_unknown_throttles_never_authorize_retry(header, body):
    from contracts.login import VerificationRateLimited

    headers = {"Retry-After": header} if header is not None else {}
    upstream = client(lambda _: httpx.Response(429, json=body, headers=headers))
    try:
        with pytest.raises(VerificationUnavailable) as exc:
            upstream.request_code("opaque", "a@b.com")
        assert not isinstance(exc.value, VerificationRateLimited)
    finally:
        upstream.close()
