import { test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { runRecordAction } from '../src/adapters/discord/recording.js';
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
  'server drain timed out',
  'server stream disconnected',
  'server omitted drain',
  'failed recovery',
  'uncertain recovery',
  'removed during recovery',
]) {
  test(`recording workflow to operator report: ${scenario}`, async () => {
    const lines = [];
    const write = (line, done) => {
      lines.push(line);
      done();
    };
    const beginTrace = createStopTracer({ write });
    const meetingId = randomUUID();
    const recoveryStatus = {
      'late link': 'saved',
      'failed recovery': 'failed',
      'uncertain recovery': 'create_uncertain',
      'removed during recovery': 'removed',
    }[scenario];
    let persistence = recoveryStatus ? 'pending' : 'saved';
    let posted = false;
    const records = {
      get: async () => ({
        meeting_id: meetingId,
        persistence_status: persistence,
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
            ...(scenario !== 'server omitted drain' && {
              audio_drain:
                {
                  'server drain timed out': 'timed_out',
                  'server stream disconnected': 'disconnected',
                }[scenario] ?? 'confirmed',
            }),
            ...(scenario !== 'pdf only' && { record: { status: persistence } }),
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
    if (recoveryStatus) {
      assert.equal(report.outcome, 'unconfirmed');
      persistence = recoveryStatus;
      await createMeetingDelivery({
        records,
        post,
        beginTrace: createStopTracer({ write }),
      }).poll();
      report = replay(lines, traceId);
      const lateSnapshots = lines
        .map(JSON.parse)
        .filter((r) => ['save.pending', 'link.pending'].includes(r.event_name))
        .map((r) => JSON.stringify({ ...r, ts: '2099-01-01T00:00:00.000Z' }));
      const shuffled = replay([...lateSnapshots, ...lines.toReversed()], traceId);
      assert.deepEqual(shuffled, report);
      const expectedSave = {
        saved: 'save.confirmed',
        failed: 'save.failed',
        create_uncertain: 'save.uncertain',
        removed: 'save.unavailable',
      };
      assert.equal(report.save, expectedSave[recoveryStatus]);
      assert.equal(report.link, recoveryStatus === 'saved' ? 'link.posted' : 'link.not_attempted');
    }
    const expected = {
      'voice lost': 'interrupted',
      'stream lost': 'interrupted',
      'pdf rejected': 'observed_error',
      'stop unavailable': 'observed_error',
      'server drain timed out': 'observed_error',
      'server stream disconnected': 'interrupted',
      'server omitted drain': 'unconfirmed',
      'failed recovery': 'observed_error',
      'uncertain recovery': 'unconfirmed',
      'removed during recovery': 'unconfirmed',
    };
    assert.equal(report.outcome, expected[scenario] ?? 'complete');
    assert.equal(report.stop_link, 'linked');
    assert.equal(
      report.drain,
      {
        'stop unavailable': 'unknown',
        'server omitted drain': 'unknown',
        'server drain timed out': 'timed_out',
        'server stream disconnected': 'disconnected',
      }[scenario] ?? 'confirmed',
    );
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
        'stop.request',
        'stop.drain_confirmed',
        'capture.started',
      ]) {
        const incomplete = lines.filter((line) => JSON.parse(line).event_name !== missing);
        assert.equal(replay(incomplete, traceId).outcome, 'unconfirmed', missing);
      }
      for (const mutation of [
        { event: 'stop.response', edits: { parent_span_id: 'ffffffffffffffff' } },
        { event: 'stop.response', edits: { span_id: 'ffffffffffffffff' } },
        { event: 'stop.drain_confirmed', edits: { span_id: 'ffffffffffffffff' } },
        { event: 'pdf.posted', edits: { span_id: 'ffffffffffffffff' } },
      ]) {
        const modified = lines.map((line) => {
          const row = JSON.parse(line);
          return row.event_name === mutation.event
            ? JSON.stringify({ ...row, ...mutation.edits })
            : line;
        });
        const damaged = replay(modified, traceId);
        assert.equal(damaged.stop_link, 'conflict');
        assert.equal(damaged.outcome, 'unconfirmed');
      }
      // A valid-looking receipt prefixed by excessive whitespace is an oversized
      // input line. Skip it, retain synchronization, and accept a later genuine receipt.
      const longReceipt = ' '.repeat(256 * 1024) + JSON.stringify(pdf);
      assert.equal(replay([...withoutPdf, longReceipt], traceId).outcome, 'unconfirmed');
      assert.equal(
        replay([...withoutPdf, longReceipt, JSON.stringify(pdf)], traceId).outcome,
        'complete',
      );
      const timeout = {
        ...JSON.parse(lines.find((line) => JSON.parse(line).event_name === 'stop.drain_confirmed')),
        event_name: 'stop.drain_timed_out',
      };
      const conflictingDrain = replay([...lines, JSON.stringify(timeout)], traceId);
      assert.equal(conflictingDrain.drain, 'unknown');
      assert.equal(conflictingDrain.outcome, 'unconfirmed');
    }
    assert.ok(!lines.join('').includes('private-'));
    assert.ok(!lines.join('').includes(meetingId));
  });
}

test('denied recording start never enters capture and a failed transport start has a terminal report', async () => {
  const lines = [];
  const beginTrace = createStopTracer({
    write(line, done) {
      lines.push(line);
      done();
    },
  });
  let admitted = false;
  const meetingSurface = createMeetingSurface({
    beginTrace,
    meetingClient: {
      openStream() {
        throw Error('fixture unavailable');
      },
    },
  });
  const request = {
    subcommand: 'start',
    guildId: 'fixture',
    requesterId: 'fixture',
    voiceChannel: { id: 'fixture' },
    textChannel: {},
  };
  const appContext = {
    meetingSurface,
    directory: { getPersonByDiscordId: async () => (admitted ? { active: true } : null) },
  };
  const command = { auth: 'linked', subcommands: [{ name: 'start' }] };
  const denied = await runRecordAction(request, appContext, command);
  assert.ok(denied.ephemeral);
  assert.deepEqual(lines, []);
  admitted = true;
  await assert.rejects(
    runRecordAction({ ...request, propagateErrors: true }, appContext, command),
    /Recording start failed/,
  );
  const records = lines.map(JSON.parse);
  assert.deepEqual(
    records.map((r) => r.event_name),
    ['record.saved_requested', 'capture.start_failed'],
  );
  assert.equal(replay(lines, records[0].trace_id).outcome, 'observed_error');
});
