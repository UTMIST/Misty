import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createLlmClient, LlmUnavailable } from '../src/llmClient.js';

function fakeFetch(responses) {
  const calls = [];
  let i = 0;
  const fetchImpl = async (url, opts) => {
    calls.push({ url, opts });
    const r = responses[i++];
    if (r.throw) throw new Error('network down');
    return {
      status: r.status,
      ok: r.ok ?? (r.status >= 200 && r.status < 300),
      json: async () => {
        if (r.badJson) throw new Error('bad json');
        return r.body;
      },
    };
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

const BASE = 'http://llm';
const KEY = 'botkey';

test('chat POSTs /chat with key header and maps a 200 response', async () => {
  const fetchImpl = fakeFetch([
    {
      status: 200,
      body: {
        content: 'hi there',
        model: 'claude-sonnet-4-6',
        stop_reason: 'end_turn',
        usage: { input_tokens: 5, output_tokens: 2 },
      },
    },
  ]);
  const client = createLlmClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const res = await client.chat({
    messages: [{ role: 'user', content: 'hi' }],
    system: 'be nice',
    maxTokens: 512,
  });

  assert.deepEqual(res, {
    content: 'hi there',
    model: 'claude-sonnet-4-6',
    usage: { input_tokens: 5, output_tokens: 2 },
  });
  const call = fetchImpl.calls[0];
  assert.match(call.url, /\/chat$/);
  assert.equal(call.opts.method, 'POST');
  assert.equal(call.opts.headers['X-API-Key'], KEY);
  assert.equal(call.opts.headers['Content-Type'], 'application/json');
  assert.ok(call.opts.signal instanceof AbortSignal); // timeout wiring present
  const sent = JSON.parse(call.opts.body);
  assert.deepEqual(sent.messages, [{ role: 'user', content: 'hi' }]);
  assert.equal(sent.system, 'be nice');
  assert.equal(sent.max_tokens, 512);
  assert.equal('model' in sent, false); // no model → service default
});

test('chat omits system when not provided and defaults max_tokens to 1024', async () => {
  const fetchImpl = fakeFetch([{ status: 200, body: { content: 'x', model: 'm', usage: {} } }]);
  const client = createLlmClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await client.chat({ messages: [{ role: 'user', content: 'q' }] });
  const sent = JSON.parse(fetchImpl.calls[0].opts.body);
  assert.equal('system' in sent, false);
  assert.equal(sent.max_tokens, 1024);
});

test('chat throws LlmUnavailable on non-2xx', async () => {
  const fetchImpl = fakeFetch([{ status: 429, body: {} }]);
  const client = createLlmClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.chat({ messages: [{ role: 'user', content: 'q' }] }),
    LlmUnavailable,
  );
});

test('chat throws LlmUnavailable on network error', async () => {
  const fetchImpl = fakeFetch([{ throw: true }]);
  const client = createLlmClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.chat({ messages: [{ role: 'user', content: 'q' }] }),
    LlmUnavailable,
  );
});

test('chat throws LlmUnavailable on malformed JSON', async () => {
  const fetchImpl = fakeFetch([{ status: 200, badJson: true }]);
  const client = createLlmClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.chat({ messages: [{ role: 'user', content: 'q' }] }),
    LlmUnavailable,
  );
});

test('chat aborts and throws LlmUnavailable when the request stalls past timeoutMs', async () => {
  const fetchImpl = (url, opts) =>
    new Promise((_, reject) => {
      opts.signal.addEventListener('abort', () => reject(new Error('aborted')));
    });
  const client = createLlmClient({ baseUrl: BASE, apiKey: KEY, fetchImpl, timeoutMs: 10 });
  await assert.rejects(
    () => client.chat({ messages: [{ role: 'user', content: 'q' }] }),
    LlmUnavailable,
  );
});
