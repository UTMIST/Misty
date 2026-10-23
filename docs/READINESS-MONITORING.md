# Readiness monitoring runbook

The external checker runs from a GitHub-hosted `ubuntu-latest` runner by
default in the `Monitor service readiness` workflow. Set
`READINESS_CHECKER_RUNNER` to an approved runner label if the endpoints are
reachable only from a private network. It runs every five minutes by default,
checks the configured HTTPS readiness URLs, and stores its debounce state in the
GitHub Actions cache so separate scheduled runs share outage state.

## Configuration

Set these repository-level values in **Settings → Secrets and variables →
Actions**:

| Name | Kind | Default | Purpose |
| --- | --- | --- | --- |
| `READINESS_CHECKER_RUNNER` | variable | `ubuntu-latest` | GitHub Actions runner label; use a controlled self-hosted runner for private endpoint access. |
| `READINESS_ENDPOINTS` | variable | `{}` | JSON object such as `{"team-tracking":"https://.../health/ready","documentation-system":"https://.../health/ready","discord-bot":"https://.../health/ready"}` |
| `READINESS_POLL_INTERVAL_SECONDS` | variable | `300` | Expected check cadence; the workflow schedule is five minutes and should be changed with this value if a different cadence is needed. |
| `READINESS_FAILURE_THRESHOLD` | variable | `2` | Consecutive failed checks before the down alert (ten minutes with defaults). |
| `READINESS_NOTIFICATION_TIMEOUT_MS` | variable | `10000` | Maximum time for one Discord webhook delivery; expiry aborts the request and retains the notification for retry. |
| `DISCORD_READINESS_WEBHOOK` | secret | unset | Dedicated Discord operations/outages channel webhook. |

The services are private on Railway by default. The selected readiness
endpoints must therefore be exposed through an approved HTTPS route or another
externally reachable monitoring host; do not put private Railway URLs in the
repository variable. The checker host is intentionally GitHub Actions rather
than a production service, so it remains independent of the services it
monitors.

The existing PR notification webhooks are not reused: readiness alerts are
operational incidents and should go to a dedicated channel. An unset webhook
does not leak endpoint details or send a misleading success notification, but
the current console summary only reports how many services were checked and
notifications sent. Failed statuses and pending alerts remain in the cached
checker state; the workflow does not print per-service failure details. Configure
the dedicated webhook for operational alerts, or explicitly inspect that state.

## Alert behavior

Each service is tracked independently. One failed check is suppressed. The
second consecutive failure sends exactly one down notification containing the
service, HTTP status (or `offline`), and UTC timestamp. Additional failures are
suppressed until a successful readiness check sends exactly one recovery
notification. A successful check resets the failure counter when no outage was
alerted.

A failed recovery delivery leaves the delivered outage open. If readiness fails
again before recovery can be delivered, the stale recovery message is discarded;
the next healthy probe still sends recovery, without another down alert. Failed,
skipped and timed-out webhook deliveries remain pending while later services
continue to be checked. The timeout cancels the webhook request so an open socket
cannot keep the checker alive after its state has been saved.

[GitHub's schedule runs only from the repository's default branch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
This repository currently defaults to `staging`, so merging this workflow there
enables its scheduled runs. Configure reachable endpoints and the dedicated
webhook, then record the controlled outage/recovery acceptance below; without
endpoints the checker only saves empty state. GitHub Actions cache is best-effort
storage: a missing/evicted cache resets the debounce history and can result in
another outage alert.

## Testing an outage

1. Temporarily point one entry in `READINESS_ENDPOINTS` at an approved test
   endpoint that returns `503`.
2. Run **Actions → Monitor service readiness → Run workflow** once. Wait at
   least `READINESS_POLL_INTERVAL_SECONDS` after the completed probe, then run
   it a second time. With the defaults, the first run is silent and the second
   sends one down alert. Account for scheduled runs, which also count as
   probes, when deciding when to dispatch.
3. Restore the real readiness URL. Wait at least the configured polling
   interval after the last completed probe, then run the workflow once. It
   sends one recovery alert.
4. Remove any test endpoint and confirm the next run reports only the real
   services.

The offline test suite exercises threshold timing, transient-failure
suppression, duplicate-alert suppression, recovery retries, configuration
defaults, and HTTP failures. CLI regressions use a loopback HTTP server with
stalled headers and response bodies; no external provider is contacted. CI's
`node-test` job and `make check` run these tests:

```bash
make test-readiness
```

## Planned maintenance

Before maintenance, disable the `Monitor service readiness` workflow in the
Actions UI. Re-enable it after all selected readiness endpoints are healthy.
If the workflow cannot be disabled, temporarily set
`READINESS_ENDPOINTS` to `{}` and restore the previous value afterward. Record
the maintenance window in the usual deployment channel so an intentional lack
of alerts is not mistaken for a monitoring failure.
