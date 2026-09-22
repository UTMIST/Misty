# Helper-bot per-user request allowance

Implements #68 under helper-bot epic #81, following the threaded helper from
#66. Configuration names and defaults are in the
[README](../README.md#helper-bot-request-limits) and [`.env.example`](../.env.example).

## Boundary and accounting

`context.js` constructs one `helperRequestLimiter` for the application context.
`adapters/discord.js` calls its synchronous `tryConsume(message.author.id)` after
linked-user authorization and before creating/fetching a thread or invoking
`helperService.answer`. A denied request sends a friendly slowdown reply and
never reaches `llmClient.chat`. Reserving before the next await prevents
concurrent mentions from sharing the last available slot.

- The key is the authenticated Discord message author's ID. Display names,
  directory person IDs, guilds, channels, thread IDs, and authors replayed from
  history do not choose the account. One user shares an allowance across all
  servers/threads served by this process; other users have independent counters.
- One admitted, nonempty, linked-user mention consumes one request immediately.
  New threads and follow-up mentions count alike. Historical turns and reply
  chunks do not count separately.
- Reservations are not refunded: failed thread creation/history fetch, empty
  history, LLM errors/timeouts/empty answers, and failed Discord replies still
  consume the slot. This deliberately conservative rule prevents retry storms
  from bypassing the guard, including when a provider may already have charged.
- Bare pings, ignored bot messages, unlinked/unauthorized callers, and directory
  authorization failures do not consume a slot. Rejections do not consume more
  slots or extend the existing reset deadline.
- This is a request-count allowance, **not an exact token or dollar cap**. The
  current helper makes one LLM call per answer and retains its existing history
  and output bounds. Variable prompt sizes/model prices still vary cost. Any
  future multi-call/tool loop must revisit accounting before adding paid calls.
  No enforcement is added to `services/llm`, other bot commands, or meeting calls.

## Reset and lifetime

Each user's fixed window starts on their first admitted request. At or after
its deadline the next request gets a fresh allowance. A rejection reports the
remaining wall-clock duration rounded up to whole seconds. Different users can
have different reset times. Fixed windows permit a burst on either side of a
reset boundary; this is not a rolling-window or concurrency limit.

Counters live only in a `Map`, with expired entries removed lazily on the next
allowance check. There is no timer, database, background service, or persisted
usage. Restart/redeploy or a new application context resets all allowances.
Environment changes require restart and therefore reset usage too. Run this
with awareness that each process/replica has its **own** allowance; it is not a
shared quota across replicas or staging/production deployments. Host clock
changes can move the effective reset time.

Configuration validation fails boot for invalid values rather than silently
turning protection off. Request limits must fit a positive JavaScript safe
integer. Window seconds must also remain safe when multiplied by 1000.

## Offline verification

From `discord-bot/`:

```sh
npm ci
npm test
npm run lint
npm run format:check
```

`test/helper-bot.test.js` exercises the Discord request path through the real
helper service with a fake LLM client: an exhausted user never makes the next
LLM call, while another Discord ID still succeeds. It also covers existing
threads, concurrent mentions, authorization/non-question exclusions, and failed
attempts. `test/helperRequestLimiter.test.js` checks independent windows, exact
expiry, non-extending denials, defaults, reset-on-new-instance, and invalid
inputs using an injected clock. `test/config.test.js` checks startup validation
and application-context wiring. No live Discord connection or paid provider
call is needed.
