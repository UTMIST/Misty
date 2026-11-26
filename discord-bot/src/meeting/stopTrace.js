import { createHash, randomBytes } from 'node:crypto';
import { writeFile } from 'node:fs';

const events = new Set([
  'stop.request',
  'stop.response',
  'stop.error',
  'record.saved_requested',
  'record.pdf_requested',
  'capture.started',
  'capture.start_failed',
  'capture.stop_requested',
  'capture.empty_channel',
  'capture.voice_lost',
  'capture.stream_lost',
  'capture.moved',
  'capture.drained',
  'capture.drain_error',
  'capture.end_signal_error',
  'save.confirmed',
  'save.pending',
  'save.failed',
  'save.unavailable',
  'link.posted',
  'link.failed',
  'link.pending',
  'link.disabled',
  'pdf.posting',
  'pdf.posted',
  'pdf.error',
  'notice.error',
]);

const fields = new Set([
  'schema',
  'trace_id',
  'span_id',
  'parent_span_id',
  'event_name',
  'ts',
  'elapsed_ms',
  'dropped_before',
]);

export function isRecordingEvent(record) {
  const span = /^[0-9a-f]{16}$/;
  return (
    record?.schema === 'misty.recording.v1' &&
    Object.keys(record).every((key) => fields.has(key)) &&
    /^[0-9a-f]{32}$/.test(record.trace_id) &&
    span.test(record.span_id) &&
    (record.parent_span_id === undefined || span.test(record.parent_span_id)) &&
    events.has(record.event_name) &&
    typeof record.ts === 'string' &&
    Number.isFinite(Date.parse(record.ts)) &&
    Number.isSafeInteger(record.elapsed_ms) &&
    record.elapsed_ms >= 0 &&
    Number.isSafeInteger(record.dropped_before) &&
    record.dropped_before >= 0
  );
}

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

  return function begin(meetingId) {
    // The bot mints a random UUID v4 per meeting. The catalog retains that UUID
    // across restarts. Derive a stable trace for recovery without emitting the ID.
    const recoverable =
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(meetingId ?? '');
    const traceId = recoverable
      ? createHash('sha256')
          .update('misty.recording.v1/' + meetingId)
          .digest('hex')
          .slice(0, 32)
      : randomBytes(16).toString('hex');
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
