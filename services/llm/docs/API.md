# llm — API reference

Base URL (local): `http://localhost:8002` · Swagger UI: `/docs` · Schema: `/openapi.json`

Three endpoints. Everything except `/health` requires `X-API-Key`.

| Method | Path | Scope | Description |
|---|---|---|---|
| POST | `/chat` | `chat` | Send chat turns to Claude, get a completion back |
| POST | `/embed` | `embed` | Turn a batch of strings into vectors |
| GET | `/health` | — | Liveness probe |

`chat` and `embed` are **independent scopes**; neither grants the other. `admin` satisfies both.

## Authentication

```
X-API-Key: llm_<prefix>_<secret>
```

A key is either the bootstrap env key (`API_KEY`, carries the `admin` wildcard) or a per-consumer key seeded from `CONSUMER_KEYS`. `admin` satisfies any scope check.

There is **no `X-Actor` header** and no `dev:spoof` scope. This is a service-to-service API; the audit actor is always the authenticated key's own name.

| Failure | Status |
|---|---|
| Missing or unparseable `X-API-Key` | 401 |
| Valid key lacking the endpoint's scope (`chat` / `embed`) | 403 — rejected **before** the provider is called, so no tokens are billed |

## Validation errors

Request field and JSON syntax errors return **422** with a `detail` list. Each item
contains `loc`, `msg`, and `type`; rejected `input` values and error context are omitted.
This format applies to both `/chat` and `/embed` without echoing the raw request body.

---

## `POST /chat`

**Request** (`ChatRequest`, `contracts/chat.py`):

| Field | Type | Default | Constraints |
|---|---|---|---|
| `messages` | `[{role, content}]` | — | Required, **min 1**. `role` is `user` or `assistant`; `content` min length 1. |
| `system` | string \| null | `null` | Optional system prompt |
| `model` | string \| null | `LLM_MODEL` | Must be `claude-sonnet-4-6` or `claude-opus-4-6` |
| `max_tokens` | int | `16000` | `1`–`64000` |
| `thinking` | bool \| null | `THINKING_DEFAULT` | Extended thinking. `null` means "use the server default", which is **not** the same as `false`. |

`model` is validated against `ALLOWED_MODELS` in `contracts/chat.py`. A model the AWS account can serve but that isn't in that set is a **422**, not a passthrough — adding a model means editing that constant.

**Response** (`ChatResponse`) — `200`:

```json
{
  "content": "...",
  "model": "us.anthropic.claude-sonnet-4-6",
  "stop_reason": "end_turn",
  "usage": {"input_tokens": 412, "output_tokens": 1288}
}
```

`model` is what the provider actually served, which may be more specific than what you asked for (the Converse provider resolves a neutral name to a regional inference profile).

**Example:**

```bash
curl -sS http://localhost:8002/chat \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
        "system": "You write terse meeting minutes.",
        "messages": [{"role": "user", "content": "Summarize: ..."}],
        "max_tokens": 2000,
        "thinking": false
      }'
```

### Multi-turn

The service is stateless — it stores no conversation. To continue a conversation, send the whole turn list:

```json
{"messages": [
  {"role": "user",      "content": "What's the deploy process?"},
  {"role": "assistant", "content": "Merge to staging..."},
  {"role": "user",      "content": "And for production?"}
]}
```

### Errors

| Condition | Status | Detail |
|---|---|---|
| Empty `messages`, empty `content`, unknown `model`, `max_tokens` out of range | 422 | Pydantic validation error |
| Missing/invalid `X-API-Key` | 401 | — |
| Valid key without `chat` | 403 | — |
| Provider rate limited (upstream 429) | 429 | `LLM provider rate limited` |
| Provider timeout | 504 | `LLM provider timeout` |
| Any other provider failure (upstream 5xx, auth, config) | 502 | `LLM provider error` |

**502 is the one to check first on a fresh deploy.** Missing or wrong AWS credentials, a region without model access, and a model id the account can't serve all normalize to `ProviderUnavailable` → 502. Once boot checks pass, `/health` does not test these credentials or model access — they are only exercised on a real call.

---

## `POST /embed`

Batch text-to-vector for the retrieval layer. Stateless like `/chat`: the service
embeds and returns, it stores nothing.

**Request** (`EmbedRequest`, `contracts/embed.py`):

| Field | Type | Default | Constraints |
|---|---|---|---|
| `inputs` | `[string]` | — | Required, **1–96** items. Each non-empty after stripping, each ≤ 32,000 characters. |
| `model` | string \| null | `EMBED_MODEL` | `openai-embed-3-small` (default) or `openai-embed-3-large`. Omitted or `null` uses the configured default. |

`model` is an allowlist (`ALLOWED_EMBED_MODELS`), same as `/chat` — an unlisted model is a **422**, not a passthrough. Unknown fields are silently ignored, matching `/chat`.

The batch must also fit `EMBED_MAX_REQUEST_CHARS` (default **400,000** total characters).
These are coarse input guards, **not exact token or cost limits**: token-dense text can
still exceed OpenAI's token limits after passing local validation.

**Example** (requires the service's `OPENAI_API_KEY` for a real call):

```bash
curl -sS http://localhost:8002/embed \
  -H "X-API-Key: dev-api-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"inputs": ["First passage", "Second passage"], "model": "openai-embed-3-small"}'
```

**Response** (`EmbedResponse`) — `200`. Vectors below are shortened for illustration;
actual vectors contain **1536 finite numeric values** each.

```json
{
  "embeddings": [
    {"index": 0, "vector": [0.0123, -0.0456]},
    {"index": 1, "vector": [0.0789, -0.0012]}
  ],
  "model": "text-embedding-3-small",
  "dimensions": 1536,
  "usage": {"input_tokens": 42}
}
```

There is one embedding per input, in input order, with zero-based `index` values.
`model` is the resolved OpenAI model id, not the neutral request alias:

| Request model | Response `model` | Dimensions |
|---|---|---|
| `openai-embed-3-small` | `text-embedding-3-small` | 1536 |
| `openai-embed-3-large` | `text-embedding-3-large` | 1536 (explicitly requested) |

**Check model and dimensions before persisting.** Equal widths do not make vectors from
different models interchangeable. `usage.input_tokens` is the upstream-reported batch
usage, or `0` when unavailable.

### Batching and timeouts

The whole batch uses **one OpenAI SDK call**, with automatic SDK retries disabled.
Embeddings bill through **OpenAI, not AWS credits**. There is no provider fallback.

At most **8** embedding provider calls run concurrently per service process; surplus
requests wait on the event loop. `REQUEST_TIMEOUT_S` (default 60) configures the SDK's
timeout phases, **not a hard whole-HTTP deadline**. Limiter waiting is not included.

### Errors

| Condition | Status | Detail |
|---|---|---|
| Empty `inputs`, blank string, over-long input, >96 items, or unlisted `model` | 422 | Pydantic validation error |
| Batch exceeds `EMBED_MAX_REQUEST_CHARS` (default 400,000) | 422 | Rejected before an upstream call; split the batch |
| Missing/invalid `X-API-Key` | 401 | — |
| Valid key without `embed` | 403 | — |
| Missing `OPENAI_API_KEY` in local development | 503 | Not configured; no upstream call |
| Provider rate limited (upstream 429) | 429 | `embedding provider rate limited` |
| Provider timeout or connection failure | 504 | `embedding provider timeout` |
| Other upstream error or malformed response, including missing/mismatched model metadata or wrong-width/non-numeric/non-finite vectors | 502 | `embedding provider error` |

For **422**, Pydantic field errors use a `detail` list; the aggregate character limit uses
a `detail` string. Invalid vectors are rejected, not returned as a successful batch.

A present but invalid OpenAI key becomes **502**, not **503**. See
[deployment](DEPLOYMENT.md#embeddings-post-embed) for local/non-local startup requirements.

---

## `GET /health`

Unauthenticated liveness probe. Railway's healthcheck path.

```json
{"status": "ok"}
```

After the [credential boot checks](DEPLOYMENT.md#variables), this answers `200` without
provider calls. A green healthcheck does **not** imply Bedrock chat or OpenAI embeddings work.

---

## Audit log

One JSON line per request with the attested actor, endpoint, status, and duration.
Requests reaching provider work record the selected **neutral model alias** from the
request or configuration, not the resolved model id returned in the response. `/chat`
adds `input_tokens` / `output_tokens` on success. `/embed` adds `input_count` / `input_chars`,
and on success `dimensions` / `input_tokens` (`request.state.audit_extra`). Prompt,
completion, and embedded text are never logged, and neither are the vectors.
