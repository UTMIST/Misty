# Observability implementation tickets

The first release lets a maintainer answer “Why did someone get a link to a recording that ended early?” from staging Railway logs. A lead can see the partial recording in the overview. A maintainer can follow it from Discord through meeting finalization, catalog persistence, the Google writer, and delivery. Both views derive their answer from the same observations. [Measurement design](OBSERVABILITY-DESIGN.md) · [Recording wireframe](OBSERVABILITY-VIEWS.md#recording-investigation)

| Delivery milestone | Tickets | Acceptance evidence |
| --- | --- | --- |
| Agree on the data that survives a backend change | 01 | Node, Python and the reader have one versioned fixture with known answers. |
| Trace one recording across Misty | 02–08 | A Railway trace identifies which component observed capture, save and delivery, including recovery after the original request ended. |
| Read state and validate it in staging | 09–11 | A lead and a maintainer can explain the same partial recording at different levels of detail. |
| Extend the pilot | 12–20 | Each workflow or operations view has its own acceptance test and can ship independently once its inputs exist. |
| Add a new feature or change destination | Follow-ups below | Each feature supplies offline evidence. A candidate backend reproduces the Railway results before cutover. |

The numbers below are draft references. When filing, use the title, **What**, **Why**, **Area**, and **Done looks like** in the feature/task form. Replace draft blocker numbers with the assigned GitHub issues. Link back to the design for rationale. Ticket 01 can start during design review. The staging trial consumes the agreed design decisions. [Feature/task form](../.github/ISSUE_TEMPLATE/feature_request.yml) · [Design issue #342](https://github.com/UTMIST/Misty/issues/342)

## Trace and collect one recording

### 01. Publish the event contract and a synthetic recording fixture

**Area** observability, integration · **Blocked by** none

**What** Specify the JSON envelope, allowed fields per event, context propagation, and correlation across recovery. **Why** Producers and readers need a shared answer for what counts as one attempt.

**Done looks like**

- [ ] A versioned, machine-checkable contract and example records specify IDs, parent spans, async links, event and receipt times, monotonic duration, bounded operation/result labels, traffic class, and deployment. The field list says which events are required for totals and which spans may be sampled.
- [ ] A content-free fixture gives expected call trees, component counts and workflow states for normal completion, retry, cancellation, interrupted capture, delayed confirmation and a missing end after restart. Include duplicate and out-of-order input.
- [ ] The measurement catalog names the source, owner, unit, denominator, allowed dimensions and expected freshness for each pilot signal. Record a retention/access decision for restricted traces and aggregates before enabling collection.

### 02. Emit traceable Python request and dependency spans

**Area** service, observability · **Blocked by** 01

**What** Extend the shared Python audit boundary with trace context and a bounded structured emitter that service clients can use. **Why** A per-request audit ID alone cannot identify the calling component. [Audit middleware](../packages/auth/platform_auth/audit.py)

**Done looks like**

- [ ] Offline tests exercise an authenticated internal parent, a fresh root at public ingress, a malformed header, concurrent requests and cleanup after an exception. Existing audit records and scope checks still pass.
- [ ] A fake HTTP dependency sees the client span as its parent on internal calls. A fake external provider receives no Misty context. Route labels use templates, and allowlist tests catch identifiers or secrets in paths and error fields.
- [ ] A full queue or failing sink leaves the request result intact. Offered/dropped counts and process start are observable when output resumes. The emitted lines validate against ticket 01.

### 03. Carry trace context through Discord commands and bot clients

**Area** bot, observability · **Blocked by** 01

**What** Start a trace at bot admission and propagate it through internal client calls. **Why** The member's attempt starts before the first HTTP service sees it. [Bot HTTP client](../discord-bot/src/clients/httpClient.js)

**Done looks like**

- [ ] Concurrent synthetic commands get distinct roots. Fan-out and retries share their command's trace and produce distinct child spans. A denied command records its outcome without creating a downstream call.
- [ ] A fake Misty endpoint receives a valid client parent. Discord and other external destinations receive no trace context. Emitted Node events pass the same contract and sensitive-field checks as Python.
- [ ] Queue overflow reports lost observations and leaves command handling and audio forwarding operational.

### 04. Read Railway observations into environment-scoped summaries

**Area** deployment, tooling, observability · **Blocked by** 01

**What** Implement a read-only ingestion path from Railway JSON logs into restricted attempt detail and stored dashboard summaries. **Why** Dashboard refreshes need stable inputs, including evidence of a collection gap.

**Done looks like**

- [ ] Document the chosen Railway read mechanism, least-privilege credential, storage location, retention/expiry and operator. A synthetic read exercise demonstrates restart from a checkpoint, overlapping reads and an expired log window.
- [ ] The reader accepts versioned, allowlisted telemetry records, records receipt time and counts malformed or unsupported records. Ordinary audit lines with actor, IP or raw path fields cannot enter dashboard storage. Replaying a batch preserves counts.
- [ ] Staging and production records remain isolated. Tests distinguish a fresh source with zero activity, a stale source, queue drops, and a failed reader. A failed read preserves the last valid summary and its timestamp.

### 05. Instrument Discord recording control and delivery

**Area** bot, observability · **Blocked by** 03

**What** Carry the recording's root across start, stop, automatic disconnect and recovered delivery. **Why** The bot owns the evidence for both capture loss and Discord acknowledgment. [Recording control](../discord-bot/src/meeting/meetingSurface.js) · [Delivery recovery](../discord-bot/docs/meeting-delivery.md)

**Done looks like**

- [ ] Bot fixtures distinguish denied start, capture started, user stop, empty-channel stop and connection loss. A later stop command resolves to the original recording attempt.
- [ ] Link and PDF delivery each record their own acknowledgment or failure. A delayed delivery links to the original attempt across a worker restart without counting a second recording. Tests cover PDF-only, failed send and recovered link.

### 06. Instrument meeting drain, generation and handoff

**Area** service, observability · **Blocked by** 02

**What** Carry recording context through the live session, stop pipeline and pending handoff. **Why** A maintainer needs to distinguish lost audio, slow minute generation and a delayed catalog handoff. [Meeting lifecycle](MEETING-RECORDING.md#hand-off-and-retry-closes-the-stop-gap)

**Done looks like**

- [ ] Session fixtures expose drain duration or timeout, discard counts by reason and unit, minutes/PDF result, and accepted, pending or failed handoff. Internal LLM and catalog calls have the right client parents.
- [ ] Tests with a stalled telemetry sink and fake speech/LLM providers preserve audio-tail drain, finalization and retry behavior. A handoff retry has its own span. Telemetry captures no audio, transcript, minutes or PDF.

### 07. Trace catalog save, reconciliation and recovery

**Area** docs-system, observability · **Blocked by** 02

**What** Record handoff acceptance, persistent save state and connector calls at the catalog boundaries that own them. **Why** A successful handoff can precede the confirmed Doc by an entire recovery run. [Catalog recording lifecycle](MEETING-RECORDING.md#durable-records)

**Done looks like**

- [ ] Tests distinguish accepted, pending, confirmed and failed saves, including an ambiguous create. Only confirmed saves enter the saved count. Indexing has its own outcome.
- [ ] A restarted recovery worker can link its work to the original recording using durable opaque trace context. Test replay and records written before instrumentation. Missing context on an older record leaves the link unknown.
- [ ] The fake writer observes the internal parent. Existing persistence/reconciliation tests still enforce a single canonical Doc and safe handling of ambiguous creates.

### 08. Instrument Google create and share operations

**Area** service, integration, observability · **Blocked by** 02

**What** Emit local dependency spans from the connectors Google writer. **Why** Operators need to distinguish time spent at Google from time spent in the catalog.

**Done looks like**

- [ ] Fake Google responses produce separate create/share timings and confirmed, failed or ambiguous outcomes. A retry permitted by the existing writer has its own span under the logical operation.
- [ ] Tests inspect the exported record and the outbound request. Names, file IDs, ACLs, content and credentials are excluded from telemetry, and Google receives no internal trace headers.

## Reconstruct, display and prove the pilot

### 09. Derive workflow state and component totals on replay

**Area** tooling, observability · **Blocked by** 01

**What** Implement deterministic projections over the event contract. **Why** A component can finish its work even when the overall recording is partial.

**Done looks like**

- [ ] The ticket 01 fixture yields its expected span tree, capture/save/delivery states and completion rate by requested output. Shuffling and duplicating records preserves the results. A late recovery acknowledgment updates the original attempt's start-time cohort.
- [ ] The fixture distinguishes open, unknown after restart or stale source, and observed timeout. Missing parents appear as gaps. A late start cannot reopen a finished span.
- [ ] Component totals include retries, outcome counts, latency units, median, p95 and sample counts. Tests cover a zero denominator, missing interval endpoints, late usage and diagnostic sampling. Unsampled lifecycle events determine workflow totals.

### 10. Build the recording overview and trace drill-down

**Area** tooling, observability · **Blocked by** 04, 09

**What** Implement the two recording views over stored summaries and restricted trace detail. Start with Railway views where they satisfy the access and navigation requirements. **Why** Leads need to see the impact and maintainers need the component that can explain it. [Wireframes and access rules](OBSERVABILITY-VIEWS.md)

**Done looks like**

- [ ] From a partial recording in the overview, a maintainer can open its milestone timeline, follow a nested span to component outcomes and timings, and reach the owning runbook. Environment, traffic class, period and comparison remain visible.
- [ ] Synthetic UI checks cover loading, zero activity, missing instrumentation, stale/partial data and access denied. A lead can read totals. A direct link cannot grant attempt access or cross an environment boundary.
- [ ] Views read stored summaries at the specified cadence and show observation time and sample size. Viewing a page triggers no provider calls or direct reads of private service tables.

### 11. Run the synthetic staging pilot and verify disable/recovery

**Area** deployment, integration, observability · **Blocked by** 05, 06, 07, 08, 10

**What** Exercise one recording end to end and rehearse a collection interruption. **Why** This is the acceptance boundary for expanding the implementation beyond recordings.

**Done looks like**

- [ ] Record the trial operator, access/retention settings, traffic marker, budget and agreed overhead limits. Document how to enable and disable each producer and reader, and how to resume from the last receipt.
- [ ] Record sanitized staging evidence for a normal recording and an interrupted recording with a recovered link. Reconcile the Railway call tree, component counts and member-visible outcome. Use controlled replay for the duplicate, reordered and missing-acknowledgment cases.
- [ ] With collection disabled or backed up, capture and delivery still complete within the agreed overhead limits. Verify that the dashboard shows the gap and resumes correctly. A lead identifies the partial recording and a maintainer locates the interruption from the views.

## Extend coverage by workflow

Each row is a separate feature/task ticket. The component named in **What and why** owns emission at that boundary. Its acceptance test runs against fakes. Existing service CI and the relevant API, deployment or operator documentation accompany each implementation.

| Ticket | Area | Blocked by | What and why | Done looks like |
| --- | --- | --- | --- | --- |
| **12. Trace answer admission, tool rounds and Discord delivery** | bot, observability | 03 | Instrument the bot's answer lifecycle to explain withheld, partial or slow replies. | A multipart fixture connects each tool/LLM round to one request. Denied, canceled, timed out, withheld, partial, full delivery and a delivered error message produce their specified counts. Receipt-to-full-delivery duration ends at the last required acknowledgment. |
| **13. Instrument authorized catalog search** | docs-system, observability | 02 | Record authorized search outcome, source coverage and duration so an empty result can be interpreted. | Fixtures distinguish complete-but-empty, incomplete, evidence returned, denied and failed. Only complete authorized searches enter the empty-rate denominator. Existing person/channel authorization tests pass and exported events contain no queries, source IDs or excerpts. |
| **14. Measure model and speech usage per provider attempt** | service, observability | 02 | Extend LLM chat/embedding and meeting speech telemetry to attribute latency and billable work to the caller. Land each producer in its owning service. | Fake providers exercise success, retry, cancellation, late usage and absent usage. Each call has one span and contributes its reported units once. Retry cost stays with the original feature, and incomplete usage is countable. |
| **15. Measure discovery, refresh and source freshness** | docs-system, service, observability | 02 | Instrument runner roots and connector listing/fetch spans. Publish separate content, index and access summaries so the operator can choose the right repair. | Fixtures produce a continued scan, a clean scan, a scan with gaps and a drained run with item/index failures. A reader can attribute a slow fetch to its upstream call. Tests verify eligible/unknown counts, last clean scan time and a stale summary. Existing runner alerts keep their current ownership. |
| **16. Trace gateway login across directory and verification** | service, integration, observability | 02 | Trace admission, scoped lookup, send/check and redemption where each operation actually occurs. This localizes delays and expected denials. | Synthetic gateway, directory and verification requests show the authenticated internal parent on each hop. Denial, rate limit, cooldown, timeout and successful redemption produce distinct outcomes. No mailbox, challenge, code, IP or key enters telemetry. Tests preserve existing login retry and redemption semantics. |
| **17. Show answer, catalog and service detail** | tooling, observability | 10, 12, 13, 14, 15, 16 | Extend the dashboard so a maintainer can drill into an affected answer, catalog run or HTTP service and see recent releases and resource pressure. | The wireframe examples reconcile to fixture denominators and component spans. A stale catalog summary displays its age and unknown state. Route-template volume, latency, denials, rate limits and errors can be compared with Railway CPU, memory and deployment times for the selected environment. |
| **18. Reconcile feature usage and cost** | tooling, deployment, observability | 10, 12, 14 | Build the cost view from provider units, effective-dated prices, Railway spend and invoice totals. This lets billing reviewers explain changes between periods. | A priced fixture with retries, late usage and one unreported call reconciles known cost by feature and period. The view shows coverage, shared/unattributed spend, invoice variance and cost per confirmed completion from the same cohort. With zero completions the rate is blank. Invoice detail enforces billing access. |
| **19. Route actionable telemetry alerts and recovery notices** | deployment, observability | 11 | Use the staging evidence to choose which workflow failures or collection gaps warrant notifying an operator. | For each enabled condition the runbook names an owner, destination, threshold, minimum traffic/window, grouping, silence and recovery rules. A synthetic breach sends one test notification with a restricted view/runbook link, then one recovery. Test receiver loss and notification failure. Existing readiness and runner alerts have distinct ownership. |
| **20. Review production readiness for recording telemetry** | deployment, observability | 11 | Give the operator a reproducible enable, verification and rollback procedure for the validated recording scope. | The rollout decision records measured overhead, estimated monthly volume/cost, retention, access reviewers and operator. The operator can reproduce the enable, smoke-test and rollback procedure in staging. Record either the authorized production scope and its activation owner or the reason for deferral and what would resolve it. |

Ticket 15 consumes the existing discovery and refresh summaries. Scheduled refresh acceptance remains with [#338](https://github.com/UTMIST/Misty/issues/338). Readiness activation remains with [#55](https://github.com/UTMIST/Misty/issues/55).

## Conditional follow-ups

File these evaluation tasks when the owning feature has a testable adapter or evaluator. Each evaluation depends on ticket 01 and the indicated feature capability. The destination migration has its own trigger in the final row. Report only aggregate offline measurements, labeled with the reference set and evaluator version. [Measurement definitions](OBSERVABILITY-DESIGN.md#components-for-proposed-features)

| Ticket to file | Area | Done looks like |
| --- | --- | --- |
| Evaluate Discord retrieval changes with [#332](https://github.com/UTMIST/Misty/issues/332) | docs-system, observability | Synthetic edits, deletes and access revocations yield measured visibility delays, disclosure counts and incomplete-history counts. Answerable and insufficient-evidence questions have separate citation and withholding scores. |
| Evaluate GitHub retrieval changes with [#333](https://github.com/UTMIST/Misty/issues/333) | docs-system, observability | Branch movement, path changes and a partial scan yield revision-specific citation scores, withholding scores, propagation delay and coverage. |
| Measure voice turns and interruptions with [#334](https://github.com/UTMIST/Misty/issues/334) | bot, service, observability | A synthetic conversation reports turn-end to playback-start and interruption to playback-stop times, observed/eligible pairs, canceled turns and unwanted restarts. |
| Trace MCP tool outcomes with [#335](https://github.com/UTMIST/Misty/issues/335) | integration, observability | One test invocation with source fan-out and a retry yields one tool attempt and distinct dependency spans. Denied, canceled, partial, empty and full responses have separate counts. |
| Score transcription changes with [#341](https://github.com/UTMIST/Misty/issues/341) | service, observability | A reviewed synthetic or consented set supports a baseline/candidate comparison of word error rate, term and meaning errors, speaker/timing accuracy, gaps, processing time and cost per audio minute. Only totals leave the evaluator. |
| Validate a candidate telemetry destination | deployment, observability | Once a migration is proposed and ticket 11 has passed, replay the same sanitized records into both readers. Compare parentage, linked recovery, inferred states, counts, usage coverage and visibility rules. Document cost, retention, switch and rollback before changing the destination. |
