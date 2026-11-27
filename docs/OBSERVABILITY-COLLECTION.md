# Collecting the telemetry pilot

William owns the pilot. The collector reads one Railway service in one environment
per invocation. It writes allowlisted operational events, source health and hourly
component totals to SQLite on a private volume. Staging enablement, a spend limit
and an overhead budget require a separate decision.

## Producer configuration

Set process environment variables on participating services. The bot's dedicated
recording marks use their existing recording tracer. The shared Node and Python
spans have these controls.

| Variable | Meaning |
| --- | --- |
| `TELEMETRY_ENABLED` | Exactly `true` enables shared spans. Default off. |
| `TELEMETRY_ENV` | Required when enabled. Choose `local`, `staging` or `production`. |
| `TELEMETRY_TRAFFIC` | `member` by default. Use `synthetic` for a dedicated test source and `background` for workers. |
| `TELEMETRY_TRUSTED_TRAFFIC_KEYS` | Optional comma-separated attested internal key names that may set traffic on this Python service. Use separate issued keys for jobs and user-facing callers. Default empty. |
| `TELEMETRY_SERVICE` | Canonical service name for Python work outside an HTTP request when the caller does not specify a service. |
| `RAILWAY_DEPLOYMENT_ID` | Deployment UUID supplied by Railway. Unavailable or invalid values become `unknown`. |

Python requests accept one valid W3C version-00 parent after internal authentication.
For mixed internal HTTP traffic, configure the destination's trusted key names.
A valid parent plus one `x-misty-traffic` header from a listed issued key sets
the request class. Bootstrap keys, repeated headers and public callers cannot
change it. Otherwise the service uses its configured default. The public gateway
starts a fresh trace. The route label comes from FastAPI's
matched template and must exist in the contract. Unmatched routes share `other`.
Private HTTP clients create child spans per request and disable redirect following
for traced requests. Public provider SDKs receive their ordinary provider headers.
Use an explicit trace ID plus a link to a persisted span for detached work.

## Reader and access

Give the collector an OAuth bearer with `project:viewer` consent for the single
intended Railway project. Inject it through the secret environment variable
`RAILWAY_VIEWER_TOKEN`. The operator supplies or rotates this access token.
The reader sends one GraphQL query to the fixed Railway Public API origin, rejects
redirects and checks environment and service UUIDs on returned rows. It reads
environment logs across deployments. [Railway scopes](https://docs.railway.com/integrations/oauth/scopes-and-user-consent) ·
[Railway environment logs](https://docs.railway.com/integrations/api/manage-environments) ·
[Environment log filters](https://docs.railway.com/observability/logs#environment-logs)

Choose a distinct persistent DB and mode-0700 directory per environment. The file
is mode 0600. Limit the OS account, volume access and detailed trace views to
developers and admins. Publish aggregate output through the scoped dashboard.
Run under the volume owner. Do not place unfiltered Railway dumps on the volume.

```sh
mkdir -m 700 /path/to/private-pilot
python3 scripts/telemetry/collect.py railway \
  --db /path/to/private-pilot/events.sqlite --environment staging \
  --environment-id "$PILOT_ENVIRONMENT_UUID" --service meeting \
  --service-id "$PILOT_MEETING_UUID" --traffic synthetic
```

Schedule a poll for each participating service at least once a minute. A poll
covers up to 60 seconds, ending five seconds behind wall time, with five seconds
of overlap. Poll more frequently to catch up after downtime. The first poll starts
one window before its target end. Supply an explicit historical `--until` to
choose an earlier starting location. Supply `--available-since` from the
provider's actual retention window. If coverage expired, the reader advances from
the earliest available time and records the gap. Without a supplied provider floor,
the reader can only detect expiration at its own retention floor.

On saturation, reduce `--window-seconds` and then `--overlap-seconds` until the
reader can certify a full page. A window with 200 or more source lines, a response
over 8 MiB, a timed-out request, invalid provider tags or a damaged transport file
preserves the last committed checkpoint, observations and summary. Errors update
the source health independently. Extremely dense windows require a provider-side
export with an independently verified bounded window or a reader upgrade. A hard
process crash can lose the undelivered emitter tail. A successful empty poll
confirms the reader covered that source window. It cannot certify an idle producer
was running. Late provider receipts beyond the overlap require a replay import.

```sh
python3 scripts/telemetry/collect.py status --environment staging --db /path/to/private-pilot/events.sqlite
python3 scripts/telemetry/collect.py summary --environment staging --db /path/to/private-pilot/events.sqlite
python3 scripts/telemetry/collect.py prune --environment staging --db /path/to/private-pilot/events.sqlite
```

Run prune independently at least daily, including when polling is disabled.
Raw events and aggregates expire at 29 days to allow a daily cleanup before the
30-day maximum. The oldest partial hour may disappear from summaries first.
Use the same expiry schedule for snapshots and backups. Monitor the cleanup job
outside this DB. Delete or expire the volume on retirement. Manual copies and
provider log retention have separate controls. If the collector host is offline
long enough to miss expiry, remove expired storage before returning it to use.

## Event contract and measurements

The JSON allowlist and reader validator define the wire contract. Each Node and
Python shared span produces `start` and `end` with the schema
`misty.telemetry.v1`. IDs are opaque nonzero lowercase hex. A parent identifies
a caller in the same trace. A link carries both trace and span IDs. UTC `ts` is
the producer's wall time. `received_at` is the Railway receipt timestamp.
`elapsed_ms` comes from a monotonic clock. A new process has a fresh
`process_id`. Shared emitters also report process start, offered sequence and
drops since the preceding successful write. Every field, result, operation,
numeric measure and route must be recognized. Independent recording marks use
`misty.recording.v1` from the selected bot source. Their traffic class is the
operator's `--traffic` value. Keep separate sources for mixed test/member runs.
[Wire labels](../packages/auth/platform_auth/telemetry_contract.json) ·
[Record validation and store schema](../scripts/telemetry/store.py)

The pilot emits all eligible observations. Required workflow marks and terminal
component spans stay unsampled. Missing terminal evidence gives an unknown
outcome. Partial trees remain visible. Process sequence gaps and reported drops
are evidence of loss. A replay uses the immutable event ID (or an exact recording
payload digest). A second end for one span cannot inflate component totals.

| Signal and source | Owner | Unit and denominator | Allowed dimensions | Expected freshness |
| --- | --- | --- | --- | --- |
| Recording outcomes from bot capture, drain, save, PDF and Doc-link marks | Bot maintainer | Attempts per distinct started recording. Count pre-capture denial separately. | Environment, traffic, mode, outcome | Next poll and projection. Recovery can revise the same attempt. |
| Component throughput and errors from shared span ends | Service maintainer | Completed calls per hour, errors per observed completed call. Each retry has its own span. | Environment, traffic, service, operation, bounded result | Next poll, target 1 minute |
| Component duration from shared span ends | Service maintainer | Monotonic milliseconds per completed call. Stored totals and maxima admit an arithmetic mean. | Same component dimensions. Restricted detail also holds route and deployment. | Next poll |
| Finalization discard reasons from meeting.finalize end | Meeting maintainer | Observed handler counters per finalized session. Preserve each reason's original frame/message/finalization unit. | Environment, traffic, fixed reason | Next poll after stop |
| Answer, search and login lifecycle from workflow spans | Workflow maintainer | Distinct observed admissions/ends per workflow, downstream attempts per span | Environment, traffic, operation, bounded result/mark | Next poll |
| Discovery and refresh from runner spans | Catalog maintainer | Run outcomes per ended run, attempted and failed item counts per run | Environment, traffic, runner, bounded measure | Next poll after run |
| Freshness from catalog.freshness end | Catalog maintainer | Current or unknown eligible items per dimension divided by eligible count | Environment, traffic, content/index/access | After completed inventory |
| Reader coverage from sources table | William | Seconds since covered time per service, cumulative gap/reject/drop counters | Environment, service, fixed status | Check after each poll. Over 180 seconds is stale. |

`events` contains private attempt detail. `summaries` counts shared span ends
once. It stores hours in UTC and has no trace IDs. `sources.checkpoint` and
`last_success` are the latest committed coverage times. `observed_at` is the last
poll attempt. An unseen source is unknown. A failed read retains last success.

## Offline replay

Run from the repository root. The fixture contains the expected trees and
outcomes for six recordings, including retries, a cancelled finalizer, interrupted
audio, delayed confirmation and a restart with no final span end. The generator
returns duplicates and puts ends before starts. Select an anchor one minute in
the past so all events fall inside the import window.

```sh
pilot_dir=$(mktemp -d)
anchor=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat())')
for service in discord-bot meeting documentation-system; do
  python3 scripts/telemetry/recording_fixture.py --anchor "$anchor" --service "$service" > "$pilot_dir/$service.jsonl"
  python3 scripts/telemetry/collect.py import --db "$pilot_dir/events.sqlite" \
    --environment local --service "$service" --traffic synthetic --input "$pilot_dir/$service.jsonl"
done
python3 scripts/telemetry/collect.py summary --environment local --db "$pilot_dir/events.sqlite"
python3 -m unittest discover -s scripts/telemetry -p test_collect.py
```

Import treats each input file as a trusted, complete transport window through its
target end. Supply `--until` when replaying a bounded historical export.
Unknown application logs are ignored in memory. Unsupported schemas or malformed
recognized events increment rejects. Keep local replays on a separate volume.
[Example and expected outcomes](../scripts/telemetry/fixtures/recording-v1.json)
