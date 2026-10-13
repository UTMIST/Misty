import { test } from 'node:test';
import assert from 'node:assert/strict';
import helperLimits from '../src/commands/helper-limits.js';
import { createHelperRequestLimiter } from '../src/helperRequestLimiter.js';
import { dispatch } from '../src/router.js';

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

test('dispatch treats Discord null optional fields as absent', async () => {
  const appContext = {
    directory: { getPersonByDiscordId: async () => ({ access_level: 'admin' }) },
    helperRequestLimiter: createHelperRequestLimiter({ maxRequests: 10, windowSeconds: 3600 }),
  };
  const result = await dispatch(
    {
      surface: 'discord',
      commandName: 'helper-limits',
      subcommand: 'set',
      options: { max_requests: '3', window_seconds: null },
      discordUserId: 'admin-user',
    },
    { commands: new Map([['helper-limits', helperLimits]]), appContext },
  );
  assert.match(result.content, /3.*3600/);
  assert.deepEqual(appContext.helperRequestLimiter.getLimits(), {
    maxRequests: 3,
    windowSeconds: 3600,
  });
});

test('dispatch rejects a helper-limits set with no Discord options', async () => {
  const appContext = {
    directory: { getPersonByDiscordId: async () => ({ access_level: 'admin' }) },
    helperRequestLimiter: createHelperRequestLimiter(),
  };
  const result = await dispatch(
    {
      surface: 'discord',
      commandName: 'helper-limits',
      subcommand: 'set',
      options: { max_requests: null, window_seconds: null },
      discordUserId: 'admin-user',
    },
    { commands: new Map([['helper-limits', helperLimits]]), appContext },
  );
  assert.match(result.content, /Provide max_requests, window_seconds, or both/);
});
