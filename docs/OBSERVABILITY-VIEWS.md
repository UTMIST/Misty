# Misty dashboard wireframes

[Overview](#overview) · [Usage and cost](#usage-and-cost)

These two views use the same header. Keep the header visible when a viewer opens a detail view.
All values are examples. The [measurement design](OBSERVABILITY-DESIGN.md) identifies the source of each value.

| Environment | Traffic | Period | Compare with |
| --- | --- | --- | --- |
| Staging | Synthetic | 6 Nov 2026, 13:00–14:00 UTC | 12:00–13:00 UTC |

## Overview

**One recording stopped early.** The catalog saved the Doc. Discord accepted the link.
[Inspect recordings](#recordings)

| Feature | Result this hour | Condition to investigate |
| --- | --- | --- |
| [Recordings](#recordings) | 4 of 5 recordings ended normally and delivered the requested output. | 1 connection loss |
| [Answers](#answers) | The bot delivered complete answers for 22 of 24 accepted requests. | 1 cancellation and 1 unknown result |
| [Catalog](#catalog) | Discovery completed a clean scan. Refresh completed its run. | 2 item failures and 1 missing summary |

The last event reached the receiver at 14:00 UTC. The readiness monitor has no configured endpoints.
[Provider estimate of USD 2.50](#usage-and-cost) · [Readiness status](https://github.com/UTMIST/Misty/issues/55)

## Recordings

[Overview](#overview) / Recordings

| Requested output | Started | Discord confirmed delivery | Normal stop and confirmed delivery |
| --- | ---: | ---: | ---: |
| Saved Doc and link | 3 | 3 | 2 |
| PDF only | 2 | 2 | 2 |

Median time from normal stop to delivery was **18 s** for 4 recordings.
The median was **16 s** for 4 recordings in the previous hour. The sample is too small to show the 95th percentile.

[Open recording R3 to examine the connection loss](#recording-investigation)

## Recording investigation

[Recordings](#recordings) / R3 · Maintainer access

| Time (UTC) | Component | Event |
| --- | --- | --- |
| 13:18:00 | Discord bot | Capture started. The user requested a saved Doc. |
| 13:38:00 | Discord bot | Connection lost. The bot requested the minutes. |
| 13:38:02 | Meeting service | Audio drain complete |
| 13:38:12 | Meeting service | Minutes and PDF ready |
| 13:38:17 | Catalog | Doc save complete |
| 13:38:18 | Discord bot | Discord accepted the link. |

**State at 13:38:18 UTC** · Capture interrupted · Doc saved · Link delivered · Recording partial

Selecting an event highlights its span in the trace. The excerpt below expands the save
operation for R3. Each indentation follows a parent span ID. Durations include child calls.

```text
Catalog      Save recording                 4.8 s  confirmed
  Catalog      Call connectors                4.1 s  success
    Connectors   Handle Google write            4.0 s  success
      Connectors   Call Google Drive              3.6 s  success
```

Selecting the Google Drive call opens the connectors component view, filtered to that
operation in the same environment and period. The example includes all observed calls.

| Operation | Calls | Success | Failure | Unknown | Median duration of finished calls |
| --- | ---: | ---: | ---: | ---: | ---: |
| Google Drive create | 3 | 2 | 0 | 1 | 3.4 s |

The unknown call belongs to another recording. Its last observation was a start before a
worker restart. The component view links that observation back to its recording trace.

**Meeting maintainer** · Examine the connection and audio processing at 13:38.
The last deployment of the meeting service was at 13:12 UTC.
The duration of missing speech is unknown. [Meeting troubleshooting](../services/meeting/docs/DEPLOYMENT.md#troubleshooting)

## Answers

[Overview](#overview) / Answers

| Received | Denied before admission | Accepted | Delivered | Canceled | Unknown |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 26 | 2 | 24 | 22 | 1 | 1 |

Median time from receipt to delivery was **4.6 s** for 22 answers.
The bot restarted before it recorded the last result for one request.

## Catalog

[Overview](#overview) / Catalog

| Source | Last evidence (UTC) | Result |
| --- | --- | --- |
| Discovery runner | 13:04 | Clean scan complete |
| Refresh runner | 13:05 | 2 of 40 items failed. The queue is empty. |
| Catalog | 12:05 | Old summary. Current document state unknown. |

At 12:05, 38 of 40 documents had current content checks. The index matched 37 documents.
Access checks were current for 39 documents. The expected summary after the 13:05 run is missing.
[Refresh runbook](../services/documentation-system/docs/DEPLOYMENT.md#failure-and-missed-run-alerts)

## Usage and cost

[Overview](#overview) / Usage and cost · All estimates in USD

| Provider work | Previous estimate | Current estimate | Calls with usage / calls started |
| --- | ---: | ---: | ---: |
| Chat for answers | 1.20 | 1.80 | 30 / 32 |
| Speech and minutes for recordings | 0.60 | 0.60 | 12 / 12 |
| Embeddings for the catalog | 0.10 | 0.10 | 2 / 2 |
| **Total for reported usage** | **1.90** | **2.50** | **44 / 46** |

The chat estimate increased by **USD 0.60**. Accepted answer requests increased from 18 to 24. Chat calls increased from 22 to 32.
Two calls have unknown cost. The separate Railway estimate is USD 0.40 for shared resources.
The provider invoice is pending.

## Offline evaluation

Select **Offline evaluation** in feature detail to compare designs. Show the reference set, scoring rules, and versions for each comparison.
Keep these results separate from the staging totals.

| Component | Example result |
| --- | --- |
| MCP server and retrieval adapters | 1 tool call, 2 source searches, 3 downstream calls. Discord returned an empty result. GitHub retried once and returned partial evidence. |
| Discord retrieval evaluator | 1 read after access removal. No disclosure. |
| GitHub retrieval evaluator | 2 of 3 citations pointed to the correct lines at the requested revision. |
| Voice adapter | Playback started for 4 of 5 turns. Median delay was 640 ms for those 4 turns. Playback stopped 90 ms after the interruption on 1 turn. |
| Transcription evaluator | Word errors decreased from 8 to 5 per 100 reference words. Correct terms increased from 3 to 5 of 5. One of 10 utterances changed meaning. The baseline had none. |

## Display states and access

| Condition | Display |
| --- | --- |
| The query is in progress. | Loading |
| The source is current and the count is zero. | No activity. Leave the rate blank. |
| A source report is late or collection stops. | Current value unknown. Show the time of the last receipt. |
| A feature has no instrumentation. | No measurements configured |
| The viewer lacks permission. | Access restricted. Show a link to the overview. |

Read stored totals once per minute. Show the observation time for each source.
Leads can see totals. Maintainers can open individual attempts in their environment. Billing reviewers can open invoices.
Apply the same access rules when a viewer opens a shared link.
