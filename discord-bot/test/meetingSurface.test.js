import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createMeetingSurface } from '../src/meeting/meetingSurface.js';

// A dropped socket now runs the full salvage (recorder.stop -> POST /stop ->
// poster), which is several awaits deep. Draining the microtask queue plus one
// macrotask turn lets it finish before assertions rather than hand-counting
// `await Promise.resolve()`s that change whenever the chain does.
const settle = async () => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
  await new Promise((r) => setImmediate(r));
};

function makeFakes({ stopImpl, posterImpl } = {}) {
  const openStreamCalls = [];
  const closeCalls = [];
  const stopCalls = [];
  const makeRecorderCalls = [];
  const recorderStartCalls = [];
  const recorderStopCalls = [];
  const posterCalls = [];
  const callOrder = [];
  const openStreamOpts = [];

  const notifies = [];
  const notify = async (n) => { notifies.push(n); };
  const stream = {
    endAudio: () => { callOrder.push('stream.endAudio'); },
    close: (...args) => {
      callOrder.push('stream.close');
      closeCalls.push(args);
    },
  };

  const meetingClient = {
    openStream: (sessionId, opts) => {
      openStreamCalls.push({ sessionId, opts });
      openStreamOpts.push(opts);
      return stream;
    },
    stop: async (sessionId) => {
      callOrder.push('client.stop');
      stopCalls.push(sessionId);
      if (stopImpl) return stopImpl(sessionId);
      return { transcript: 't', minutes: 'm', pdf_b64: 'p' };
    },
  };

  const recorder = {
    start: async (voiceChannel) => {
      recorderStartCalls.push(voiceChannel);
    },
    stop: async () => {
      callOrder.push('recorder.stop');
      recorderStopCalls.push(true);
    },
  };

  const makeRecorder = (args) => {
    makeRecorderCalls.push(args);
    return recorder;
  };

  const poster = async (args) => {
    posterCalls.push(args);
    if (posterImpl) return posterImpl(args);
  };

  return {
    meetingClient,
    makeRecorder,
    poster,
    stream,
    recorder,
    openStreamCalls,
    openStreamOpts,
    closeCalls,
    stopCalls,
    makeRecorderCalls,
    recorderStartCalls,
    recorderStopCalls,
    posterCalls,
    callOrder,
    notify,
    notifies,
  };
}

test('start opens a stream, starts the recorder, and returns recording + sessionId', async () => {
  const fakes = makeFakes();
  const genId = () => 'sess-1';
  const surface = createMeetingSurface({ ...fakes, genId });

  const voiceChannel = { id: 'vc1' };
  const textChannel = { id: 'tc1' };
  const result = await surface.start({ guildId: 'g1', voiceChannel, textChannel });

  assert.deepEqual(result, { status: 'recording', sessionId: 'sess-1' });
  assert.equal(fakes.openStreamCalls.length, 1);
  assert.equal(fakes.openStreamCalls[0].sessionId, 'sess-1');
  assert.equal(fakes.openStreamCalls[0].opts.guildId, 'g1');
  assert.equal(typeof fakes.openStreamCalls[0].opts.onError, 'function');
  assert.equal(fakes.makeRecorderCalls.length, 1);
  assert.equal(fakes.makeRecorderCalls[0].sink, fakes.stream);
  // allow the fire-and-forget recorder.start promise to settle
  await Promise.resolve();
  assert.deepEqual(fakes.recorderStartCalls, [voiceChannel]);
});

test('double start for the same guild returns already-recording and does not open a second stream', async () => {
  const fakes = makeFakes();
  let n = 0;
  const genId = () => `sess-${++n}`;
  const surface = createMeetingSurface({ ...fakes, genId });

  const voiceChannel = { id: 'vc1' };
  const textChannel = { id: 'tc1' };
  const first = await surface.start({ guildId: 'g1', voiceChannel, textChannel });
  const second = await surface.start({ guildId: 'g1', voiceChannel, textChannel });

  assert.equal(first.status, 'recording');
  assert.deepEqual(second, { status: 'already-recording' });
  assert.equal(fakes.openStreamCalls.length, 1);
});

test('stop calls recorder.stop, stream.close, meetingClient.stop, then poster with the report, and returns stopped', async () => {
  const fakes = makeFakes();
  const genId = () => 'sess-1';
  const surface = createMeetingSurface({ ...fakes, genId });

  const voiceChannel = { id: 'vc1' };
  const textChannel = { id: 'tc1' };
  await surface.start({ guildId: 'g1', voiceChannel, textChannel, requesterId: 'u-99' });

  const result = await surface.stop('g1');

  assert.deepEqual(result, { status: 'stopped' });
  assert.equal(fakes.recorderStopCalls.length, 1);
  assert.equal(fakes.closeCalls.length, 1);
  assert.deepEqual(fakes.stopCalls, ['sess-1']);
  assert.equal(fakes.posterCalls.length, 1);
  assert.equal(fakes.posterCalls[0].channel, textChannel);
  // Whoever ran `/record start` is carried through the session so the poster
  // can @-mention them -- including when the stop came from auto-stop rather
  // than from a `/record stop` interaction.
  assert.equal(fakes.posterCalls[0].requesterId, 'u-99');
  assert.deepEqual(fakes.posterCalls[0].report, {
    transcript: 't',
    minutes: 'm',
    pdf_b64: 'p',
  });
  // Critical ordering: meetingClient.stop (finalize server-side) MUST happen
  // before stream.close() -- closing the WS first races the session into
  // being discarded server-side (WS-disconnect-without-prior-/stop => discard,
  // no finalize), which would lose the minutes/PDF/audio.
  assert.deepEqual(fakes.callOrder, ['recorder.stop', 'stream.endAudio', 'client.stop', 'stream.close']);
});

test('stop with no active session returns not-recording', async () => {
  const fakes = makeFakes();
  const surface = createMeetingSurface({ ...fakes, genId: () => 'sess-1' });
  const result = await surface.stop('unknown-guild');
  assert.deepEqual(result, { status: 'not-recording' });
});

test('a throwing meetingClient.stop clears the session (subsequent start succeeds) and returns error', async () => {
  const fakes = makeFakes({
    stopImpl: async () => {
      throw new Error('boom');
    },
  });
  let n = 0;
  const genId = () => `sess-${++n}`;
  const surface = createMeetingSurface({ ...fakes, genId });

  const voiceChannel = { id: 'vc1' };
  const textChannel = { id: 'tc1' };
  await surface.start({ guildId: 'g1', voiceChannel, textChannel });

  const result = await surface.stop('g1');
  assert.deepEqual(result, { status: 'error' });

  // stream.close() must still run (in the finally) even though
  // meetingClient.stop() threw, so the WS is never left orphaned.
  assert.equal(fakes.closeCalls.length, 1);

  // session should be cleared: a fresh start for the same guild succeeds
  const second = await surface.start({ guildId: 'g1', voiceChannel, textChannel });
  assert.equal(second.status, 'recording');
});

test('a throwing poster clears the session (subsequent start succeeds) and returns error', async () => {
  const fakes = makeFakes({
    posterImpl: async () => {
      throw new Error('poster failed');
    },
  });
  let n = 0;
  const genId = () => `sess-${++n}`;
  const surface = createMeetingSurface({ ...fakes, genId });

  const voiceChannel = { id: 'vc1' };
  const textChannel = { id: 'tc1' };
  await surface.start({ guildId: 'g1', voiceChannel, textChannel });

  const result = await surface.stop('g1');
  assert.deepEqual(result, { status: 'error' });

  const second = await surface.start({ guildId: 'g1', voiceChannel, textChannel });
  assert.equal(second.status, 'recording');
});

test('status returns recording + elapsedMs while active, not-recording otherwise', async () => {
  let t = 1000;
  const now = () => t;
  const fakes = makeFakes();
  const surface = createMeetingSurface({ ...fakes, now, genId: () => 'sess-1' });

  assert.deepEqual(surface.status('g1'), { status: 'not-recording' });

  const voiceChannel = { id: 'vc1' };
  const textChannel = { id: 'tc1' };
  await surface.start({ guildId: 'g1', voiceChannel, textChannel });

  t = 1500;
  const active = surface.status('g1');
  assert.equal(active.status, 'recording');
  assert.equal(active.elapsedMs, 500);

  await surface.stop('g1');
  assert.deepEqual(surface.status('g1'), { status: 'not-recording' });
});

test('a WS stream error tears down the session so a subsequent start for that guild succeeds', async () => {
  const fakes = makeFakes();
  const surface = createMeetingSurface({ ...fakes, genId: () => 'sess-1' });

  const voiceChannel = { id: 'vc1' };
  const textChannel = { id: 'tc1' };
  const result = await surface.start({ guildId: 'g1', voiceChannel, textChannel });
  assert.equal(result.status, 'recording');

  // the surface must have passed an onError callback into openStream
  const onError = fakes.openStreamOpts[0].onError;
  assert.equal(typeof onError, 'function');

  // simulate the transport reporting a WS error
  onError(new Error('ECONNREFUSED'));
  await settle();

  // stream should have been closed as part of the salvage
  assert.equal(fakes.closeCalls.length, 1);

  // and the guild's session slot must be free again
  const second = await surface.start({ guildId: 'g1', voiceChannel, textChannel });
  assert.equal(second.status, 'recording');
});

test('a failed join reports error, not recording', async () => {
  const fakes = makeFakes();
  fakes.recorder.start = async () => { throw new Error('join failed'); };
  const surface = createMeetingSurface({ ...fakes, genId: () => 'sess-1' });

  const originalError = console.error;
  console.error = () => {};
  try {
    // Telling the user "🔴 Recording…" when the bot never joined means a
    // missing Connect permission or a full channel looks like success, and they
    // only discover the meeting was never captured at /record stop.
    const result = await surface.start({ guildId: 'g1', voiceChannel: { id: 'vc1' }, textChannel: { id: 'tc1' } });
    assert.equal(result.status, 'error');

    // ...and the guild is left clean, so they can just try again.
    fakes.recorder.start = async () => {};
    const second = await surface.start({ guildId: 'g1', voiceChannel: { id: 'vc1' }, textChannel: { id: 'tc1' } });
    assert.equal(second.status, 'recording');
  } finally {
    console.error = originalError;
  }
});

test('a failed join is logged and never throws', async () => {
  const fakes = makeFakes();
  fakes.recorder.start = async () => { throw new Error('join failed'); };
  const surface = createMeetingSurface({ ...fakes, genId: () => 'sess-1' });

  const originalError = console.error;
  const errors = [];
  console.error = (...args) => errors.push(args);
  try {
    await assert.doesNotReject(() =>
      surface.start({ guildId: 'g1', voiceChannel: { id: 'vc1' }, textChannel: { id: 'tc1' } }),
    );
    assert.ok(errors.length >= 1);
  } finally {
    console.error = originalError;
  }
});

test('activeSession returns {sessionId, voiceChannel} while active and null otherwise', async () => {
  const fakes = makeFakes();
  const surface = createMeetingSurface({ ...fakes, genId: () => 'sess-1' });

  const voiceChannel = { id: 'vc1' };
  const textChannel = { id: 'tc1' };

  assert.equal(surface.activeSession('g1'), null);
  await surface.start({ guildId: 'g1', voiceChannel, textChannel });
  assert.deepEqual(surface.activeSession('g1'), { sessionId: 'sess-1', voiceChannel });

  await surface.stop('g1');
  assert.equal(surface.activeSession('g1'), null);
});

test('stop signals end-of-audio after the recorder stops and before /stop', async () => {
  const fakes = makeFakes();
  const surface = createMeetingSurface({ ...fakes, genId: () => 'sess-1' });
  await surface.start({ guildId: 'g1', voiceChannel: { id: 'vc1' }, textChannel: { id: 'tc1' } });

  await surface.stop('g1');

  // Order is the whole point. The recorder must stop first (no more frames),
  // then the end-of-audio signal goes down the WS behind all the audio, and
  // only then does /stop run -- which waits for that signal before finalizing.
  // Any other order lets /stop cut the meeting off mid-tail.
  assert.deepEqual(fakes.callOrder, [
    'recorder.stop',
    'stream.endAudio',
    'client.stop',
    'stream.close',
  ]);
});

test('a WS error stops the recorder, not just the stream', async () => {
  const fakes = makeFakes();
  const surface = createMeetingSurface({ ...fakes, genId: () => 'sess-1' });
  await surface.start({ guildId: 'g1', voiceChannel: { id: 'vc1' }, textChannel: { id: 'tc1' } });
  fakes.callOrder.length = 0;

  fakes.openStreamOpts.at(-1).onError(new Error('socket died'));
  await settle();

  // Closing the stream but leaving the voice connection up parks the bot in the
  // channel: it keeps one receive stream open per speaker while /record status
  // reports nothing recording, so only a restart can remove it.
  assert.ok(fakes.callOrder.includes('recorder.stop'), `recorder not stopped: ${fakes.callOrder}`);
  // The service holds a disconnected session for its grace period, so the right
  // response to a dead socket is to FINALIZE it, not to announce a lost
  // meeting. The channel gets the minutes; nobody gets a "died" warning.
  assert.equal(fakes.stopCalls.length, 1, 'the transcript was never salvaged via POST /stop');
  assert.equal(fakes.posterCalls.length, 1, 'the minutes were never posted');
  assert.equal(fakes.notifies.length, 0, `unexpected failure notice: ${JSON.stringify(fakes.notifies)}`);
});


test('a dropped socket that cannot be salvaged tells the channel, and still leaves the voice channel', async () => {
  // The grace period has expired, the service redeployed, or /stop failed --
  // whatever the reason, the minutes are genuinely gone and the meeting must
  // not be left silently dead with the bot parked in the voice channel.
  const fakes = makeFakes({ stopImpl: () => { throw new Error('404 session gone'); } });
  const surface = createMeetingSurface({ ...fakes, genId: () => 'sess-1' });
  await surface.start({ guildId: 'g1', voiceChannel: { id: 'vc1' }, textChannel: { id: 'tc1' } });
  fakes.callOrder.length = 0;

  fakes.openStreamOpts.at(-1).onClose();
  await settle();

  assert.equal(fakes.posterCalls.length, 0, 'nothing should have been posted');
  assert.equal(fakes.notifies.length, 1, 'the channel was never told the minutes were lost');
  assert.match(fakes.notifies[0].content, /could not be recovered/);
  assert.ok(fakes.callOrder.includes('recorder.stop'), `recorder not stopped: ${fakes.callOrder}`);
  assert.equal(surface.status('g1').status, 'not-recording');
});

test('a clean WS close tears down too', async () => {
  const fakes = makeFakes();
  const surface = createMeetingSurface({ ...fakes, genId: () => 'sess-1' });
  await surface.start({ guildId: 'g1', voiceChannel: { id: 'vc1' }, textChannel: { id: 'tc1' } });
  fakes.callOrder.length = 0;

  // An auth rejection, a duplicate session, or a redeploy closes the socket
  // with no error at all; frames after that are dropped silently.
  fakes.openStreamOpts.at(-1).onClose();
  await settle();

  assert.ok(fakes.callOrder.includes('recorder.stop'), `recorder not stopped: ${fakes.callOrder}`);
  assert.equal(surface.status('g1').status, 'not-recording');
  // Salvaged rather than discarded: the whole point of the grace period.
  assert.equal(fakes.posterCalls.length, 1, 'the minutes were never posted');
});

test('a stream that fails synchronously never registers a session', async () => {
  // openStream can invoke onError before it returns (a bad URL, a transport
  // that throws on construction). The teardown fires first, finds no session
  // to key off, and no-ops -- so without a separate "was a teardown asked
  // for?" flag, start() carried on and registered a session over an
  // already-dead socket: /record status says recording, every frame is
  // discarded, and only /record stop reveals it.
  const fakes = makeFakes();
  fakes.meetingClient.openStream = (sessionId, opts) => {
    fakes.openStreamOpts.push(opts);
    opts.onError(new Error('connect refused'));
    return fakes.stream;
  };
  const surface = createMeetingSurface({ ...fakes, genId: () => 'sess-1' });

  const originalError = console.error;
  console.error = () => {};
  try {
    const result = await surface.start({ guildId: 'g1', voiceChannel: { id: 'vc1' }, textChannel: { id: 'tc1' } });
    assert.equal(result.status, 'error');
  } finally {
    console.error = originalError;
  }

  assert.equal(surface.status('g1').status, 'not-recording');
  // The command reply already tells them it failed; a second "the recording
  // stopped unexpectedly" notice for a recording that never began is noise.
  assert.equal(fakes.notifies.length, 0, `spurious notice: ${JSON.stringify(fakes.notifies)}`);
});

test('a normal stop does not announce that the recording died', async () => {
  // A REGRESSION GUARD, not a bug fix -- this already held. stop() deletes the
  // session and then closes the stream, which fires onClose straight back into
  // teardown; the sessionId check is what stops it there. The `tornDown` flag
  // added above runs BEFORE that check, so it would be easy to widen teardown
  // into posting "⚠️ the recording stopped unexpectedly" seconds after the
  // minutes landed. Pin the behaviour so that stays impossible.
  const fakes = makeFakes();
  const surface = createMeetingSurface({ ...fakes, genId: () => 'sess-1' });
  await surface.start({ guildId: 'g1', voiceChannel: { id: 'vc1' }, textChannel: { id: 'tc1' } });

  const result = await surface.stop('g1');
  assert.equal(result.status, 'stopped');

  fakes.openStreamOpts.at(-1).onClose();
  await new Promise((r) => setImmediate(r));

  assert.equal(fakes.notifies.length, 0, `told the user a completed meeting died: ${JSON.stringify(fakes.notifies)}`);
  assert.equal(fakes.recorderStopCalls.length, 1, 'the recorder was stopped twice');
});

test('a permanently lost voice connection finalizes the meeting', async () => {
  // The recorder detects the loss (a 4014 park, or retries exhausted) and calls
  // onVoiceLost. Nothing used to pass that callback, so it defaulted to a no-op
  // and the detection never left recorder.js: capture stopped, the session
  // stayed open, and /record stop later returned a truncated transcript with no
  // hint anything was missing.
  //
  // Audio already sent is safe -- the service has it -- so this FINALIZES
  // rather than tearing down, and the user gets minutes for what was captured.
  const fakes = makeFakes();
  const surface = createMeetingSurface({ ...fakes, genId: () => 'sess-1' });
  await surface.start({ guildId: 'g1', voiceChannel: { id: 'vc1' }, textChannel: { id: 'tc1' } });

  const { onVoiceLost } = fakes.makeRecorderCalls.at(-1);
  assert.equal(typeof onVoiceLost, 'function', 'onVoiceLost was never wired to the recorder');

  onVoiceLost();
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));

  assert.equal(fakes.posterCalls.length, 1, 'the captured audio never became minutes');
  assert.equal(fakes.stopCalls.length, 1, 'the service session was never finalized');
  assert.equal(surface.status('g1').status, 'not-recording');
  // ...and they are told why the minutes stop where they do.
  assert.match(fakes.notifies.at(0)?.content ?? '', /voice connection/i);
});
