import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createSubtitles } from '../src/meeting/subtitles.js';
import { createMeetingSurface } from '../src/meeting/meetingSurface.js';
import { makeSubtitleAdapter } from '../src/adapters/discord/subtitles.js';

const settle = async () => {
  for (let i = 0; i < 30; i++) await Promise.resolve();
};

function clock() {
  let now = 0;
  let next = 0;
  const timers = new Map();
  return {
    setTimer(fn, ms) {
      const id = ++next;
      timers.set(id, { fn, at: now + ms });
      return id;
    },
    clearTimer(id) {
      timers.delete(id);
    },
    async advance(ms) {
      now += ms;
      for (const [id, t] of [...timers]) {
        if (t.at <= now) {
          timers.delete(id);
          t.fn();
        }
      }
      await settle();
    },
    size: () => timers.size,
  };
}

function harness(overrides = {}) {
  const timer = clock();
  const sent = [];
  const archived = [];
  const notices = [];
  const thread = { id: 'thread-1' };
  const adapter = {
    createThread: async () => thread,
    send: async (target, content) => sent.push({ target, content }),
    archive: async (target) => archived.push(target),
    ...overrides,
  };
  const captions = createSubtitles({
    sessionId: 's1',
    channel: {},
    adapter,
    notify: async (notice) => notices.push(notice),
    ...timer,
  });
  const emit = (sequence, text, start_ms = 0) =>
    captions.event({
      type: 'subtitles.chunk',
      session_id: 's1',
      sequence,
      speaker_id: 'u1',
      display_name: 'Alice',
      start_ms,
      text,
    });
  const ready = async () => {
    captions.activate();
    captions.event({ type: 'session.ready', session_id: 's1', subtitle_events: true });
    await settle();
  };
  const complete = (last_sequence) =>
    captions.event({
      type: 'subtitles.complete',
      session_id: 's1',
      last_sequence,
      status: 'complete',
    });
  return { ...timer, captions, sent, archived, notices, thread, emit, ready, complete };
}

test('final chunks batch for two seconds, sort by speech time, then archive after tail', async () => {
  const f = harness();
  await f.ready();
  f.emit(1, 'later speech', 12000);
  await f.advance(1000);
  f.emit(2, 'earlier speech', 10000);
  await f.advance(999);
  assert.equal(f.sent.length, 0);
  await f.advance(1);
  assert.match(f.sent[0].content, /earlier speech[\s\S]*later speech/);
  f.emit(3, 'tail', 13000);
  f.complete(3);
  await f.captions.finish();
  assert.match(f.sent.at(-2).content, /tail/);
  assert.match(f.sent.at(-1).content, /ended/i);
  assert.deepEqual(f.archived, [f.thread]);
  assert.equal(f.size(), 0);
  f.captions.connectionLost(); // normal socket close after successful finalization
  await settle();
  assert.equal(f.notices.length, 0);
});

test('duplicates and other sessions are ignored; sequence gaps mark subtitles incomplete', async () => {
  const f = harness();
  await f.ready();
  f.emit(1, 'once');
  f.emit(1, 'duplicate');
  f.captions.event({ type: 'subtitles.chunk', session_id: 'other', sequence: 2, text: 'leak' });
  f.emit(3, 'gap');
  await f.captions.finish();
  const content = f.sent.map((e) => e.content).join('\n');
  assert.match(content, /once/);
  assert.doesNotMatch(content, /duplicate|leak|gap/);
  assert.match(content, /incomplete/i);
  assert.equal(f.notices.length, 1);
});

test('incomplete completion, missing tail and socket loss each notify exactly once', async () => {
  for (const reason of ['provider', 'missing-tail', 'socket-loss']) {
    const f = harness();
    await f.ready();
    if (reason === 'socket-loss') {
      f.captions.connectionLost();
      f.captions.connectionLost();
    } else {
      f.captions.event({
        type: 'subtitles.complete',
        session_id: 's1',
        last_sequence: reason === 'missing-tail' ? 1 : 0,
        status: reason === 'provider' ? 'incomplete' : 'complete',
      });
    }
    await f.captions.finish();
    assert.equal(f.notices.length, 1, reason);
    assert.match(f.sent.at(-1).content, /incomplete/);
    assert.deepEqual(f.archived, [f.thread]);
  }
});

test('oversized Unicode chunks fit Discord messages without splitting surrogate pairs', async () => {
  const f = harness();
  await f.ready();
  f.emit(1, '😀'.repeat(2100) + ' @everyone **hello**');
  f.complete(1);
  await f.captions.finish();
  assert.ok(f.sent.length >= 4);
  for (const { content } of f.sent) {
    assert.ok(content.length <= 2000);
    assert.ok(content.isWellFormed());
  }
  assert.match(f.sent.map((e) => e.content).join(''), /\\\*\\\*hello\\\*\\\*/);
});

test('thread setup failure and unsupported service each notify only once', async () => {
  const f = harness({
    createThread: async () => {
      throw new Error('missing permission');
    },
  });
  await f.ready();
  f.emit(1, 'ignored');
  await f.captions.finish();
  assert.equal(f.notices.length, 1);
  assert.equal(f.sent.length, 0);
  const oldService = harness();
  oldService.captions.activate();
  await oldService.advance(5000);
  await oldService.captions.finish();
  assert.equal(oldService.notices.length, 1);
  assert.equal(oldService.archived.length, 0);
});

test('queue overflow disables captions while a slow Discord send is pending', async () => {
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const f = harness({ send: async () => gate });
  await f.ready();
  f.emit(1, 'first');
  await f.advance(2000);
  f.emit(2, 'x'.repeat(256 * 1024));
  await settle();
  assert.equal(f.notices.length, 1);
  release();
  await f.captions.finish();
  assert.deepEqual(f.archived, [f.thread]);
});

test('a stalled Discord request cannot extend finish beyond its thirty-second deadline', async () => {
  const f = harness({ send: async () => new Promise(() => {}) });
  await f.ready();
  f.emit(1, 'received');
  await f.advance(2000);
  let finished = false;
  f.captions.finish().then(() => {
    finished = true;
  });
  await settle();
  await f.advance(30000);
  assert.equal(finished, true);
});

test('a failed report closes subtitle history immediately without waiting for completion', async () => {
  const timer = clock();
  const archived = [];
  const surface = createMeetingSurface({
    ...timer,
    subtitleAdapter: {
      createThread: async () => 'thread',
      send: async () => {},
      archive: async (thread) => archived.push(thread),
    },
    meetingClient: {
      openStream(sessionId, opts) {
        queueMicrotask(() =>
          opts.onEvent({ type: 'session.ready', session_id: sessionId, subtitle_events: true }),
        );
        return { endAudio() {}, close() {} };
      },
      stop: async () => {
        throw new Error('report unavailable');
      },
    },
    makeRecorder: () => ({ async start() {}, async stop() {} }),
    poster: async () => {},
  });
  await surface.start({ guildId: 'g1', textChannel: {} });
  let result;
  surface.stop('g1').then((value) => {
    result = value;
  });
  await settle();
  assert.deepEqual(archived, ['thread']);
  assert.equal(result?.status, 'error');
});

test('Discord adapter creates a sibling thread and suppresses mentions on every send', async () => {
  const messages = [];
  let options;
  let archives = 0;
  const thread = {
    send: async (payload) => messages.push(payload),
    setArchived: async () => archives++,
  };
  const parent = {
    send: async (payload) => {
      messages.push(payload);
      return {
        startThread: async (opts) => {
          options = opts;
          return thread;
        },
      };
    },
  };
  const launchThread = {
    isThread: () => true,
    parent,
    setArchived: () => {
      throw new Error('must not archive launch thread');
    },
  };
  const adapter = makeSubtitleAdapter();
  const result = await adapter.createThread({
    channel: launchThread,
    name: 'Planning',
    startedAt: 0,
  });
  assert.equal(result, thread);
  assert.match(options.name, /Subtitles.*Planning/);
  await adapter.send(result, '@everyone hello');
  await adapter.archive(result);
  assert.equal(archives, 1);
  assert.ok(messages.every((m) => m.allowedMentions.parse.length === 0));
});

test('different servers and successive sessions keep tail captions and archives isolated', async () => {
  const sockets = new Map();
  const sent = [];
  const archived = [];
  let id = 0;
  const surface = createMeetingSurface({
    genId: () => `s${++id}`,
    subtitleAdapter: {
      createThread: async ({ channel }) => channel,
      send: async (channel, text) => sent.push([channel, text]),
      archive: async (channel) => archived.push(channel),
    },
    meetingClient: {
      openStream(sessionId, opts) {
        sockets.set(sessionId, opts);
        queueMicrotask(() =>
          opts.onEvent({ type: 'session.ready', session_id: sessionId, subtitle_events: true }),
        );
        return { close() {}, endAudio() {} };
      },
      async stop(sessionId) {
        // Start a new session while the old session has left the server map.
        if (sessionId === 's1') await surface.start({ guildId: 'g1', textChannel: 'next' });
        sockets.get(sessionId).onEvent({
          type: 'subtitles.chunk',
          session_id: sessionId,
          sequence: 1,
          speaker_id: 'u1',
          display_name: 'Alice',
          start_ms: 0,
          text: sessionId,
        });
        sockets.get(sessionId).onEvent({
          type: 'subtitles.complete',
          session_id: sessionId,
          last_sequence: 1,
          status: 'complete',
        });
        return { pdf_b64: 'fake' };
      },
    },
    makeRecorder: () => ({ async start() {}, async stop() {} }),
    poster: async () => {},
  });
  await surface.start({ guildId: 'g1', textChannel: 'first' });
  await surface.start({ guildId: 'g2', textChannel: 'other' });
  await surface.stop('g1');
  await surface.stop('g2');
  await surface.stop('g1');
  assert.deepEqual(archived, ['first', 'other', 'next']);
  assert.ok(sent.some(([channel, text]) => channel === 'first' && text.includes('s1')));
  assert.ok(sent.some(([channel, text]) => channel === 'other' && text.includes('s2')));
  assert.ok(sent.some(([channel, text]) => channel === 'next' && text.includes('s3')));
});

test('subtitles:false skips subscription and thread creation while still posting the PDF', async () => {
  let options;
  let posted = false;
  const surface = createMeetingSurface({
    subtitleAdapter: {
      createThread: () => {
        throw new Error('must not create thread');
      },
    },
    meetingClient: {
      openStream(_id, opts) {
        options = opts;
        return { close() {}, endAudio() {} };
      },
      async stop() {
        return { pdf_b64: 'pdf' };
      },
    },
    makeRecorder: () => ({ async start() {}, async stop() {} }),
    poster: async () => {
      posted = true;
    },
  });
  await surface.start({ guildId: 'g1', subtitles: false });
  assert.equal(options.subtitles, false);
  assert.deepEqual(await surface.stop('g1'), { status: 'stopped' });
  assert.equal(posted, true);
});
