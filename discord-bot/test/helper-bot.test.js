import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  startsWithBotMention,
  stripLeadingMention,
  threadHistoryToTurns,
  chunkForDiscord,
  handleMention,
  wireDiscordClient,
} from '../src/adapters/discord.js';
import { DirectoryUnavailable } from '../src/directoryClient.js';

const BOT = '999';

test('startsWithBotMention: true only when the ping leads the message', () => {
  assert.equal(startsWithBotMention(`<@${BOT}> hello`, BOT), true);
  assert.equal(startsWithBotMention(`  <@!${BOT}> hi`, BOT), true); // nickname form + leading space
  assert.equal(startsWithBotMention(`hey <@${BOT}> covered this`, BOT), false); // mid-message
  assert.equal(startsWithBotMention('no mention', BOT), false);
  assert.equal(startsWithBotMention('', BOT), false);
});

test('stripLeadingMention removes the leading ping and following whitespace', () => {
  assert.equal(stripLeadingMention(`<@${BOT}>   how do I link?`, BOT), 'how do I link?');
  assert.equal(stripLeadingMention(`<@!${BOT}> hi`, BOT), 'hi');
  assert.equal(stripLeadingMention('no mention here', BOT), 'no mention here');
});

test('threadHistoryToTurns maps roles, keeps speakers separate, drops leading assistant', () => {
  const fetched = [
    { author: { id: BOT, username: 'misty' }, content: 'Hi! How can I help?' }, // leading assistant → dropped
    { author: { id: '1', username: 'alexx' }, content: `<@${BOT}> question one` }, // mention stripped
    { author: { id: '2', username: 'bobl' }, content: 'and also this' }, // different human → own turn
    { author: { id: BOT, username: 'misty' }, content: 'answer part 1' },
    { author: { id: BOT, username: 'misty' }, content: 'answer part 2' },
    {
      author: { id: '1', username: 'alexx' },
      member: { displayName: 'Alex Q' },
      content: `<@${BOT}> follow up`,
    },
  ];
  assert.deepEqual(threadHistoryToTurns(fetched, BOT), [
    { role: 'user', text: 'question one', authorId: '1', authorName: 'alexx' },
    { role: 'user', text: 'and also this', authorId: '2', authorName: 'bobl' },
    { role: 'assistant', text: 'answer part 1' },
    { role: 'assistant', text: 'answer part 2' },
    { role: 'user', text: 'follow up', authorId: '1', authorName: 'Alex Q' }, // nickname wins
  ]);
});

test('threadHistoryToTurns drops empty turns', () => {
  const fetched = [
    { author: { id: '1', username: 'alexx' }, content: `<@${BOT}>` }, // only a ping → empty → dropped
    { author: { id: '1', username: 'alexx' }, content: 'real question' },
  ];
  assert.deepEqual(threadHistoryToTurns(fetched, BOT), [
    { role: 'user', text: 'real question', authorId: '1', authorName: 'alexx' },
  ]);
});

test('chunkForDiscord splits on newline boundaries under 2000 chars', () => {
  assert.deepEqual(chunkForDiscord('short'), ['short']);
  assert.deepEqual(chunkForDiscord(''), []);
  const big = 'a'.repeat(1500) + '\n' + 'b'.repeat(1500);
  const chunks = chunkForDiscord(big);
  assert.equal(chunks.length, 2);
  assert.ok(chunks.every((c) => c.length <= 2000));
  assert.equal(chunks[0], 'a'.repeat(1500));
  assert.equal(chunks[1], 'b'.repeat(1500));
});

test('chunkForDiscord hard-splits a single oversized line', () => {
  const chunks = chunkForDiscord('x'.repeat(4500));
  assert.equal(chunks.length, 3);
  assert.ok(chunks.every((c) => c.length <= 2000));
  assert.equal(chunks.join(''), 'x'.repeat(4500));
});

const BOT_ID = '999';

// Fake channel/thread: records sent messages + typing; can be a thread or not.
// `starter` models ThreadChannel#fetchStarterMessage: the message the thread was
// opened from, which for a text-channel thread lives in the parent channel and
// is therefore absent from `history`. `starterThrows` models a deleted one.
function fakeChannel({ isThread = false, history = [], starter, starterThrows = false } = {}) {
  const sent = [];
  const fetched = [];
  return {
    sent,
    fetched, // args of each messages.fetch call
    typing: 0,
    isThread: () => isThread,
    async send(content) {
      sent.push(content);
    },
    async sendTyping() {
      this.typing += 1;
    },
    async fetchStarterMessage() {
      if (starterThrows) throw new Error('unknown message');
      return starter ?? null;
    },
    messages: {
      fetch: async (opts) => {
        fetched.push(opts);
        return { values: () => history };
      },
    },
  };
}

function fakeMessage({ content, authorId = '1', authorName = 'alexx', channel, thread }) {
  return {
    content,
    author: { id: authorId, bot: false, username: authorName },
    channel,
    reply: async (c) => {
      (channel.replies ??= []).push(c);
    },
    startThread: async () => thread,
  };
}

function ctx({
  principal = { person: { id: 'p1', display_name: 'Alex' } },
  answer,
  dirThrows = false,
} = {}) {
  return {
    directory: {
      getPersonByDiscordId: async () =>
        dirThrows
          ? (() => {
              throw new DirectoryUnavailable('down');
            })()
          : (principal?.person ?? null),
    },
    helperService: { answer: answer ?? (async () => ({ content: 'the answer' })) },
  };
}

test('handleMention in a channel: opens a thread and posts the answer', async () => {
  const thread = fakeChannel({ isThread: true });
  const channel = fakeChannel({ isThread: false });
  const message = fakeMessage({ content: `<@${BOT_ID}> how do I link?`, channel, thread });
  await handleMention(message, { appContext: ctx(), botId: BOT_ID });
  assert.deepEqual(thread.sent, ['the answer']);
  assert.equal(thread.typing, 1);
});

test('handleMention in a thread: replays history and posts in the thread', async () => {
  let seen;
  const answer = async ({ turns }) => {
    seen = turns;
    return { content: 'reply' };
  };
  const history = [
    { author: { id: BOT_ID, username: 'misty' }, content: 'earlier answer' },
    { author: { id: '1', username: 'alexx' }, content: `<@${BOT_ID}> follow up` },
  ];
  const thread = fakeChannel({ isThread: true, history });
  const message = fakeMessage({ content: `<@${BOT_ID}> follow up`, channel: thread });
  await handleMention(message, { appContext: ctx({ answer }), botId: BOT_ID });
  assert.deepEqual(thread.sent, ['reply']);
  assert.deepEqual(seen, [{ role: 'user', text: 'follow up', authorId: '1', authorName: 'alexx' }]);
});

test('handleMention in a thread: every speaker reaches the service separately attributed', async () => {
  let seen;
  const answer = async ({ turns }) => {
    seen = turns;
    return { content: 'reply' };
  };
  const history = [
    { author: { id: '1', username: 'alexx' }, content: `<@${BOT_ID}> how do I get repo access?` },
    { author: { id: BOT_ID, username: 'misty' }, content: 'Ask Infra.' },
    { author: { id: '2', username: 'bobl' }, content: 'seconded' },
    { author: { id: '3', username: 'cara' }, content: `<@${BOT_ID}> what about mine?` },
  ];
  const thread = fakeChannel({ isThread: true, history });
  const message = fakeMessage({
    content: `<@${BOT_ID}> what about mine?`,
    authorId: '3',
    authorName: 'cara',
    channel: thread,
  });
  await handleMention(message, { appContext: ctx({ answer }), botId: BOT_ID });
  assert.deepEqual(thread.fetched, [{ limit: 100 }], "fetches Discord's single-call maximum");
  assert.deepEqual(
    seen.map((t) => [t.role, t.authorId]),
    [
      ['user', '1'],
      ['assistant', undefined],
      ['user', '2'],
      ['user', '3'],
    ],
  );
});

test('handleMention in a thread: replays the starter message that lives in the parent channel', async () => {
  let seen;
  const answer = async ({ turns }) => {
    seen = turns;
    return { content: 'reply' };
  };
  // The founding question isn't in the thread's own fetch — only the bot's
  // answer and what came after.
  const starter = {
    id: 's1',
    author: { id: '1', username: 'alexx' },
    content: `<@${BOT_ID}> how do I get repo access?`,
  };
  const history = [
    { id: 'm1', author: { id: BOT_ID, username: 'misty' }, content: 'Ask Infra.' },
    { id: 'm2', author: { id: '1', username: 'alexx' }, content: `<@${BOT_ID}> and for staging?` },
  ];
  const thread = fakeChannel({ isThread: true, history, starter });
  const message = fakeMessage({ content: `<@${BOT_ID}> and for staging?`, channel: thread });
  await handleMention(message, { appContext: ctx({ answer }), botId: BOT_ID });

  assert.deepEqual(
    seen.map((t) => t.text),
    [
      'how do I get repo access?', // would have been lost entirely
      'Ask Infra.', // and this with it, as a leading assistant turn
      'and for staging?',
    ],
  );
  assert.equal(seen[0].authorId, '1', 'starter keeps its author attribution');
});

test('handleMention in a thread: a starter already in the fetch is not duplicated', async () => {
  let seen;
  const answer = async ({ turns }) => {
    seen = turns;
    return { content: 'reply' };
  };
  // Forum threads return a starter that IS the thread's first message.
  const starter = {
    id: 'm1',
    author: { id: '1', username: 'alexx' },
    content: `<@${BOT_ID}> first post`,
  };
  const history = [
    starter,
    { id: 'm2', author: { id: BOT_ID, username: 'misty' }, content: 'an answer' },
    { id: 'm3', author: { id: '1', username: 'alexx' }, content: `<@${BOT_ID}> follow up` },
  ];
  const thread = fakeChannel({ isThread: true, history, starter });
  const message = fakeMessage({ content: `<@${BOT_ID}> follow up`, channel: thread });
  await handleMention(message, { appContext: ctx({ answer }), botId: BOT_ID });

  assert.deepEqual(
    seen.map((t) => t.text),
    ['first post', 'an answer', 'follow up'],
  );
});

test('handleMention in a thread: a deleted starter costs context, not the answer', async () => {
  let seen;
  const answer = async ({ turns }) => {
    seen = turns;
    return { content: 'reply' };
  };
  const history = [
    { id: 'm1', author: { id: '1', username: 'alexx' }, content: `<@${BOT_ID}> follow up` },
  ];
  const thread = fakeChannel({ isThread: true, history, starterThrows: true });
  const message = fakeMessage({ content: `<@${BOT_ID}> follow up`, channel: thread });
  await handleMention(message, { appContext: ctx({ answer }), botId: BOT_ID });

  assert.deepEqual(thread.sent, ['reply'], 'still answers');
  assert.deepEqual(
    seen.map((t) => t.text),
    ['follow up'],
  );
});

test('handleMention in a thread: survives a channel with no fetchStarterMessage', async () => {
  let seen;
  const answer = async ({ turns }) => {
    seen = turns;
    return { content: 'reply' };
  };
  const history = [
    { id: 'm1', author: { id: '1', username: 'alexx' }, content: `<@${BOT_ID}> follow up` },
  ];
  const thread = fakeChannel({ isThread: true, history });
  delete thread.fetchStarterMessage;
  const message = fakeMessage({ content: `<@${BOT_ID}> follow up`, channel: thread });
  await handleMention(message, { appContext: ctx({ answer }), botId: BOT_ID });

  assert.deepEqual(
    seen.map((t) => t.text),
    ['follow up'],
  );
});

test('handleMention in a channel: the opening turn carries the asker identity', async () => {
  let seen;
  const answer = async ({ turns }) => {
    seen = turns;
    return { content: 'the answer' };
  };
  const thread = fakeChannel({ isThread: true });
  const channel = fakeChannel({ isThread: false });
  const message = fakeMessage({
    content: `<@${BOT_ID}> how do I link?`,
    authorId: '7',
    authorName: 'dee',
    channel,
    thread,
  });
  await handleMention(message, { appContext: ctx({ answer }), botId: BOT_ID });
  assert.deepEqual(seen, [
    { role: 'user', text: 'how do I link?', authorId: '7', authorName: 'dee' },
  ]);
});

test('handleMention: unlinked author gets the link prompt, no thread', async () => {
  const channel = fakeChannel({ isThread: false });
  const message = fakeMessage({ content: `<@${BOT_ID}> hi`, channel });
  await handleMention(message, { appContext: ctx({ principal: null }), botId: BOT_ID });
  assert.equal(channel.replies.length, 1);
  assert.match(channel.replies[0], /link/i);
});

test('handleMention: LlmUnavailable posts a friendly error, no throw', async () => {
  const thread = fakeChannel({ isThread: true });
  const channel = fakeChannel({ isThread: false });
  const message = fakeMessage({ content: `<@${BOT_ID}> hi`, channel, thread });
  const answer = async () => {
    throw new Error('llm down');
  };
  await handleMention(message, { appContext: ctx({ answer }), botId: BOT_ID });
  assert.equal(thread.sent.length, 1);
  assert.match(thread.sent[0], /trouble/i);
});

test('handleMention: empty LLM answer posts a friendly fallback, no silent dead-end', async () => {
  const thread = fakeChannel({ isThread: true });
  const channel = fakeChannel({ isThread: false });
  const message = fakeMessage({ content: `<@${BOT_ID}> hi`, channel, thread });
  const answer = async () => ({ content: '' });
  await handleMention(message, { appContext: ctx({ answer }), botId: BOT_ID });
  assert.equal(thread.sent.length, 1);
  assert.match(thread.sent[0], /couldn't come up with an answer/i);
});

test('handleMention: startThread failure gets a friendly reply, no throw', async () => {
  const channel = fakeChannel({ isThread: false });
  const message = fakeMessage({ content: `<@${BOT_ID}> hi`, channel });
  message.startThread = async () => {
    throw new Error('missing permission');
  };
  await assert.doesNotReject(() => handleMention(message, { appContext: ctx(), botId: BOT_ID }));
  assert.equal(channel.replies.length, 1);
  assert.match(channel.replies[0], /couldn't open a thread/i);
});

test('handleMention: a bare ping with no question does nothing', async () => {
  const channel = fakeChannel({ isThread: false });
  const message = fakeMessage({ content: `<@${BOT_ID}>`, channel });
  await handleMention(message, { appContext: ctx(), botId: BOT_ID });
  assert.equal(channel.replies, undefined);
});

test('handleMention: directory-down fails closed, no thread created', async () => {
  const channel = fakeChannel({ isThread: false });
  const message = fakeMessage({ content: `<@${BOT_ID}> hi`, channel });
  await handleMention(message, { appContext: ctx({ dirThrows: true }), botId: BOT_ID });
  assert.equal(channel.replies.length, 1);
  assert.match(channel.replies[0], /can't verify you right now|directory is unavailable/i);
  assert.equal(channel.sent.length, 0);
});

// ctx variant whose directory lookup throws a plain (non-DirectoryUnavailable)
// error, to exercise handleMention's re-throw + the listener's own catch.
function ctxThrowingPlainError() {
  return {
    directory: {
      getPersonByDiscordId: async () => {
        throw new Error('boom');
      },
    },
    helperService: { answer: async () => ({ content: 'the answer' }) },
  };
}

test('messageCreate listener: ignores bot-authored messages (no self-trigger)', async () => {
  const handlers = {};
  const client = {
    user: { id: BOT_ID },
    on: (evt, fn) => {
      handlers[evt] = fn;
    },
  };
  wireDiscordClient(client, { commands: new Map(), appContext: ctx() });

  const thread = fakeChannel({ isThread: true });
  const channel = fakeChannel({ isThread: false });
  const message = fakeMessage({ content: `<@${BOT_ID}> hi`, channel, thread });
  message.author.bot = true;

  await handlers.messageCreate(message);

  assert.equal(channel.replies, undefined);
  assert.equal(channel.sent.length, 0);
  assert.equal(thread.sent.length, 0);
  assert.equal(thread.typing, 0);
});

test('messageCreate listener: dispatches a real leading-mention message to handleMention', async () => {
  const handlers = {};
  const client = {
    user: { id: BOT_ID },
    on: (evt, fn) => {
      handlers[evt] = fn;
    },
  };
  wireDiscordClient(client, { commands: new Map(), appContext: ctx() });

  const thread = fakeChannel({ isThread: true });
  const channel = fakeChannel({ isThread: false });
  const message = fakeMessage({ content: `<@${BOT_ID}> how do I link?`, channel, thread });

  await handlers.messageCreate(message);

  assert.deepEqual(thread.sent, ['the answer']);
});

test('messageCreate listener: swallows a throw from handleMention', async () => {
  const handlers = {};
  const client = {
    user: { id: BOT_ID },
    on: (evt, fn) => {
      handlers[evt] = fn;
    },
  };
  wireDiscordClient(client, { commands: new Map(), appContext: ctxThrowingPlainError() });

  const channel = fakeChannel({ isThread: false });
  const message = fakeMessage({ content: `<@${BOT_ID}> hi`, channel });

  await assert.doesNotReject(() => handlers.messageCreate(message));
});
