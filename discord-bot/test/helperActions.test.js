import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createAppContext } from '../src/context.js';
import { wireDiscordClient } from '../src/adapters/discord/index.js';
import { commands } from '../src/commands/index.js';
import { sendHelperReply } from '../src/adapters/discord/mentions.js';
import * as mentions from '../src/adapters/discord/mentions.js';
import { createTeamService } from '../src/services/teamService.js';

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

function buttonId(messages, label) {
  return messages
    .flatMap((p) => p.components ?? [])
    .flatMap((r) => r.components)
    .find((b) => b.label === label)?.custom_id;
}

// Keep the executor, registry, authorization, router and delivery real. Only
// external model/directory/meeting/Discord boundaries are replaced.
async function commandAction(f, name, input = {}, generate = () => {}) {
  const person = { id: 'p1', display_name: 'Alex', active: true, access_level: 'admin' };
  let calls = 0;
  f.ctx.directory = {
    getPersonByDiscordId: async () => person,
    getTeamBySlug: async () => ({ id: 't1', slug: 'infra' }),
    updateTeam: async (_id, patch) => {
      calls++;
      return { id: 't1', ...patch };
    },
  };
  f.ctx.teamService = createTeamService({ directory: f.ctx.directory });
  f.ctx.llmClient.chat = async () => {
    generate();
    return {
      stop_reason: 'tool_use',
      content_blocks: [{ type: 'tool_use', id: 't', name, input }],
    };
  };
  const result = await f.ctx.toolExecutor.run({
    messages: [{ role: 'user', content: 'Please help' }],
    principal: { person },
    context: { onBehalfOf: person.id },
    request: {
      discordUserId: 'owner',
      runMeeting: async ({ commandName, options }) => {
        assert.equal(commandName, 'qna');
        assert.deepEqual(options, { question: 'When?' });
        calls++;
        return { content: 'Private meeting answer' };
      },
    },
  });
  assert.equal(result.outcome, 'action');
  f.action = result.action;
  return { calls: () => calls, person };
}

test('closed DM sends defer Q&A until the private button and execute it only once', async () => {
  const f = fixture();
  const qna = await commandAction(f, 'qna', { question: 'When?' });
  f.message.author.createDM = async () => ({
    send: async () => {
      throw Object.assign(new Error('Cannot send messages to this user'), { code: 50007 });
    },
  });
  await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
  const before = qna.calls();
  const id = buttonId(f.publicMessages, 'View privately');
  assert.ok(id);
  const i = button(id);
  await Promise.all([f.handlers.interactionCreate(i), f.handlers.interactionCreate(button(id))]);
  assert.deepEqual({ before, after: qna.calls() }, { before: 0, after: 1 });
  assert.match(JSON.stringify(i.replies), /Private meeting answer/);
  assert.doesNotMatch(JSON.stringify(f.publicMessages), /Private meeting answer/);
});

test('a result delivery failure after execution never offers a retry button', async () => {
  const f = fixture();
  f.action.confirm = false;
  f.message.author.createDM = async () => ({
    send: async (payload) => {
      if (payload.content === 'Private result') throw new Error('Delivery failed after execution');
    },
  });
  await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
  assert.equal(f.executed(), 1);
  assert.equal(buttonId(f.publicMessages, 'View privately'), undefined);
  assert.match(JSON.stringify(f.publicMessages), /not retried|not run again/i);
  assert.doesNotMatch(JSON.stringify(f.publicMessages), /Private result/);
});

test('a partial DM result failure cannot rerun Q&A through fallback', async () => {
  const f = fixture();
  const qna = await commandAction(f, 'qna', { question: 'When?' });
  const execute = f.action.execute;
  f.action.execute = async () => {
    await execute();
    return { content: 'Private meeting answer\n'.repeat(150) };
  };
  let resultParts = 0;
  f.message.author.createDM = async () => ({
    send: async (p) => {
      if (p.content.includes('Private meeting answer') && ++resultParts === 2) {
        throw new Error('Second part failed');
      }
      f.privateMessages.push(p);
    },
  });
  await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
  assert.equal(qna.calls(), 1);
  assert.equal(resultParts, 2);
  assert.equal(buttonId(f.publicMessages, 'View privately'), undefined);
  assert.doesNotMatch(JSON.stringify(f.publicMessages), /Private meeting answer/);
});

test('a failed execution is consumed and never offered again', async () => {
  const f = fixture();
  f.action.confirm = false;
  let attempts = 0;
  f.action.execute = async () => {
    attempts++;
    throw new Error('Backend failure');
  };
  await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
  assert.equal(attempts, 1);
  assert.equal(buttonId(f.publicMessages, 'View privately'), undefined);
  assert.match(JSON.stringify(f.publicMessages), /not retried/i);
  // The failed action must not occupy a pending slot.
  for (let i = 0; i < 8; i++) f.ctx.helperActions.add({ ...f.action });
});

test('concurrent presentations reserve the final pending slot before awaiting delivery', async () => {
  const f = fixture();
  f.action.confirm = false;
  for (let i = 0; i < 7; i++) f.ctx.helperActions.add({ ...f.action });
  let release;
  const blocked = new Promise((resolve) => {
    release = resolve;
  });
  f.message.author.createDM = async () => {
    await blocked;
    throw new Error('Closed DMs');
  };
  const first = sendHelperReply(
    f.send,
    { action: { ...f.action } },
    { message: f.message, appContext: f.ctx },
  );
  await sendHelperReply(
    f.send,
    { action: { ...f.action } },
    { message: f.message, appContext: f.ctx },
  );
  assert.match(JSON.stringify(f.publicMessages), /too many pending/i);
  release();
  await first;
  assert.equal(f.executed(), 0);
  assert.ok(buttonId(f.publicMessages, 'View privately'));
});

test('closed-DM confirmation gets one full window after private review; reviewing again cannot extend it', async (t) => {
  const f = fixture();
  const epoch = Date.now();
  let clock = 0;
  t.mock.method(Date, 'now', () => epoch + clock);
  t.mock.method(performance, 'now', () => clock);
  const write = await commandAction(f, 'team_rename', { slug: 'infra', new_label: 'New label' });
  f.message.author.createDM = async () => ({
    send: async () => {
      throw new Error('Closed DMs');
    },
  });
  await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
  const reviewId = buttonId(f.publicMessages, 'View privately');
  clock = 110000;
  const i = button(reviewId);
  await f.handlers.interactionCreate(i);
  const confirmId = buttonId(i.replies, 'Confirm');
  assert.ok(confirmId);
  assert.equal(write.calls(), 0);
  clock = 220000;
  await f.handlers.interactionCreate(button(reviewId));
  assert.equal(write.calls(), 0);
  clock = 230000;
  const expired = button(confirmId);
  await f.handlers.interactionCreate(expired);
  assert.equal(write.calls(), 0);
  assert.match(JSON.stringify(expired.replies), /expired/);
});

test('a fresh confirmation window still rejects a revoked directory permission', async (t) => {
  const f = fixture();
  let clock = 0;
  const epoch = Date.now();
  t.mock.method(Date, 'now', () => epoch + clock);
  t.mock.method(performance, 'now', () => clock);
  const write = await commandAction(
    f,
    'team_rename',
    { slug: 'infra', new_label: 'New label' },
    () => {
      clock = 70000;
    },
  );
  await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
  clock = 150000;
  write.person.access_level = 'member';
  const i = button(buttonId(f.privateMessages, 'Confirm'));
  await f.handlers.interactionCreate(i);
  assert.equal(write.calls(), 0);
  assert.match(JSON.stringify(i.replies), /permissions/);
});

for (const limit of ['requester', 'global']) {
  test(`the ${limit} pending cap rejects before checks, DM delivery or execution with a capacity message`, async () => {
    const f = fixture();
    f.action.confirm = false;
    for (let i = 0; i < (limit === 'requester' ? 8 : 500); i++) {
      f.ctx.helperActions.add({
        ...f.action,
        userId: limit === 'requester' ? 'owner' : `user-${i}`,
      });
    }
    let checks = 0,
      deliveries = 0;
    f.action.check = async () => {
      checks++;
    };
    f.message.author.createDM = async () => ({
      send: async () => {
        deliveries++;
        throw new Error('Closed DMs');
      },
    });
    await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
    assert.deepEqual(
      { checks, deliveries, executions: f.executed() },
      { checks: 0, deliveries: 0, executions: 0 },
    );
    assert.match(JSON.stringify(f.publicMessages), /too many pending|capacity/i);
    assert.doesNotMatch(JSON.stringify(f.publicMessages), /permissions|expired/i);
  });
}

for (const clickAfter of [119999, 120000]) {
  test(`confirmation uses the presentation clock after slow generation and preview (click +${clickAfter}ms)`, async (t) => {
    const f = fixture();
    const epoch = Date.now();
    let clock = 0;
    t.mock.method(Date, 'now', () => epoch + clock);
    t.mock.method(performance, 'now', () => clock);
    const write = await commandAction(
      f,
      'team_rename',
      { slug: 'infra', new_label: 'New label' },
      () => {
        clock = 70000;
      },
    );
    f.message.author.createDM = async () => ({
      send: async (p) => {
        clock += 10000;
        f.privateMessages.push(p);
      },
    });
    await sendHelperReply(f.send, { action: f.action }, { message: f.message, appContext: f.ctx });
    assert.equal(write.calls(), 0);
    const id = buttonId(f.privateMessages, 'Confirm');
    assert.ok(id);
    clock += clickAfter;
    const i = button(id);
    await f.handlers.interactionCreate(i);
    assert.equal(write.calls(), clickAfter < 120000 ? 1 : 0);
    if (clickAfter === 120000) assert.match(JSON.stringify(i.replies), /expired/);
    await f.handlers.interactionCreate(button(id));
    assert.equal(write.calls(), clickAfter < 120000 ? 1 : 0);
  });
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
