// Inspect one environment's JSON log lines. Retain only bounded event labels
// and the single recording span and stop attempt owned by the bot surface.
import { stdin } from 'node:process';
import { isRecordingEvent } from '../src/meeting/stopTrace.js';

const traceId = process.argv[2];
if (!/^[0-9a-f]{32}$/.test(traceId ?? '')) {
  console.error('Usage: node scripts/recording-report.js TRACE_ID < environment.jsonl');
  process.exit(2);
}

// A v1 event is a few hundred ASCII characters. Discard oversized lines,
// including unrelated audit data, even when stdin never receives a newline.
async function* eventLines(input) {
  let partial = '';
  for await (const chunk of input.setEncoding('utf8')) {
    let start = 0;
    while (start < chunk.length) {
      const newline = chunk.indexOf('\n', start);
      const end = newline === -1 ? chunk.length : newline;
      if (partial !== null) {
        partial = partial.length + end - start <= 2048 ? partial + chunk.slice(start, end) : null;
      }
      if (newline === -1) break;
      if (partial) yield partial;
      partial = '';
      start = end + 1;
    }
  }
  if (partial) yield partial;
}

const events = new Set();
const latestTimes = new Map();
let recordingSpan;
let stopSpan;
let stopParent;
let spanConflict = false;
for await (const line of eventLines(stdin)) {
  let record;
  try {
    record = JSON.parse(line);
  } catch {
    continue;
  }
  if (record?.trace_id !== traceId || !isRecordingEvent(record)) continue;
  const { event_name: event, span_id: span, parent_span_id: parent } = record;
  events.add(event);
  if (event.startsWith('save.') || event.startsWith('link.')) {
    latestTimes.set(event, Math.max(latestTimes.get(event) ?? -Infinity, Date.parse(record.ts)));
  }
  if (event.startsWith('stop.')) {
    if (stopSpan === undefined) {
      stopSpan = span;
      stopParent = parent;
    } else if (stopSpan !== span || stopParent !== parent) {
      spanConflict = true;
    }
  } else if (parent !== undefined) {
    spanConflict = true;
  } else if (!event.startsWith('save.') && !event.startsWith('link.')) {
    // Save and link observations may also come from a new recovery worker.
    if (recordingSpan !== undefined && recordingSpan !== span) spanConflict = true;
    recordingSpan = span;
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
if (stopParent && recordingSpan && (stopParent !== recordingSpan || stopSpan === recordingSpan)) {
  spanConflict = true;
}
let stopLink = 'incomplete';
if (spanConflict) stopLink = 'conflict';
else if (recordingSpan && stopParent === recordingSpan && events.has('stop.request')) {
  stopLink = 'linked';
}

const finalized =
  stopLink === 'linked' ? first('stop.error', 'stop.response', 'stop.request') : 'unknown';
const drainEvents = ['confirmed', 'timed_out', 'disconnected', 'unknown'].filter((value) =>
  events.has(`stop.drain_${value}`),
);
const drain =
  finalized === 'stop.response' && drainEvents.length === 1 ? drainEvents[0] : 'unknown';
// Wall-clock receipt order is unavailable during offline replay. Use the most
// recent terminal observation for each delivery dimension. A delayed pending
// snapshot cannot reopen a terminal outcome. Equal timestamps prefer failure.
function latest(pending, ...choices) {
  let label = first(pending);
  let newest = -Infinity;
  for (const choice of choices) {
    const time = latestTimes.get(choice);
    if (time !== undefined && time > newest) {
      label = choice;
      newest = time;
    }
  }
  return label;
}
const save = latest(
  'save.pending',
  'save.unavailable',
  'save.failed',
  'save.uncertain',
  'save.confirmed',
);
const link = latest(
  'link.pending',
  'link.disabled',
  'link.failed',
  'link.not_attempted',
  'link.posted',
);
const pdf = first('pdf.error', 'pdf.posted', 'pdf.posting');
const outputs =
  mode === 'record.pdf_requested' ||
  (mode === 'record.saved_requested' && save === 'save.confirmed' && link === 'link.posted');
const normal = ['capture.empty_channel', 'capture.stop_requested'].includes(stop);
let outcome = 'unconfirmed';
if (
  ['capture.voice_lost', 'capture.stream_lost', 'capture.moved'].includes(stop) ||
  drain === 'disconnected'
)
  outcome = 'interrupted';
else if (
  events.has('capture.start_failed') ||
  events.has('capture.drain_error') ||
  finalized === 'stop.error' ||
  drain === 'timed_out' ||
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
  drain === 'confirmed' &&
  !(events.has('record.saved_requested') && events.has('record.pdf_requested')) &&
  outputs &&
  pdf === 'pdf.posted'
)
  outcome = 'complete';
console.log(
  JSON.stringify(
    {
      outcome,
      mode,
      stop,
      stop_link: stopLink,
      finalized,
      drain,
      save,
      link,
      pdf,
      events: [...events].sort(),
    },
    null,
    2,
  ),
);
if (events.size === 0) process.exitCode = 1;
