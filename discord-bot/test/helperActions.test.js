import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createAppContext } from '../src/context.js';
import { wireDiscordClient } from '../src/adapters/discord/index.js';
import { commands } from '../src/commands/index.js';
import { sendHelperReply } from '../src/adapters/discord/mentions.js';
import * as mentions from '../src/adapters/discord/mentions.js';

function fixture() {
  const ctx = createAppContext({ helperToolsEnabled: true });
  const handlers = {};
  wireDiscordClient(
    {
      on: (name, fn) => {
        handlers[name] = fn;
      },
      user: { id: 'bot' },
    },
    { commands, appContext: ctx },
  );
  const privateMessages = [],
    publicMessages = [];
  const message = {
    author: { id: 'owner', createDM: async () => ({ send: async (p) => privateMessages.push(p) }) },
  };
  let allowed = true,
    executed = 0;
  const action = {
    userId: 'owner',
    summary: '/team rename\n{"slug":"infra","new_label":"Private name"}',
    confirm: true,
    check: async () => {
      if (!allowed) throw new Error('Permission revoked');
    },
    execute: async () => {
      executed++;
      return { content: 'Private result', ephemeral: false };
    },
  };
  const send = async (p) => publicMessages.push(p);
  return {
    ctx,
    handlers,
    message,
    action,
    send,
    privateMessages,
    publicMessages,
    revoke: () => {
      allowed = false;
    },
    executed: () => executed,
  };
}

function button(customId, userId = 'owner') {
  const replies = [];
  return {
    customId,
    user: { id: userId },
    replies,
    deferred: false,
    ephemeral: true,
    isButton: () => true,
    isAutocomplete: () => false,
    isChatInputCommand: () => false,
    async deferReply() {
      this.deferred = true;
    },
    async editReply(p) {
      replies.push(p);
      this.replied = true;
    },
    async followUp(p) {
      replies.push(p);
    },
  };
}

test('write proposals send private details and do not execute until the requester confirms once', async () => {
  const f = fixture();
  await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
  assert.equal(f.executed(), 0);
  assert.match(JSON.stringify(f.privateMessages), /Private name/);
  assert.equal(JSON.stringify(f.publicMessages).includes('Private name'), false);
  const customId = f.privateMessages
    .flatMap((p) => p.components ?? [])
    .flatMap((r) => r.components)
    .find((b) => b.label === 'Confirm').custom_id;
  const intruder = button(customId, 'someone-else');
  await f.handlers.interactionCreate(intruder);
  assert.equal(f.executed(), 0);
  assert.equal(JSON.stringify(intruder.replies).includes('Private name'), false);
  await Promise.all([
    f.handlers.interactionCreate(button(customId)),
    f.handlers.interactionCreate(button(customId)),
  ]);
  assert.equal(f.executed(), 1);
});

test('revocation after a proposal blocks the confirmation', async () => {
  const f = fixture();
  await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
  const id = f.privateMessages
    .flatMap((p) => p.components ?? [])
    .flatMap((r) => r.components)
    .find((b) => b.label === 'Confirm').custom_id;
  f.revoke();
  await f.handlers.interactionCreate(button(id));
  assert.equal(f.executed(), 0);
});

test('closed DMs offer private review without exposing proposal arguments publicly', async () => {
  const f = fixture();
  f.message.author.createDM = async () => {
    throw new Error('DM closed');
  };
  await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
  assert.equal(JSON.stringify(f.publicMessages).includes('Private name'), false);
  const id = f.publicMessages
    .flatMap((p) => p.components ?? [])
    .flatMap((r) => r.components)[0].custom_id;
  const i = button(id);
  await f.handlers.interactionCreate(i);
  assert.match(JSON.stringify(i.replies), /Private name/);
  assert.equal(f.executed(), 0);
});

test('a failed private preview never exposes a confirmation button', async () => {
  const f = fixture();
  f.message.author.createDM = async () => {
    throw new Error('DM closed');
  };
  await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
  const id = f.publicMessages
    .flatMap((p) => p.components ?? [])
    .flatMap((r) => r.components)[0].custom_id;
  const i = button(id);
  let failed = false;
  i.editReply = async (payload) => {
    if (!failed) {
      failed = true;
      throw new Error('Discord delivery failure');
    }
    i.replies.push(payload);
    i.replied = true;
  };
  await f.handlers.interactionCreate(i);
  assert.equal(JSON.stringify(i.replies).includes('Confirm'), false);
  assert.equal(f.executed(), 0);
});

test('a failed public acknowledgement does not invalidate a delivered private confirmation', async () => {
  const f = fixture();
  await sendHelperReply(
    async () => {
      throw new Error('Thread deleted');
    },
    { action: f.action },
    { message: f.message, appContext: f.ctx },
  ).catch(() => {});
  const id = f.privateMessages
    .flatMap((p) => p.components ?? [])
    .flatMap((r) => r.components)
    .find((b) => b.label === 'Confirm').custom_id;
  await f.handlers.interactionCreate(button(id));
  assert.equal(f.executed(), 1);
});

test('read results go to the requester and never to the channel', async () => {
  const f = fixture();
  f.action.confirm = false;
  await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
  assert.equal(f.executed(), 1);
  assert.match(JSON.stringify(f.privateMessages), /Private result/);
  assert.equal(JSON.stringify(f.publicMessages).includes('Private result'), false);
});

test('pending actions are bounded, expire, and are inaccessible to another user', (t) => {
  const f = fixture();
  assert.ok(f.ctx.helperActions, 'pending action store must be wired');
  const token = f.ctx.helperActions.add(f.action);
  assert.equal(f.ctx.helperActions.get(token, 'other'), null);
  assert.equal(f.ctx.helperActions.take(token, 'owner'), f.action);
  assert.equal(f.ctx.helperActions.take(token, 'owner'), null);
  const expiring = f.ctx.helperActions.add(f.action);
  const later = Date.now() + 120001;
  t.mock.method(Date, 'now', () => later);
  assert.equal(f.ctx.helperActions.get(expiring, 'owner'), null);
  for (let i = 0; i < 8; i++) f.ctx.helperActions.add(f.action);
  assert.throws(() => f.ctx.helperActions.add(f.action), /pending/);
});

test('meeting tools bind the exact source thread and re-fetch the requester voice state', async () => {
  let fetched = [],
    seen;
  const voiceChannel = { id: 'voice-now' };
  const message = {
    author: { id: 'owner', username: 'alex' },
    guild: {
      id: 'guild',
      members: {
        fetch: async (id) => {
          fetched.push(id);
          return { voice: { channel: voiceChannel } };
        },
      },
      channels: { fetch: async () => message.channel },
    },
    channel: {
      id: 'source-thread',
      parentId: 'parent',
      isThread: () => true,
      permissionsFor: () => ({ has: () => true }),
    },
  };
  let session = { sessionId: 'first', name: 'First meeting' };
  let stopped = 0;
  const ctx = {
    meetingSurface: {
      activeSession: () => session,
      stop: async () => {
        stopped++;
        return { status: 'stopped' };
      },
      qna: async (args) => {
        seen = args;
        return { status: 'wrong-channel', channelId: 'other' };
      },
      status: () => ({ status: 'not-recording' }),
    },
  };
  const request = mentions.createHelperRequest?.(message, ctx, 'bot');
  assert.ok(request, 'trusted helper request must be constructed by the Discord adapter');
  assert.equal(request.discordUserId, 'owner');
  assert.equal(request.discordChannelId, 'parent');
  const reply = await request.runMeeting({ commandName: 'qna', options: { question: 'When?' } });
  assert.deepEqual(seen, { guildId: 'guild', channelId: 'source-thread', question: 'When?' });
  assert.match(reply.content, /other/);
  const status = await request.runMeeting({
    commandName: 'record',
    subcommand: 'status',
    options: {},
  });
  assert.match(status.content, /voice-now/);
  assert.ok(fetched.some((value) => value === 'owner' || value.user === 'owner'));
  session = { sessionId: 'replacement', name: 'Replacement meeting' };
  const stop = await request.runMeeting({ commandName: 'record', subcommand: 'stop', options: {} });
  assert.equal(stopped, 0);
  assert.match(stop.content, /changed|ended/);
  message.channel.permissionsFor = () => ({ has: () => false });
  await assert.rejects(
    request.runMeeting({ commandName: 'qna', options: { question: 'When?' } }),
    /access/,
  );
});

test('a Discord mention reaches the wired tool, authenticates its author, and privately delivers the real command result', async (t) => {
  const privateMessages = [],
    publicMessages = [],
    identities = [];
  let modelCalls = 0;
  const person = { id: 'p1', display_name: 'Alex', active: true, access_level: 'member' };
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url.startsWith('http://directory/people/by-identifier/discord/')) {
      identities.push(url.split('/').at(-1));
      return Response.json(person);
    }
    if (url.startsWith('http://directory/teams'))
      return Response.json([{ id: 't1', slug: 'infra', label: 'Infrastructure' }]);
    if (url.startsWith('http://directory/memberships')) return Response.json([]);
    if (url === 'http://llm/chat') {
      modelCalls++;
      const body = JSON.parse(options.body);
      assert.ok(body.tools.some((tool) => tool.name === 'team_list'));
      assert.equal(JSON.stringify(body.messages).includes('FUTURE_ATTACK'), false);
      return Response.json({
        content: '',
        stop_reason: 'tool_use',
        content_blocks: [{ type: 'tool_use', id: 't1', name: 'team_list', input: {} }],
      });
    }
    assert.fail(`Unexpected offline request: ${url}`);
  });
  const ctx = createAppContext({
    helperToolsEnabled: true,
    directoryBaseUrl: 'http://directory',
    directoryApiKey: 'fake',
    llmBaseUrl: 'http://llm',
    llmApiKey: 'fake',
  });
  const channel = {
    id: '222222222222222222',
    parentId: '333333333333333333',
    isThread: () => true,
    permissionsFor: () => ({ has: () => true }),
    send: async (p) => publicMessages.push(p),
    sendTyping: async () => {},
    messages: {
      fetch: async () =>
        new Map([
          ['current', message],
          ['future', { id: '101', content: 'FUTURE_ATTACK', author: { id: 'attacker' } }],
        ]),
    },
  };
  const message = {
    id: '100',
    content: '<@bot> list teams',
    channel,
    author: {
      id: '111111111111111111',
      username: 'alex',
      createDM: async () => ({ send: async (p) => privateMessages.push(p) }),
    },
    guild: {
      id: '444444444444444444',
      members: {
        fetch: async (arg) => {
          assert.deepEqual(arg, { user: '111111111111111111', force: true });
          return {};
        },
      },
      channels: {
        fetch: async (_id, options) => {
          assert.equal(options.force, true);
          return channel;
        },
      },
    },
  };
  await mentions.handleMention(message, { appContext: ctx, botId: 'bot' });
  assert.equal(modelCalls, 1);
  assert.ok(identities.length >= 3);
  assert.ok(identities.every((id) => id === '111111111111111111'));
  assert.match(JSON.stringify(privateMessages), /Infrastructure/);
  assert.equal(JSON.stringify(publicMessages).includes('Infrastructure'), false);
});
