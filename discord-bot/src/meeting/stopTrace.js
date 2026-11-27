import { randomBytes } from 'node:crypto';
import { writeFile } from 'node:fs';

const events = new Set([
  'stop.request',
  'stop.response',
  'stop.error',
  'stop.drain_confirmed',
  'stop.drain_timed_out',
  'stop.drain_disconnected',
  'stop.drain_unknown',
]);

// One asynchronous write at a time. A slow Railway pipe consumes at most
// 64 queued records. Report lost observations on the next successful write.
export function createStopTracer({ write = (line, done) => writeFile(1, line, done) } = {}) {
  const queue = [];
  let writing = false;
  let dropped = 0;

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

  return function begin() {
    const traceId = randomBytes(16).toString('hex');
    function span(parentId) {
      const spanId = randomBytes(8).toString('hex');
      const start = performance.now();
      return {
        traceparent: `00-${traceId}-${spanId}-01`,
        child: () => span(spanId),
        mark(event) {
          if (!events.has(event)) return;
          if (queue.length >= 64) {
            dropped++;
            return;
          }
          queue.push({
            schema: 'misty.recording.v1',
            trace_id: traceId,
            span_id: spanId,
            ...(parentId && { parent_span_id: parentId }),
            event_name: event,
            ts: new Date().toISOString(),
            elapsed_ms: Math.round(performance.now() - start),
          });
          drain();
        },
      };
    }
    return span();
  };
}
