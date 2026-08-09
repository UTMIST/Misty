# Contributing

Task walkthroughs for working on `discord-bot`. Assumes you've read the [README](../README.md), which covers the architecture, env vars, and the complete cold-start sequence. This doc is the *how do I change it* companion.

The bot is the platform's only consumer-facing surface. It's Node 20+ with `discord.js`, no build step, and no framework beyond that.

## Conventions you need to know first

- **Commands are surface-neutral.** A command is a declarative definition plus a handler. The same handler serves both the real Discord surface and the browser playground — it never imports `discord.js` and never touches a Discord interaction object. Adapters translate.
- **`router.js` is the single Policy Enforcement Point.** Authenticate → authorize → dispatch, in one place. **Handlers never re-implement auth.** If you find yourself checking `access_level` inside a handler, use the declarative `auth` field instead.
- **`commands/index.js` is the single source of truth for the command set.** It's consumed by both the router (dispatch) and `registerCommands.js` (Discord registration). Adding a command is one import plus one map entry.
- **Auth policies fail closed.** `public` / `linked` / `admin` / `superuser`, evaluated in `auth/policy.js`. An unknown policy denies.
- **Replies default to ephemeral.** Bot replies carry personal directory and team information. `ephemeral: false` is an explicit, deliberate choice.
- **Service clients degrade, they don't crash.** Every backend call goes through a client (`directoryClient.js`, `docClient.js`, …) that normalizes failures. A directory outage produces "temporarily unavailable", not a stack trace in a channel.
- **The bot hard-fails at boot on missing config.** `src/config.js` lists ten required env vars and exits with `Missing required env vars: …`. That's deliberate — see `startupGuard.js`.

## Local setup

The playground is the recommended dev loop. No Discord token needed:

```bash
cd discord-bot
cp .env.example .env
npm install
npm run dev:web
```

Open `http://127.0.0.1:3001`, paste `100000000000000000` into "Acting as", pick a command, run it. `Ctrl+C` tears the whole stack down.

`dev:web` is not free of infrastructure. It shells out to `docker compose up -d postgres` in `services/team-tracking` (so **Docker must be running**), clones a scratch `team_tracking_playground` database, and spawns its own team-tracking on **port 8001**.

> ⚠️ Port **8001** is also `documentation-system`'s API port. Stop the catalog before running `npm run dev:web`.

`/record` has **no playground equivalent** — voice capture needs the real Discord surface (`npm start`).

Other modes: `npm run dev:web:plain` (web only, no scratch stack), `npm run dev:discord` (Discord only), `npm run dev` (both).

## Walkthrough: add a command

1. **Create `src/commands/<name>.js`** using `defineCommand`:

   ```js
   import { defineCommand } from '../defineCommand.js';

   export default defineCommand({
     name: 'my-command',
     description: 'What it does, in one line — this is what users see.',
     auth: 'linked',          // 'public' | 'linked' | 'admin' | 'superuser'
     ephemeral: true,         // default; false only if the reply is meant to be public
     options: [
       { name: 'target', type: 'string', required: true, description: 'Who to look up' },
     ],
     async handler({ options, principal, ctx }) {
       const person = await ctx.teamService.lookup(options.target);
       return { content: `...` };
     },
   });
   ```

   The router calls the handler with a **single destructured object**, not a
   positional context. The full set of properties it passes:

   | Property | What it is |
   |---|---|
   | `options` | The parsed option values, keyed by option name (`{}` if none) |
   | `subcommand` | The invoked subcommand name, or `null` |
   | `principal` | The resolved caller, or `null` for a `public` command with no identity |
   | `ctx` | Application context — the service clients, deployment metadata, and `commands` (already filtered to what this surface may see) |
   | `discordUserId` | The caller's Discord id |
   | `discordHandle` | The caller's Discord handle |

   - `defineCommand` validates the definition and throws on nonsense at import time — an option can't set both `choices` and `autocomplete`, a subcommand can't lack a handler.
   - **Never import `discord.js` here.** If your handler needs something only Discord provides, that's a signal it belongs in an adapter, not a command.
   - Set `beta: true` while iterating — beta commands register **only** to the testing guild and never reach production servers.
   - `identifyCaller: true` gives a `public` command a best-effort identity lookup without making auth mandatory (`/help` uses this to list only the commands you can run).

2. **Register it** in `src/commands/index.js` — one import, one entry in the `commands` map.

3. **Add subcommands** if the command is a group. `auth` and `ephemeral` inherit from the parent when unset (`sub.auth ?? auth ?? 'linked'`). **`options` does not inherit** — `defineCommand` reads `sub.options` alone, so an omitted subcommand `options` silently becomes `[]` rather than picking up the parent's. Declare options on every subcommand that needs them. `/team` and `/doc` are the models — reads public, writes admin-gated.

4. **Write tests** in `test/commands.test.js` (or a new file) using `node --test`. Test the handler directly with a fake context — that's the payoff of surface-neutral commands. Cover the auth policy too: a `linked` command called with no principal must be denied by the router.

5. **Register with Discord** when you're ready:

   ```bash
   npm run register                 # local, using your .env
   npm run register:staging         # via railway run
   npm run register:production
   ```

   Registration is a separate, manual step — merging does **not** register commands.

6. **Update the README's command list** and the root [`README.md`](../../README.md)'s "What you can do with it" section. Both enumerate the live commands, and a new command that isn't listed is invisible to users.

## Walkthrough: call a new backend service

1. **Add a client** at `src/<name>Client.js`, built on `httpClient.js`. Follow `verificationClient.js` — it's the simplest.
2. **Normalize failures.** Export a named error (`DirectoryUnavailable` is the model) so callers can distinguish "the service said no" from "the service is down". A raw fetch rejection reaching a handler produces an ugly user-facing failure.
3. **Add its config** to `src/config.js`. Decide whether it's **required** (hard-fails at boot, like `DIRECTORY_*`) or **optional** (degrades, like `MEETING_*`). Optional is right when the platform is still usable without it — `/record` reporting "not configured" is better than the bot refusing to start.
4. **Add the vars to `.env.example`** with working local defaults where possible.
5. **Mint a scoped key** against that service and document the required scopes in the README. A key with too few scopes lets the bot boot and then 403s at runtime, which is a confusing way to find out.

## Walkthrough: change auth on a command

Change the `auth` field. That's the whole change — `router.js` enforces it.

Do **not** add a check inside a handler. The single-enforcement-point property is what makes the auth model auditable: `auth/policy.js` plus the `auth` fields in `commands/` is the complete picture.

If you need a policy that doesn't exist, add it to `authorize()` in `auth/policy.js` and to the vocabulary comment at the top. Remember it must fail closed.

## Testing

```bash
npm test        # node --test
```

No Docker, no network, no Discord token. Tests drive handlers and the router directly with fake contexts and stubbed clients.

Cover both halves of a change:

- **The handler** — given options and a principal, does it return the right reply?
- **The policy** — does the router deny the calls it should? `test/auth.test.js` is the model.

## Linting

There's no ESLint config in this service. Match the surrounding style: ES modules, 2-space indent, semicolons, JSDoc blocks on exported functions.

CI runs `node-test` (`npm ci` + `npm test`) plus a Docker build with a boot smoke test.

## Gotchas

- **`DIRECTORY_API_KEY` must be issued against main (port 8000), not the playground.** A key issued against the scratch DB dies when that DB is wiped, and the symptom is "directory is temporarily unavailable". Verify with `curl http://localhost:8000/api-keys/self -H "X-API-Key: <key>"`.
- **Mint directory keys with `uv --project services/team-tracking run team-tracking-keys …`.** A bare invocation can resolve documentation-system's CLI and mint a `doc_`-prefixed key that team-tracking rejects. The token must start with `tt_`.
- **The bot needs ten env vars to boot** — `DISCORD_TOKEN`, `DISCORD_CLIENT_ID`, `DIRECTORY_BASE_URL`, `DIRECTORY_API_KEY`, `DOC_BASE_URL`, `DOC_API_KEY`, `LLM_BASE_URL`, `LLM_API_KEY`, `VERIFICATION_BASE_URL`, `VERIFICATION_API_KEY`. `MEETING_*` is genuinely optional.
- **Registering commands is manual and separate from deploying.** A merged command that was never registered doesn't exist to users.
- **Beta commands only appear in the testing guild.** If a `beta: true` command isn't showing up, check `DISCORD_GUILD_ID`.

## Checklist before you push

- [ ] Command is registered in `commands/index.js`.
- [ ] Handler imports no `discord.js` and works from the playground.
- [ ] Auth is declarative — no policy checks inside handlers.
- [ ] Replies are ephemeral unless there's a reason not to be.
- [ ] New backend calls go through a client that normalizes failures.
- [ ] New config is in `src/config.js` **and** `.env.example`, with required-vs-optional chosen deliberately.
- [ ] Tests cover the handler and the auth policy.
- [ ] Command lists updated in this service's README and the root README.
- [ ] `npm test` is clean.
