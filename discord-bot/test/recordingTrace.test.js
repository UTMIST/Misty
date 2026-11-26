import { test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createMeetingSurface } from '../src/meeting/meetingSurface.js';
import { createMeetingClient } from '../src/meeting/meetingClient.js';
import { createMeetingDelivery } from '../src/meeting/meetingDelivery.js';
import { createStopTracer } from '../src/meeting/stopTrace.js';
import { makeAttachmentPoster } from '../src/adapters/discord/meetingPosts.js';

const script = fileURLToPath(new URL('../scripts/recording-report.js', import.meta.url));

function replay(lines, traceId) {
  // Simulate overlapping, reordered reads plus an unrelated audit line.
  const input = [
    '{"remote":"private-ip"}',
    'damaged log line',
    ...lines.toReversed(),
    ...lines,
  ].join('\n');
  return JSON.parse(execFileSync(process.execPath, [script, traceId], { input, encoding: 'utf8' }));
}

for (const scenario of [
  'saved',
  'pdf only',
  'voice lost',
  'stream lost',
  'pdf rejected',
  'late link',
  'stop unavailable',
]) {
  test(`recording workflow to operator report: ${scenario}`, async () => {
    const lines = [];
    const write = (line, done) => {
      lines.push(line);
      done();
    };
    const beginTrace = createStopTracer({ write });
    const meetingId = randomUUID();
    let saved = scenario !== 'late link';
    let posted = false;
    const records = {
      get: async () => ({
        meeting_id: meetingId,
        persistence_status: saved ? 'saved' : 'pending',
        delivery_status: posted ? 'posted' : 'pending',
      }),
      list: async (status) => (status === 'pending' ? [{ meeting_id: meetingId }] : []),
      update: async () => {
        posted = true;
        return {};
      },
    };
    const post = async () => 'private-message-id';
    const delivery = createMeetingDelivery({ records, post, beginTrace });
    let header;
    let streamLost;
    let voiceLost;
    const client = createMeetingClient({
      baseUrl: 'http://meeting',
      apiKey: 'private-key',
      fetchImpl: async (_url, opts) => {
        header = opts.headers.traceparent;
        return {
          ok: scenario !== 'stop unavailable',
          status: 503,
          json: async () => ({
            pdf_b64: 'cGRm',
            transcript: 'private-transcript',
            ...(scenario !== 'pdf only' && { record: { status: saved ? 'saved' : 'pending' } }),
          }),
        };
      },
    });
    const surface = createMeetingSurface({
      beginTrace,
      genId: () => meetingId,
      meetingClient: {
        ...client,
        openStream: (_id, callbacks) => {
          streamLost = callbacks.onClose;
          return { endAudio() {}, close() {} };
        },
      },
      makeRecorder: ({ onVoiceLost }) => {
        voiceLost = onVoiceLost;
        return { start: async () => {}, stop: async () => {} };
      },
      poster: makeAttachmentPoster(),
      deliverRecord: delivery.deliver,
    });
    const textChannel = {
      id: 'private-channel',
      send: async () => {
        if (scenario === 'pdf rejected') throw new Error('private-error');
        return { id: 'private-pdf-message' };
      },
    };
    const start = await surface.start({
      guildId: 'private-guild',
      voiceChannel: { id: 'private-voice' },
      textChannel,
      save: scenario !== 'pdf only',
    });
    assert.equal(start.status, 'recording');
    if (scenario === 'voice lost') {
      voiceLost();
      voiceLost();
      await new Promise((resolve) => setImmediate(resolve));
    } else if (scenario === 'stream lost') await streamLost();
    else await surface.stop('private-guild');
    const [, traceId, clientSpan] = header.split('-');
    const outgoing = lines
      .map((line) => JSON.parse(line))
      .find((r) => r.event_name === 'stop.request');
    assert.equal(outgoing.span_id, clientSpan);
    assert.ok(lines.some((line) => JSON.parse(line).span_id === outgoing.parent_span_id));
    let report = replay(lines, traceId);
    if (scenario === 'late link') {
      assert.equal(report.outcome, 'unconfirmed');
      saved = true;
      await createMeetingDelivery({
        records,
        post,
        beginTrace: createStopTracer({ write }),
      }).poll();
      report = replay(lines, traceId);
      assert.equal(report.link, 'link.posted');
    }
    const expected = {
      'voice lost': 'interrupted',
      'stream lost': 'interrupted',
      'pdf rejected': 'observed_error',
      'stop unavailable': 'observed_error',
    };
    assert.equal(report.outcome, expected[scenario] ?? 'complete');
    if (scenario === 'saved') {
      const withoutPdf = lines.filter((line) => JSON.parse(line).event_name !== 'pdf.posted');
      const pdf = JSON.parse(lines.find((line) => JSON.parse(line).event_name === 'pdf.posted'));
      const contaminated = JSON.stringify({ ...pdf, raw_content: 'private-extra' });
      assert.equal(replay([...withoutPdf, contaminated], traceId).outcome, 'unconfirmed');
      for (const missing of [
        'pdf.posted',
        'link.posted',
        'save.confirmed',
        'stop.response',
        'capture.started',
      ]) {
        const incomplete = lines.filter((line) => JSON.parse(line).event_name !== missing);
        assert.equal(replay(incomplete, traceId).outcome, 'unconfirmed', missing);
      }
    }
    assert.ok(!lines.join('').includes('private-'));
    assert.ok(!lines.join('').includes(meetingId));
  });
}
