import { test } from 'node:test';
import assert from 'node:assert/strict';
import { MessageFlags } from 'discord.js';
import qna from '../src/commands/qna.js';
import { commands, partitionCommands } from '../src/commands/index.js';
import { buildDiscordData } from '../src/registerCommands.js';
import { createMeetingClient, MeetingQnaError } from '../src/meeting/meetingClient.js';
import { createMeetingSurface } from '../src/meeting/meetingSurface.js';
import { renderQnaAnswer } from '../src/messages/qna.js';
import { handleQnaInteraction } from '../src/adapters/discord/qna.js';
import { wireDiscordClient } from '../src/adapters/discord/index.js';
import { makeAttachmentPoster } from '../src/adapters/discord/meetingPosts.js';

const response = {
  answer: 'The deadline is Friday.',
  references: [{ speaker: 'Alice', start_ms: 1_000_000 }],
  window_start_ms: 900_000,
  window_end_ms: 1_800_000,
  shortened: false,
};

function interaction({ guildId = 'g', channelId = 'text', question = 'When?' } = {}) {
  const calls = [];
  return {
    guildId,
    channelId,
    commandName: 'qna',
    calls,
    deferred: false,
    options: { getString: () => question },
    isAutocomplete: () => false,
    isChatInputCommand: () => true,
    async deferReply(options) {
      calls.push(['defer', options]);
      this.deferred = true;
    },
    async editReply(payload) {
      calls.push(['edit', payload]);
    },
  };
}

const context = (impl) => ({
  discordGuildId: 'g',
  directory: {
    getPersonByDiscordId: () => {
      throw new Error('Must not look up a user');
    },
  },
  meetingSurface: { qna: impl },
});

function lastContent(i) {
  return i.calls.at(-1)[1].content;
}

test('qna is a private public-policy in-progress command with a bounded required question', () => {
  assert.equal(commands.get('qna'), qna);
  assert.equal(qna.auth, 'public');
  assert.equal(qna.ephemeral, true);
  assert.equal(qna.beta, false);
  assert.ok(partitionCommands([...commands.values()]).stable.includes(qna));
  const data = buildDiscordData(qna);
  assert.equal(data.options[0].required, true);
  assert.equal(data.options[0].max_length, 1000);
});

test('transport sends authenticated JSON and preserves scope and citations', async () => {
  const client = createMeetingClient({
    baseUrl: 'http://meeting',
    apiKey: 'key',
    fetchImpl: async (url, options) => {
      assert.equal(url, 'http://meeting/meetings/s/qna');
      assert.equal(options.method, 'POST');
      assert.equal(options.headers['X-API-Key'], 'key');
      assert.deepEqual(JSON.parse(options.body), { question: 'When?' });
      assert.ok(options.signal);
      return { ok: true, json: async () => response };
    },
  });
  assert.deepEqual(await client.qna('s', 'When?'), response);
});

for (const status of [404, 409, 413, 422, 429, 502, 503, 504]) {
  test(`transport preserves ${status} without including response content`, async () => {
    const client = createMeetingClient({
      baseUrl: 'http://meeting',
      fetchImpl: async () => ({ ok: false, status }),
    });
    await assert.rejects(
      client.qna('s', 'When?'),
      (e) => e instanceof MeetingQnaError && e.status === status,
    );
  });
}

test('transport rejects malformed responses and network failures safely', async () => {
  for (const json of [
    null,
    {},
    { ...response, references: [{ speaker: 'x', start_ms: -1 }] },
    { ...response, shortened: 'false' },
  ]) {
    const client = createMeetingClient({
      baseUrl: 'http://meeting',
      fetchImpl: async () => ({ ok: true, json: async () => json }),
    });
    await assert.rejects(client.qna('s', 'When?'), (e) => e.status === 502);
  }
  const client = createMeetingClient({
    fetchImpl: async () => {
      throw new Error('PRIVATE');
    },
  });
  await assert.rejects(
    client.qna('s', 'When?'),
    (e) => e.status === 503 && !e.message.includes('PRIVATE'),
  );
});

test('transport timeout covers response-body reading', async () => {
  const client = createMeetingClient({
    qnaTimeoutMs: 5,
    fetchImpl: async (url, { signal }) => ({
      ok: true,
      json: () =>
        new Promise((resolve, reject) =>
          signal.addEventListener('abort', () => reject(new Error('aborted'))),
        ),
    }),
  });
  await assert.rejects(client.qna('s', 'When?'), (e) => e.status === 504);
});

test('surface preserves the original meeting name across stop and a replacement recording', async () => {
  let finish;
  let called = 0;
  const sessionIds = ['s', 'replacement'];
  const surface = createMeetingSurface({
    genId: () => sessionIds.shift(),
    now: () => Date.UTC(2026, 9, 4, 23, 30),
    meetingClient: {
      openStream: () => ({ close() {}, endAudio() {} }),
      stop: async () => ({}),
      qna: (id, question) => {
        called++;
        assert.equal(id, 's');
        assert.equal(question, 'When?');
        return new Promise((r) => {
          finish = r;
        });
      },
    },
    makeRecorder: () => ({ start: async () => {}, stop: async () => {} }),
    poster: async () => {},
  });
  assert.equal(
    (await surface.qna({ guildId: 'g', channelId: 'text', question: 'When?' })).status,
    'not-recording',
  );
  await surface.start({
    guildId: 'g',
    voiceChannel: {},
    textChannel: { id: 'text' },
    name: 'Weekly team sync',
  });
  assert.equal(
    (await surface.qna({ guildId: 'other', channelId: 'text', question: 'When?' })).status,
    'not-recording',
  );
  const wrong = await surface.qna({ guildId: 'g', channelId: 'thread', question: 'When?' });
  assert.equal(wrong.status, 'wrong-channel');
  assert.equal(wrong.channelId, 'text');
  assert.equal(called, 0);
  const pending = surface.qna({ guildId: 'g', channelId: 'text', question: 'When?' });
  await surface.stop('g');
  assert.equal(
    (await surface.qna({ guildId: 'g', channelId: 'text', question: 'When?' })).status,
    'not-recording',
  );
  await surface.start({
    guildId: 'g',
    voiceChannel: {},
    textChannel: { id: 'text' },
    name: 'Next meeting',
  });
  finish(response);
  assert.deepEqual(await pending, {
    status: 'answered',
    response,
    meetingName: 'Weekly team sync_2026-10-04_1930.pdf',
  });
  assert.equal(surface.activeSession('g').sessionId, 'replacement');
  await surface.stop('g');
});

test('renderer includes verified citations and explicit shortened scope, within Discord limits', () => {
  let rendered = renderQnaAnswer(response);
  assert.match(rendered.content, /Meeting: Meeting/);
  assert.match(rendered.content, /Alice.*16:40/);
  assert.match(rendered.content, /15:00–30:00/);
  assert.deepEqual(rendered.allowedMentions, { parse: [] });
  rendered = renderQnaAnswer(
    {
      ...response,
      shortened: true,
      answer: '😀'.repeat(4000),
      references: [{ speaker: '@everyone *'.repeat(500), start_ms: 1_000_000 }],
    },
    'Weekly *sync* @everyone'.repeat(100),
  );
  assert.ok(rendered.content.length <= 2000);
  assert.match(rendered.content, /15-minute transcript exceeded the size limit/);
  assert.match(rendered.content, /Answer shortened for display/);
  assert.ok(!/[\uD800-\uDBFF]$/.test(rendered.content));
});

test('adapter defers privately before work, skips directory, and disables mentions', async () => {
  const i = interaction();
  await handleQnaInteraction(
    i,
    context(async (args) => {
      assert.equal(i.calls[0][0], 'defer');
      assert.equal(i.calls[0][1].flags, MessageFlags.Ephemeral);
      assert.deepEqual(args, { guildId: 'g', channelId: 'text', question: 'When?' });
      return { status: 'answered', response, meetingName: 'Weekly team sync' };
    }),
    qna,
  );
  assert.match(lastContent(i), /Meeting: Weekly team sync/);
  assert.match(lastContent(i), /Friday/);
  assert.deepEqual(i.calls.at(-1)[1].allowedMentions, { parse: [] });
});

for (const [options, expected] of [
  [{ guildId: null }, /server text channel/],
  [{ question: '  ' }, /1,000/],
  [{ question: 'x'.repeat(1001) }, /1,000/],
]) {
  test(`adapter rejects invalid invocation ${JSON.stringify(options).slice(0, 60)} before work`, async () => {
    const i = interaction(options);
    await handleQnaInteraction(
      i,
      context(() => {
        throw new Error('Must not call');
      }),
      qna,
    );
    assert.match(lastContent(i), expected);
    assert.equal(i.calls[0][1].flags, MessageFlags.Ephemeral);
  });
}

for (const [result, expected] of [
  [{ status: 'not-recording' }, /No active meeting/],
  [{ status: 'wrong-channel', channelId: '123' }, /<#123>/],
  [{ status: 'unconfigured' }, /isn’t configured/],
]) {
  test(`adapter renders ${result.status} privately`, async () => {
    const i = interaction();
    await handleQnaInteraction(
      i,
      context(async () => result),
      qna,
    );
    assert.match(lastContent(i), expected);
  });
}

for (const [status, expected] of [
  [404, /ending or has ended/],
  [409, /ending or has ended/],
  [413, /too large/],
  [422, /1,000/],
  [429, /busy answering other questions/],
  [502, /reliable answer/],
  [503, /temporarily unavailable/],
  [504, /too long/],
]) {
  test(`adapter renders backend ${status} privately`, async () => {
    const i = interaction();
    await handleQnaInteraction(
      i,
      context(async () => {
        throw new MeetingQnaError(status);
      }),
      qna,
    );
    assert.match(lastContent(i), expected);
    assert.equal(i.calls[0][1].flags, MessageFlags.Ephemeral);
  });
}

test('Discord wiring intercepts qna before neutral dispatch', async () => {
  const events = new Map();
  const client = { on: (event, handler) => events.set(event, handler), user: { id: 'bot' } };
  const i = interaction();
  wireDiscordClient(client, {
    commands: new Map([['qna', qna]]),
    appContext: context(async () => ({ status: 'answered', response })),
  });
  await events.get('interactionCreate')(i);
  assert.match(lastContent(i), /Friday/);
});

for (const stage of ['defer', 'edit']) {
  test(`Discord ${stage} failure never falls back to a public reply or logs content`, async (t) => {
    const logs = [];
    t.mock.method(console, 'error', (...args) => logs.push(args));
    const events = new Map();
    const client = { on: (event, handler) => events.set(event, handler), user: { id: 'bot' } };
    const i = interaction();
    if (stage === 'defer')
      i.deferReply = async () => {
        throw new Error('PRIVATE QUESTION');
      };
    else
      i.editReply = async () => {
        throw new Error('PRIVATE ANSWER');
      };
    i.reply = () => assert.fail('No public fallback');
    i.followUp = () => assert.fail('No public fallback');
    wireDiscordClient(client, {
      commands: new Map([['qna', qna]]),
      appContext: context(async () => ({ status: 'answered', response })),
    });
    await events.get('interactionCreate')(i);
    assert.deepEqual(logs, [
      [
        stage === 'defer'
          ? 'Could not deliver private /qna reply'
          : 'Could not deliver Discord reply',
      ],
    ]);
  });
}

for (const testingGuild of ['g', undefined]) {
  test(`qna works outside the testing guild when DISCORD_GUILD_ID is ${testingGuild}`, async () => {
    const i = interaction({ guildId: 'production' });
    const ctx = context(async (args) => {
      assert.equal(args.guildId, 'production');
      return { status: 'answered', response };
    });
    ctx.discordGuildId = testingGuild;
    await handleQnaInteraction(i, ctx, qna);
    assert.match(lastContent(i), /Friday/);
    assert.equal(i.calls[0][1].flags, MessageFlags.Ephemeral);
  });
}

for (const [name, startedAt, expected] of [
  ['_'.repeat(100), Date.UTC(2026, 9, 4, 23, 30), `${'_'.repeat(100)}_2026-10-04_1930.pdf`],
  [undefined, Date.UTC(2026, 9, 4, 23, 30), 'meeting_2026-10-04_1930.pdf'],
  ['  ', Date.UTC(2026, 11, 4, 23, 30), 'meeting_2026-12-04_1830.pdf'],
  [
    'Sponsorship / Sync: Q4',
    Date.UTC(2026, 9, 4, 23, 30),
    'Sponsorship Sync Q4_2026-10-04_1930.pdf',
  ],
  [' / : ? * ', Date.UTC(2026, 9, 4, 23, 30), 'meeting_2026-10-04_1930.pdf'],
]) {
  test(`qna label matches the posted PDF filename: ${expected}`, async () => {
    let clock = startedAt;
    const posts = [];
    const textChannel = { id: 'text', send: async (payload) => posts.push(payload) };
    const surface = createMeetingSurface({
      now: () => clock,
      genId: () => 's',
      meetingClient: {
        openStream: () => ({ close() {}, endAudio() {} }),
        qna: async () => response,
        stop: async () => ({ pdf_b64: Buffer.from('fake pdf').toString('base64') }),
      },
      makeRecorder: () => ({ start: async () => {}, stop: async () => {} }),
      poster: makeAttachmentPoster(),
    });
    await surface.start({ guildId: 'g', voiceChannel: {}, textChannel, name });
    clock += 20 * 60 * 1000;
    const result = await surface.qna({ guildId: 'g', channelId: 'text', question: 'When?' });
    assert.equal(result.meetingName, expected);
    const rendered = renderQnaAnswer(result.response, result.meetingName);
    assert.ok(rendered.content.includes(expected.replaceAll('_', '\\_')));
    await surface.stop('g');
    assert.equal(posts[0].files[0].name, result.meetingName);
  });
}
