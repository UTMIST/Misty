import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTelemetry, traceHeaders } from '../src/telemetry.js';

const config = {
  TELEMETRY_ENABLED: 'true',
  TELEMETRY_ENV: 'local',
  TELEMETRY_TRAFFIC: 'synthetic',
};

test('commands keep independent parents across asynchronous fanout and retries', async () => {
  const rows = [];
  const { withSpan } = createTelemetry({
    config,
    write(line, done) {
      rows.push(JSON.parse(line));
      done();
    },
  });
  async function command() {
    return withSpan('command', async (root) => {
      const dependency = () =>
        withSpan('http.client', async (child) => {
          await new Promise((done) => setImmediate(done));
          assert.equal(traceHeaders().traceparent, child.traceparent);
          return child.spanId;
        });
      const ids = await Promise.all([dependency(), dependency()]);
      ids.push(await dependency());
      assert.equal(new Set(ids).size, 3);
      return root.traceId;
    });
  }
  const traces = await Promise.all([command(), command()]);
  assert.equal(new Set(traces).size, 2);
  for (const trace of traces) {
    const family = rows.filter((r) => r.trace_id === trace && r.event_name === 'end');
    const root = family.find((r) => r.operation === 'command');
    assert.equal(family.filter((r) => r.parent_span_id === root.span_id).length, 3);
  }
  assert.deepEqual(traceHeaders(), {});
});

test('slow and failed sink drops bounded observations without changing completed work', async () => {
  const rows = [];
  let finish;
  const { withSpan } = createTelemetry({
    config,
    write(line, done) {
      rows.push(JSON.parse(line));
      finish = done;
    },
  });
  for (let i = 0; i < 160; i++) {
    assert.equal(
      await withSpan('command', async (call) => {
        call.outcome('private-canary');
        return 7;
      }),
      7,
    );
  }
  assert.equal(rows.length, 1);
  finish(new Error('private-canary'));
  assert.equal(rows[1].dropped_before, 64);
  for (let i = 0; i < 256; i++) finish();
  assert.equal(rows.length, 257);
  assert.equal(new Set(rows.map((r) => r.process_id)).size, 1);
  assert.ok(!JSON.stringify(rows).includes('private-canary'));
});
