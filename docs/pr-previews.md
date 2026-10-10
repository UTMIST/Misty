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

One-time Actions setup: create a GitHub environment named `dev`, restrict its
deployment branches to `staging`, and add its `RAILWAY_TOKEN` secret using a
Railway project token scoped to Misty's **`dev` environment only**. Do not use
an account token or a production/staging token. The workflow reads the selector
from `staging`; it never checks out or executes the selected PR on the runner.
Railway builds that PR's commit with the preview's existing service credentials.

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

Misty's project is the default; no project ID or environment export is needed.
For another installation, `MISTY_PREVIEW_PROJECT_ID` overrides the default and
`--project <id>` overrides both.

The command prints the PR's exact commit and target services. Before confirming,
stop any recording and wait for its minutes to arrive. Switching will disconnect
the dev bot and restart the backends. For a noninteractive invocation, pass
`--recordings-stopped` to attest that this is already done. `--plan` never
changes deployments.

The selector stops the old bot and waits until Railway confirms termination.
It deploys each configured backend from the selected commit, waits for each
deployment to succeed, and deploys the bot last. Its pre-deploy step registers
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
not provide an atomic transaction across all backend deployments.

## Failure and recovery

- A backend deployment failure leaves the bot disconnected. Inspect the named
  deployment, fix the cause, and rerun the selection command. It does not
  reconnect to staging or silently fall back to an older PR.
- A timeout or interrupted CLI may leave a Railway build running. Inspect and
  wait for or cancel that deployment before retrying; the command refuses to
  switch while deployments are unfinished.
- Database contents persist across PR selections. The command does not reset
  databases or reverse migrations. Before switching between incompatible
  schemas, provision fresh development branches and update the preview URLs
  and scoped keys using the deployment runbook.
- To take the preview offline, remove the active deployment of the bot in the
  **`dev` environment**. The environment and Discord identity remain for
  the next selection.

## Automatic Railway PR environments

The bot's `railway.json` uses Railway's special `environments.pr` overrides to
start an idle health listener and skip command registration. Runtime guards also
leave unknown Railway environments idle, regardless of copied enable flags or
credentials. This avoids depending on an undocumented PR-name regex. Local
development, `staging`, and `production` retain their existing behavior.

This is only a bot safeguard. Before enabling full-stack automatic PR
environments, separately arrange isolated database branches: copied Neon URLs
would otherwise let preview migrations alter the base database. The persistent
`dev` workflow does not require automatic PR environments to be enabled.

## Verification

Run `npm test`, `npm run lint`, and `npm run format:check` in `discord-bot`.
Offline tests cover environment ownership, copied credentials and flags,
registration suppression, idle readiness, the stop-before-deploy sequence,
failed backends, timeouts, competing deployments, and production/staging
exclusion. A live recording round trip is still required after provisioning;
the automated suite never logs in to Discord or calls paid providers.
