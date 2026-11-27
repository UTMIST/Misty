# Recording telemetry operations

William owns the recording pilot. The local tool reads a private collector store and a recording snapshot. It does not need Discord, Google or service database credentials to display a page. Use the collector from [#416](https://github.com/UTMIST/Misty/issues/416) with this dashboard.

## Run a local synthetic exercise

Use Node 20+ and Python 3.10+ from the repository root. The importer, projector, server and alert evaluator use the standard libraries. Keep a separate owner-only directory for each environment. The pilot refuses to overwrite either input path.

```sh
umask 077
pilot_dir=$(mktemp -d)
python3 scripts/telemetry/pilot.py \
  --db "$pilot_dir/raw.sqlite" --snapshot "$pilot_dir/recordings.sqlite"
```

The fixture sends the real importer a PDF-only success, an interrupted saved recording with both deliveries, and a normal saved recording with late acknowledgment. It reverses the input and repeats observations. The local exercise should show one complete PDF-only recording and one complete of two started saved recordings. Both saved recordings reached Discord.

Generate a random individual credential and save its digest. Run this command only on the private local operator terminal. The plaintext is displayed once for account setup. Repeat for each authorized person in the same access file. End a grant by removing its entry and restarting the server.

```sh
python3 - "$pilot_dir/access.json" <<'PY'
import hashlib, json, pathlib, secrets, sys
path = pathlib.Path(sys.argv[1])
grants = json.loads(path.read_text()) if path.exists() else []
user, environment, role = "william", "local", "developer"
secret = secrets.token_urlsafe(32)
grants.append(dict(user=user, environment=environment, role=role,
                   sha256=hashlib.sha256(secret.encode()).hexdigest()))
path.write_text(json.dumps(grants))
path.chmod(0o600)
print(user, secret)
PY
python3 scripts/telemetry/dashboard.py --environment local \
  --db "$pilot_dir/raw.sqlite" --snapshot "$pilot_dir/recordings.sqlite" \
  --access "$pilot_dir/access.json"
```

Open `http://127.0.0.1:8090/local/overview` and use the generated credentials. Select **Synthetic** and the hour covering the fixture. For a remote private host use an authenticated SSH tunnel to its loopback port. The server accepts HTTP Basic credentials only on loopback. Run one process, credential file and database pair per environment. A lead can open aggregate recording, component and health views. Only a developer or admin can open individual attempts. Switching an environment in the URL or copying a detail link still requires a corresponding grant.

## Investigate a recording

1. Start with the requested output, started count and completion rate. The cohort is the UTC time of capture start. A request that failed before capture appears in the outcome counts. A recovered link updates its original cohort. Filters preserve the selected traffic class, end time, period and previous period.
2. Open the interrupted attempt. Check the capture stop reason, service drain result, save, link and PDF independently. A voice loss stays interrupted after successful delivery. A normal stop requires linked stop and confirmed drain evidence plus all requested acknowledgments. An old client can leave the drain unknown.
3. Follow the nested operation in the call tree. Compare its results and elapsed times with the previous window. A retry is a separate call. Empty latency cells indicate missing timing evidence. The median uses all timed observations, and p95 requires at least 20 samples. The component page links the incident owner’s service runbook.
4. For a lost Google create reply, inspect catalog reconciliation and the connector search before diagnosing another create. For Discord acknowledgment failures use the [delivery recovery procedure](../discord-bot/docs/meeting-delivery.md#recovery). For speech loss or drain timeout use [meeting troubleshooting](../services/meeting/docs/DEPLOYMENT.md#troubleshooting).

The overview refreshes every minute on a rolling window. An explicit end time pins the investigated window. The snapshot generation time and per-source coverage times indicate freshness. The source table shows cumulative gaps, rejected records and reported producer drops. A dropped-record count cannot identify the affected session. Missing parents, stale observations and pending delivery remain visible. Open capture requires a matching recent bot boot and a current source. A later observed boot or stale source leaves its outcome unconfirmed. Detail pages show up to 500 observations across at most 101 linked traces. A component call duration is measured by its producer and includes its children.

## Collect, project and recover

Schedule the #416 reader once per minute for each trusted service, then run the projection and alert evaluation. Use `collect.py railway --help` for the required source, environment and token parameters. Keep synthetic and member traffic separate. In particular, a legacy bot record derives its traffic class from the configured source. Use an isolated synthetic bot/log source for a pilot. Do not label a mixed-traffic source synthetic.

```sh
python3 scripts/telemetry/project.py --environment local \
  --db "$pilot_dir/raw.sqlite" --snapshot "$pilot_dir/recordings.sqlite"
```

The projector rebuilds the last 30 days from validated events into a private temporary snapshot. A successful rebuild replaces the previous snapshot atomically. Schedule a single projector per database. Track command exit status, collector checkpoints, disk use and snapshot age. All page reads use read-only SQLite connections. A snapshot built for another environment causes the request to fail closed.

To rehearse loss of collection, stop polling for longer than the configured stale interval. Keep the dashboard, alert evaluator and retention schedule running. Check that activity becomes unknown and the collection alert opens. Resume the same source and checkpoint, rerun the projector, and check the healthy source advances and the collection incident resolves. Expired Railway history leaves a gap. Recovery cannot reconstruct missing provider logs. To turn off collection permanently, stop the poller and projector, remove the reader credential and erase the private pilot files before their retention limit. To disable producer spans use the producer controls in [the collector and emitter documentation](OBSERVABILITY-COLLECTION.md).

## Alerts and recovery

The evaluator reads stored observations and supports two groups. Collection health covers the bot, meeting, catalog and connectors plus snapshot freshness in one environment. Recording health counts observed interruptions or errors among started recordings within one traffic class. It needs a fresh bot and snapshot plus the configured minimum started count. Empty or stale cohorts leave an existing recording incident open until new evidence can settle it.

For a local rehearsal use the sample values below. These thresholds are fixture inputs. William must choose thresholds and an approved destination for any operational environment.

```sh
python3 scripts/telemetry/alerts.py --environment local --traffic synthetic \
  --db "$pilot_dir/raw.sqlite" --snapshot "$pilot_dir/recordings.sqlite" \
  --state "$pilot_dir/incidents.sqlite" --owner William \
  --dashboard-url http://127.0.0.1:8090 --window-minutes 60 \
  --min-started 3 --failure-count 1 --max-age-seconds 300
```

The default prints the two conditions and makes no network call. To deliver real alerts, provision an approved Discord webhook through the private `RECORDING_ALERT_WEBHOOK` environment variable and add `--send`. Each message contains environment, traffic, condition, owner and dashboard/runbook links. Mentions are disabled. Use `--silence-until` with an offset-aware timestamp to suppress transitions during planned work. On expiry, evaluation uses the current observations.

The first acknowledged breach opens an incident. A repeated breach is suppressed. A healthy observation resolves it once. A failed webhook call exits nonzero and leaves the transition eligible for retry. A crash after remote acceptance and before the local commit can repeat a message. Monitor evaluator process failures independently. All material here is safe to exercise using the test receiver injected by the offline test suite.

## Access, retention and rollout decisions

Limit read access to raw and projected SQLite files, temporary copies and backups to the pilot operator. Direct filesystem access bypasses dashboard roles. Keep the host, volume and backups under the same environment boundary. Keep the collector prune job running during collection outages, and rebuild or expire snapshots independently of traffic. Remove offline volumes and backups before their oldest trace reaches 30 days. The local stores constrain raw traces and associated projections to 30 days. A longer-lived aggregate retention period needs an owner decision.

With the producer disabled and enabled, measure the same synthetic audio workload, output acknowledgments, finalization latency, queue drops, reader lag, database growth, projection wall time and Railway/provider charges. Agree an overhead limit and spend cap with William before enabling a real pilot. No budget, overhead threshold or production rollout has been approved. The staging recording, staging collection interruption and external notification delivery remain deferred. The local replay and HTTP tests exercise the code and its recovery rules.

```sh
python3 -m unittest discover -s scripts/telemetry -p test_dashboard.py
```
