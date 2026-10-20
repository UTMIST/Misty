# OpenAI chat migration

This change selects **GPT-6.1 Sol**, with thinking enabled by default, entirely
within `services/llm`. It implements `LLMProvider` using OpenAI Responses and
reuses the existing OpenAI SDK and `SecretStr` credential. Embeddings retain their
model, dimensions, and implementation. No lockfile, consumer, or platform
configuration files change.

The bot and meeting clients currently send text-only requests. They keep their
existing `/chat` requests and response fields. The service also preserves its
client-executed tool capability through the additive continuation blocks described
in [API.md](API.md#thinking-and-continuation). It does not execute tools.

GPT-6.1 Sol requires Responses for function calls and always reasons. The existing
`thinking` boolean maps to medium/low effort; output limits include reasoning.
The adapter uses `store=false` and replays encrypted state in consumer-supplied
history. Bedrock remains available as an explicit rollback configuration.
See [OpenAI's model specification](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
and [reasoning guidance](https://developers.openai.com/api/docs/guides/reasoning).

## Rollout and verification

Follow [the service deployment steps](DEPLOYMENT.md#switching-chat-to-openai).
The defaults do not override existing environment variables. The read-only
Railway inspection on 2026-10-16 found `OPENAI_API_KEY` listed on staging, but not
production, and explicit `LLM_PROVIDER` / `LLM_MODEL` variables on both. Variable
values were redacted. Provision the production credential before a production
rollout; no deployed configuration was changed by this work.

Verification uses the real pinned OpenAI SDK over an offline HTTP mock transport:
text requests, encrypted-state and parallel-tool replay, result errors, assistant
phases, truncation, malformed raw responses, auth/scopes, configuration,
normalized upstream failures, and safe diagnostic logging for schema/SDK failures.
The full LLM pytest suite, `ruff check`, and `ruff format --check` pass, with source
paths verified against this checkout.
Initial verification made no live model calls or deployment changes. The staging
cutover check on 2026-10-17 then received a successful OpenAI response that the
adapter rejected because of extra envelope metadata. Staging was restored to
Claude while the compatibility fix was prepared; the patched provider returned
`OK` from GPT-6.1 Sol in a separate local smoke call using the staging credential.
A second local smoke check completed a synthetic function-call/result round trip
with medium reasoning and the broader compatibility fix. These confirmed account
access and provider behavior, not the deployed HTTP path. Representative bot and
minutes prompts still need deployment verification, especially at small output budgets.

## Response compatibility

OpenAI explicitly permits added JSON response properties as
[backwards-compatible changes](https://developers.openai.com/api/reference/overview#backwards-compatibility).
The adapter therefore validates required fields and supported semantics, while
ignoring extra metadata on known response objects. It preserves bounded extensions
inside native reasoning items because the
[reasoning guide](https://developers.openai.com/api/docs/guides/reasoning#preserve-reasoning-without-stored-responses)
requires those items to survive stateless replay. Unknown item types and unsupported
tool execution modes still need explicit support. Incomplete responses retain text
and usage while withholding executable calls, including for new or absent reasons.
See [the architecture](ARCHITECTURE.md#openai-chat-default) for the validation boundary.

Offline regressions cover additions to the envelope, nested messages/text, tool
calls, usage, and replayed reasoning items, plus incomplete responses and malformed
or unsupported executable output. A list of today's field names is insufficient.

## Usage estimate recorded on 2026-10-16

Available Railway `/chat` success audit records for 2026-09-16 through 2026-10-16:

| Environment | Calls | Input tokens | Output tokens | GPT-6.1 Sol equivalent |
|---|---:|---:|---:|---:|
| Production | 45 | 165,911 | 13,297 | $0.46 |
| Staging | 52 | 42,009 | 5,491 | $0.14 |
| Dev | 9 | 1,838 | 682 | $0.01 |
| Total | 106 | 209,758 | 19,470 | $0.61 |

USD, using the then-current standard short-context rates of $2 per million input
tokens and $10 per million output tokens from [OpenAI pricing](https://developers.openai.com/api/docs/pricing).
Formula: `(input_tokens × 2 + output_tokens × 10) / 1,000,000`.
This reprices observed Bedrock tokens; it is not an OpenAI bill or a guarantee of
future usage. OpenAI tokenization, reasoning, cache reads/writes, and any missing
log history can change the result. Hosting and existing embeddings are excluded.

## Changed-file rationale

Paths below are relative to `services/llm`.

| Files | Why they change |
|---|---|
| `src/providers/openai_chat.py` | Responses adapter, request/result mapping, lazy client, safe failures, and content-free diagnostic warnings. |
| `src/providers/registry.py` | Register and construct the new provider using the existing key. |
| `src/providers/base.py` | Neutral continuation-state type, assistant phase, and unsupported-request error. |
| `src/providers/tool_blocks.py` | Validate the new neutral blocks at the reusable provider boundary. |
| `contracts/models.py` | One catalog for wire, config, and provider model compatibility. |
| `contracts/chat.py` | Use the catalog and validate assistant-only phases. |
| `contracts/tools.py` | Strict, additive encrypted-state and assistant-phase wire fields. |
| `contracts/tool_validation.py` | Validate known reasoning fields while retaining bounded native extensions for replay. |
| `src/api/routers/chat.py` | Map new blocks, reject incompatible models/state, and map 422/503 failures. |
| `src/api/app.py` | Describe the supported providers accurately in OpenAPI. |
| `src/config.py`, `.env.example` | OpenAI defaults; validate provider/model pairs and require AWS only for Bedrock. |
| `pyproject.toml` | Correct the dependency comment; dependency versions do not change. |
| `tests/test_openai_chat.py` | Offline SDK/HTTP and route regressions, including diagnostic usefulness and log redaction. |
| `tests/test_registry.py` | Provider selection, catalog alignment, and credential unwrapping. |
| `tests/test_config.py` | Defaults, provider/model boot checks, and conditional AWS requirements. |
| `tests/conftest.py` | Clear cached chat providers to isolate local credential/config tests. |
| `tests/test_audit_log.py` | Update the default-model assertion and explicitly configure the Bedrock override case. |
| `README.md`, `docs/API.md` | Describe defaults, supported models, errors, reasoning, and continuation. |
| `docs/ARCHITECTURE.md`, `docs/CONTRIBUTING.md` | Explain Responses, stateless replay, catalog maintenance, and safe diagnostic logging. |
| `docs/DEPLOYMENT.md` | Concrete environment cutover and rollback instructions. |
| `docs/OPENAI-MIGRATION.md` | Record scope, verification, measured usage estimate, and file rationale. |
