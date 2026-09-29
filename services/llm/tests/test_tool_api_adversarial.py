import json
import random
import socket

import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_llm
from src.config import get_settings
from src.providers.base import LLMResult, LLMTextBlock, LLMToolUseBlock


class _Provider:
    def __init__(self):
        self.requests = []
        self.result = LLMResult("ok", "claude-sonnet-4-6", "end_turn", 1, 1, [LLMTextBlock("ok")])

    def chat(self, request):
        self.requests.append(request)
        return self.result


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in this test")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("LLM_ENV", "local")
    monkeypatch.setenv("API_KEY", "offline-http-audit-key")
    monkeypatch.setenv("CONSUMER_KEYS", "")
    get_settings.cache_clear()
    from src.api.app import create_app

    provider = _Provider()
    app = create_app()
    app.dependency_overrides[get_llm] = lambda: provider
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, provider


def _history():
    return {
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "AUDIT-PRIVATE-PROMPT"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "old_call", "name": "lookup", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "old_call", "content": None}],
            },
        ],
    }


def _post(client, payload):
    return client.post(
        "/chat",
        content=json.dumps(payload, ensure_ascii=True),
        headers={"X-API-Key": "offline-http-audit-key", "Content-Type": "application/json"},
    )


def _json_values():
    rng = random.Random(70)
    atoms = [None, False, True, 0, -0.0, 1.0, 2**80, 1e-200, "", "e\u0301 雪", "\n\t\x00"]

    def value(depth):
        if depth == 0 or rng.randrange(3) == 0:
            return rng.choice(atoms)
        if rng.randrange(2):
            return [value(depth - 1) for _ in range(rng.randrange(4))]
        return {f"key{i}": value(depth - 1) for i in range(rng.randrange(4))}

    return atoms + [value(4) for _ in range(40)]


@pytest.mark.parametrize("value", _json_values())
def test_json_types_and_values_survive_both_http_translation_directions(api, value):
    client, provider = api
    payload = _history()
    payload["messages"][1]["content"][0]["input"] = {"value": value}
    payload["messages"][2]["content"][0]["content"] = value
    provider.result = LLMResult(
        "",
        "claude-sonnet-4-6",
        "tool_use",
        1,
        1,
        [LLMToolUseBlock("new_call", "lookup", {"v": value})],
    )
    response = _post(client, payload)
    assert response.status_code == 200
    assert len(provider.requests) == 1
    received = provider.requests[0]

    def normalized(item):
        return json.dumps(item, ensure_ascii=False, sort_keys=True)

    assert normalized(received.messages[1].content[0].input["value"]) == normalized(value)
    assert normalized(received.messages[2].content[0].content) == normalized(value)
    assert normalized(response.json()["content_blocks"][0]["input"]["v"]) == normalized(value)


@pytest.mark.parametrize(
    "path,bad",
    [
        (("tools", 0, "name"), True),
        (("tools", 0, "input_schema"), {"type": "object", "required": [[]]}),
        (("messages", 1, "content", 0, "id"), 1),
        (("messages", 1, "content", 0, "input"), False),
        (("messages", 1, "content", 0, "input"), {"nested": [float("nan")]}),
        (("messages", 2, "content", 0, "content"), {"nested": [float("inf")]}),
        (("messages", 2, "content", 0, "content"), {"AUDIT-PRIVATE-KEY\ud800": "value"}),
        (("messages", 2, "content", 0, "is_error"), "false"),
        (("messages", 2, "content", 0, "is_error"), 0),
        (("messages", 2, "content", 0, "type"), "AUDIT-PRIVATE-TYPE"),
        (("messages", 2, "content", 0, "AUDIT-PRIVATE-EXTRA"), "AUDIT-PRIVATE-VALUE"),
    ],
)
def test_malformed_nested_values_are_private_and_rejected_before_inference(api, capsys, path, bad):
    client, provider = api
    payload = _history()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad
    response = _post(client, payload)
    assert response.status_code == 422
    assert provider.requests == []
    assert "AUDIT-PRIVATE" not in response.text
    assert "AUDIT-PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize("wrong_position", [0, 1, 3])
def test_results_cannot_be_moved_out_of_the_immediately_following_turn(api, wrong_position):
    client, provider = api
    payload = _history()
    result = payload["messages"].pop()
    payload["messages"].append({"role": "user", "content": "a new question"})
    payload["messages"].insert(wrong_position, result)
    response = _post(client, payload)
    assert response.status_code == 422
    assert provider.requests == []


def test_cyclic_neutral_tool_input_becomes_a_private_provider_failure(api, capsys):
    client, provider = api
    data = {"AUDIT-PRIVATE": []}
    data["AUDIT-PRIVATE"].append(data)
    provider.result = LLMResult(
        "", "claude-sonnet-4-6", "tool_use", 1, 1, [LLMToolUseBlock("new_call", "lookup", data)]
    )
    response = _post(client, _history())
    assert response.status_code == 502
    assert response.json() == {"detail": "LLM provider error"}
    assert "AUDIT-PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"body": b"{}"},
        {"body": b"{}", "headers": {}},
        {"body": b"{}", "status_code": 200},
        {"headers": {}, "status_code": 200},
    ],
)
def test_incomplete_parser_event_envelopes_are_validation_errors(response):
    from src.providers.raw_responses import validate_converse_response

    with pytest.raises(ValueError, match="envelope"):
        validate_converse_response(response, None)
