// Read one environment's JSON log lines on stdin. Print only fixed outcomes.
import { createInterface } from 'node:readline';
import { isRecordingEvent } from '../src/meeting/stopTrace.js';
import { stdin } from 'node:process';

const traceId = process.argv[2];
if (!/^[0-9a-f]{32}$/.test(traceId ?? '')) {
  console.error('Usage: node scripts/recording-report.js TRACE_ID < environment.jsonl');
  process.exit(2);
}

const events = new Set();
for await (const line of createInterface({ input: stdin, crlfDelay: Infinity })) {
  let r;
  try {
    r = JSON.parse(line);
  } catch {
    continue;
  }
  if (r?.trace_id === traceId && isRecordingEvent(r)) {
    events.add(r.event_name);
  }
}
function first(...choices) {
  return choices.find((choice) => events.has(choice)) ?? 'unknown';
}

const mode = first('record.saved_requested', 'record.pdf_requested');
const stop = first(
  'capture.voice_lost',
  'capture.stream_lost',
  'capture.moved',
  'capture.empty_channel',
  'capture.stop_requested',
);
const interrupted = ['capture.voice_lost', 'capture.stream_lost', 'capture.moved'].includes(stop);
const save = first('save.confirmed', 'save.unavailable', 'save.failed', 'save.pending');
const link = first('link.posted', 'link.disabled', 'link.failed', 'link.pending');
const pdf = first('pdf.posted', 'pdf.error', 'pdf.posting');
const finalized = first('stop.response', 'stop.error', 'stop.request');
const outputs =
  mode === 'record.pdf_requested' ||
  (mode === 'record.saved_requested' && save === 'save.confirmed' && link === 'link.posted');
const normal = ['capture.empty_channel', 'capture.stop_requested'].includes(stop);
let outcome = 'unconfirmed';
if (interrupted) outcome = 'interrupted';
else if (
  events.has('capture.start_failed') ||
  events.has('capture.drain_error') ||
  finalized === 'stop.error' ||
  pdf === 'pdf.error' ||
  save === 'save.failed' ||
  link === 'link.failed' ||
  events.has('notice.error')
)
  outcome = 'observed_error';
else if (
  normal &&
  events.has('capture.started') &&
  events.has('capture.drained') &&
  !events.has('capture.end_signal_error') &&
  finalized === 'stop.response' &&
  outputs &&
  pdf === 'pdf.posted'
)
  outcome = 'complete';
console.log(
  JSON.stringify(
    { outcome, mode, stop, finalized, save, link, pdf, events: [...events].sort() },
    null,
    2,
  ),
);
if (events.size === 0) process.exitCode = 1;
