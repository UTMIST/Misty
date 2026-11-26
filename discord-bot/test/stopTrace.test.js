import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStopTracer } from '../src/meeting/stopTrace.js';
import { createMeetingClient } from '../src/meeting/meetingClient.js';

test('blocked and failed writes are bounded and reported after recovery', () => {
  const records = [];
  let finish;
  const begin = createStopTracer({
    write(line, done) {
      records.push(JSON.parse(line));
      finish = done;
    },
  });
  const span = begin();
  span.mark('private-canary');
  for (let i = 0; i < 100; i++) span.mark('stop.request');
  assert.equal(records.length, 1);
  finish(new Error('failed write'));
  assert.equal(records[1].dropped_before, 36);
  for (let i = 0; i < 64; i++) finish();
  assert.equal(records.length, 65);
  assert.ok(!JSON.stringify(records).includes('canary'));
});

test('overlapping stops generate distinct internal parents and omit request content', async () => {
  const records = [];
  const headers = [];
  const beginTrace = createStopTracer({
    write(line, done) {
      records.push(JSON.parse(line));
      done();
    },
  });
  const client = createMeetingClient({
    baseUrl: 'http://meeting',
    apiKey: 'private-key',
    beginTrace,
    fetchImpl: async (_url, opts) => {
      headers.push(opts.headers.traceparent);
      assert.equal(opts.redirect, 'error');
      return { ok: true, json: async () => ({ pdf_b64: 'private-pdf' }) };
    },
  });
  await Promise.all([client.stop('private-a'), client.stop('private-b')]);
  assert.equal(new Set(headers).size, 2);
  for (const h of headers) {
    const [, trace, span] = h.split('-');
    assert.deepEqual(
      records.filter((r) => r.trace_id === trace && r.span_id === span).map((r) => r.event_name),
      ['stop.request', 'stop.response'],
    );
  }
  assert.ok(!JSON.stringify(records).includes('private'));
});
