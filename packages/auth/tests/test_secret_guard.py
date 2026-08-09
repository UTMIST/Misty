"""The unwrap-at-the-boundary convention, enforced.

Services hold credentials as `pydantic.SecretStr` and must call
`.get_secret_value()` before handing them to this package. Forgetting does
raise — but on a missing `.encode()`/`.strip()`, which points at this library
rather than at the service's wiring. These tests pin the specific, actionable
error that replaces it.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from platform_auth.factory import build_auth
from platform_auth.memory_store import key_store_from_config
from platform_auth.secret_guard import reject_secret_wrapper


class _FakeSecret:
    """Any object exposing get_secret_value — the guard is duck-typed, so this
    stands in for SecretStr, SecretBytes, or another library's equivalent."""

    def __init__(self, value: str):
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


def test_plain_values_pass_through():
    # The guard must be invisible to correct callers.
    for ok in ("a-key", "", None, 0, [], {"a": 1}):
        reject_secret_wrapper(ok, param="whatever")


def test_secret_wrapper_is_rejected_with_an_actionable_message():
    with pytest.raises(TypeError) as exc:
        reject_secret_wrapper(_FakeSecret("s3cret"), param="get_env_key()")
    message = str(exc.value)
    assert "get_env_key()" in message  # names the offending parameter
    assert "get_secret_value()" in message  # names the fix
    assert "s3cret" not in message  # never echoes the value itself


def test_real_pydantic_secretstr_is_rejected():
    # The duck-typed check must actually cover the type it exists for.
    SecretStr = pytest.importorskip("pydantic").SecretStr
    with pytest.raises(TypeError, match="get_secret_value"):
        reject_secret_wrapper(SecretStr("s3cret"), param="get_env_key()")


# --- the two real call sites -------------------------------------------------


class _EmptyStore:
    def get_api_key_hash(self, prefix):
        return None

    def get_api_key_by_prefix(self, prefix):
        return None

    def touch_api_key_last_used(self, api_key_id):
        return None


def _client(get_env_key):
    deps = build_auth(lambda: _EmptyStore(), envelope="tt_", get_env_key=get_env_key)
    app = FastAPI()

    @app.get("/read")
    def read(_=Depends(deps.require_api_key)):
        return {"ok": True}

    # Default raise_server_exceptions=True: the guard's TypeError must reach the
    # test, not be swallowed into a 500.
    return TestClient(app)


def test_build_auth_rejects_a_wrapped_env_key():
    # Unguarded this raised AttributeError on the missing .encode(), which
    # reads like a bug in platform_auth rather than in the service's wiring.
    client = _client(lambda: _FakeSecret("env-key"))
    with pytest.raises(TypeError, match="get_env_key"):
        client.get("/read", headers={"X-API-Key": "env-key"})


def test_build_auth_still_accepts_a_plain_env_key():
    client = _client(lambda: "env-key")
    assert client.get("/read", headers={"X-API-Key": "env-key"}).status_code == 200


def test_build_auth_tolerates_none_env_key():
    # An unset API_KEY is a valid state — it just means no bootstrap key.
    client = _client(lambda: None)
    assert client.get("/read", headers={"X-API-Key": "anything"}).status_code == 401


def test_key_store_from_config_rejects_a_wrapped_value():
    with pytest.raises(TypeError, match="key_store_from_config"):
        key_store_from_config(_FakeSecret("[]"))


def test_key_store_from_config_still_accepts_a_plain_value():
    assert key_store_from_config("") is not None
    assert key_store_from_config(None) is not None
