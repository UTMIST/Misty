import hashlib
import hmac
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.deps import get_github_linking
from src.config import get_settings


class Service:
    calls = []

    def start(self, person_id, discord_id):
        self.calls.append((person_id, discord_id))
        return {"authorize_url": "https://github.com/login/oauth/authorize?state=PRIVATE"}

    def confirm(self, person_id, discord_id, code):
        self.calls.append((person_id, discord_id, code))
        return {"status": "linked"}

    def callback(self, state, code):
        self.calls.append((state, code))
        return {"confirmation_code": "browser-only-completion"}

    def revoked(self, github_id):
        self.calls.append(github_id)


def test_auth_callback_and_signed_revocation(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_OAUTH_WEBHOOK_SECRET", "webhook-secret")
    get_settings.cache_clear()
    service = Service()
    service.calls = []
    app = create_app()
    app.dependency_overrides[get_github_linking] = lambda: service
    with TestClient(app) as client:
        body = {"person_id": str(uuid4()), "discord_id": "123"}
        assert client.post("/github/oauth/start", json=body).status_code == 401
        assert not service.calls
        result = client.post("/github/oauth/start", json=body, headers={"X-API-Key": "test-key"})
        assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
        assert (
            client.post(
                "/github/oauth/start",
                json={**body, "actor": "fake"},
                headers={"X-API-Key": "test-key"},
            ).status_code
            == 422
        )
        confirmed = client.post(
            "/github/oauth/confirm",
            json={**body, "confirmation_code": "browser-code"},
            headers={"X-API-Key": "test-key"},
        )
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "linked"
        assert confirmed.headers["Cache-Control"] == "no-store"
        assert (body["person_id"], "123", "browser-code") in service.calls
        callback = client.get("/github/oauth/callback?state=state&code=code")
        assert callback.status_code == 200 and "browser-only-completion" in callback.text
        assert callback.headers["Referrer-Policy"] == "no-referrer"
        data = json.dumps({"action": "revoked", "sender": {"id": 77}}).encode()
        headers = {"X-GitHub-Event": "github_app_authorization", "X-Hub-Signature-256": "invalid"}
        assert (
            client.post("/github/oauth/revoked", content=data, headers=headers).status_code == 401
        )
        assert 77 not in service.calls
        headers["X-Hub-Signature-256"] = (
            "sha256=" + hmac.new(b"webhook-secret", data, hashlib.sha256).hexdigest()
        )
        assert (
            client.post("/github/oauth/revoked", content=data, headers=headers).status_code == 204
        )
        assert 77 in service.calls


def test_callback_access_logging_redacts_codes_and_state():
    import logging
    from src.api.github_logging import GitHubCallbackLogFilter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %s',
        (
            "127.0.0.1",
            "GET",
            "/github/oauth/callback?state=SECRET-STATE&code=SECRET-CODE",
            "1.1",
            200,
        ),
        None,
    )
    assert GitHubCallbackLogFilter().filter(record)
    assert "SECRET" not in record.getMessage()
    assert "/github/oauth/callback" in record.getMessage()
