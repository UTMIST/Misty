# llm — API reference

Base URL (local): `http://localhost:8002` · Swagger UI: `/docs` · Schema: `/openapi.json`

Two endpoints. Everything except `/health` requires `X-API-Key`.

| Method | Path | Scope | Description |
|---|---|---|---|
| POST | `/chat` | `chat` | Send chat turns to Claude, get a completion back |
| GET | `/health` | — | Liveness probe |

## Authentication

```
X-API-Key: llm_<prefix>_<secret>
```

A key is either the bootstrap env key (`API_KEY`, carries the `admin` wildcard) or a per-consumer key seeded from `CONSUMER_KEYS`. `admin` satisfies any scope check.

There is **no `X-Actor` header** and no `dev:spoof` scope. This is a service-to-service API; the audit actor is always the authenticated key's own name.

| Failure | Status |
|---|---|
| Missing or unparseable `X-API-Key` | 401 |
| Valid key lacking the `chat` scope | 403 — rejected **before** the provider is called, so no tokens are billed |

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

**502 is the one to check first on a fresh deploy.** Missing or wrong AWS credentials, a region without model access, and a model id the account can't serve all normalize to `ProviderUnavailable` → 502. The service boots and `/health` stays green regardless — the credential is only exercised on a real call.

---

## `GET /health`

Unauthenticated liveness probe. Railway's healthcheck path.

```json
{"status": "ok"}
```

Answers `200` without AWS credentials. A green healthcheck does **not** imply Bedrock calls work.

---

## Audit log

One JSON line per request with the resolved actor, endpoint, status, and duration. `/chat` additionally records the resolved `model`, and on success `input_tokens` / `output_tokens` (`request.state.audit_extra`). Prompt and completion text are never logged.
