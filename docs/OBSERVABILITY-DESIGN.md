# Misty dashboard design

Start with recordings in staging. A recording can end early and still produce a PDF and a link.
The dashboard must show both the end of capture and the delivery of the requested output.
Use the same measurement rules for answers, catalog updates, and the proposed features.
[Dashboard wireframes](OBSERVABILITY-VIEWS.md#overview) · [Implementation tickets and definitions of done](OBSERVABILITY-IMPLEMENTATION.md) · [Design issue #342](https://github.com/UTMIST/Misty/issues/342)

## Distributed traces in Railway logs

Keep logs in Railway for the first implementation. Emit one structured JSON line to stdout per
observation. A maintainer can filter the environment's logs by `trace_id` to follow one attempt
across deployments, or by component and operation to investigate the same dependency across attempts.
Use the OpenTelemetry trace model for the shape of the data. [OpenTelemetry traces](https://opentelemetry.io/docs/concepts/signals/traces/) · [Railway logs](https://docs.railway.com/observability/logs)

| Field | What the producer records |
| --- | --- |
| `trace_id`, `span_id`, `parent_span_id` | Generate a trace at a Discord command, public request, or scheduled run. Each timed operation gets its own span. Omit the parent on the root. |
| `service`, `component`, `operation` | Name the emitting service, the component that observed the work, and a stable operation such as `catalog.search`. Use route templates for HTTP. |
| `event_id`, `event_name`, `event_time` | Give each observation a unique ID and a UTC timestamp. A span start, a domain event such as `capture.interrupted`, and a span end are distinct observations on the same span. Preserve the event ID on redelivery. |
| `result`, `duration_ms` | On span end, record the observed outcome and locally measured elapsed time. Use bounded reason codes for failures or interruptions. |

On authenticated Misty HTTP calls, inject W3C `traceparent` from the outgoing client span.
The receiving service makes that span the parent of its server span. Start a fresh trace at public
ingress or when internal context is missing or invalid. Keep external provider calls visible as
local client spans and keep Misty context inside the platform. A retry gets a sibling span under
the same logical operation. For a recovery job with its own root, record a span link to the
original operation when scheduling it so later confirmation can update the original attempt.
Trace context confers no access. [W3C Trace Context](https://www.w3.org/TR/trace-context/)

## State inferred from component events

For each instrumented operation in the tables below, use its start, milestones and outcome as
the evidence for its timeline and component totals. Derive state when reading those observations.
For example, capture start opens the recording, connection loss marks capture interrupted,
catalog confirmation marks the save complete, and Discord acknowledgment marks delivery complete.
Each component owns its own outcome. After those four events the workflow is partial, with a
saved Doc and a delivered link. [Recording investigation](OBSERVABILITY-VIEWS.md#recording-investigation)

Deduplicate by event ID. Match starts and ends by span and follow parent or linked spans across
processes. A late start cannot reopen a finished span. Show the last observed milestone and its
age while work is open. If the source restarts or its expected report goes stale, mark unfinished
work unknown until evidence arrives. Only an observed timeout marks work timed out.
Retain unsampled lifecycle events for these state views and totals. Sample finer diagnostic spans
independently. Read resource usage, invoices and offline evaluation totals as measurements in
their own right.

## Recording components

Each row names the component that can supply the evidence. The tables specify the proposed measurements.
An attempt starts when the bot receives a request. An accepted request has passed admission.

| Component | Event to record | Measurement | Reason |
| --- | --- | --- | --- |
| Discord bot, recording control | Request received, request accepted, capture started, capture stopped. Record the requested output and the stop reason. | Accepted requests / received requests. Started recordings / accepted requests. Interrupted recordings / started recordings. | Find a failure to start. Distinguish a normal stop from a lost connection. |
| Meeting service | The meeting service finishes the audio drain or reports a timeout. Minutes are ready, or generation fails. Record discard counts by reason and unit at session end. | Drain time and time to prepare minutes, in ms. Drain timeouts / drain attempts. Discard counts by reason. | Find a delay after stop. Find possible audio loss. Measure transcription accuracy in a separate test. |
| Connectors service, Google writer | A create or share operation finishes, fails, or has an unknown result. | Duration and count by operation and result. | Find a delay at Google. An unknown create result requires recovery before another create attempt. |
| Documentation service, catalog | The catalog confirms a saved Doc or records a pending or failed save. | Confirmed saves / recordings that requested a saved Doc. Age of the oldest pending save, in minutes. | Find the save result before you examine a missing link. |
| Discord bot, delivery | Discord accepts the link or PDF. Record a failed delivery and any later confirmation for the same recording. | Confirmed link deliveries / confirmed saves. Confirmed PDF deliveries / PDF-only requests. Time from stop to requested delivery, in ms. | Find a delivery failure after a successful save. A PDF-only request can succeed without a Doc. |

**Recording completion** = recordings with a normal stop and confirmed requested output / started recordings.
Group recordings by start time and requested output. Show open recordings and unknown results beside this rate.
A user stop and an automatic stop when the channel becomes empty are normal stops.
An interrupted recording stays partial if the bot delivers the recovered minutes.
[Recording control](../discord-bot/src/meeting/meetingSurface.js) · [Audio processing](../services/meeting/src/sessions.py) · [Link recovery](../discord-bot/docs/meeting-delivery.md#recovery)

## Answer, catalog, and support components

| Component | Event or existing source | Measurement | Reason |
| --- | --- | --- | --- |
| Discord bot, answers | Request received, admitted, denied, canceled, or timed out. Discord confirms each part of the reply. | Accepted / received. Answers with all parts delivered / accepted. Time from receipt to full delivery, in ms. Count partial and withheld replies separately. | Distinguish failure to admit a request from failure to deliver an answer. A delivered error message is a failed answer. |
| Catalog, authorized search | A search ends with evidence, an empty result, incomplete coverage, denial, or failure. | Empty searches with complete coverage / completed authorized searches with complete coverage. Search duration in ms. Counts by result and source type. | Select the next test for access, source coverage, or search performance. |
| LLM service and meeting service, provider calls | Record each call, retry, result, and usage report. The LLM service already logs tokens on successful chat responses. | Call duration in ms. Failures / calls. Reported input and output tokens or billed audio seconds. Calls with complete usage / calls started. | Find the slow provider. Include retry costs. Missing usage leaves part of the cost unknown. |
| Discovery runner | Use the run summary, the checkpoint, and the last clean scan. | Time since the last clean scan, in hours. Count continued scans and scans with gaps separately. | Find repeated runs without a clean scan. |
| Refresh runner | Use the drain result and the item counts in the run summary. | Failed refresh items / attempted refresh items. Count failed index operations separately. | Find item failures after a successful drain. |
| Catalog, document state | At the end of a run, produce one summary for eligible documents. | Count current content checks, matching index versions, and current access checks separately. Show eligible and unknown counts for each measure. Show summary age in minutes. | Select the repair for content, the index, or access. An old summary leaves the current state unknown. |
| Gateway, directory, and verification | Trace gateway admission, directory lookup, verification send/check and gateway redemption as separate operations where they occur. | Counts by operation and result, downstream duration, and rate-limited attempts. | Locate a login delay or denial without recording a mailbox, challenge, code, or key. |
| Connectors, source reads | Trace discovery listing and content or access fetches under the calling operation. | Calls, retries, failures and duration by operation and source type. | Distinguish a slow upstream read from time spent indexing in the catalog. |
| HTTP services and Railway | Use the request audit and Railway resource data. Include the gateway, directory, and verification services. | Request count and duration by route template. Separate access denials, rate limits, and service errors. Compare CPU and memory use with deployment times. | Find the service that caused a failure or delay. |
| Telemetry exporter and receiver | Record queue drops, process restarts, and the last receipt from each expected source. | Dropped events / events offered to the queue. Age of the last receipt against the source schedule. | Distinguish a quiet period from a loss of measurements. |
| Billing reports | Use reported usage, effective unit prices, Railway cost data, and invoices. | Estimated provider cost by feature and period. Show shared cost separately. Compare the estimate with the invoice for the same period. | Find the cause of a cost change. Show costs that have no known owner. |

[Chat usage](../services/llm/src/api/routers/chat.py) · [Request audit](../packages/auth/platform_auth/audit.py) · [Discovery monitors](../services/documentation-system/docs/DISCOVERY.md#failure-stuck-run-and-completion-alerts) · [Refresh monitoring](../services/documentation-system/docs/DEPLOYMENT.md#failure-and-missed-run-alerts)

## Components for proposed features

Use offline tests to review these measurements before the features enter service.
Use the same search measures for Discord and GitHub.
For both sources, measure supported answers with correct citations / answerable test questions.
Also measure correct decisions to withhold an answer / test questions with insufficient evidence.

| Component | Observation | Measurement | Reason |
| --- | --- | --- | --- |
| Discord retrieval adapter and evaluator [#332](https://github.com/UTMIST/Misty/issues/332) | A test changes or deletes a message, or removes access before a second read. | Time from the change to the first verified correct search result. Disclosures / reads after access removal. Count incomplete histories separately. | Find old evidence and access failures after a change. |
| GitHub retrieval adapter and evaluator [#333](https://github.com/UTMIST/Misty/issues/333) | A test moves a branch or changes a file path. The evaluator examines the cited lines at the requested revision. | Correct citations / citations examined. Time until the change appears. Count partial repository scans. | Find answers that mix revisions or omit part of a repository. |
| Voice adapter [#334](https://github.com/UTMIST/Misty/issues/334) | The adapter records the end of a turn and the start of local playback. It records an interruption and the end of playback. | Turn end to playback start and interruption to playback stop, in ms. Show observed pairs / eligible turns for each interval. Count unwanted restarts / interrupted turns. | Find late speech and speech that continues after an interruption. Keep canceled turns visible. |
| Model Context Protocol (MCP) server [#335](https://github.com/UTMIST/Misty/issues/335) | The server receives a tool call and sends a response. It can record denial, cancellation, or failure. | Accepted / received by tool. Count full, partial, and empty responses. Measure time from receipt to response in ms. | Distinguish incomplete evidence from a failed call. The server cannot observe how the client uses the response. |
| Transcription evaluator [#341](https://github.com/UTMIST/Misty/issues/341) | Compare recognized text and corrected text with the same reviewed reference. | Word error rate = (substitutions + deletions + insertions) / reference words. Count correct terms / reference occurrences and false substitutions / non-target words. Count utterances with a change of meaning / scored utterances. | Find a correction that improves spelling but changes a quantity or decision. |

For transcription, also measure correct speaker assignments and timing matches per aligned reference segment.
Record the timing tolerance. Compare the added processing time in ms and the cost per audio minute.
Report capture gaps and cases that the evaluator cannot score. Use synthetic material or material with specific consent.
Export only totals from the evaluation. [Offline examples](OBSERVABILITY-VIEWS.md#offline-evaluation)

## Rules for all components

- Use the root trace ID as the opaque attempt ID. Count each provider call, including retries.
- Record the schema version, component, feature, stage, result, environment, traffic type, deployment revision, and UTC event time. The receiver records receipt time.
- Measure local duration with a monotonic clock. Show the median, the 95th percentile, and the sample count. Show how many intervals have a missing start or end.
- Group requests by start time. A missing end event after a restart gives an unknown result. A confirmed timeout gives a timeout result.
- Use all observed attempts for totals. Sampled traces supply diagnostic detail. Late usage belongs to the original attempt, including after cancellation.
- Separate member traffic, background work, and test traffic. Keep individual IDs in restricted timelines. Use approved categories for metric labels.
- Exclude message text, prompts, tool arguments and results, audio, transcripts, document content, member identifiers, credentials, and email codes.
- Use a bounded export queue. Requests and audio processing must continue if the queue is full. Read stored summaries when a viewer opens the dashboard.

For cost per completion, use the same group of attempts for the cost and the count.
Divide their known provider cost, including retry costs, by their confirmed completions.
Show usage coverage with the result. Leave the result blank when the group has no confirmed completions.

Start the staging trial with a synthetic recording. From its Railway records, reconstruct the
call tree and the capture, save, and delivery outcomes. Replay the records out of order with a
duplicate delivery acknowledgment and verify the same state and counts. Repeat with the
acknowledgment missing and a bot restart to verify unknown delivery. A future migration can
map the same records to an OpenTelemetry exporter or another log store. Use this replay to
check the new destination against Railway before switching readers.

Test the overview with a lead and the timeline with a maintainer.
Keep the existing runner alerts. Readiness polling has a separate approval process.
Agree on access, data retention, alert limits, an operator, and a budget before live collection.
[Readiness status](https://github.com/UTMIST/Misty/issues/55)
