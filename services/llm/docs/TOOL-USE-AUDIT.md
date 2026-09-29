# Issue #70 — tool-use implementation and audit

This records the implementation against `staging` baseline `e8e362a`, the initial
review at `a847327`, and the subsequent test cleanup. It is not a certification of
future changes or a live AWS rollout.

## Test cleanup

After the initial audit, the user explicitly requested removal of all test
additions from PR #241, rather than trimming overlapping coverage. The cleanup
removes the new test files and restores `tests/test_adapter_base.py` to the
baseline. Runtime code is unchanged, and no pre-existing tests are removed.

The [initial audited tests](https://github.com/UTMIST/Misty/tree/a847327f9d6a5608975a2ee676b17be09c0dee39/services/llm/tests)
remain available in Git history, not in the current checkout or its CI suite.
All tool-specific test evidence below refers to that historical revision.
The final PR does not retain the new tool-use tests requested by #70's acceptance
criteria. Passing the remaining baseline suite is not ongoing regression coverage
for the new tool protocol, its validation, or its raw SDK boundary.

## Scope

The service remains a stateless inference proxy. A `chat`-scoped consumer can send
tool definitions, receive ordered tool calls and continuation blocks, execute
permitted operations outside the service, and send results in the next request.
The helper-bot execution loop and authorization belong to #71; RAG remains outside
this change. No dependency, credential, service setting, scope, database, or
deployment policy was added.

Mantle support and structured replay complement the Converse requirement:
switching the configured backend must not drop tool data, and returning only a
tool name/input would not let #71 continue a thinking-enabled exchange correctly.

## Initial review method

Implementation was split across API/contracts, provider adapters, and independent
HTTP-to-SDK round-trip tests. Subsequent reviews covered the full changed tree,
architecture/documentation, and actual SDK wire parsing. The lead added HTTP
mutation and deterministic JSON-preservation checks. A separate API audit worker
could not run; that part was covered directly, not counted as an independent review.

The wire auditor used actual SDKs with dummy credentials and intercepted HTTP
transports. Botocore Stubber alone was insufficient because it bypasses parsing.
After fixes, the original auditor reran the regressions and checked additional
retry and shared-client concurrency scenarios. The architecture reviewer performed
static review, not test execution. No live inference, real credentials, deployment,
or tool execution was used.

## Findings and resolutions at the initial audited revision

| Finding | Why it mattered | Fix and historical verification |
|---|---|---|
| Adaptive-thinking restrictions | Initial validators rejected valid interleaving, reasoning-less replies, and new thinking-enabled turns after completed thinking-disabled exchanges. | Removed invented ordering/presence requirements while preserving exact block order. Both-backend HTTP and provider regressions passed. |
| P2: SDK coercion and discarded fields | Botocore converted malformed token counts, dropped duplicate/unknown fields and explicit nulls, and accepted invalid base64 before validation. | Bounded strict JSON and modeled-shape checks now run before SDK parsing. Real-wire regressions reproduced the failures and passed after the fix. |
| P2: parser exceptions became 500 | Malformed success/error bodies could throw inside SDK parsing, outside normalized error handling. | Reject malformed data at the raw boundary with a fixed `ProviderUnavailable`; do not blanket-catch programmer exceptions. Both safe-502 and programmer-error controls passed. |
| Conditional SDK INFO logging exposure | An unknown union member's input-derived name could be logged before rejection when INFO logging was enabled. | Reject unknown unions before SDK parsing/logging. A private-marker regression passed with the SDK INFO logger enabled. |
| P2: valid Mantle stop rejected | The pinned ordinary SDK enum omitted the documented `model_context_window_exceeded` stop reason. | Added it to Mantle's supported stops, matching Converse. Both-backend completion regressions passed. |
| Bounds and empty-field mismatches | Normalization could exceed payload limits; empty descriptions/text and absent extended blocks crossed boundaries inconsistently. | Validate normalized payloads and required nonempty fields, while preserving signature-only reasoning. Contract/API/provider regressions passed. |
| Incomplete parser-event envelopes | Missing keys or wrong containers could cause indexing/type errors instead of validation failures. | Validate the envelope before indexing and keep the hook's catch narrow. Targeted regressions failed before the fix and passed afterward. |
| Documentation drift | Extra-field guidance, layout, error shape, and descriptions of test coverage were inaccurate. | Updated the relevant docs; the later cleanup also distinguishes historical audit evidence from retained tests. |

The reproduced defects were resolved, and the initial review found no additional
concrete defect after final dispositions. Runtime code has not changed during the
test cleanup, but those tool-specific regressions are no longer retained. Synthetic
signatures never established deployed-model cryptographic acceptance.

### Deliberate review dispositions

- Malformed Converse error bodies fail closed with 502 before SDK parsing/retries,
  even on an otherwise retryable status. Native retries remain for well-formed
  errors. This policy and its historical verification are not a claim that all
  upstream errors preserve retry behavior.
- Unknown extra-field names are omitted from 422 locations to avoid echoing
  caller-controlled data. Successful legacy JSON remains unchanged.
- History invariants are checked at the HTTP boundary for early 422s and at the
  neutral provider boundary for independent callers. Both must stay aligned.
- Unsupported future wire shapes fail closed and may require an SDK/codec update.

See [ARCHITECTURE.md](ARCHITECTURE.md#stateless-tool-use-transport) for the complete
boundary and maintenance rationale.

## Why each remaining changed file is necessary

Paths are relative to `services/llm`, except the root guidance entry.

| File | Change and justification |
|---|---|
| `contracts/chat.py` | Adds definitions and structured messages/results, preserves legacy defaults, and validates histories and normalized bounds before paid inference. |
| `contracts/tools.py` | Separates reusable strict tagged wire blocks and opaque continuation fields from chat-history validation. |
| `contracts/tool_validation.py` | Shares bounded JSON, identifier, schema-shape, UTF-8, and base64 checks without making providers depend on Pydantic/FastAPI. |
| `src/providers/base.py` | Adds neutral tool/content dataclasses and independent default lists so both backends use one stateless protocol. |
| `src/api/routers/chat.py` | Provides the sole DTO/dataclass translation, conditionally returns ordered blocks, validates results, and preserves content-free audit metadata. |
| `src/api/app.py` | Prevents validation-error serialization failures and input/context disclosure, including sensitive tool data and caller-controlled field names. |
| `src/providers/bedrock_converse.py` | Maps Converse definitions/calls/results and binary reasoning; isolates pre-parser validation by provider context while preserving the legacy path. |
| `src/providers/bedrock.py` | Implements equivalent Mantle transport and strict raw decoding; per-call timeout avoids the pinned SDK's option-copy issue on the new path. |
| `src/providers/tool_blocks.py` | Centralizes neutral history, declaration, usage, payload, and stop-reason checks needed by both codecs. |
| `src/providers/raw_responses.py` | Validates raw JSON before SDK coercion, field loss, parser failures, or unknown-union logging; shares decoding across the two backends. |
| `README.md` | Introduces the transport and source layout, and accurately distinguishes retained baseline tests from the removed tool-use suite. |
| `docs/API.md` | Specifies definitions, blocks, replay, limits, legacy success compatibility, redacted errors, and consumer responsibilities. |
| `docs/ARCHITECTURE.md` | Explains continuation, raw validation, context isolation, execution/authorization boundaries, and the current coverage limitation. |
| `docs/CONTRIBUTING.md` | Records how to maintain both codecs/validators and why raw-boundary tests need real transports rather than Stubber alone; discloses that the original tests are no longer retained. |
| `docs/DEPLOYMENT.md` | Documents rollout/rollback sequencing, stable provider/model context, retry/buffering limits, and authorized live verification. |
| `docs/TOOL-USE-AUDIT.md` | Preserves the requested findings and file-specific rationale while separating historical verification from the final PR's coverage. |
| Root `AGENTS.md` | Corrects the blanket claim that all LLM DTOs ignore extras and records the requirement to justify every changed file. This deliberately includes the `root` ownership zone. |

## Why each test file was removed or restored

These changes implement the explicit all-test-additions cleanup, not a conclusion
that the deleted cases were unnecessary or replaced by baseline coverage.

| File | Cleanup and justification |
|---|---|
| `tests/test_adapter_base.py` | Restore the baseline by removing only the added tool-list/default-result cases and their import; preserve its original tests. |
| `tests/test_chat_tools.py` | Remove the newly generated HTTP tool-mode suite, including translation, auth, and privacy cases. |
| `tests/test_tool_contract.py` | Remove the newly generated DTO/history/bounds suite; runtime contract validation remains unchanged. |
| `tests/test_tool_validation.py` | Remove the newly generated primitive and neutral-boundary checks without changing their production helpers. |
| `tests/test_tool_providers.py` | Remove the newly generated codec/raw-boundary suite, including concurrency and retry cases. |
| `tests/test_tool_roundtrip.py` | Remove the newly generated end-to-end SDK exchange suite; no consumer or runtime behavior is removed. |
| `tests/test_tool_api_adversarial.py` | Remove the newly generated HTTP mutation, JSON preservation, cyclic-data, and envelope checks. |
| `tests/test_tool_wire_adversarial.py` | Remove the newly generated actual-wire regressions; their original evidence remains linked above in Git history. |

## Verification and remaining rollout work

The initial audited revision passed its offline LLM and shared-auth suites, raw-wire
regressions, pinned Ruff checks, lockfile/label/whitespace checks, app/OpenAPI import,
and GitHub CI including Docker build/import. Its wire auditor also ran the LLM suite
with process-wide network blocking. Those are historical results, not checks still
executed by the cleaned-up checkout.

Current checkout verification uses the remaining baseline suite and pinned tools:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The cleanup also checks that runtime paths match `a847327` and the entire test tree
matches `e8e362a`. Fresh CI applies to the reduced tree, not the removed regressions.
The pre-existing Starlette/httpx TestClient deprecation may still be reported.

Docker is unavailable locally; image-build/import verification comes from CI. No
live AWS account/model access, real signature acceptance, latency/spend behavior,
or consumer tool execution was verified. An explicitly authorized deployed-model
round trip remains required before enabling the consumer feature; see
[DEPLOYMENT.md](DEPLOYMENT.md#tool-use-rollout).

Parsed-data/raw-response caps are not HTTP ingress or streaming download limits.
The baseline synchronous `/health` behavior is unchanged. The separate embedding
work in PR #234 and the original user's uncommitted checkout remain untouched.
