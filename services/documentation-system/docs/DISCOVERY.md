# Automatic Google sharing discovery

Issue [#291](https://github.com/UTMIST/Misty/issues/291) imports newly shared files.
[#292](https://github.com/UTMIST/Misty/issues/292) owns recurring refresh of catalogued
content and source access. Both are required for the sharing release. Discovery
uses authenticated HTTP to connectors and the catalog; it has no catalog database
connection and never holds the Google credential.

## Members: sharing with Misty

Ask the operator for the connector service-account email and confirmation that
**discovery is enabled in your environment**. Share a supported file or My Drive
folder with that address. The current ACL-reading setup requires sufficient source
sharing to inspect the complete permission list; the verified setup uses **Editor**
sharing. Viewer access can permit content reads while preventing the required ACL
inspection. Misty uses read-only Google API scopes and never changes your sharing.

No per-file `/doc add` or folder registration is required. Supported descendants,
including nested folders and later additions, are discovered on recurring scans.
The proposed schedule is every 15 minutes. Small scans normally import on the next
run plus fetch/index time; large scans continue across runs, so this is not a
15-minute latency guarantee. Staging must measure actual latency before release.

Sharing with Misty does **not** make a document visible to everyone. Intended people
also need their own source permission and a primary/verified matching email in the
directory, or a supported, synchronized team Google Group grant. Unknown principals,
link-wide/domain-wide shares and unmapped groups create no audience. Search with
`/doc search` as the intended member. If a file is absent, ask the operator to inspect
registration, ACL mapping and indexing. Sharing more broadly is not a recovery step.

## Coverage and exclusions

The connector enumerates direct shares using `sharedWithMe = true` in Drive's `user`
corpus, then explicitly pages each discovered folder with `'<id>' in parents`.
It does not assume a root listing contains inherited descendants. Both queries use
`trashed = false`. Empty intermediate pages with continuation tokens are followed.
This follows Google's [file-list API](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list)
and [search syntax](https://developers.google.com/workspace/drive/api/guides/search-files).

Coverage is My Drive files directly shared with the configured identity and readable
descendants of shared My Drive folders. The offline tests exercise this topology;
**live staging acceptance is still required**. This is not organization-wide search,
user OAuth, or discovery of arbitrary URLs that members can access.

Supported formats come from the connector's existing extractor registry: native
Docs, Sheets, Slides, Forms (Drive/edit identity), uploaded PDF, `.docx`, and `text/*`.
Docs/Sheets/Slides map to `gdocs`/`gsheets`/`gslides`; the others use `gdrive`.
The catalog checks that the source is active and content fetching is enabled before
import. See [connector format limits](../../connectors/README.md) for extraction
loss, text caps and upload limits. No new extractor is introduced here.

Shortcuts are counted and not followed. Their targets can still be imported if
independently shared/enumerable. Shared Drive items returned by the user corpus are
counted as `shared_drive` and skipped; membership-based Shared Drive enumeration is
not implemented or claimed. Unsupported MIME types are counted as `unsupported`.
Published Forms response URLs, link-only public content without enumerable shares,
and folders with denied/limited access do not establish discovery coverage.

`DISCOVERY_EXCLUDED_IDS` excludes exact file ids and folder traversal. A descendant
shared directly or reachable through another non-excluded folder remains eligible;
this is not a security denylist. Excluding or disabling discovery never revokes
existing catalog records or grants. Manage source permissions and use normal catalog
retirement for that purpose.

## Enable, inspect and disable

1. Deploy the connectors and documentation-system revisions together. This deliberate
   cross-zone change has no schema migration. Keep discovery disabled initially.
2. Issue a dedicated catalog key with `docs:write` and a connectors consumer key with
   `discover`. Install the latter hash in connectors' `CONSUMER_KEYS`, following its
   [key runbook](../../connectors/docs/DEPLOYMENT.md#consumer-keys). Keep the catalog
   API's existing `fetch`-scoped connectors key separate. Discovery itself requires
   no Google OAuth scope beyond the connector's existing read-only scopes.
3. Create exactly one dedicated staging cron service using
   `services/documentation-system/railway.discovery.json`, repository-root build
   context and the same revision as the APIs. Disable PR-preview schedules. It runs
   `python -m src.discovery` every 15 minutes, one replica, no HTTP healthcheck,
   pre-deploy migration, or automatic failure restart.
4. Attach a persistent volume at `/data`, set
   `DISCOVERY_STATE_PATH=/data/discovery-state.json` and an explicit
   `DISCOVERY_IDENTITY` identifying this environment and connector account. Set both
   service base URLs and the two dedicated API keys on **the runner**. Do not copy
   API service database or Google variables. See the [configuration table](../README.md#automatic-sharing-discovery).
5. Set `DISCOVERY_ENABLED=true` and trigger the initial run. Subsequent invocations
   resume unfinished scans or start another full scan. No folder ids are configured.
6. Inspect sanitized run summaries and `python -m src.discovery --status` with the
   same volume and settings when no run holds the lock. Status shows enabled state,
   scan start/completion times, pending work and counts, never ids or page tokens.
7. Disable by setting `DISCOVERY_ENABLED=false` and removing/pausing the cron schedule.
   An already running process finishes its current bounded invocation unless stopped;
   a stopped process resumes from its checkpoint after re-enabling. Retain the volume.

Local invocation (export variables from `.env.example` deliberately; the runner does
not load dotenv):

```bash
uv --directory services/documentation-system --project . run python -m src.discovery
uv --directory services/documentation-system --project . run python -m src.discovery --status
```

To rotate keys, install replacements at their APIs and on the runner, verify a run,
then revoke the old keys. To switch connector accounts or environments, stop the
schedule and use a new identity label **and a separate checkpoint path**. Never reuse
one checkpoint across accounts, API environments, or independent services.

## Bounds, checkpoints and monitoring

Each invocation defaults to at most 20 Drive listing pages and 100 handled items,
with 100 results per page and 10,000 unique folders per full scan. A metadata page
is checkpointed before its files are imported. Each confirmed import result advances
that checkpoint. Pending work, retry attempts, visited folders and each listing's
consumed cursors survive interruption. An OS lock rejects overlapping runs on the same
volume. Run only one service: locks on separate volumes do not coordinate.
Checkpoints contain file ids and opaque page tokens and
are operator-only (mode 0600); they contain no content or credentials.

The 900-second budget gates new requests; HTTP phase timeouts do not cancel server
work already accepted. Catalog requests execute the existing fetch/index pipeline;
Google transport timeouts, extractor caps and an ACL limit of 20 pages of 100
permissions bound that path. A missing or looping ACL page fails closed before
content extraction. Listing has no internal retries. Import transport failures,
HTTP 408/429 and server errors (5xx) allow at most three attempts per pending item
across invocations. The first two failures stop without an immediate replay; the next
invocation safely rechecks identity. The third failure counts as `import_failed` and
advances to later work. A later full scan retries the file. Authentication,
configuration and malformed-response errors stop without consuming this retry budget
or skipping the pending item. Existing checkpoints default to zero retry attempts
and empty cursor history; history is collected as their current listing resumes.

Exit codes:

- `0`: full scan completed without import/listing gaps (or explicitly disabled).
- `1`: traversal finished with incomplete listings, failed imports, registered files
  without content, or indexing failures. Completion is not proof all files indexed.
- `2`: budget reached, overlap, configuration, HTTP or response failure; checkpoint
  retained. Bounded continuation is expected for a large corpus but must converge.

Counts distinguish `indexed`, `existing` (current stored index), `registered`
(missing content or initial Google ACL sync), `index_failed`, `retired`, `disabled`,
unsupported types,
shortcuts, Shared Drives, exclusions, deferred `import_retries` and listing/import
failures. A full scan's last counts remain available while another scan runs. Unresolved audience/extraction
warnings are counted, not printed. Do not log upstream bodies, source URLs, content,
checkpoint payloads, or API keys.

Before enabling staging, assign an operator and alert destination. Alert on exits
1/2, repeated continuation without a completed scan, overlap, stuck runs, and no
completed scan for 24 hours. Tune limits/cadence from measured workload and alert
on loss of progress, not merely the existence of a checkpoint. No alert integration
is provisioned by this code change.

## Recovery and lifecycle

New discoveries store native Docs, Sheets and Slides editor links ending in `/edit`;
other Drive files use `/view`. The display URL is separate from the normalized Drive
identity used for deduplication. Migration `017` repairs existing Drive display links
without a re-crawl; see the [migration and rollback notes](DEPLOYMENT.md#migrations-alembic).

Existing catalog entries are identified by Google file id across supported Docs,
Sheets, Slides, Forms edit, Drive file and `open?id=` URL aliases, including old
manual records and editor URLs with `u/<number>/` account selectors. New ingests
normalize those aliases to one key, so concurrent new
imports share the existing active-URL uniqueness constraint. Published `/d/e/` links
are not Drive file identities and keep their distinct URL keys. Legacy duplicates are
not merged or deleted: lookup prefers the active, earliest record. Explicitly retired
records are skipped, preventing recurring discovery from undoing an operator action.
The final discovery identity check and insert coordinate with all Google inserts in
storage, so a manual registration/retirement during fetching also prevents discovery
from creating a replacement. Explicit manual re-ingest after retirement stays allowed.

Discovery does not refetch/re-embed existing records. It reports missing content or
stale/missing chunks for recovery. Registration can succeed before fetching/indexing:
use [source refresh](DEPLOYMENT.md#source-access-refresh-schedule) or
`POST /docs/{id}/refetch` to recover content/ACLs; use `POST /docs/index` with a separate
`docs:index` operator key for index failures. Both reuse the existing pipeline and its
unchanged-content guard. Do not add a second content refresh loop to discovery.

Old Forms edit URLs may have been catalogued as `web`. Discovery corrects an active
Google identity with that source to the enabled Google source from its MIME listing,
preserving the record id, URL, content, tags and manual grants. It reports `registered`
until the first successful Google ACL reconciliation, even if an old web index exists.
The existing source-refresh schedule can now select the record; an operator can use
`POST /docs/{id}/refetch` to run reconciliation sooner. Failed ACL reads add no grants
or fetched content and retain the normal refresh retry cadence. Retired records and
disabled discovery sources are not reclassified. New manual Forms edit imports use
`gdrive` by default; explicit caller sources and configured URL patterns still win.

An inaccessible folder or a failed page is counted; later queued folders continue.
A later full scan retries it. Rejected/expired Drive tokens become failed listings;
the next full scan starts them without an old token. A repeated cursor, including a
multi-page cycle across scheduled invocations, discards that page and counts as
`listing_failed` so later folders can proceed. Cursor history is kept separately for
each listing and discarded when that listing finishes or fails. Other malformed HTTP
responses stop without advancing the current checkpoint. After correcting
configuration/provider problems, retry normally. For a corrupt checkpoint or persistent pagination failure,
stop the schedule, retain a restricted backup, and start from a fresh checkpoint;
catalog identity dedup makes replay safe. Increasing a folder bound can resume a
bounded checkpoint; never edit raw cursors while the job is running.

Deletion, lost access, folder moves and incomplete listings never trigger retirement,
content deletion or permission widening in discovery. A file moved out of one folder
can remain eligible via another share. Existing documents remain #292's responsibility:
confirmed disappearance removes content/index via the established `SourceGone`
contract; temporary access/ACL failures retain only the prior grants until expiry.
Default refresh eligibility is 24 hours and grant expiry 48 hours. Discovery does
not change that access-change latency. Manual grants retain their independent semantics.

## Controlled staging acceptance — pending

No live acceptance or production activation is implied by passing offline tests.
Record environment, deployed revision, operator, configured cadence/limits, start
and completion timestamps, sanitized counts, latency and alert destination here or
in linked release evidence. Keep document contents, tokens and credentials out.

1. Share a new supported file directly and a folder containing supported files and
   a nested folder with the connector. Verify registration and indexed state without
   any `/doc add` or folder-root setup. Record share-to-index/search latency.
2. Add a new file to that folder later, then share another folder. Observe a scheduled
   scan discovering both. Exercise enough pages/items to prove continuation.
3. Run `/doc search` as an explicitly authorized member and as an unauthorized member;
   the latter must receive no private result. Coordinate share → discovery → answer
   acceptance with [#278](https://github.com/UTMIST/Misty/issues/278).
4. Repeat the scan and verify no duplicates or unchanged re-embedding. Test a manual
   URL alias, an unsupported format and a denied folder. Interrupt and resume a run.
5. Modify content and revoke a source grant. Verify #292 refresh changes indexed text
   and effective access; verify expiry on refresh failure. Test confirmed deletion,
   folder movement and failed-index recovery through the documented endpoints.
6. Observe a manual and a scheduled run, failure/missed-run alerts, disable/re-enable,
   and checkpoint persistence across redeploy. Record unsupported topology explicitly.

Production activation remains a separate rollout decision after these gates.

Offline verification and the rationale for each changed file are recorded in
[PR #293](https://github.com/UTMIST/Misty/pull/293); they are not staging evidence.
