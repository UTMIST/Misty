// Inspect one environment's JSON log lines. Retain only bounded event labels
// and the single recording span and stop attempt owned by the bot surface.
import { createRecordingProjection } from '../src/meeting/recordingProjection.js';
import { stdin } from 'node:process';
import { isRecordingEvent } from '../src/meeting/stopTrace.js';

const traceId = process.argv[2];
if (traceId !== '--grouped' && !/^[0-9a-f]{32}$/.test(traceId ?? '')) {
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

const grouped = traceId === '--grouped';
let current;
let projection = createRecordingProjection();
function output() {
  const report = projection.report();
  console.log(
    JSON.stringify(grouped ? { trace_id: current, ...report } : report, null, grouped ? 0 : 2),
  );
  if (!grouped && report.events.length === 0) process.exitCode = 1;
}
for await (const line of eventLines(stdin)) {
  let r;
  try {
    r = JSON.parse(line);
  } catch {
    continue;
  }
  if ((!grouped && r?.trace_id !== traceId) || !isRecordingEvent(r)) continue;
  if (grouped && current && current !== r.trace_id) {
    output();
    projection = createRecordingProjection();
  }
  current = r.trace_id;
  projection.observe(r);
}
if (!grouped || current) output();
