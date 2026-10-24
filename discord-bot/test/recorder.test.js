import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createRecorder } from '../src/meeting/recorder.js';

// A controllable clock + wait, so drain timing is deterministic rather than
// dependent on real timers.
function makeClock(onWait) {
  let t = 0;
  return {
    now: () => t,
    advance: (ms) => {
      t += ms;
    },
    // `onWait` runs on every poll of the drain loop. It is what lets a test
    // simulate audio still arriving DURING the drain -- a setInterval never
    // gets a turn here, because these awaits resolve as microtasks and the
    // loop finishes before the event loop reaches any macrotask.
    wait: async (ms) => {
      t += ms;
      if (onWait) onWait();
    },
  };
}

function makeConnection() {
  const streams = new Map();
  const conn = {
    destroyed: false,
    destroyedAt: null,
    receiver: {
      speaking: new EventEmitter(),
      subscribeCalls: [],
      subscribe(userId, options) {
        conn.receiver.subscribeCalls.push({ userId, options });
        // Mirror the real receiver: an existing subscription is REUSED, and it
        // is only forgotten once the stream closes.
        const existing = streams.get(userId);
        if (existing) return existing;
        const s = new EventEmitter();
        s.destroy = () => {
          s.destroyed = true;
          streams.delete(userId);
          s.emit('close');
        };
        streams.set(userId, s);
        return s;
      },
    },
    destroy() {
      this.destroyed = true;
    },
  };
  conn.streams = streams;
  return conn;
}

function makeSink() {
  const frames = [];
  return { frames, sendControl() {}, sendFrame: (id, ts, p) => frames.push([id, ts, p]) };
}

async function startedRecorder(clock, conn, sink) {
  const recorder = createRecorder({
    sink,
    monotonic: clock.now,
    wait: clock.wait,
    join: () => conn,
    ready: async () => {},
  });
  await recorder.start({
    id: 'vc1',
    guild: { id: 'g1', voiceAdapterCreator: null, members: { cache: new Map() } },
  });
  return recorder;
}

test('stop drains in-flight audio before destroying the connection', async () => {
  const clock = makeClock();
  const conn = makeConnection();
  const sink = makeSink();
  const recorder = await startedRecorder(clock, conn, sink);

  conn.receiver.speaking.emit('start', 'u1');
  const stream = conn.streams.get('u1');
  stream.emit('data', Buffer.from('early'));

  // A packet still in Discord's receive buffer lands just after /record stop.
  // Destroying immediately discards it, clipping the last words of the meeting.
  const stopping = recorder.stop();
  stream.emit('data', Buffer.from('LAST'));
  await stopping;

  const payloads = sink.frames.map(([, , p]) => p.toString());
  assert.ok(payloads.includes('LAST'), `tail packet dropped; got ${JSON.stringify(payloads)}`);
  assert.equal(conn.destroyed, true);
});

test('stop stops forwarding once the connection is torn down', async () => {
  const clock = makeClock();
  const conn = makeConnection();
  const sink = makeSink();
  const recorder = await startedRecorder(clock, conn, sink);
  conn.receiver.speaking.emit('start', 'u1');
  const stream = conn.streams.get('u1');

  await recorder.stop();
  const before = sink.frames.length;
  stream.emit('data', Buffer.from('after-teardown'));

  assert.equal(sink.frames.length, before);
});

test('stop gives up draining if audio keeps arriving', { timeout: 5000 }, async () => {
  let onPoll = () => {};
  const clock = makeClock(() => onPoll());
  const conn = makeConnection();
  const sink = makeSink();
  const recorder = await startedRecorder(clock, conn, sink);
  conn.receiver.speaking.emit('start', 'u1');
  const stream = conn.streams.get('u1');

  // Someone talks straight through the stop: every poll delivers another
  // packet, so the quiet condition can NEVER be met and only the hard deadline
  // can end the drain. Without that deadline this loops forever and the test
  // times out -- which is the point of the timeout above.
  onPoll = () => stream.emit('data', Buffer.from('x'));

  await recorder.stop();

  assert.equal(conn.destroyed, true);
  assert.ok(clock.now() >= 2000, `drained only ${clock.now()}ms; the 2s bound did not apply`);
  assert.ok(clock.now() < 2500, `drained ${clock.now()}ms; the bound is not tight`);
});

test('stop is safe to call twice', async () => {
  const clock = makeClock();
  const conn = makeConnection();
  const recorder = await startedRecorder(clock, conn, makeSink());
  await recorder.stop();
  await assert.doesNotReject(() => recorder.stop());
});

test('a speaker subscription is never allowed to self-end', async () => {
  const clock = makeClock();
  const conn = makeConnection();
  await startedRecorder(clock, conn, makeSink());

  conn.receiver.speaking.emit('start', 'u1');

  const { options } = conn.receiver.subscribeCalls.at(-1);
  // AfterSilence tears the subscription down 1s after someone stops talking,
  // and the receiver DISCARDS packets for a user with no subscription
  // (Receiver.onUdpMessage: `if (!stream) return`). Everything that speaker
  // says next is then lost until a re-subscribe happens to win the race.
  assert.notEqual(
    options?.end?.behavior,
    1, // EndBehaviorType.AfterSilence
    'subscription may self-end, which drops that speaker for the rest of the meeting',
  );
});

test('a speaker who pauses and resumes is still captured', async () => {
  const clock = makeClock();
  const conn = makeConnection();
  const sink = makeSink();
  await startedRecorder(clock, conn, sink);

  conn.receiver.speaking.emit('start', 'u1');
  const stream = conn.streams.get('u1');
  stream.emit('data', Buffer.from('first'));

  // Long pause. The real stream would have self-ended here; discord.js then
  // drops packets until something re-subscribes.
  clock.advance(30_000);
  conn.receiver.speaking.emit('start', 'u1'); // SpeakingMap re-fires
  const resumed = conn.streams.get('u1');
  assert.ok(resumed, 'the speaker lost their subscription entirely');
  resumed.emit('data', Buffer.from('second'));

  const payloads = sink.frames.map(([, , p]) => p.toString());
  assert.deepEqual(payloads, ['first', 'second']);
});

test('stop tears down the persistent subscriptions', async () => {
  const clock = makeClock();
  const conn = makeConnection();
  const recorder = await startedRecorder(clock, conn, makeSink());
  conn.receiver.speaking.emit('start', 'u1');
  const stream = conn.streams.get('u1');

  await recorder.stop();

  assert.equal(stream.destroyed, true, 'subscriptions must not outlive the recording');
});

test('a clock that jumps backwards never produces a negative timestamp', async () => {
  // Date.now() steps backwards on an NTP correction. A negative ts_ms makes
  // writeBigUInt64BE throw RangeError inside the opus 'data' handler, which
  // nothing catches -- and the bot registers no uncaughtException handler, so
  // that is a crash mid-meeting.
  let t = 10_000;
  const clock = {
    now: () => t,
    wait: async (ms) => {
      t += ms;
    },
  };
  const conn = makeConnection();
  const sink = makeSink();
  const recorder = createRecorder({
    sink,
    monotonic: clock.now,
    wait: clock.wait,
    join: () => conn,
    ready: async () => {},
  });
  await recorder.start({
    id: 'vc1',
    guild: { id: 'g1', voiceAdapterCreator: null, members: { cache: new Map() } },
  });

  conn.receiver.speaking.emit('start', 'u1');
  const stream = conn.streams.get('u1');
  t = 9_000; // the clock steps back one second
  stream.emit('data', Buffer.from('x'));

  const [, ts] = sink.frames.at(-1);
  assert.ok(ts >= 0, `negative timestamp ${ts} would throw on encode`);
  assert.doesNotThrow(() => Buffer.alloc(8).writeBigUInt64BE(BigInt(ts), 0));
});

test('an unidentified speaker gets a real name once the API answers', async () => {
  // The transcript showed raw 18-digit snowflakes for anyone already in the
  // channel when the bot joined: their voice_states entry carries no member
  // object, so nothing is cached and every local lookup returns the userId.
  // (A voice-state lookup is NOT an independent source -- VoiceState.member is
  // a getter over guild.members.cache -- so only the fetch closes that gap.)
  //
  // The service takes a speaker's name from the LATEST control frame, so a late
  // answer still fixes the name everywhere in the finished transcript.
  const clock = makeClock();
  const conn = makeConnection();
  const controls = [];
  const sink = makeSink();
  sink.sendControl = (c) => controls.push(c);

  let resolveFetch;
  const guild = {
    id: 'g1',
    voiceAdapterCreator: null,
    members: {
      cache: new Map(),
      fetch: () =>
        new Promise((r) => {
          resolveFetch = r;
        }),
    },
    voiceStates: { cache: new Map([['u1', { member: null }]]) },
  };
  const recorder = createRecorder({
    sink,
    monotonic: clock.now,
    wait: clock.wait,
    join: () => conn,
    ready: async () => {},
  });
  await recorder.start({ id: 'vc1', guild, client: { user: { id: 'bot' } } });

  conn.receiver.speaking.emit('start', 'u1');
  assert.deepEqual(controls, [{ speakerId: 'u1', displayName: 'u1' }]);

  resolveFetch({ user: { bot: false }, displayName: 'Priya' });
  await new Promise((r) => setImmediate(r));

  assert.deepEqual(
    controls.at(-1),
    { speakerId: 'u1', displayName: 'Priya' },
    'the raw snowflake was never corrected',
  );
});

test('the recorder never transcribes itself', async () => {
  const clock = makeClock();
  const conn = makeConnection();
  const sink = makeSink();
  const guild = {
    id: 'g1',
    voiceAdapterCreator: null,
    members: { cache: new Map() },
    voiceStates: { cache: new Map() },
  };
  const recorder = createRecorder({
    sink,
    monotonic: clock.now,
    wait: clock.wait,
    join: () => conn,
    ready: async () => {},
  });
  await recorder.start({ id: 'vc1', guild, client: { user: { id: 'bot-self' } } });

  conn.receiver.speaking.emit('start', 'bot-self');

  assert.equal(conn.streams.size, 0, 'the bot subscribed to its own audio');
});

test('an unresolved user is captured, then dropped if they turn out to be a bot', async () => {
  const clock = makeClock();
  const conn = makeConnection();
  const sink = makeSink();
  let resolveFetch;
  const guild = {
    id: 'g1',
    voiceAdapterCreator: null,
    members: {
      cache: new Map(),
      fetch: () =>
        new Promise((r) => {
          resolveFetch = r;
        }),
    },
    voiceStates: { cache: new Map() },
  };
  const recorder = createRecorder({
    sink,
    monotonic: clock.now,
    wait: clock.wait,
    join: () => conn,
    ready: async () => {},
  });
  await recorder.start({ id: 'vc1', guild, client: { user: { id: 'bot' } } });

  // Unknown user: subscribe optimistically rather than dropping a real person.
  conn.receiver.speaking.emit('start', 'u9');
  assert.equal(conn.streams.size, 1);

  // ...the API then says it was a music bot.
  resolveFetch({ user: { bot: true }, displayName: 'Groovy' });
  await new Promise((r) => setImmediate(r));

  assert.equal(conn.streams.get('u9'), undefined, 'a bot kept being transcribed and billed');
});

test('a bot dropped after the fetch stays dropped', async () => {
  const clock = makeClock();
  const conn = makeConnection();
  let resolveFetch;
  const guild = {
    id: 'g1',
    voiceAdapterCreator: null,
    members: {
      cache: new Map(),
      fetch: () =>
        new Promise((r) => {
          resolveFetch = r;
        }),
    },
    voiceStates: { cache: new Map() },
  };
  const recorder = createRecorder({
    sink: makeSink(),
    monotonic: clock.now,
    wait: clock.wait,
    join: () => conn,
    ready: async () => {},
  });
  await recorder.start({ id: 'vc1', guild, client: { user: { id: 'bot' } } });

  conn.receiver.speaking.emit('start', 'u9');
  resolveFetch({ user: { bot: true }, displayName: 'Groovy' });
  await new Promise((r) => setImmediate(r));
  assert.equal(conn.streams.get('u9'), undefined);

  // It speaks again. Clearing only the subscription let it back in, because
  // knownSpeakers already had the id so neither the fetch nor the bot check ran.
  conn.receiver.speaking.emit('start', 'u9');
  assert.equal(conn.streams.get('u9'), undefined, 'the dropped bot re-subscribed');
});
