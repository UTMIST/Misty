import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createHttpClient } from '../src/clients/httpClient.js';

class TypeA extends Error {}
class TypeB extends Error {}

test('send prefixes baseUrl and applies base headers', async () => {
  let seen;
  const { send } = createHttpClient({
    baseUrl: 'http://svc',
    headers: { 'X-API-Key': 'k' },
    fetchImpl: async (url, opts) => {
      seen = { url, opts };
      return { ok: true };
    },
  });
  await send('/things');
  assert.equal(seen.url, 'http://svc/things');
  assert.equal(seen.opts.headers['X-API-Key'], 'k');
});

test('send merges per-call headers on top of the base headers', async () => {
  let seen;
  const { send } = createHttpClient({
    baseUrl: 'http://svc',
    headers: { 'X-API-Key': 'k' },
    fetchImpl: async (url, opts) => {
      seen = opts.headers;
      return { ok: true };
    },
  });
  await send('/things', { headers: { 'X-On-Behalf-Of': 'p1' } });
  assert.equal(seen['X-API-Key'], 'k');
  assert.equal(seen['X-On-Behalf-Of'], 'p1');
});

test('send throws the caller-supplied networkError type on fetch failure', async () => {
  const { send } = createHttpClient({
    baseUrl: 'http://svc',
    headers: {},
    fetchImpl: async () => {
      throw new Error('econnrefused');
    },
    networkError: () => new TypeA('down'),
  });
  await assert.rejects(
    () => send('/x'),
    (e) => e instanceof TypeA,
  );
});

test('parseJson throws the caller-supplied parseError type on malformed body', async () => {
  const { parseJson } = createHttpClient({
    baseUrl: 'http://svc',
    headers: {},
    fetchImpl: async () => ({ ok: true }),
    parseError: () => new TypeB('malformed'),
  });
  const resp = {
    json: async () => {
      throw new Error('unexpected token');
    },
  };
  await assert.rejects(
    () => parseJson(resp),
    (e) => e instanceof TypeB,
  );
});

test('per-client error types stay distinct across two clients sharing the helper', async () => {
  const a = createHttpClient({
    baseUrl: 'http://a',
    headers: {},
    fetchImpl: async () => {
      throw new Error('x');
    },
    networkError: () => new TypeA('a down'),
  });
  const b = createHttpClient({
    baseUrl: 'http://b',
    headers: {},
    fetchImpl: async () => {
      throw new Error('x');
    },
    networkError: () => new TypeB('b down'),
  });
  await assert.rejects(
    () => a.send('/x'),
    (e) => e instanceof TypeA && !(e instanceof TypeB),
  );
  await assert.rejects(
    () => b.send('/x'),
    (e) => e instanceof TypeB && !(e instanceof TypeA),
  );
});

test('no signal is attached when timeoutMs is omitted', async () => {
  let seen;
  const { send } = createHttpClient({
    baseUrl: 'http://svc',
    headers: {},
    fetchImpl: async (url, opts) => {
      seen = opts;
      return { ok: true };
    },
  });
  await send('/x');
  assert.equal('signal' in seen, false);
});

test('timeoutMs arms an AbortController and the abort surfaces as networkError', async () => {
  let gotSignal = false;
  const { send } = createHttpClient({
    baseUrl: 'http://svc',
    headers: {},
    timeoutMs: 5,
    networkError: () => new TypeA('timed out'),
    // never resolves on its own — only the AbortController can end it.
    fetchImpl: (url, opts) =>
      new Promise((_resolve, reject) => {
        gotSignal = !!opts.signal;
        opts.signal.addEventListener('abort', () => reject(new Error('aborted')));
      }),
  });
  await assert.rejects(
    () => send('/x'),
    (e) => e instanceof TypeA,
  );
  assert.equal(gotSignal, true);
});

test('authenticated requests refuse cross-origin redirects with telemetry on and off', async (t) => {
  async function listen(handler) {
    const server = createServer(handler);
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', resolve);
    });
    t.after(() => new Promise((resolve) => server.close(resolve)));
    return `http://127.0.0.1:${server.address().port}`;
  }

  let forwarded = 0;
  const target = await listen((_req, res) => {
    forwarded++;
    res.end();
  });
  const seen = [];
  const origin = await listen((req, res) => {
    seen.push(req.headers);
    res.writeHead(307, { Location: target });
    res.end();
  });
  const { send } = createHttpClient({
    baseUrl: origin,
    headers: { 'X-API-Key': 'fixture' },
    networkError: () => new TypeA('redirect refused'),
  });

  for (const name of ['TELEMETRY_ENABLED', 'TELEMETRY_ENV', 'TELEMETRY_TRAFFIC']) {
    const prior = process.env[name];
    t.after(() => {
      if (prior === undefined) delete process.env[name];
      else process.env[name] = prior;
    });
  }
  process.env.TELEMETRY_ENV = 'local';
  process.env.TELEMETRY_TRAFFIC = 'synthetic';
  for (const enabled of ['false', 'true']) {
    process.env.TELEMETRY_ENABLED = enabled;
    await assert.rejects(send('/private', { method: 'POST', redirect: 'follow' }), TypeA);
  }
  assert.equal(forwarded, 0);
  assert.deepEqual(
    seen.map((h) => h['x-api-key']),
    ['fixture', 'fixture'],
  );
  assert.equal(seen[0].traceparent, undefined);
  assert.match(seen[1].traceparent, /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
});
