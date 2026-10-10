# Discord PR previews

`dev` is one persistent Railway environment with a dedicated Discord
application. Selecting a PR deploys that commit into the environment. The bot's
identity, invite, and test server stay the same. Its gateway, recording, and
command handlers all run the selected PR's code, including the meeting backend.

There is no event mux, worker protocol, `/use` command, or public event endpoint.
All communication between the preview services stays on Railway's private
network. Ordinary Railway PR environments keep their Discord bot idle; they
cannot register commands or open a gateway connection with inherited tokens.

## One-time setup

The selected PR must include this preview support. After it lands on `staging`,
update older PR branches from staging so their startup and registration code
includes it. The initial preview-support PR can bootstrap the environment.
The selector checks for `discord-bot/src/runtimeMode.js` at the PR's exact head
commit before contacting Railway. A missing file fails preflight; this checks
compatibility, not the correctness of arbitrary changes inside that file.

1. Create a Discord application named `misty-dev` in the Developer Portal,
   separate from production and staging. Create its bot, enable **Message
   Content Intent**, and invite it to a dedicated test server with the same
   permissions as Misty (including the voice and thread permissions). Leave
   the Interactions Endpoint URL unset: this bot receives interactions over
   its gateway connection.
2. Create a persistent Railway environment named exactly `dev`. Copy
   staging's service configuration as **staged changes**, without deploying
   it yet. Remove the copied `DISCORD_TOKEN`, `DISCORD_CLIENT_ID`, and
   `DISCORD_GUILD_ID` from this environment's bot service before deployment.
   Do not make `dev` the base for automatic PR environments.
3. Give the three database services their own Neon development branches and
   replace their `DATABASE_URL` values **before running any migrations**.
   Follow [the deployment runbook](RAILWAY-DEPLOYMENT.md) for service settings
   and key provisioning. Keep backend URLs as same-environment Railway
   references. Use development email/provider settings appropriate to your
   tests; copying Railway config does not sandbox external providers.
4. Keep the services connected to the `UTMIST/Misty` repository, but disable
   automatic GitHub deployments for **every service in `dev`**. The
   selector refuses environments with deployment triggers. All preview
   deployment changes should go through the selector so a staging merge or a
   push to another branch cannot replace the selected PR during a recording.
5. Attach a Railway volume to the preview's `discord-bot` service, for example
   at `/data`, in the same region as the bot. No bot database is stored there:
   its purpose is Railway's guarantee that deployments sharing that volume
   cannot overlap. A replica
   count of one or an overlap time of zero alone does not prevent simultaneous
   old/new processes during startup. Keep `meeting` at one replica as usual.
   See [Railway's volume caveats](https://docs.railway.com/volumes/reference#caveats).

   The startup volume requirement applies only to `dev` so this feature does
   not break existing staging/production deployments that have no volume.
   Gateway overlap is also a risk there; this PR does not establish a singleton
   guarantee for those environments. Extending it requires a coordinated
   infrastructure rollout with volumes provisioned before enforcing the guard.
6. Set the following on the bot in the `dev` environment's variable editor:

   | Variable | Value |
   |---|---|
   | `MISTY_PREVIEW_ENVIRONMENT_ID` | The literal UUID of this environment, copied from its Railway URL. **Do not** use `${{RAILWAY_ENVIRONMENT_ID}}`: that reference would resolve to each copy's ID. |
   | `DISCORD_TOKEN_DEV` | New dev bot's token; enter it in the variable editor, never in chat or Git. Seal it after entry. |
   | `DISCORD_CLIENT_ID_DEV` | New dev application's ID. |
   | `DISCORD_GUILD_ID_DEV` | Dedicated test server ID. |
   | `ENABLE_DISCORD` / `ENABLE_WEB` | `true` / `false`. |

   Set the existing backend URL/key variables using the deployment runbook,
   including `MEETING_BASE_URL` and `MEETING_API_KEY` for recording tests.
   Missing dev credentials never fall back to staging credentials. The
   selector does not read or copy Discord credentials.

   These guards are deliberate: the name selects the runtime, the literal ID
   prevents a copied environment renamed `dev` from inheriting ownership, and
   the dedicated credential names prevent falling back to copied staging
   credentials. If recreating `dev`, update its literal ID in Railway before
   deploying; the startup error identifies this setting.
7. Commit the reviewed environment configuration with deployments skipped
   (`environmentPatchCommitStaged` with `skipDeploys: true`). Verify automatic
   deployment triggers are disabled after this commit: committing a source
   configuration can recreate them. Select a PR containing preview support
   using the command below for the first deployment. Confirm it works in the
   dev server. The environment needs no public domain.

Only trusted same-repository PRs targeting `staging` are accepted by the
selector. Their code runs with the preview's credentials. This is a development
deployment, not a sandbox for untrusted code.

## Select a PR

### From GitHub

After this workflow has merged into `staging` (the repository's default branch),
open **Actions → Preview Discord PR → Run workflow**. Leave the branch on
`staging`, enter the PR number, and confirm recordings have finished and their
minutes have arrived. The workflow uses the same selector as the terminal and
serializes its runs without cancelling an active switch.
Dispatching from another branch fails the validation job with a staging-branch
instruction, before the deployment job accesses the `dev` environment.

One-time Actions setup: create a GitHub environment named `dev`, restrict its
deployment branches to `staging`, and add its `RAILWAY_DEV_TOKEN` secret using a
Railway project token scoped to Misty's **`dev` environment only**. Do not use
an account token or a production/staging token. A repository Actions secret
with the same name also works. The workflow passes it to the Railway CLI as
`RAILWAY_TOKEN`. The workflow reads the selector from `staging`; it never checks
out or executes the selected PR on the runner.
Railway builds that PR's commit with the preview's existing service credentials.
With `RAILWAY_TOKEN`, the selector resolves the token's project and environment
through Railway's [project-token query](https://docs.railway.com/integrations/api#using-a-project-token),
checks the project's PR-base ID, and reads only that environment. It does not
enumerate other environments. A token for another project or an environment
other than persistent `dev` fails before any deployment changes.

GitHub only exposes manual workflows after they exist on the default branch;
see [GitHub's workflow-dispatch documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
The terminal command can bootstrap this PR before that merge.

### From the terminal

Install and authenticate `gh` and Railway CLI (5.28+), then:

```bash
cd discord-bot
npm ci
npm run preview -- 123 --plan
npm run preview -- 123
```

The required `api --variables --compact --allow-errors` options are present in
[CLI 5.28.0](https://github.com/railwayapp/cli/blob/v5.28.0/src/commands/api.rs).
Actions pins 5.49.6 for reproducible runs.

Misty's project is the default; no project ID or environment export is needed.
For another installation, see the selector override in the
[bot configuration table](../discord-bot/README.md).

Each new deployment has a 45-minute wait budget. For a slower cold build, use
`--timeout-minutes 60` (a positive whole number, applied per deployment). Old bot
shutdown has one shared 15-minute deadline for all prior bot deployments.
Build/queue polls back off from 3 to 30 seconds; shutdown and startup-transition
polls stay at 3 seconds. Actions allows six hours: the
current seven-service maximum needs up to 330 minutes of waits plus setup and
API overhead. Review that budget when adding services or changing timeouts;
use the terminal for sequences that could exceed the Actions limit.

The command prints the PR's exact commit and target services. Before confirming,
stop any recording and wait for its minutes to arrive. Switching will disconnect
the dev bot and restart the backends. For a noninteractive invocation, pass
`--recordings-stopped` to attest that this is already done. `--plan` never
changes deployments.

Preflight compares `dev` with the `services/*` directories at the selected SHA,
the same directory inventory enforced by the label-consistency workflow. Every
backend must be provisioned in `dev`, except the currently optional `connectors`
service; an omitted connector is explicitly reported as untested. A Railway
service absent from the PR also fails preflight. New backends are included
automatically and run after the known backend dependencies, before the bot.
If a new backend must start earlier, update the ordering preference in
`discord-bot/scripts/lib/preview.js`.

The selector scans the bot's paginated deployment history and retains gateway
IDs observed before confirmation. The shutdown set includes runnable/removing
deployments, observed gateways, and historical records with nonterminal
instances. Historical failed, skipped, or removed records without live
instances are ignored even if their stop flag is stale. A newer failed build
cannot hide an older live or draining gateway. The selector waits until Railway
confirms shutdown of that set before restarting any backend.
Historical records already confirmed stopped, including crashes, need no new
removal. A current crash between restarts is still removed to prevent it from
restarting during the switch. An operator-initiated bot removal in `REMOVING`
passes preflight and is allowed to drain; backend removals must settle first.
It deploys each configured backend from the selected commit, accepts `SUCCESS`
or `SLEEPING` for backends, and deploys the bot last. The bot must reach
`SUCCESS`; sleeping is not sufficient for its gateway. Its pre-deploy step registers
that PR's command definitions against the dev application. Normal readiness
requires a connected gateway.

Use the dev bot in Discord to test `/record start`, audio capture, `/record
stop`, auto-stop, reconnects, threads, and other native behavior. Each selection
is pinned to the PR's current head commit; run the command again to test later
commits. Closing or merging a PR does not automatically replace the persistent
slot. Select another open PR when ready.

Run one selector at a time, including terminal runs while an Actions run is
active. The command checks for unfinished or competing
deployments, and the bot volume prevents overlapping gateway owners. It does
not provide an atomic transaction across all backend deployments. Each service
must retain its pre-switch deployment ID until selected; afterward, its ID must
match the deployment started by this switch. A competing deployment of a backend
still waiting its turn aborts the sequence before it can be overwritten.

## Failure and recovery

- A backend deployment failure leaves the bot disconnected. Inspect the named
  deployment, fix the cause, and rerun the selection command. It does not
  reconnect to staging or silently fall back to an older PR.
- A timeout or interrupted CLI may leave a Railway build running. Inspect and
  wait for or cancel that deployment before retrying; the command refuses to
  switch while builds are unfinished.
- Existing sleeping deployments do not block selection. A deployment awaiting
  approval must be approved or cancelled first. While switching, the CLI logs
  status changes. New deployments fail on failed terminal states; backend
  `SLEEPING` is accepted and explicitly reported. After a removal request,
  even `CRASHED` or `FAILED` may still be a stale status: the selector polls
  until both `REMOVED` and `deploymentStopped: true` are observed, or the stop
  deadline expires. It starts no new deployment before confirmation.
- CLI failures include their installation, login, access, or GraphQL error
  details. A missing API resource identifies its field and requested ID;
  disappearance is not treated as confirmed shutdown. Address the reported
  cause before rerunning the command.
- Database contents persist across PR selections. The command does not reset
  databases or reverse migrations. Before switching between incompatible
  schemas, provision fresh development branches and update the preview URLs
  and scoped keys using the deployment runbook.
- To take the preview offline, remove the active deployment of the bot in the
  **`dev` environment**. The environment and Discord identity remain for
  the next selection.

## Automatic Railway PR environments

One runtime environment check controls both gateway startup and command
registration. Local development and the exact names `staging` and `production`
retain their existing behavior. Only `dev` with the configured literal
environment ID can use the preview credentials. All other Railway environments
keep Discord disabled regardless of copied enable flags or credentials.
The environment guard applies to Discord only: `ENABLE_WEB=true` still starts
the existing playground with its required backend configuration and `dev:spoof`
scope check. In that case, liveness starts only after the web server starts;
a failed playground boot cannot leave a healthy idle process behind.

Disabled Discord returns **503** from `/health/ready`. This includes automatic
PR copies and misspellings such as `prod` or `Production`: they must not appear
healthy while disconnected. Railway's documented
[runtime variables](https://docs.railway.com/variables/reference) do not expose
whether the environment is ephemeral, so the bot does not infer a healthy idle
mode from a PR-name pattern. There is no idle CLI flag. The persistent `dev`
slot is the supported path for Discord previews.

For automatic PR copies, `railway.json` uses an
[`environments.pr` override](https://docs.railway.com/config-as-code/reference#pr-environment-overrides)
to check `/health/live`, which returns 200 for a running idle process with
`discord: disabled`. Railway selects that override for ephemeral deployments;
it sets the liveness path and replaces registration with the no-op script
`node scripts/pr-predeploy.js`, which logs the skip and exits successfully.
No shell quoting or empty-array clearing is required. Directly running the
registration CLI in any unknown environment, including a PR copy, exits nonzero
with an environment error. Runtime mode
still comes from the same environment check, and persistent environments keep
their registration step and `/health/ready`. This
avoids failed-deployment notifications for intentional PR no-ops while an
accidentally renamed persistent environment still fails readiness.

This is only a bot safeguard. Before enabling full-stack automatic PR
environments, separately arrange isolated database branches: copied Neon URLs
would otherwise let preview migrations alter the base database. The persistent
`dev` workflow does not require automatic PR environments to be enabled.

## Verification

Run `npm test`, `npm run lint`, and `npm run format:check` in `discord-bot`.
Offline tests cover environment ownership, copied credentials and flags,
registration suppression, PR liveness and unavailable persistent readiness,
the stop-before-deploy sequence, stale history exclusion, paginated shutdown
tracking behind failed builds, crash loops, scoped-token target selection,
already-stopped crashes, manual removal, first deployment, playground boot and
its scope guard, source compatibility and service inventory, sleeping
backends, CLI diagnostics, argument validation, confirmation races, cold builds,
polling backoff, timeouts, competing deployments,
and production/staging exclusion. A live recording round trip is still required
after provisioning; the automated suite never logs in to Discord or calls paid
providers.
