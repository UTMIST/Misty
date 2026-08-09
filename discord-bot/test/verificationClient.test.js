import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  createVerificationClient,
  VerificationUnavailable,
  RateLimited,
  CodeExpired,
  TooManyAttempts,
  InvalidCode,
  NoPendingCode,
} from '../src/verificationClient.js';

function fakeFetch(responses) {
  const calls = [];
  let i = 0;
  const fetchImpl = async (url, opts) => {
    calls.push({ url, opts });
    const r = responses[i++];
    return {
      status: r.status,
      ok: r.ok ?? (r.status >= 200 && r.status < 300),
      json: async () => r.body,
    };
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

const BASE = 'http://v';
const KEY = 'k';

test('requestCode resolves on 202 and sends subject/email with X-API-Key', async () => {
  const fetchImpl = fakeFetch([{ status: 202, body: {} }]);
  const client = createVerificationClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await client.requestCode({ subject: 'discord:123', email: 'alex@utmist.ca' });
  const { url, opts } = fetchImpl.calls[0];
  assert.match(url, /\/verification\/request-code$/);
  assert.equal(opts.method, 'POST');
  assert.equal(opts.headers['X-API-Key'], 'k');
  const sent = JSON.parse(opts.body);
  assert.equal(sent.subject, 'discord:123');
  assert.equal(sent.email, 'alex@utmist.ca');
});

test('requestCode throws RateLimited on 429', async () => {
  const fetchImpl = fakeFetch([{ status: 429, body: { detail: 'rate_limited' } }]);
  const client = createVerificationClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.requestCode({ subject: 'discord:123', email: 'a@x' }),
    (e) => e instanceof RateLimited,
  );
});

test('requestCode throws VerificationUnavailable on 500', async () => {
  const fetchImpl = fakeFetch([{ status: 500, body: {} }]);
  const client = createVerificationClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.requestCode({ subject: 'discord:123', email: 'a@x' }),
    VerificationUnavailable,
  );
});

test('requestCode network error becomes VerificationUnavailable', async () => {
  const fetchImpl = async () => {
    throw new Error('econnrefused');
  };
  const client = createVerificationClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.requestCode({ subject: 'discord:123', email: 'a@x' }),
    VerificationUnavailable,
  );
});

test('confirmCode returns body on 200', async () => {
  const fetchImpl = fakeFetch([
    { status: 200, body: { verified: true, subject: 'discord:123', email: 'a@x' } },
  ]);
  const client = createVerificationClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const result = await client.confirmCode({ subject: 'discord:123', code: '123456' });
  assert.deepEqual(result, { verified: true, subject: 'discord:123', email: 'a@x' });
  const { url, opts } = fetchImpl.calls[0];
  assert.match(url, /\/verification\/confirm-code$/);
  assert.equal(opts.method, 'POST');
  const sent = JSON.parse(opts.body);
  assert.equal(sent.subject, 'discord:123');
  assert.equal(sent.code, '123456');
});

test('confirmCode throws NoPendingCode on 404', async () => {
  const fetchImpl = fakeFetch([{ status: 404, body: { detail: 'no_pending_code' } }]);
  const client = createVerificationClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.confirmCode({ subject: 'discord:123', code: '000000' }),
    (e) => e instanceof NoPendingCode,
  );
});

test('confirmCode throws CodeExpired on 410', async () => {
  const fetchImpl = fakeFetch([{ status: 410, body: { detail: 'expired' } }]);
  const client = createVerificationClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.confirmCode({ subject: 'discord:123', code: '000000' }),
    (e) => e instanceof CodeExpired,
  );
});

test('confirmCode throws TooManyAttempts on 429', async () => {
  const fetchImpl = fakeFetch([{ status: 429, body: { detail: 'too_many_attempts' } }]);
  const client = createVerificationClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.confirmCode({ subject: 'discord:123', code: '000000' }),
    (e) => e instanceof TooManyAttempts,
  );
});

test('confirmCode throws InvalidCode on 400', async () => {
  const fetchImpl = fakeFetch([{ status: 400, body: { detail: 'invalid_code' } }]);
  const client = createVerificationClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.confirmCode({ subject: 'discord:123', code: '000000' }),
    (e) => e instanceof InvalidCode,
  );
});

test('confirmCode throws VerificationUnavailable on 500', async () => {
  const fetchImpl = fakeFetch([{ status: 500, body: {} }]);
  const client = createVerificationClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.confirmCode({ subject: 'discord:123', code: '000000' }),
    VerificationUnavailable,
  );
});

test('confirmCode network error becomes VerificationUnavailable', async () => {
  const fetchImpl = async () => {
    throw new Error('econnrefused');
  };
  const client = createVerificationClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.confirmCode({ subject: 'discord:123', code: '000000' }),
    VerificationUnavailable,
  );
});

test('requestCode aborts and fails when the service hangs past the timeout', async () => {
  // fetch that never resolves on its own — only the AbortController can end it.
  const fetchImpl = (url, opts) =>
    new Promise((_resolve, reject) => {
      opts.signal.addEventListener('abort', () => reject(new Error('aborted')));
    });
  const client = createVerificationClient({ baseUrl: BASE, apiKey: KEY, fetchImpl, timeoutMs: 5 });
  await assert.rejects(
    () => client.requestCode({ subject: 'discord:123', email: 'a@b.com' }),
    VerificationUnavailable,
  );
});
