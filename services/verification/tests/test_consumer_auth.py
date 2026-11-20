import json

import pytest
from fastapi.testclient import TestClient
from platform_auth import generate_key

from src.api.app import create_app
from src.api.deps import get_email_sender, get_storage
from src.config import get_settings
from src.email.fake import FakeSender
from src.storage.in_memory import InMemoryVerificationStore


@pytest.mark.parametrize("scopes,expected", [(["verification:write"], 202), ([], 403)])
def test_scoped_gateway_key_can_send_without_bootstrap(monkeypatch, scopes, expected):
    token, prefix, key_hash = generate_key("vf_")
    monkeypatch.setenv(
        "CONSUMER_KEYS",
        json.dumps([{"name": "gateway", "prefix": prefix, "key_hash": key_hash, "scopes": scopes}]),
    )
    get_settings.cache_clear()
    sender, store = FakeSender(), InMemoryVerificationStore()
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_email_sender] = lambda: sender
    with TestClient(app) as client:
        result = client.post(
            "/verification/request-code",
            headers={"X-API-Key": token},
            json={"subject": "gateway-login:test", "email": "member@example.com"},
        )
    assert result.status_code == expected
    assert (store.get_code("gateway-login:test") is not None) == (expected == 202)


def test_bad_consumer_config_is_rejected_at_boot(monkeypatch):
    monkeypatch.setenv("CONSUMER_KEYS", "{bad-json")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="CONSUMER_KEYS"):
        create_app()
