import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createMeetingRecordClient } from '../src/clients/meetingRecordClient.js';
import { DocUnavailable } from '../src/clients/docClient.js';

test('meeting record transport only reads references and PATCHes delivery with service auth', async () => {
  const calls = [];
  const client = createMeetingRecordClient({
    baseUrl: 'http://docs',
    apiKey: 'test-key',
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return {
        ok: true,
        status: 200,
        json: async () => (url.includes('?') ? [] : { meeting_id: 'id' }),
      };
    },
  });
  await client.get('id/with space');
  await client.list('failed');
  await client.update('id', 'posted', '123');
  assert.equal(calls[0].url, 'http://docs/meeting-records/id%2Fwith%20space');
  assert.equal(
    calls[1].url,
    'http://docs/meeting-records?persistence_status=saved&delivery_status=failed',
  );
  assert.equal(calls[2].options.method, 'PATCH');
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    status: 'posted',
    discord_message_id: '123',
  });
  for (const { options } of calls) {
    assert.equal(options.headers['X-API-Key'], 'test-key');
    assert.ok(options.signal instanceof AbortSignal);
  }
});

test('missing reference is null; network/auth/malformed responses normalize to DocUnavailable', async () => {
  const make = (fetchImpl) =>
    createMeetingRecordClient({ baseUrl: 'http://docs', apiKey: 'k', fetchImpl });
  assert.equal(await make(async () => ({ status: 404 })).get('id'), null);
  for (const fetchImpl of [
    async () => {
      throw new Error('network');
    },
    async () => ({ ok: false, status: 403 }),
    async () => ({
      ok: true,
      status: 200,
      json: async () => {
        throw new Error('bad json');
      },
    }),
  ]) {
    await assert.rejects(() => make(fetchImpl).get('id'), DocUnavailable);
  }
  await assert.rejects(
    () => make(async () => ({ ok: true, status: 200, json: async () => ({}) })).list('pending'),
    DocUnavailable,
  );
});

for (const [operation, request] of [
  ['get', (client) => client.get('id')],
  ['list', (client) => client.list('pending')],
  ['update', (client) => client.update('id', 'posted', '123')],
]) {
  test(`${operation} shares one deadline across headers and body, then allows retry`, async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const signals = [];
    const result = operation === 'list' ? [] : { meeting_id: 'id' };
    let bodyStarted;
    const readingBody = new Promise((resolve) => (bodyStarted = resolve));
    let finishBody;
    t.after(() => finishBody?.(result));
    const client = createMeetingRecordClient({
      baseUrl: 'http://docs',
      apiKey: 'k',
      fetchImpl: async (_url, { signal }) => {
        signals.push(signal);
        if (signals.length > 1) return { ok: true, status: 200, json: async () => result };
        // Headers consume part of the request budget before the body stalls.
        t.mock.timers.tick(6_000);
        return {
          ok: true,
          status: 200,
          json: () =>
            new Promise((resolve, reject) => {
              finishBody = resolve;
              signal.addEventListener('abort', () => reject(new Error('body aborted')), {
                once: true,
              });
              bodyStarted();
            }),
        };
      },
    });
    const pending = request(client);
    await readingBody;
    t.mock.timers.tick(3_999);
    assert.equal(signals[0].aborted, false);
    t.mock.timers.tick(1);
    assert.equal(signals[0].aborted, true);
    await assert.rejects(pending, DocUnavailable);

    assert.deepEqual(await request(client), result);
    t.mock.timers.tick(10_000);
    assert.equal(signals[1].aborted, false, 'completed requests release their deadline');
  });
}
