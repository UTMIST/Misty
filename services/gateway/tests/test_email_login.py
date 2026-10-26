from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.app import create_app
from src.api.hashing import generate_key
from src.config import Settings, get_settings
from src.storage.in_memory import InMemoryStorageAdapter

EMAIL = "member@example.com"
PERSON_ID = str(uuid4())
PERSON = {"id": PERSON_ID, "primary_email": EMAIL, "active": True}


class Directory:
    def __init__(self):
        self.person = dict(PERSON)

    def get_person_by_email(self, email):
        if self.person and email == self.person["primary_email"]:
            return self.person
        return None

    def get_person(self, person_id):
        return self.person if self.person and self.person["id"] == person_id else None


class Verification:
    """Only replace private HTTP; retain gateway state and authentication."""

    def __init__(self):
        self.sent = {}
        self.guesses = []

    def request_code(self, subject, email):
        self.sent[subject] = email

    def confirm_code(self, subject, code):
        self.guesses.append((subject, code))
        return self.sent.get(subject) if code == "123456" else None


@pytest.fixture
def login(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("GATEWAY_ENV", "local")
    monkeypatch.setenv("EMAIL_LOGIN_ENABLED", "true")
    get_settings.cache_clear()
    store, directory, verification = InMemoryStorageAdapter(), Directory(), Verification()
    token, prefix, key_hash = generate_key()
    store.create_api_key(
        name="compute",
        prefix=prefix,
        key_hash=key_hash,
        scopes=["auth:email", "auth:status"],
        actor="test",
    )
    app = create_app()
    app.dependency_overrides[deps.get_storage] = lambda: store
    app.dependency_overrides[deps.get_directory] = lambda: directory
    app.dependency_overrides[deps.get_verification] = lambda: verification
    with TestClient(app, headers={"X-API-Key": token}) as client:
        yield client, store, directory, verification
    get_settings.cache_clear()


def start(client, email=EMAIL, **extra):
    return client.post(
        "/v1/auth/email/start", json={"email": email, "client_ip": "192.0.2.1", **extra}
    )


def confirm(client, challenge, code="123456", **extra):
    return client.post(
        "/v1/auth/email/confirm",
        json={"challenge_id": challenge, "code": code, "client_ip": "192.0.2.1", **extra},
    )


@pytest.mark.parametrize("eligible", ["active", "missing", "inactive"])
def test_start_is_generic_and_only_sends_to_active_people(login, eligible):
    client, _, directory, verification = login
    if eligible == "missing":
        directory.person = None
    elif eligible == "inactive":
        directory.person["active"] = False
    response = start(client, " MEMBER@EXAMPLE.COM ")
    assert response.status_code == 202
    assert set(response.json()) == {"challenge_id", "expires_in", "message"}
    assert response.json()["expires_in"] == 600
    assert EMAIL not in response.text
    assert len(verification.sent) == (1 if eligible == "active" else 0)
    result = confirm(client, response.json()["challenge_id"])
    assert result.status_code == (200 if eligible == "active" else 400)
    if eligible == "active":
        assert result.json() == {"person_id": PERSON_ID, "email": EMAIL}


def test_success_is_single_use_even_under_concurrent_requests(login):
    client, _, _, _ = login
    challenge = start(client).json()["challenge_id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: confirm(client, challenge), range(8)))
    assert sum(r.status_code == 200 for r in responses) == 1
    assert all(r.status_code in {200, 400} for r in responses)


@pytest.mark.parametrize("change", ["retired", "email_changed", "reassigned"])
def test_confirmation_rechecks_original_person_and_email(login, change):
    client, _, directory, _ = login
    challenge = start(client).json()["challenge_id"]
    if change == "retired":
        directory.person["active"] = False
    elif change == "email_changed":
        directory.person["primary_email"] = "changed@example.com"
    else:
        directory.person["id"] = str(uuid4())
    response = confirm(client, challenge)
    assert response.status_code == 400
    assert EMAIL not in response.text


def test_concurrent_wrong_guesses_cannot_exceed_five_attempts(login):
    client, _, _, verification = login
    challenge = start(client).json()["challenge_id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: confirm(client, challenge, "999999"), range(8)))
    assert all(r.status_code == 400 for r in responses)
    assert len(verification.guesses) == 5
    assert confirm(client, challenge).status_code == 400


def test_repeated_send_is_generic_and_does_not_send_again(login):
    client, _, _, verification = login
    first, second = start(client), start(client)
    assert first.status_code == second.status_code == 202
    assert first.json()["message"] == second.json()["message"]
    assert len(verification.sent) == 1
    assert confirm(client, second.json()["challenge_id"]).status_code == 400
    assert confirm(client, first.json()["challenge_id"]).status_code == 200


def test_confirm_is_bound_to_the_issued_consumer(login):
    client, store, _, _ = login
    challenge = start(client).json()["challenge_id"]
    token, prefix, key_hash = generate_key()
    store.create_api_key(
        name="another-app",
        prefix=prefix,
        key_hash=key_hash,
        scopes=["auth:email"],
        actor="test",
    )
    response = client.post(
        "/v1/auth/email/confirm",
        headers={"X-API-Key": token},
        json={"challenge_id": challenge, "code": "123456", "client_ip": "192.0.2.1"},
    )
    assert response.status_code == 400
    assert confirm(client, challenge).status_code == 200


@pytest.mark.parametrize(
    "person,active", [(PERSON, True), ({**PERSON, "active": False}, False), (None, False)]
)
def test_status_is_minimal_and_checks_current_state(login, person, active):
    client, _, directory, _ = login
    directory.person = person
    response = client.get(f"/v1/auth/people/{PERSON_ID}/status")
    assert response.status_code == 200
    assert response.json() == {"active": active}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "endpoint,body",
    [
        ("/v1/auth/email/start", {"email": EMAIL, "client_ip": "192.0.2.1"}),
        (
            "/v1/auth/email/confirm",
            {"challenge_id": str(uuid4()), "code": "123456", "client_ip": "192.0.2.1"},
        ),
    ],
)
def test_wrong_scope_stops_before_upstream_construction(login, endpoint, body):
    client, store, _, _ = login
    token, prefix, key_hash = generate_key()
    store.create_api_key(
        name="resolve-only",
        prefix=prefix,
        key_hash=key_hash,
        scopes=["resolve:discord"],
        actor="test",
    )

    def unexpected():
        raise AssertionError("upstream was constructed before scope validation")

    client.app.dependency_overrides[deps.get_directory] = unexpected
    client.app.dependency_overrides[deps.get_verification] = unexpected
    assert client.post(endpoint, headers={"X-API-Key": token}, json=body).status_code == 403


def test_strict_inputs_and_no_input_echo_in_errors(login):
    client, _, _, verification = login
    assert start(client, subject="attacker-chosen").status_code == 422
    response = confirm(client, str(uuid4()), "secret-typed-by-mistake")
    assert response.status_code == 422
    assert "secret-typed-by-mistake" not in response.text
    assert not verification.sent


def test_disabled_login_fails_closed(login, monkeypatch):
    client, _, _, verification = login
    monkeypatch.setenv("EMAIL_LOGIN_ENABLED", "false")
    get_settings.cache_clear()
    assert start(client).status_code == 503
    assert not verification.sent


def test_large_body_rejected_before_dependencies(login):
    client, _, _, _ = login

    def unexpected():
        raise AssertionError("large bodies must be rejected before upstream work")

    client.app.dependency_overrides[deps.get_directory] = unexpected
    response = client.post(
        "/v1/auth/email/start", content=b"x" * 4097, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_upstream_failures_do_not_leak_details(login, caplog):
    from contracts.directory import DirectoryUnavailable
    from contracts.login import VerificationUnavailable

    client, _, directory, verification = login

    def unavailable(*args):
        raise VerificationUnavailable("private-secret-provider-response")

    verification.request_code = unavailable
    result = start(client)
    assert result.status_code == 202
    assert "private-secret-provider-response" not in caplog.text

    def down(*args):
        raise DirectoryUnavailable("directory unavailable")

    directory.get_person = down
    response = client.get(f"/v1/auth/people/{PERSON_ID}/status")
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


def test_wrong_verification_email_cannot_authenticate(login):
    client, _, _, verification = login
    challenge = start(client).json()["challenge_id"]
    verification.confirm_code = lambda subject, code: "other@example.com"
    assert confirm(client, challenge).status_code == 400


def test_bad_directory_shape_fails_closed(login):
    client, _, directory, verification = login
    directory.person["active"] = "true"
    assert start(client).status_code == 202
    assert not verification.sent
    assert client.get(f"/v1/auth/people/{PERSON_ID}/status").status_code == 503


def test_login_start_response_is_sent_before_mail_delivery(login):
    # Observe ASGI send order; TestClient itself waits for background tasks.
    client, _, _, verification = login
    app = client.app
    response_sent = []

    async def observed(scope, receive, send):
        async def record(message):
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_sent.append(True)
            await send(message)

        await app(scope, receive, record)

    original = verification.request_code

    def deliver(subject, email):
        assert response_sent
        original(subject, email)

    verification.request_code = deliver
    with TestClient(observed, headers=client.headers) as other:
        assert start(other).status_code == 202
    assert len(verification.sent) == 1


def test_failed_resend_keeps_previous_code_usable(login, monkeypatch):
    from datetime import timedelta
    from contracts.login import VerificationUnavailable
    from src import login_policy

    client, _, _, verification = login
    first = start(client).json()["challenge_id"]
    later = login_policy.now() + timedelta(seconds=61)
    monkeypatch.setattr(login_policy, "now", lambda: later)

    def failed(*args):
        raise VerificationUnavailable("delivery rejected")

    verification.request_code = failed
    second = start(client).json()["challenge_id"]
    assert confirm(client, second).status_code == 400
    assert confirm(client, first).status_code == 200


@pytest.mark.parametrize("bad_id", [1, True, [], {}])
def test_invalid_upstream_id_returns_sanitized_503(login, bad_id):
    client, _, directory, _ = login
    directory.get_person = lambda _: {**PERSON, "id": bad_id}
    assert client.get(f"/v1/auth/people/{PERSON_ID}/status").status_code == 503


def test_missing_and_active_challenges_share_verification_failure_behavior(login):
    from contracts.login import VerificationUnavailable

    client, _, directory, verification = login
    active = start(client).json()["challenge_id"]
    directory.person = None
    missing = start(client, "missing@example.com").json()["challenge_id"]

    def unavailable(*args):
        raise VerificationUnavailable("upstream down")

    verification.confirm_code = unavailable
    first = confirm(client, active, "999999")
    second = confirm(client, missing, "999999")
    assert first.status_code == second.status_code == 503
    assert first.json() == second.json()
