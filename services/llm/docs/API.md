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

## Validation errors

Request field and JSON syntax errors return **422** with a `detail` list. Each item
contains `loc`, `msg`, and `type`; rejected `input` values and error context are omitted.
Unknown discriminator values are not echoed, and forbidden extra-field locations
identify the parent object rather than the caller-supplied field name.
Text sent upstream must be valid UTF-8; invalid text is rejected before a provider call.

---

## `POST /chat`

**Request** (`ChatRequest`, `contracts/chat.py`):

| Field | Type | Default | Constraints |
|---|---|---|---|
| `messages` | `[{role, content}]` | — | Required, **min 1**. `role` is `user` or `assistant`; `content` is a non-empty string or a non-empty list of the structured blocks below. |
| `system` | string \| null | `null` | Optional system prompt |
| `model` | string \| null | `LLM_MODEL` | Must be `claude-sonnet-4-6` or `claude-opus-4-6` |
| `max_tokens` | int | `16000` | `1`–`64000` |
| `thinking` | bool \| null | `THINKING_DEFAULT` | Extended thinking. `null` means "use the server default", which is **not** the same as `false`. |
| `tools` | tool definitions \| null | `null` | Optional client-executed tools. Omitted, `null`, and `[]` mean no tool definitions; at most 32 definitions. |

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

### Client-executed tools

Both Bedrock providers support this contract. Supplying tools or structured message
content opts into block-level responses. Existing string-message requests without
tools retain the response above, with no extra `null` or empty fields.

A tool definition contains `name`, an object-typed JSON `input_schema`, and an
optional non-empty `description`. Names are unique, 1–64 characters, using only
ASCII letters, digits, `_`, and `-`. A description is at most 4,096 characters.
The schema must have `"type": "object"`; schema references are not fetched or
resolved by this service. Structural validation is not a complete JSON Schema or
model-specific schema-compatibility check. Tool definitions and structured blocks
reject unknown fields and coercion; the legacy chat/message wrappers keep their
existing extra-field behavior.

```json
{
  "model": "claude-sonnet-4-6",
  "thinking": false,
  "messages": [{"role": "user", "content": "Find the deployment guide."}],
  "tools": [{
    "name": "search_documents",
    "description": "Search documents the caller is authorized to retrieve.",
    "input_schema": {
      "type": "object",
      "properties": {"query": {"type": "string"}},
      "required": ["query"]
    }
  }]
}
```

A tool request can produce an empty visible `content` with `stop_reason: "tool_use"`:

```json
{
  "content": "",
  "model": "us.anthropic.claude-sonnet-4-6",
  "stop_reason": "tool_use",
  "usage": {"input_tokens": 120, "output_tokens": 25},
  "content_blocks": [{
    "type": "tool_use",
    "id": "lookup_1",
    "name": "search_documents",
    "input": {"query": "deployment guide"}
  }]
}
```

The service has **not** executed `search_documents`. The authenticated consumer
must validate and authorize each requested operation, execute permitted tools,
and submit another `/chat` request. Preserve the original messages, append an
`assistant` message whose `content` is the returned `content_blocks`, then append
a `user` message containing the results:

```json
{
  "role": "user",
  "content": [{
    "type": "tool_result",
    "tool_use_id": "lookup_1",
    "content": {"matches": []},
    "is_error": false
  }]
}
```

Keep the same tool definitions, system prompt, thinking setting, and neutral
request model during the exchange. Do not copy the provider-specific response
`model` into the request's model allowlist. The consumer owns round limits,
timeouts, spending limits, tool allowlisting, and document/person/channel access.
Tool definitions and model-generated arguments are not authorization credentials.
Service audit logs exclude definitions, arguments, results, reasoning, signatures,
and redacted continuation data.

#### Structured content blocks

| `type` | Fields | Allowed role |
|---|---|---|
| `text` | Non-empty `text` string | `user`, `assistant` |
| `reasoning` | `text` string, non-empty `signature` string | `assistant` |
| `redacted_reasoning` | Non-empty canonical base64 `data` | `assistant` |
| `tool_use` | `id`, `name`, JSON-object `input` | `assistant` |
| `tool_result` | `tool_use_id`, JSON `content`, optional strict boolean `is_error` (default `false`) | `user` |

Tool-use identifiers are 1–64 ASCII letters, digits, `_`, `-`, `.`, or `:`.
A result's content may be a string, object, array, number, boolean, or `null`;
`false`, `0`, and `null` are real results, not missing content. Tool arguments
must be objects. All JSON numbers must be finite, and strings must be valid UTF-8.

Tool calls in history must reference declared tools and have unique identifiers.
All calls in an assistant turn need exactly one matching result in the immediately
following user turn, before any ordinary user text. Orphaned, duplicated,
unanswered, wrong-role, or mismatched calls/results are rejected before inference.
The service never turns a tool failure into an executed operation; consumers can
report a failure with `is_error: true`.

`content_blocks` preserves assistant block order. A final response can contain
text and reasoning blocks but no tool calls; use `content` for visible answer text.
Do not concatenate, reorder, trim, or reconstruct the blocks for continuation.

#### Thinking and continuation

The ordinary `THINKING_DEFAULT` behavior is unchanged, including for tool requests.
With adaptive thinking enabled, the returned assistant turn can contain signed
`reasoning` or opaque `redacted_reasoning` interleaved with other blocks, or the
model can skip reasoning entirely. Replay the returned blocks unchanged and in
order; do not fabricate a reasoning block when none was returned. Reasoning text
can be empty when only a signature is available. Never display or log continuation
data as the answer. Its signature is checked by the upstream provider, not
cryptographically verified by this service.

Dropping reasoning, editing previous messages, switching providers, or changing
the model mid-exchange can invalidate continuation. If authorization changes make
old context unsafe to replay, start a fresh authorized conversation rather than
editing a signed transcript. Consumers that do not need thinking can explicitly
send `thinking: false` from the start.

See [AWS's continuation requirements](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html)
and [thinking with tool use](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-extended-thinking.html).

#### Bounds

Tool mode means non-empty `tools` or any structured message content. Its bounds
are defined in `contracts/tool_validation.py`:

| Data | Limit |
|---|---|
| Tool definitions | 32 |
| Messages in tool mode | 256 |
| Blocks in one message | 128 |
| Individual schema, tool arguments, or tool result | 65,536 compact UTF-8 JSON bytes |
| Individual JSON value nesting / traversal | Depth 20 / 10,000 nodes |
| Aggregate tool-mode payload | 1,048,576 compact UTF-8 JSON bytes |

These are parsed-data limits, not an HTTP ingress body-size limit. They do not
truncate content or signatures. Existing string-only chat requests keep their
previous limits. Malformed provider tool responses normalize to a safe **502**,
including unknown or duplicate calls, invalid JSON, and contradictory stop reasons.
Raw successful provider JSON bodies are also capped at 1,048,576 bytes before
parsing, after the SDK has buffered them. Malformed Converse error bodies are
rejected as 502 before SDK parsing/retries; the normal throttling/error mappings
apply to well-formed upstream errors.

### Errors

See the shared [validation-error format](#validation-errors). Successful legacy
response bodies are unchanged; error-input echoing is intentionally not backward compatible.

| Condition | Status | Detail |
|---|---|---|
| Empty `messages`, empty `content`, unknown `model`, `max_tokens` out of range | 422 | Safe validation error |
| Invalid tool definitions, blocks, history, JSON values, or payload bounds | 422 | Rejected before inference; raw input and validation context are not echoed |
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

One JSON line per request with the attested actor, endpoint, status, and duration.
Requests reaching provider work record the selected **neutral model alias** from the
request or configuration, not the resolved model id returned in the response.
Successful calls add token usage through `request.state.audit_extra`; `/chat` records
`input_tokens` / `output_tokens`. Request and response content are never logged.
