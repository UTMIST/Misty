# llm — architecture

Why this service is shaped the way it is. For *what it does* and how to run it, see the [README](../README.md); for *how to change it*, see [CONTRIBUTING.md](CONTRIBUTING.md).

## The choke-point principle

Every internal tool that wants model access — a docs helper bot, the `meeting` service's minutes generation, future dashboards — could call Bedrock or OpenAI directly. The reason none of them do:

- **Credentials live in one place.** AWS/OpenAI credentials and the model catalog are configured once here, not scattered across every bot and job.
- **Consumers are revocable individually.** Each holds its own scoped `llm_` key. Cutting one off is dropping its entry from `CONSUMER_KEYS` and redeploying — it doesn't touch anyone else.
- **The chat provider can change without touching consumers.** Re-pointing at a different Bedrock endpoint is a config change here. No consumer redeploys.
- **Consumers can be any language, anywhere.** The contract is HTTP + JSON, not an SDK.

This is the platform's [API-only principle](../../../docs/ARCHITECTURE.md#the-core-principle-a-source-of-truth-is-api-only) applied to a service that owns *credentials* rather than a *domain*. Nothing runs inside llm except the chat and embedding proxy: no job queues, no scheduled jobs, no UI, no persistence.

## Layering

```
contracts/chat.py        Pydantic wire types (ChatRequest, ChatResponse, Usage)
contracts/embed.py       Pydantic wire types (EmbedRequest, EmbedResponse, Embedding)
       ▲
src/api/                 FastAPI — routing, auth, error→status mapping
       │  depends on
       ▼
src/providers/base.py    LLMRequest / LLMResult / LLMProvider Protocol
       ▲                 EmbeddingRequest / EmbeddingResult / EmbeddingProvider Protocol
       │                 + one ProviderError hierarchy shared by both
       │
src/providers/bedrock_converse.py   ─┐  chat implementations,
src/providers/bedrock.py            ─┘  selected by LLM_PROVIDER
src/providers/openai_embed.py          OpenAI embeddings; model set by EMBED_MODEL
```

The rules:

- `contracts/` imports nothing from `src/`.
- `src/providers/` imports no FastAPI. It speaks in chat/embedding dataclasses, not Pydantic models — deliberately, so the wire format and the provider interface can drift apart without dragging each other along.
- `src/api/deps.py` is the **single wiring point**. The `@lru_cache` sits on the private `_key_store()` / `_provider()` / `_embedder()` builders; `get_key_store`, `get_llm`, and `get_embedder` are thin uncached wrappers, and tests override those via `app.dependency_overrides`. The distinction matters when you need to reset state between tests — it's `deps._key_store.cache_clear()`, not `get_key_store.cache_clear()` (the wrapper has no `cache_clear`). See `tests/conftest.py`.

The mapping between the two type families happens in exactly one place per capability: `src/api/routers/chat.py` and `src/api/routers/embed.py`. That's the whole job of a router: translate the wire model to the neutral one, catch `ProviderError`, translate the result back.

## The neutral provider boundary

`LLMProvider` is a one-method Protocol:

```python
class LLMProvider(Protocol):
    def chat(self, request: LLMRequest) -> LLMResult: ...
```

Vendor-specific model resolution lives behind the Protocol. Routers never import `boto3`, `anthropic`, or `openai`; they treat the returned provider model id as data. Tests inject fakes through `dependency_overrides`, so they need no provider credentials or network.

Chat provider selection is a `Callable` registry in `src/providers/registry.py` — `LLM_PROVIDER` names a key, the builder constructs the implementation from `Settings`. Adding a chat backend is a new module plus one dict entry.

## Embedding capability

`EmbeddingProvider.embed(EmbeddingRequest) -> EmbeddingResult` is a separate Protocol:
chat providers should not implement an unrelated vector operation. Both capabilities use
neutral dataclasses, the same `ProviderError` hierarchy, and injected clients in tests.

`get_embedder` wires the OpenAI implementation through `src/providers/registry.py`.
`LLM_PROVIDER` selects only the Bedrock chat backend; embeddings have no provider selector
or automatic fallback. The embedding SDK client is built lazily on first use.

Float-array vectors are validated in the raw JSON response before SDK parsing, which
would otherwise coerce boolean components into floats and hide malformed upstream data.
Decoded base64 vectors pass the same validation after parsing.

The async `/embed` route offloads SDK work; `/health` is also async.
See [API.md](API.md#batching-and-timeouts) for concurrency and timeout behavior.

## Initial embedding model

**#173 decision for #174:** William and Ethan chose OpenAI using the existing OpenAI key,
avoiding a separate Bedrock embedding account allocation. The initial profile is fixed as:

| Backend | `EMBED_MODEL` | Resolved response `model` | Dimensions |
|---|---|---|---|
| OpenAI | `openai-embed-3-small` | `text-embedding-3-small` | **1536** |

`openai-embed-3-large` is also supported, explicitly requested at **1536** rather than its
native 3072 dimensions. Both widths fit pgvector's 2,000-dimension `vector` index limit;
`_MODEL_DIMENSIONS` in `openai_embed.py` is the provider's source of truth.

Consumers must check the returned model and dimensions before persisting. Changing models
requires re-embedding even when widths match: vectors from different models are not
interchangeable. The provider rejects missing/mismatched model metadata and wrong-width
or invalid vectors. Schema, chunking, and indexing work remain outside this service.

## The two Bedrock chat providers

Both bill as **standard Amazon Bedrock** (AWS credits apply) — deliberately not Claude Platform on AWS / Marketplace. They differ only in which Bedrock endpoint they call and how model ids are formed:

| | `bedrock-converse` (default) | `bedrock` |
|---|---|---|
| Endpoint | `bedrock-runtime` **Converse** API | **Mantle Messages** via `AnthropicBedrockMantle` |
| Model ids | US-regional cross-region inference profiles (`us.anthropic.claude-sonnet-4-6`) | Global model ids |
| Requires | Regional profile access | Global/Messages model access |

`bedrock-converse` is the default because **this account's model access is US-regional inference profiles**, which the Messages endpoint cannot target. It maps neutral model names (`claude-sonnet-4-6`) to profile ids through an explicit table. If model access on the AWS account changes, that's the first thing to revisit.

## Error normalization

`src/providers/base.py` defines normalized provider errors; the routers map them to HTTP:

| `ProviderError` subclass | Status | Cause |
|---|---|---|
| `ProviderRateLimited` | 429 | Upstream returned 429 |
| `ProviderTimeout` | 504 | Connection or timeout failure |
| `ProviderUnavailable` | 502 | Upstream error or malformed output |
| `ProviderUnsupportedModel` | 422 (`/embed`) | Model unsupported by the OpenAI embedding adapter |
| `ProviderNotConfigured` | 503 (`/embed`) | Missing OpenAI credential in local development |

`ProviderError` itself is the catch-all → 502. A provider that raises a raw vendor exception is a bug: the router's `except ProviderError` won't catch it, and it becomes a 500.

Validation and scope checks reject locally checkable failures before any paid call.
The app's request-validation handler omits rejected inputs and error context so non-finite
numbers or invalid Unicode in those values cannot turn a validation failure into a 500.
See [API.md](API.md#validation-errors) for the shared field-error format and
[embedding limits and HTTP errors](API.md#post-embed).

## Why no persistence

There is no database, no Alembic, no `docker compose` for a DB, and no conversation storage. Consumers keep their own history and send the full turn list on each call.

The trade: a chattier wire format, in exchange for a service that has no schema to migrate, no state to lose on restart, and no retention policy to reason about for what is often sensitive text. For a service whose entire job is proxying one call, that's the right side of the trade.

The same reasoning drives the key model. Keys are seeded at boot from a `CONSUMER_KEYS` JSON array into an in-memory store satisfying `platform_auth`'s `ApiKeyStore` protocol — the stateless equivalent of team-tracking's `api_keys` table. Rotating a key is a variable edit plus a redeploy. If that ever becomes too coarse, a DB-backed store drops in behind the same protocol with no route changes.

## What's deliberately absent

- **Streaming.** `/chat` returns the completed response. No SSE, no token streaming. Consumers that want progressive output would need a second endpoint and a different contract.
- **Retrieval.** `/embed` returns vectors only: no vector store, similarity search,
  chunking, or document ingestion. Those belong to the consumer that owns the corpus.
- **Prompt templates.** The service does not own prompts. `meeting` composes its own minutes prompt and sends it as `system` + `messages`; llm just relays. This keeps prompt iteration in the service that cares about the output.
