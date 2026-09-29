# Issue #70 — tool-use implementation and audit

This records the implementation and pre-submission review of tool-use transport
against `staging` baseline `e8e362a`. It describes this change, not a certification
of future changes or a live AWS rollout.

## Scope and acceptance criteria

The service remains a stateless inference proxy. A `chat`-scoped consumer can send
tool definitions, receive ordered tool calls and continuation blocks, execute
permitted operations outside the service, and send results in the next request.
The helper-bot execution loop and authorization belong to #71; RAG remains outside
this change. No dependency, credential, setting, scope, database, or deployment
policy was added.

| Issue #70 requirement | Implementation and evidence |
|---|---|
| Optional name/description/JSON-schema definitions | Strict tool DTOs, bounded structural schema validation, API and contract tests |
| Provider-neutral definitions and returned calls | Plain dataclasses and ordered content blocks; only the router translates wire DTOs |
| Converse `toolConfig` and `toolUse`, `stop_reason: tool_use` | Both request/result codecs, consistency checks, real-SDK transport tests |
| Unchanged ordinary chat responses | Separate legacy paths; exact response bytes and serialized request bytes for omitted/null/empty tools |
| Offline API/provider coverage | Injected providers, Botocore Stubber, real SDKs with intercepted HTTP transports, network-blocked audit runs |

Mantle support and structured replay are necessary complements: switching the
configured backend must not drop tool data, and returning only a tool name/input
would not let #71 continue a thinking-enabled exchange correctly.

## Review method

Implementation was split across API/contracts, both provider adapters, and an
independent HTTP-to-SDK round-trip test track. Subsequent reviews covered the full
changed tree, architecture/documentation, and the actual SDK wire parser. The lead
performed additional HTTP mutation and deterministic JSON-preservation checks; a
separate API audit worker could not run, so that track is not represented as a
completed independent review.

The wire auditor used actual boto3/Botocore and Anthropic SDKs with dummy
credentials and intercepted transports. Botocore Stubber alone is insufficient:
it returns already-parsed objects and bypasses the parser where several defects
were found. No live inference, real credentials, deployment, or tool execution
was used. The original wire auditor independently reran the retained regressions
and checked additional retry and shared-client concurrency scenarios after fixes.
The architecture reviewer performed static review, not test execution.

## Findings and resolutions

| Finding | Why it mattered | Resolution and regression evidence |
|---|---|---|
| Adaptive thinking was treated like manual thinking | The initial validators rejected interleaved reasoning, an API response replayed without reasoning, and a new thinking-enabled turn after a completed thinking-disabled exchange. | Removed invented ordering/presence requirements. Preserve every returned block in order without fabricating reasoning. Both-backend HTTP regressions live in `test_tool_roundtrip.py`; API/provider tests cover the same invariants. |
| P2: Converse SDK coercion/discarding preceded validation | Raw boolean/fraction/string token counts were converted to integers; duplicate JSON fields, unknown members, explicit null reasoning text, and invalid base64 could be lost or normalized before validation. | Bounded strict raw JSON and SDK-shape validation now run in `before-parse`, before information is lost. Retained real-wire regressions are in `test_tool_wire_adversarial.py`. |
| P2: malformed Converse wire bodies escaped as 500 | Invalid UTF-8, envelopes, unions, binary values, numbers, and error bodies could raise inside SDK parsing, outside the existing normalized error catches. | Reject malformed wire data at the parsing boundary with a fixed `ProviderUnavailable`. Unrelated transport/programmer exceptions are not blanket-caught. Wire tests cover both safe 502s and programmer-error propagation. |
| Related INFO logging exposure | Botocore could log an unknown union member's input-derived name before post-SDK rejection. Production exposure depended on logger configuration. | Unknown unions are rejected before the SDK logger sees them. The retained test enables the SDK INFO logger and verifies the private marker is absent. |
| P2: Mantle rejected a valid context-window stop | `model_context_window_exceeded` is documented for supported models, despite its omission from the pinned ordinary SDK enum. | Added it to Mantle's supported stops, matching Converse. Both-backend wire tests verify the valid completion. |
| API/provider bounds and empty-field mismatches | Defaults could push a request over the normalized payload limit; empty descriptions/text or missing extended output blocks could cross boundaries inconsistently. | Validate normalized payloads before inference, require nonempty descriptions/text blocks and extended response lists, but preserve empty signature-only reasoning. Contract, API, and neutral-boundary tests cover each case. |
| Incomplete parser-event envelopes could raise indexing errors | Missing callback-envelope keys or an unexpected container type could produce `KeyError`/`TypeError` instead of a validation failure. | Check envelope type and required keys before indexing. Targeted regressions first reproduced the errors, then passed; the hook keeps a narrow validation-error catch. |
| Documentation drift | The previous blanket extra-field guidance, layout tree, fake-only test description, and validation-error shape no longer described the change accurately. | Updated the affected guidance and documented strict child models, error redaction, wire tests, SDK boundaries, and rollout limitations. |

The reproduced defects above are resolved. No additional concrete defect remained
in the reviewed feature after the final verification and review dispositions.
This does not establish real-model acceptance of synthetic reasoning signatures.

### Deliberate review dispositions

- **Malformed Converse errors fail closed.** A reviewer proposed best-effort
  status mapping and retries for invalid error bodies. This change instead returns
  502 before parsing/retry evaluation, including on an otherwise retryable HTTP
  status. Letting corrupted bodies through would reintroduce parser exceptions or
  input-derived logging. Native SDK retries and normal status mapping remain for
  well-formed errors; this narrower guarantee is explicit in API/architecture/
  deployment documentation and exercised by real-transport tests.
- **Unknown extra-field names are not echoed in 422 locations.** The location
  identifies the parent object, trading some diagnostics for avoiding
  caller-controlled data in error paths. Successful legacy JSON is unchanged;
  legacy validation-error input echoing is intentionally removed.
- **History rules are checked at two boundaries.** HTTP validation provides 422
  before inference; provider validation protects independent neutral callers.
  Shared primitive checks reduce duplication, and the contributing guide requires
  changing/testing both history validators together.
- **Future unsupported wire shapes fail closed.** An AWS response-field/shape
  addition can require an SDK/codec update rather than being silently discarded.
  This is documented maintenance work, not an unbounded passthrough guarantee.

## Why each changed area is necessary

Paths below are relative to `services/llm`, except the root guidance entry.

| Files | Change and justification |
|---|---|
| `contracts/chat.py` | Adds optional definitions and structured messages/results, preserves legacy defaults, and validates complete tool histories and normalized bounds before paid inference. |
| `contracts/tools.py` | Defines strict tagged wire blocks, valid role-specific data, and opaque continuation fields without importing application code. |
| `contracts/tool_validation.py` | Shares bounded finite-JSON, identifier, schema-shape, UTF-8, and canonical-base64 checks without binding providers to Pydantic/FastAPI. |
| `src/providers/base.py` | Adds provider-neutral tool/content dataclasses and independent default lists so both backends and test doubles use one stateless protocol. |
| `src/api/routers/chat.py` | Performs the sole DTO/dataclass translation, includes ordered blocks only in extended mode, checks returned data, and preserves safe errors and content-free audit metadata. |
| `src/api/app.py` | Sanitizes validation errors so rejected non-finite/Unicode/tool data neither becomes a serialization 500 nor appears in error input/context. |
| `src/providers/bedrock_converse.py` | Implements Converse definitions/calls/results and binary reasoning conversion; gates pre-parser validation by context-local provider identity while preserving the legacy path. |
| `src/providers/bedrock.py` | Implements the equivalent Mantle contract, JSON result encoding, and strict raw-response decoding. Per-call timeout avoids the pinned SDK's option-copy issue on the new path. |
| `src/providers/tool_blocks.py` | Centralizes neutral provider-side declarations, role/history, usage, payload, and stop-reason checks shared by the two codecs. |
| `src/providers/raw_responses.py` | Prevents SDK coercion, field loss, parser failures, and unknown-union logging by validating raw JSON and modeled wire data first; shared decoding avoids divergent backend checks. |
| `tests/test_adapter_base.py` | Pins backwards-compatible defaults and prevents shared mutable tool lists between requests. |
| `tests/test_tool_validation.py` | Tests primitive validation boundaries, canonical encoding, finite values, structural limits, and neutral empty-text rejection. |
| `tests/test_tool_contract.py` | Tests DTO strictness, JSON/history semantics, declared tools, bounds, and adaptive continuation behavior. |
| `tests/test_chat_tools.py` | Tests HTTP translation, auth, legacy bytes, private errors/logs, malformed neutral results, and OpenAPI. |
| `tests/test_tool_providers.py` | Tests both concrete codecs and raw-boundary isolation, including nested/concurrent/shared-client calls, cleanup on failure, and native retries. |
| `tests/test_tool_roundtrip.py` | Independently exercises full stateless call/result/final-answer exchanges through the HTTP API and concrete SDK adapters. |
| `tests/test_tool_api_adversarial.py` | Adds deterministic JSON type/value preservation, malformed nested data and history mutations, cyclic neutral data, and invalid parser-envelope regressions. |
| `tests/test_tool_wire_adversarial.py` | Retains independent actual-wire reproductions for coercion, discarded fields, parser exceptions, privacy, stop reasons, and exact legacy serialization. |
| `README.md` | Introduces the transport contract, links detailed usage, and updates the layout/testing/status descriptions. |
| `docs/API.md` | Specifies definitions, blocks, replay, limits, unchanged success responses, intentionally redacted errors, and consumer responsibilities. |
| `docs/ARCHITECTURE.md` | Explains why continuation data, raw validation, context isolation, and separate execution/authorization boundaries are necessary. |
| `docs/CONTRIBUTING.md` | Records how to maintain both codecs/validators and why real wire-parser tests must accompany Stubber tests. |
| `docs/DEPLOYMENT.md` | Documents service-before-consumer rollout, rollback coordination, stable provider/model context, retry/buffering limits, and authorized live verification. |
| `docs/TOOL-USE-AUDIT.md` | Records findings, evidence, limitations, and the rationale requested for this commit. |
| Root `AGENTS.md` | Corrects the now-false claim that all LLM DTOs ignore extras; only new tool/block models are strict. This deliberate guidance correction adds the `root` ownership zone alongside `services/llm`. |

## Verification and remaining rollout work

Executed locally with the pinned workspace environment:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The shared-auth suite was also run from `packages/auth`. At repository root,
`uv lock --check --offline`, `make labels`, and whitespace checks passed. The app
and OpenAPI imported successfully. The wire auditor additionally ran the complete
LLM suite with process-wide network blocking. The only test warning was the
pre-existing Starlette/httpx TestClient deprecation.

Docker was unavailable locally; the draft PR's CI is the source of image-build and
container import-smoke verification. No live AWS account access, deployed model
availability, real signature acceptance, latency/spend behavior, or consumer tool
execution was verified. An explicitly authorized deployed-model round trip is
still required before enabling the consumer feature, as described in
[DEPLOYMENT.md](DEPLOYMENT.md#tool-use-rollout).

The parsed-data/raw-response caps are not HTTP ingress limits or streaming download
limits. The existing synchronous `/health` behavior on this baseline is unchanged;
this change does not claim a new thread-saturation health guarantee. The separate
embedding work in PR #234 and the original user's uncommitted checkout were not
modified or merged into this branch.
