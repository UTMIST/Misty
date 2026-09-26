import threading

import anyio
import httpx
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.deps import get_embedder
from src.api.routers import embed as embed_router
from src.providers.base import EmbeddingResult


def test_health_ok_without_auth_or_local_openai_key(monkeypatch):
    monkeypatch.setenv("LLM_ENV", "local")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_embed_concurrency_does_not_block_health(monkeypatch):
    monkeypatch.setenv("LLM_ENV", "local")
    monkeypatch.setenv("API_KEY", "synthetic-health-test-key")
    release = threading.Event()

    async def exercise():
        full = anyio.Event()
        calls = 0

        def record_call():
            nonlocal calls
            calls += 1
            if calls == 8:
                full.set()

        class BlockingEmbedder:
            def embed(self, request):
                anyio.from_thread.run_sync(record_call)
                release.wait()
                return EmbeddingResult(
                    vectors=[[0.25] * 1536],
                    model="text-embedding-3-small",
                    dimensions=1536,
                    input_tokens=1,
                )

        limiter = anyio.CapacityLimiter(embed_router._EMBED_LIMITER.total_tokens)
        monkeypatch.setattr(embed_router, "_EMBED_LIMITER", limiter)
        app = create_app()
        embedder = BlockingEmbedder()
        app.dependency_overrides[get_embedder] = lambda: embedder
        responses = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:

            async def post_embed():
                responses.append(
                    await client.post(
                        "/embed",
                        json={"inputs": ["synthetic input"]},
                        headers={"X-API-Key": "synthetic-health-test-key"},
                    )
                )

            async with anyio.create_task_group() as tasks:
                try:
                    for _ in range(9):
                        tasks.start_soon(post_embed)
                    with anyio.fail_after(10):
                        await full.wait()
                        await anyio.wait_all_tasks_blocked()
                        assert calls == 8
                        assert limiter.statistics().tasks_waiting == 1
                        assert anyio.to_thread.current_default_thread_limiter().borrowed_tokens == 0
                        health = await client.get("/health")
                        assert health.status_code == 200
                        assert health.json() == {"status": "ok"}
                        assert calls == 8
                finally:
                    release.set()
            assert calls == 9
            assert len(responses) == 9
            assert all(response.status_code == 200 for response in responses)

    anyio.run(exercise)
