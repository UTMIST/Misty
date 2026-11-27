import { AsyncLocalStorage } from 'node:async_hooks';
import { randomBytes } from 'node:crypto';
import { writeFile } from 'node:fs';
import { createRequire } from 'node:module';

const contract = createRequire(import.meta.url)('./telemetryContract.json');
const context = new AsyncLocalStorage();
const noop = Object.freeze({ traceparent: null, outcome() {}, mark() {}, measure() {} });
const hex = (n) => randomBytes(n).toString('hex');

export function createTelemetry({
  config = process.env,
  write = (line, done) => writeFile(1, line, done),
} = {}) {
  const queue = [];
  let writing = false;
  let dropped = 0;
  const processId = hex(8);
  const processStartedAt = new Date().toISOString();
  let sequence = 0;

  function drain() {
    if (writing || !queue.length) return;
    writing = true;
    const lost = dropped;
    dropped = 0;
    const record = queue.shift();
    const done = (error) => {
      if (error) dropped += lost + 1;
      writing = false;
      drain();
    };
    try {
      write(JSON.stringify({ ...record, dropped_before: lost }) + '\n', done);
    } catch (error) {
      done(error);
    }
  }

  async function withSpan(operation, run) {
    const traffic = config.TELEMETRY_TRAFFIC ?? 'member';
    if (
      config.TELEMETRY_ENABLED !== 'true' ||
      !['local', 'staging', 'production'].includes(config.TELEMETRY_ENV) ||
      !['synthetic', 'member', 'background'].includes(traffic) ||
      ![...contract.operations['discord-bot'], ...contract.operations.common].includes(operation)
    )
      return run(noop);
    const parent = context.getStore();
    const traceId = parent?.traceId ?? hex(16);
    const spanId = hex(8);
    const started = performance.now();
    let result = 'success';
    const measures = {};
    const dep = config.RAILWAY_DEPLOYMENT_ID ?? '';
    const base = {
      schema: 'misty.telemetry.v1',
      trace_id: traceId,
      span_id: spanId,
      ...(parent && { parent_span_id: parent.spanId }),
      service: 'discord-bot',
      operation,
      environment: config.TELEMETRY_ENV,
      traffic,
      process_id: processId,
      deployment: /^(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{40})$/.test(dep)
        ? dep
        : 'unknown',
    };
    function emit(name, state) {
      sequence++;
      if (queue.length >= 256) {
        dropped++;
        return;
      }
      queue.push({
        ...base,
        sequence,
        process_started_at: processStartedAt,
        event_id: hex(16),
        event_name: name,
        result: state,
        ts: new Date().toISOString(),
        elapsed_ms: Math.round(performance.now() - started),
        ...(name === 'end' && Object.keys(measures).length && { measures: { ...measures } }),
      });
      drain();
    }
    const call = {
      traceId,
      spanId,
      traffic,
      traceparent: `00-${traceId}-${spanId}-01`,
      outcome(value) {
        if (contract.result.includes(value)) result = value;
      },
      mark(name) {
        if (contract.marks[operation]?.includes(name)) emit(name, 'success');
      },
      measure(name, value) {
        if (
          contract.measures[operation]?.includes(name) &&
          Number.isSafeInteger(value) &&
          value >= 0
        )
          measures[name] = value;
      },
    };
    emit('start', 'started');
    try {
      return await context.run(call, () => run(call));
    } catch (error) {
      if (result === 'success') result = error?.name === 'AbortError' ? 'cancelled' : 'failed';
      throw error;
    } finally {
      emit('end', result);
    }
  }
  return { withSpan };
}

export const { withSpan } = createTelemetry();
export function traceHeaders() {
  const parent = context.getStore();
  return parent ? { traceparent: parent.traceparent, 'x-misty-traffic': parent.traffic } : {};
}
