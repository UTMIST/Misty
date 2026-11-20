import asyncio

import pytest
from starlette.requests import Request
from starlette.responses import Response

from src.api import middleware
from src.api.app import create_app
from src.config import Settings, get_settings


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("GATEWAY_ENV", "local")
    monkeypatch.setenv("EDGE_REQUESTS_PER_MINUTE", "121")
    monkeypatch.setenv("LOGIN_STATUS_PER_MINUTE", "1")
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


async def request(app, receive, *, method="POST", path="/v1/auth/probe", http_version="1.1"):
    messages = []

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": http_version,
        "method": method,
        "scheme": "http",
        "path": path,
        "query_string": b"",
        "headers": [],
        "client": ("192.0.2.1", 12345),
        "server": ("test", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=1)
    return messages


def assert_private_status(messages, status, *, http_version="1.1"):
    response = next(m for m in messages if m["type"] == "http.response.start")
    assert response["status"] == status
    assert dict(response["headers"])[b"cache-control"] == b"no-store"
    headers = dict(response["headers"])
    if status in {408, 413, 429} and http_version in {"1.0", "1.1"}:
        assert headers.get(b"connection") == b"close"
    else:
        assert b"connection" not in headers


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_throttled_login_is_rejected_without_reading_body(app, monkeypatch, enabled):
    monkeypatch.setenv("EMAIL_LOGIN_ENABLED", enabled)
    get_settings.cache_clear()

    async def never_read():
        pytest.fail("a throttled request must not wait for its body")

    async def exercise():
        for _ in range(121):
            assert_private_status(await request(app, never_read, method="GET"), 404)
        assert_private_status(await request(app, never_read, path="/v1/auth/email/start"), 429)

    asyncio.run(exercise())


@pytest.mark.parametrize("chunk", [None, b"x", b""])
def test_body_deadline_is_not_extended_by_trickled_chunks(app, monkeypatch, chunk):
    # Shorten the production deadline so stalled and trickling streams are cheap to test.
    monkeypatch.setattr(middleware, "LOGIN_BODY_TIMEOUT_SECONDS", 0.05)

    @app.post("/v1/auth/probe")
    async def unreachable():
        pytest.fail("an incomplete body must not reach the router")

    async def slow_receive():
        if chunk is None:
            await asyncio.Future()
        await asyncio.sleep(0.01)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def exercise():
        assert_private_status(await request(app, slow_receive), 408)

    asyncio.run(exercise())


def test_chunked_body_is_preserved(app):
    body = b'{"example":"' + b"x" * 4082 + b'"}'
    assert len(body) == 4096

    @app.post("/v1/auth/probe")
    async def echo(request: Request):
        return Response(await request.body())

    async def exercise():
        chunks = iter([b"", body[:1000], b"", body[1000:]])

        async def receive():
            chunk = next(chunks)
            return {"type": "http.request", "body": chunk, "more_body": chunk != body[1000:]}

        messages = await request(app, receive)
        assert_private_status(messages, 200)
        assert b"".join(m.get("body", b"") for m in messages) == body

    asyncio.run(exercise())


@pytest.mark.parametrize("http_version", ["1.0", "1.1", "2"])
def test_chunked_oversized_body_is_rejected_before_router(app, http_version):
    @app.post("/v1/auth/probe")
    async def unreachable():
        pytest.fail("an oversized body must not reach the router")

    async def receive():
        return {"type": "http.request", "body": b"x" * 2049, "more_body": True}

    async def exercise():
        assert_private_status(
            await request(app, receive, http_version=http_version), 413, http_version=http_version
        )

    asyncio.run(exercise())
