# llm — architecture

Why this service is shaped the way it is. For *what it does* and how to run it, see the [README](../README.md); for *how to change it*, see [CONTRIBUTING.md](CONTRIBUTING.md).

## The choke-point principle

Every internal tool that wants an LLM — a docs helper bot, the `meeting` service's minutes generation, future dashboards — could call Bedrock directly. The reason none of them do:

- **Credentials live in one place.** AWS credentials and the model catalog are configured once here, not scattered across every bot and job that wants a completion.
- **Consumers are revocable individually.** Each holds its own scoped `llm_` key. Cutting one off is dropping its entry from `CONSUMER_KEYS` and redeploying — it doesn't touch anyone else.
- **The provider can change without touching consumers.** Re-pointing at a different Bedrock endpoint, or swapping the provider entirely, is a config change here. No consumer redeploys.
- **Consumers can be any language, anywhere.** The contract is HTTP + JSON, not an SDK.

This is the platform's [API-only principle](../../../docs/ARCHITECTURE.md#the-core-principle-a-source-of-truth-is-api-only) applied to a service that owns a *credential* rather than a *domain*. Nothing runs inside llm except the chat proxy: no queues, no scheduled jobs, no UI, no persistence.

## Layering

```
contracts/chat.py        Pydantic wire types (ChatRequest, ChatResponse, Usage)
       ▲
src/api/                 FastAPI — routing, auth, error→status mapping
       │  depends on
       ▼
src/providers/base.py    LLMRequest / LLMResult / LLMProvider Protocol
       ▲                 + ProviderError hierarchy
       │
src/providers/bedrock_converse.py   ─┐  two implementations,
src/providers/bedrock.py            ─┘  selected by config
```

The rules:

- `contracts/` imports nothing from `src/`.
- `src/providers/` imports no FastAPI. It speaks in dataclasses (`LLMRequest`/`LLMResult`), not Pydantic models — deliberately, so the wire format and the provider interface can drift apart without dragging each other along.
- `src/api/deps.py` is the **single wiring point**. The `@lru_cache` sits on the private `_key_store()` / `_provider()` builders; `get_key_store` and `get_llm` are thin uncached wrappers over them, and tests override those via `app.dependency_overrides`. The distinction matters when you need to reset state between tests — it's `deps._key_store.cache_clear()`, not `get_key_store.cache_clear()` (the wrapper has no `cache_clear`). See `tests/conftest.py`.

The mapping between the two type families happens in exactly one place, `src/api/routers/chat.py`. That's the whole job of the router: translate `ChatRequest` → `LLMRequest`, catch `ProviderError`, translate `LLMResult` → `ChatResponse`.

## The neutral provider boundary

`LLMProvider` is a one-method Protocol:

```python
class LLMProvider(Protocol):
    def chat(self, request: LLMRequest) -> LLMResult: ...
```

Everything vendor-specific lives behind it. The router never imports `boto3` or `anthropic`, never sees a Bedrock model id, and never knows which provider is configured. That is what makes the test suite run with no AWS credentials and no network: a `_FakeProvider` satisfying the same Protocol is injected via `dependency_overrides`, and the router cannot tell the difference.

Provider selection is a `Callable` registry in `src/providers/registry.py` — `LLM_PROVIDER` names a key, the builder constructs the implementation from `Settings`. Adding a backend is a new module plus one dict entry.

## The two Bedrock providers

Both bill as **standard Amazon Bedrock** (AWS credits apply) — deliberately not Claude Platform on AWS / Marketplace. They differ only in which Bedrock endpoint they call and how model ids are formed:

| | `bedrock-converse` (default) | `bedrock` |
|---|---|---|
| Endpoint | `bedrock-runtime` **Converse** API | **Mantle Messages** via `AnthropicBedrockMantle` |
| Model ids | US-regional cross-region inference profiles (`us.anthropic.claude-sonnet-4-6`) | Global model ids |
| Requires | Regional profile access | Global/Messages model access |

`bedrock-converse` is the default because **this account's model access is US-regional inference profiles**, which the Messages endpoint cannot target. It maps neutral model names (`claude-sonnet-4-6`) to profile ids through an explicit table. If model access on the AWS account changes, that's the first thing to revisit.

## Error normalization

`src/providers/base.py` defines three provider errors; the router maps each to one status:

| `ProviderError` subclass | Status | Cause |
|---|---|---|
| `ProviderRateLimited` | 429 | Upstream returned 429 |
| `ProviderTimeout` | 504 | Connection or timeout failure |
| `ProviderUnavailable` | 502 | Upstream 5xx, auth failure, other status |

`ProviderError` itself is the catch-all → 502. A provider that raises a raw vendor exception is a bug: the router's `except ProviderError` won't catch it, and it becomes a 500.

Validation failures never reach the provider at all — Pydantic rejects an empty `messages` list or an unknown `model` with 422 before the handler body runs. Likewise, a valid key **without** the `chat` scope is rejected with 403 by the dependency, before any provider call. **No budget is spent on a request that was going to fail.** That ordering is deliberate.

## Why no persistence

There is no database, no Alembic, no `docker compose` for a DB, and no conversation storage. Consumers keep their own history and send the full turn list on each call.

The trade: a chattier wire format, in exchange for a service that has no schema to migrate, no state to lose on restart, and no retention policy to reason about for what is often sensitive text. For a service whose entire job is proxying one call, that's the right side of the trade.

The same reasoning drives the key model. Keys are seeded at boot from a `CONSUMER_KEYS` JSON array into an in-memory store satisfying `platform_auth`'s `ApiKeyStore` protocol — the stateless equivalent of team-tracking's `api_keys` table. Rotating a key is a variable edit plus a redeploy. If that ever becomes too coarse, a DB-backed store drops in behind the same protocol with no route changes.

## What's deliberately absent

- **Streaming.** `/chat` returns the completed response. No SSE, no token streaming. Consumers that want progressive output would need a second endpoint and a different contract.
- **RAG / retrieval.** No embedding, no document search. That belongs in the consumer.
- **Prompt templates.** The service does not own prompts. `meeting` composes its own minutes prompt and sends it as `system` + `messages`; llm just relays. This keeps prompt iteration in the service that cares about the output.
