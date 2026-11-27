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
      ['stop.request', 'stop.drain_unknown', 'stop.response'],
    );
  }
  assert.ok(!JSON.stringify(records).includes('private'));
});

test('stop preserves server drain evidence on the response span and leaves unknown input unconfirmed', async () => {
  for (const [value, event] of [
    ['confirmed', 'stop.drain_confirmed'],
    ['timed_out', 'stop.drain_timed_out'],
    ['disconnected', 'stop.drain_disconnected'],
    ['private-invalid-value', 'stop.drain_unknown'],
    [null, 'stop.drain_unknown'],
  ]) {
    const rows = [];
    const beginTrace = createStopTracer({
      write(line, done) {
        rows.push(JSON.parse(line));
        done();
      },
    });
    const client = createMeetingClient({
      baseUrl: 'http://meeting',
      beginTrace,
      fetchImpl: async () => ({ ok: true, json: async () => ({ audio_drain: value }) }),
    });
    await client.stop('s');
    assert.deepEqual(
      rows.map((r) => r.event_name),
      ['stop.request', event, 'stop.response'],
    );
    assert.equal(new Set(rows.map((r) => r.span_id)).size, 1);
    assert.ok(!JSON.stringify(rows).includes('private-invalid-value'));
  }
});

test('a broken trace sink leaves stop and its returned report usable', async () => {
  const report = { audio_drain: 'confirmed', pdf_b64: 'fake' };
  const client = createMeetingClient({
    baseUrl: 'http://meeting',
    beginTrace: createStopTracer({
      write() {
        throw new Error('closed');
      },
    }),
    fetchImpl: async () => ({ ok: true, json: async () => report }),
  });
  assert.equal(await client.stop('s'), report);
});
