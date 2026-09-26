import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from platform_auth import InMemoryKeyStore

from contracts.chat import ALLOWED_MODELS
from contracts.embed import ALLOWED_EMBED_MODELS
from src.api.deps import get_embedder, get_key_store, get_llm
from src.api.hashing import generate_key
from src.config import get_settings
from src.providers.base import EmbeddingResult, LLMResult

_API_KEY = "validation-bootstrap-key-value"
_OPENAI_KEY = "validation-private-upstream-key"
_PRIVATE_TEXT = "validation-private-request-text"


@pytest.fixture(params=["/embed", "/chat"])
def path(request):
    return request.param


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LLM_ENV", "local")
    monkeypatch.setenv("API_KEY", _API_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", _OPENAI_KEY)
    monkeypatch.setenv("CONSUMER_KEYS", "")
    get_settings.cache_clear()

    from src.api.app import create_app

    provider = Mock()
    provider.chat.return_value = LLMResult("ok", "claude-sonnet-4-6", "end_turn", 1, 1)
    provider.embed.return_value = EmbeddingResult(
        [[0.25] * 1536], "text-embedding-3-small", 1536, 1
    )
    store = InMemoryKeyStore()
    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: store
    app.dependency_overrides[get_llm] = lambda: provider
    app.dependency_overrides[get_embedder] = lambda: provider
    with TestClient(
        app, raise_server_exceptions=False, headers={"Content-Type": "application/json"}
    ) as test_client:
        yield test_client, provider, store


def _payload(path, text=_PRIVATE_TEXT):
    if path == "/embed":
        return {"inputs": [text]}
    return {"messages": [{"role": "user", "content": text}]}


def _assert_rejected(response, path, provider, capsys, status=422, secrets=()):
    assert response.status_code == status
    provider.chat.assert_not_called()
    provider.embed.assert_not_called()
    captured = capsys.readouterr()
    entries = [json.loads(line) for line in captured.out.splitlines() if line.startswith("{")]
    assert entries
    assert entries[-1]["path"] == path
    assert entries[-1]["status"] == status
    for private in (_PRIVATE_TEXT, _API_KEY, _OPENAI_KEY, *secrets):
        assert private not in response.text
        assert private not in captured.out
        assert private not in captured.err
    detail = response.json()["detail"]
    if status == 422:
        assert isinstance(detail, list)
        assert detail
        assert all(set(error) == {"type", "loc", "msg"} for error in detail)
    return detail


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_rejected_input_is_safe_422(client, path, capsys, number):
    test_client, provider, _ = client
    payload = {**_payload(path, float("nan")), "private": _PRIVATE_TEXT}
    raw = json.dumps(payload).replace("NaN", number)

    response = test_client.post(path, content=raw, headers={"X-API-Key": _API_KEY})

    detail = _assert_rejected(response, path, provider, capsys)
    loc = ["body", "inputs", 0] if path == "/embed" else ["body", "messages", 0, "content"]
    assert detail == [{"type": "string_type", "loc": loc, "msg": "Input should be a valid string"}]


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"], ids=["high-surrogate", "low-surrogate"])
def test_surrogate_in_rejected_model_is_safe_422(client, path, capsys, surrogate):
    test_client, provider, _ = client
    payload = {**_payload(path), "model": _PRIVATE_TEXT + surrogate}

    response = test_client.post(path, content=json.dumps(payload), headers={"X-API-Key": _API_KEY})

    detail = _assert_rejected(response, path, provider, capsys)
    models = ALLOWED_EMBED_MODELS if path == "/embed" else ALLOWED_MODELS
    assert detail == [
        {
            "type": "value_error",
            "loc": ["body", "model"],
            "msg": f"Value error, model must be one of {sorted(models)}",
        }
    ]


@pytest.mark.parametrize(
    "value,error_type,message",
    [
        (None, "missing", "Field required"),
        (_PRIVATE_TEXT, "list_type", "Input should be a valid list"),
        ([], "too_short", "List should have at least 1 item after validation, not 0"),
    ],
    ids=["missing", "wrong-type", "empty-list"],
)
def test_ordinary_validation_preserves_details(client, path, capsys, value, error_type, message):
    test_client, provider, _ = client
    field = "inputs" if path == "/embed" else "messages"
    payload = {"private": _PRIVATE_TEXT}
    if value is not None:
        payload[field] = value

    response = test_client.post(path, content=json.dumps(payload), headers={"X-API-Key": _API_KEY})

    detail = _assert_rejected(response, path, provider, capsys)
    assert detail == [{"type": error_type, "loc": ["body", field], "msg": message}]


@pytest.mark.parametrize("authenticated", [True, False])
def test_malformed_json_remains_safe_422(client, path, capsys, authenticated):
    test_client, provider, _ = client
    raw = json.dumps(_payload(path))[:-1]
    headers = {"X-API-Key": _API_KEY} if authenticated else {}

    response = test_client.post(path, content=raw, headers=headers)

    detail = _assert_rejected(response, path, provider, capsys)
    assert detail == [
        {"type": "json_invalid", "loc": ["body", len(raw)], "msg": "JSON decode error"}
    ]


@pytest.mark.parametrize("auth", ["missing", "invalid", "wrong-scope"])
@pytest.mark.parametrize("model", [float("nan"), "\ud800"], ids=["nan", "surrogate"])
def test_invalid_bodies_do_not_override_auth_denials(client, path, capsys, auth, model):
    test_client, provider, store = client
    key = None
    status = 401
    if auth == "invalid":
        key = "validation-invalid-key"
    elif auth == "wrong-scope":
        key, prefix, key_hash = generate_key()
        store.add(prefix=prefix, key_hash=key_hash, name="consumer", scopes=[])
        status = 403
    payload = {**_payload(path), "model": model}

    response = test_client.post(
        path,
        content=json.dumps(payload),
        headers={"X-API-Key": key} if key else {},
    )

    detail = _assert_rejected(
        response, path, provider, capsys, status=status, secrets=(key,) if key else ()
    )
    assert detail == (
        f"missing scope: {path[1:]}" if status == 403 else "Invalid or missing X-API-Key"
    )


def test_valid_text_and_ignored_extras_are_unchanged(client, path):
    test_client, provider, _ = client
    text = " NaN Infinity -Infinity 1e999 café 世界 "
    payload = {**_payload(path, text), "ignored": {"number": float("nan"), "surrogate": "\ud800"}}

    response = test_client.post(path, content=json.dumps(payload), headers={"X-API-Key": _API_KEY})

    assert response.status_code == 200
    if path == "/embed":
        provider.embed.assert_called_once()
        assert provider.embed.call_args.args[0].inputs == [text]
        provider.chat.assert_not_called()
    else:
        provider.chat.assert_called_once()
        assert provider.chat.call_args.args[0].messages[0].content == text
        provider.embed.assert_not_called()
