# Issue #70 — tool-use implementation and audit

This records the implementation against `staging` baseline `e8e362a`, the initial
review at `a847327`, the test cleanup at `69fe1ac`, the owner-audit follow-up, and
Ethan's subsequent stop-reason findings. It is not a certification of future changes
or a live AWS rollout.

## Test cleanup

After the initial audit, the user explicitly requested removal of all test
additions from PR #241, rather than trimming overlapping coverage. The cleanup
removed the new test files and restored `tests/test_adapter_base.py` to the
baseline without changing runtime code or removing pre-existing tests.

The [initial audited tests](https://github.com/UTMIST/Misty/tree/a847327f9d6a5608975a2ee676b17be09c0dee39/services/llm/tests)
remain available in Git history, not in the current checkout or its CI suite.
Initial-review evidence below refers to that historical revision; the owner follow-up
separately identifies replays against corrected code. The user subsequently approved
retaining only the focused stop-reason regressions requested by Ethan. These do not
restore the broad suite or establish complete ongoing coverage of the tool protocol.

## PR review follow-up

Ethan's findings were reproduced against `b5583b9`, which includes the subsequent
`staging` merges, using dummy credentials and intercepted real-SDK HTTP responses:

- **[Empty `end_turn`](https://github.com/UTMIST/Misty/pull/241#discussion_r4171396845):**
  both providers returned 502 after a valid tool-result continuation because output
  reused a non-empty request-list rule. Response parsing, neutral validation, the
  router, and `ChatResponse` now accept an empty list while request messages remain
  non-empty. The successful response retains its stop reason and usage.
- **[Truncated tool calls](https://github.com/UTMIST/Misty/pull/241#discussion_r4176480492):**
  `max_tokens` with tool blocks returned 502 instead of exposing recovery metadata.
  A shared non-mutating filter removes every tool call from that turn before
  executable-call validation, including partial JSON arguments and complete-looking
  siblings. Other supported blocks retain their relative order. Both providers and
  the router enforce this policy; metadata and success-audit token counts survive.
  Retry the original history with an appropriate output budget, not the truncated
  turn. No automatic retry or tool execution is added.

`tests/test_tool_stop_reasons.py` retains HTTP-through-SDK regressions for both
backends, mixed parallel calls, metadata/audit privacy, router-only controls, and
strict empty-request/malformed-response checks. Missing/null lists, invalid usage,
oversized lists, and malformed executable calls remain errors. This deliberately
small suite replaces neither the removed tests nor an authorized live-model check.
See the [consumer recovery rules](API.md#completion-and-truncation).

## Owner-audit follow-up

- **Invalid legacy system text:** high/low surrogate probes returned 200 and reached
  the provider. `ChatRequest` now checks UTF-8 encodability and raises a fixed
  validation error before inference. Valid Unicode, empty/omitted/null system prompts,
  ignored legacy extras, and ordinary response bodies remain compatible. This was the
  only runtime change in that owner follow-up; the same guard was applied in #234.
- **Cross-PR conflicts:** #234 and #241 conflicted in the bootstrap and four guides.
  Their validation handler/contract and shared documentation now agree. Feature-specific
  source descriptions, audit fields, and maintenance guidance remain in their own
  sections so either merge order retains both capabilities. The embedding change keeps
  its async health route and lazy provider boot validation. No feature is merged here.
- **Incorrect audit metadata description:** logs record the selected neutral model alias,
  not the resolved provider identifier. The shared API wording now matches the router.
  Troubleshooting distinguishes SDK timeouts from whole-request deadlines and links the
  applicable boot checks rather than assuming chat is the only capability.
- **Verification isolation:** the historical suite and new scratch probes were replayed
  against the corrected branch with settings isolated before collection, outbound
  networking blocked, and imported `src`/`contracts` paths checked. An earlier cross-tree
  run imported historical contracts and was discarded as evidence for the new fix.
  Combined-tree results and exact revision/count information belong in the PR test plan.

The embedding duplicate-field and strict-usage fixes belong to #234. RAG extraction,
source-offset, relevance-floor, and person/channel handoff corrections are a separate
documentation change. No removed tool tests are reintroduced by this follow-up.

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

## File-by-file implementation rationale

Paths are relative to `services/llm`, except the root guidance entry. This table
retains the rationale across revisions, including changes later inherited from
`staging`; the PR description lists the files in its current diff.

| File | Change and justification |
|---|---|
| `contracts/chat.py` | Adds definitions and structured messages/results, preserves legacy defaults, and validates histories and normalized bounds before paid inference. Follow-ups reject invalid system Unicode and permit empty response blocks without relaxing request content. |
| `contracts/tools.py` | Separates reusable strict tagged wire blocks and opaque continuation fields from chat-history validation. |
| `contracts/tool_validation.py` | Shares bounded JSON, identifier, schema-shape, UTF-8, and base64 checks without making providers depend on Pydantic/FastAPI. |
| `src/providers/base.py` | Adds neutral tool/content dataclasses and independent default lists so both backends use one stateless protocol. |
| `src/api/routers/chat.py` | Provides the sole DTO/dataclass translation and validates results. The review fix independently withholds truncated calls from any provider, accepts empty completions, and preserves the success-audit usage path. |
| `src/api/app.py` | Prevents validation-error serialization failures and input/context disclosure, including sensitive tool data and caller-controlled field names. |
| `src/providers/bedrock_converse.py` | Maps Converse tools and reasoning with request-scoped pre-parser validation. The review fix accepts empty output and applies the shared truncated-call policy while keeping stop reason, usage, and the legacy path intact. |
| `src/providers/bedrock.py` | Implements equivalent Mantle transport and strict raw decoding. The review fix accepts empty output and suppresses truncated calls before executable-argument validation, retaining response metadata. |
| `src/providers/tool_blocks.py` | Centralizes neutral validation. Empty lists are opt-in for responses, never requests; shared non-mutating filtering suppresses all `max_tokens` calls after checking the original list bounds. |
| `src/providers/raw_responses.py` | Validates raw JSON before SDK coercion, field loss, parser failures, or unknown-union logging; shares decoding across the two backends. |
| `tests/test_tool_stop_reasons.py` | Retains the narrowly approved regressions for Ethan's findings through both real SDKs and the router, including usage/audit preservation, partial parallel batches, and strict-request/malformed-response controls. |
| `README.md` | Introduces the transport and source layout. The review follow-up distinguishes focused retained coverage from the removed broad suite and accurately describes shared provider-neutral validation helpers. |
| `docs/API.md` | Specifies definitions, blocks, replay, limits, and consumer responsibilities. The review follow-up documents empty completions, withheld truncated calls, and retrying original history without executing a partial batch. |
| `docs/ARCHITECTURE.md` | Explains continuation, raw validation, and execution boundaries. The review follow-up explains why response emptiness and safe truncation differ from request validation and records the limited retained coverage. |
| `docs/CONTRIBUTING.md` | Records both-codec maintenance and real-transport testing. The review follow-up points maintainers to the focused regressions and the explicit truncation exception rather than claiming all tool tests remain removed. |
| `docs/DEPLOYMENT.md` | Documents rollout/rollback, stable provider/model context, retry/buffering limits, and live verification. Shared troubleshooting now composes with #234 and avoids claiming SDK timeouts are whole-request deadlines. |
| `docs/TOOL-USE-AUDIT.md` | Preserves initial and follow-up findings, file-specific rationale, and the distinction between retained tests and separately replayed regressions. The review follow-up links both comments and records the changed test-retention decision. |
| Root `AGENTS.md` | Corrects strict-DTO guidance, requires file-specific rationale, and records the reproduced cross-worktree import hazard. This deliberately includes the `root` ownership zone. |

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

Current checkout verification uses the retained suite, focused review regressions,
and pinned tools:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

At `69fe1ac`, runtime paths matched `a847327` and the test tree matched `e8e362a`.
The owner follow-up changed the system-prompt validator, not the test tree. Later
`staging` merges brought in the embedding implementation and its tests. This review
follow-up adds only the focused stop-reason suite; neither runtime equality with
the initial revision nor restoration of the removed broad suite is claimed.
The pre-existing Starlette/httpx TestClient deprecation may still be reported.

Docker is unavailable locally; image-build/import verification comes from CI. No
live AWS account/model access, real signature acceptance, latency/spend behavior,
or consumer tool execution was verified. An explicitly authorized deployed-model
round trip remains required before enabling the consumer feature; see
[DEPLOYMENT.md](DEPLOYMENT.md#tool-use-rollout).

Parsed-data/raw-response caps are not HTTP ingress or streaming download limits.
The current branch includes #234's async health route through the later `staging`
merges. This review follow-up changes neither embedding behavior nor health handling,
and leaves the original user's uncommitted checkout untouched.
