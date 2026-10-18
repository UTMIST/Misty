import test from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig, runChecks, shouldPoll } from './readiness-checker.js';

const config = {
  endpoints: { bot: 'https://bot.example.test/health/ready' },
  failureThreshold: 2,
  timeoutMs: 100,
  webhook: undefined,
};

function fetchSequence(results) {
  let index = 0;
  return async () => {
    const result = results[Math.min(index++, results.length - 1)];
    if (result instanceof Error) throw result;
    return result;
  };
}

test('alerts on the check that reaches the sustained outage threshold', async () => {
  const notifications = [];
  const first = await runChecks(config, {
    fetchImpl: fetchSequence([{ ok: false, status: 503 }]),
    sendNotification: async (content) => notifications.push(content),
    now: () => new Date('2026-10-17T18:00:00.000Z'),
  });
  assert.equal(first.state.services.bot.consecutiveFailures, 1);
  assert.equal(notifications.length, 0);

  const second = await runChecks(config, {
    state: first.state,
    fetchImpl: fetchSequence([{ ok: false, status: 503 }]),
    sendNotification: async (content) => notifications.push(content),
    now: () => new Date('2026-10-17T18:05:00.000Z'),
  });
  assert.equal(second.state.services.bot.consecutiveFailures, 2);
  assert.match(notifications[0], /Service outage/);
  assert.match(notifications[0], /18:05:00/);
});

test('does not alert for a transient failure and alerts once on recovery', async () => {
  const notifications = [];
  const failed = await runChecks(config, {
    fetchImpl: fetchSequence([new Error('connection refused')]),
    sendNotification: async (content) => notifications.push(content),
  });
  const recovered = await runChecks(config, {
    state: failed.state,
    fetchImpl: fetchSequence([{ ok: true, status: 200 }]),
    sendNotification: async (content) => notifications.push(content),
  });
  assert.equal(notifications.length, 0);
  assert.equal(recovered.state.services.bot.consecutiveFailures, 0);
});

test('sends exactly one down alert and one recovery alert', async () => {
  const notifications = [];
  const sendNotification = async (content) => notifications.push(content);
  let state = { services: {} };
  for (let i = 0; i < 3; i += 1) {
    ({ state } = await runChecks(config, {
      state,
      fetchImpl: fetchSequence([{ ok: false, status: 503 }]),
      sendNotification,
    }));
  }
  ({ state } = await runChecks(config, {
    state,
    fetchImpl: fetchSequence([{ ok: true, status: 200 }]),
    sendNotification,
  }));
  assert.equal(notifications.filter((message) => message.includes('outage')).length, 1);
  assert.equal(notifications.filter((message) => message.includes('recovered')).length, 1);
});

test('loads safe defaults and rejects non-HTTPS endpoints', () => {
  const loaded = loadConfig({ READINESS_ENDPOINTS: '{"bot":"https://bot.test/health/ready"}' });
  assert.equal(loaded.failureThreshold, 2);
  assert.equal(loaded.pollIntervalSeconds, 300);
  assert.throws(
    () => loadConfig({ READINESS_ENDPOINTS: '{"bot":"http://bot.test/health/ready"}' }),
    /must be an HTTPS URL/,
  );
});

test('respects the configured polling interval between scheduled runs', () => {
  const state = { services: { bot: { lastCheckedAt: '2026-10-17T18:00:00.000Z' } } };
  assert.equal(shouldPoll(state, new Date('2026-10-17T18:04:59.000Z'), 300), false);
  assert.equal(shouldPoll(state, new Date('2026-10-17T18:05:00.000Z'), 300), true);
});

test('continues checking later services and retains a failed alert for retry', async () => {
  const checked = [];
  const failed = await runChecks(
    {
      endpoints: {
        first: 'https://first.example.test/health/ready',
        second: 'https://second.example.test/health/ready',
      },
      failureThreshold: 1,
      timeoutMs: 100,
    },
    {
      fetchImpl: async (url) => {
        checked.push(url);
        return { ok: false, status: 503 };
      },
      sendNotification: async () => {
        throw new Error('webhook unavailable');
      },
    },
  );

  assert.deepEqual(checked, [
    'https://first.example.test/health/ready',
    'https://second.example.test/health/ready',
  ]);
  assert.equal(failed.notifications.length, 0);
  assert.equal(failed.state.services.first.alertedDown, false);
  assert.equal(failed.state.services.first.pendingNotification.type, 'down');
});

test('retries an outage alert after the webhook is configured', async () => {
  const first = await runChecks(
    { ...config, failureThreshold: 1 },
    {
      fetchImpl: fetchSequence([{ ok: false, status: 503 }]),
    },
  );
  assert.equal(first.notifications.length, 0);
  assert.equal(first.state.services.bot.alertedDown, false);
  assert.equal(first.state.services.bot.pendingNotification.type, 'down');

  const notifications = [];
  const second = await runChecks(
    { ...config, failureThreshold: 1, webhook: 'https://discord.example.test/webhook' },
    {
      state: first.state,
      fetchImpl: fetchSequence([{ ok: false, status: 503 }]),
      sendNotification: async (content) => notifications.push(content),
    },
  );
  assert.equal(notifications.length, 1);
  assert.match(notifications[0], /Service outage/);
  assert.equal(second.state.services.bot.alertedDown, true);
  assert.equal(second.state.services.bot.pendingNotification, undefined);
});

test('retains a failed recovery alert for retry', async () => {
  const down = await runChecks(
    { ...config, failureThreshold: 1 },
    {
      fetchImpl: fetchSequence([{ ok: false, status: 503 }]),
      sendNotification: async () => {},
    },
  );
  const recovered = await runChecks(
    { ...config, failureThreshold: 1 },
    {
      state: { ...down.state, services: { bot: { ...down.state.services.bot, alertedDown: true } } },
      fetchImpl: fetchSequence([{ ok: true, status: 200 }]),
      sendNotification: async () => {
        throw new Error('webhook unavailable');
      },
    },
  );
  assert.equal(recovered.state.services.bot.alertedDown, true);
  assert.equal(recovered.state.services.bot.pendingNotification.type, 'recovery');
});
