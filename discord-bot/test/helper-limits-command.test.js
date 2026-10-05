import { test } from 'node:test';
import assert from 'node:assert/strict';
import helperLimits from '../src/commands/helper-limits.js';
import { createHelperRequestLimiter } from '../src/helperRequestLimiter.js';

function ctx() {
  return {
    helperRequestLimiter: createHelperRequestLimiter({ maxRequests: 10, windowSeconds: 3600 }),
  };
}

test('helper-limits show reports the current values', async () => {
  const result = await helperLimits.subcommands[0].handler({ ctx: ctx() });
  assert.match(result.content, /10.*3600/);
});

test('helper-limits set changes values without clearing usage', async () => {
  const appContext = ctx();
  appContext.helperRequestLimiter.tryConsume('user');
  const result = await helperLimits.subcommands[1].handler({
    options: { max_requests: '1', window_seconds: '60' },
    ctx: appContext,
  });
  assert.match(result.content, /1.*60/);
  assert.equal(appContext.helperRequestLimiter.tryConsume('user').allowed, false);
});

test('helper-limits set rejects missing and invalid values', async () => {
  const appContext = ctx();
  const missing = await helperLimits.subcommands[1].handler({ options: {}, ctx: appContext });
  assert.match(missing.content, /Provide/);
  const invalid = await helperLimits.subcommands[1].handler({
    options: { max_requests: '0' },
    ctx: appContext,
  });
  assert.match(invalid.content, /Invalid helper limit/);
});
