import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
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

test('discards a pending recovery when the latest probe is down', async () => {
  const notifications = [];
  let state = { services: {} };
  for (let i = 0; i < 2; i += 1) {
    ({ state } = await runChecks(
      config,
      {
        state,
        fetchImpl: fetchSequence([{ ok: false, status: 503 }]),
        sendNotification: async (content) => notifications.push(content),
      },
    ));
  }
  const recoveryFailed = await runChecks(
    config,
    {
      state,
      fetchImpl: fetchSequence([{ ok: true, status: 200 }]),
      sendNotification: async () => {
        throw new Error('webhook unavailable');
      },
    },
  );
  let downAgain = await runChecks(config, {
    state: recoveryFailed.state,
    fetchImpl: fetchSequence([{ ok: false, status: 503 }]),
    sendNotification: async (content) => notifications.push(content),
  });
  downAgain = await runChecks(config, {
    state: downAgain.state,
    fetchImpl: fetchSequence([{ ok: false, status: 503 }]),
    sendNotification: async (content) => notifications.push(content),
  });
  assert.equal(notifications.filter((message) => message.includes('recovered')).length, 0);
  assert.equal(notifications.filter((message) => message.includes('outage')).length, 1);
  assert.equal(downAgain.state.services.bot.pendingNotification, undefined);
  assert.equal(downAgain.state.services.bot.alertedDown, true);
});

test('recovers after a failed recovery delivery and a single failing probe', async () => {
  const notifications = [];
  let recoveryAttempts = 0;
  let state = { services: {} };
  for (const status of [503, 503, 200, 503, 200, 200]) {
    const result = await runChecks(config, {
      state,
      fetchImpl: fetchSequence([{ ok: status === 200, status }]),
      sendNotification: async (content) => {
        if (content.includes('recovered') && ++recoveryAttempts === 1) {
          throw new Error('webhook unavailable');
        }
        notifications.push(content);
      },
    });
    // Scheduled runs reload JSON rather than sharing objects in memory.
    state = JSON.parse(JSON.stringify(result.state));
  }
  assert.equal(notifications.filter((message) => message.includes('outage')).length, 1);
  assert.equal(notifications.filter((message) => message.includes('recovered')).length, 1);
  assert.equal(state.services.bot.alertedDown, false);
  assert.equal(state.services.bot.pendingNotification, undefined);
});

for (const webhookStatus of [undefined, 503, 200]) {
  test(`CLI releases stalled HTTP requests (${webhookStatus ?? 'no webhook headers'}), saves state and exits`, { timeout: 10_000 }, async (t) => {
    const directory = await mkdtemp(join(tmpdir(), 'misty-readiness-'));
    t.after(() => rm(directory, { recursive: true, force: true }));
    let requests = 0;
    const server = createServer((request, response) => {
      request.resume();
      if (request.url === '/webhook') {
        requests += 1;
        if (webhookStatus === undefined) return;
        response.writeHead(webhookStatus);
      } else {
        response.writeHead(503);
      }
      // Send headers but leave the body open until the client cancels it.
      response.flushHeaders();
    });
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    t.after(() => {
      server.closeAllConnections();
      server.close();
    });

    const stateFile = join(directory, 'state.json');
    const probeFixture = `
      const realFetch = globalThis.fetch;
      globalThis.fetch = (url, options) => {
        if (String(url).startsWith('https://readiness.example.test/')) {
          return realFetch('http://127.0.0.1:${server.address().port}/probe', options);
        }
        return realFetch(url, options);
      };
    `;
    const child = spawn(process.execPath, [
      '--import', `data:text/javascript,${encodeURIComponent(probeFixture)}`,
      fileURLToPath(new URL('./readiness-checker.js', import.meta.url)),
    ], {
      cwd: directory,
      env: {
        READINESS_ENDPOINTS: JSON.stringify({
          first: 'https://readiness.example.test/first',
          second: 'https://readiness.example.test/second',
        }),
        READINESS_FAILURE_THRESHOLD: '1',
        READINESS_NOTIFICATION_TIMEOUT_MS: '100',
        READINESS_STATE_FILE: stateFile,
        DISCORD_READINESS_WEBHOOK: `http://127.0.0.1:${server.address().port}/webhook`,
      },
    });
    t.after(() => child.kill('SIGKILL'));
    let stderr = '';
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.stdout.resume();
    const deadline = setTimeout(() => child.kill('SIGKILL'), 3000);
    t.after(() => clearTimeout(deadline));
    const [code, signal] = await once(child, 'close');
    clearTimeout(deadline);
    assert.equal(signal, null, `CLI kept stalled requests alive: ${stderr}`);
    assert.equal(code, 0, stderr);
    assert.equal(requests, 2);
    const state = JSON.parse(await readFile(stateFile, 'utf8'));
    for (const service of ['first', 'second']) {
      assert.equal(state.services[service].lastStatus, 503);
      assert.equal(state.services[service].alertedDown, webhookStatus === 200);
      assert.equal(state.services[service].pendingNotification?.type, webhookStatus === 200 ? undefined : 'down');
    }
  });
}

test('times out a stalled notification and continues polling later services', async () => {
  const checked = [];
  const result = await runChecks(
    {
      endpoints: {
        first: 'https://first.example.test/health/ready',
        second: 'https://second.example.test/health/ready',
      },
      failureThreshold: 1,
      notificationTimeoutMs: 5,
    },
    {
      fetchImpl: async (url) => {
        checked.push(url);
        return { ok: false, status: 503 };
      },
      sendNotification: async () => new Promise(() => {}),
    },
  );
  assert.deepEqual(checked, [
    'https://first.example.test/health/ready',
    'https://second.example.test/health/ready',
  ]);
  assert.equal(result.state.services.first.pendingNotification.type, 'down');
});
