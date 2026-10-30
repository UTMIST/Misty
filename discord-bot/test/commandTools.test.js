import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createAppContext } from '../src/context.js';
import { createTeamService } from '../src/services/teamService.js';

function fixture(level = 'member') {
  const ctx = createAppContext({ helperToolsEnabled: true });
  let person = { id: 'p1', display_name: 'Alex', active: true, access_level: level };
  const calls = [];
  ctx.directory = {
    async getPersonByDiscordId(id) {
      calls.push(['identify', id]);
      return person;
    },
    async listTeams() {
      calls.push(['list']);
      return [{ id: 't1', slug: 'infra', label: 'Infrastructure' }];
    },
    async getTeamBySlug(slug) {
      return { id: 't1', slug, label: 'Infrastructure' };
    },
    async updateTeam(id, patch) {
      calls.push(['update', id, patch]);
      return { id, ...patch };
    },
  };
  ctx.teamService = createTeamService({ directory: ctx.directory });
  const request = {
    discordUserId: '111111111111111111',
    discordHandle: 'alex',
    discordGuildId: '222222222222222222',
    discordChannelId: '333333333333333333',
    sourceChannelId: '444444444444444444',
    channelUsedParent: true,
  };
  return {
    ctx,
    calls,
    request,
    principal: { person },
    setPerson: (value) => {
      person = value;
    },
  };
}

async function tool(f, name, input = {}) {
  const handler = f.ctx.toolRegistry.handlers.get(name);
  assert.equal(typeof handler, 'function', `${name} must be available`);
  return handler(input, { request: f.request, principal: f.principal });
}

test('team lookup prepares a public read, runs as the actual requester, and keeps results out of model input', async () => {
  const f = fixture();
  const result = await tool(f, 'team_list');
  assert.ok(result.action);
  assert.equal(result.action.confirm, false);
  assert.equal(result.action.publicRead, true);
  assert.equal(
    f.calls.some((c) => c[0] === 'list'),
    false,
  );
  const reply = await result.action.execute();
  assert.match(reply.content, /Infrastructure/);
  assert.match(reply.publicReply.payload.content, /Infrastructure/);
  assert.ok(
    f.calls.filter((c) => c[0] === 'identify').every((c) => c[1] === f.request.discordUserId),
  );
  assert.equal(JSON.stringify(result.content).includes('Infrastructure'), false);
});

test('only approved directory/help reads are public by default; DM results stay private', async () => {
  for (const name of [
    'team_list',
    'team_roster',
    'help',
    'bug',
    'my_teams',
    'whoami',
    'doc_list',
    'team_google_status',
    'channel_access_show',
    'helper_limits_show',
    'record_status',
    'qna',
  ]) {
    const f = fixture('admin');
    const input =
      name === 'team_roster' || name === 'team_google_status'
        ? { team: 'infra' }
        : name === 'qna'
          ? { question: 'When?' }
          : {};
    const { action } = await tool(f, name, input);
    assert.equal(
      action.publicRead,
      ['team_list', 'team_roster', 'help', 'bug'].includes(name),
      name,
    );
  }
  const f = fixture();
  delete f.request.discordGuildId;
  const { action } = await tool(f, 'team_list');
  assert.equal(action.publicRead, false);
  assert.equal((await action.execute()).publicReply, undefined);
});

test('recording status can run publicly only in its exact origin channel', async () => {
  const f = fixture();
  f.request.recordingTextChannelId = f.request.discordChannelId;
  assert.equal((await tool(f, 'record_status')).action.publicRead, false);
  f.request.recordingTextChannelId = f.request.sourceChannelId;
  assert.equal((await tool(f, 'record_status')).action.publicRead, true);
});

test('member cannot prepare an admin write despite a claimed admin principal', async () => {
  const f = fixture();
  f.principal.person = { ...f.principal.person, access_level: 'superuser' };
  const result = await tool(f, 'team_rename', { slug: 'infra', new_label: 'Changed' });
  assert.equal(result.is_error, true);
  assert.equal(result.action, undefined);
  assert.equal(
    f.calls.some((c) => c[0] === 'update'),
    false,
  );
});

test('admin write requires confirmation and rechecks privileges before executing', async () => {
  const f = fixture('admin');
  const result = await tool(f, 'team_rename', { slug: 'infra', new_label: 'Changed' });
  assert.equal(result.action.confirm, true);
  assert.equal(
    f.calls.some((c) => c[0] === 'update'),
    false,
  );
  f.setPerson({ ...f.principal.person, access_level: 'member' });
  await assert.rejects(result.action.execute(), /permission|authorized|access/i);
  assert.equal(
    f.calls.some((c) => c[0] === 'update'),
    false,
  );
});

test('all tools including help reject unlinked, inactive and relinked requesters', async () => {
  for (const person of [null, { id: 'p1', active: false }, { id: 'other', active: true }]) {
    const f = fixture();
    f.setPerson(person);
    const result = await tool(f, 'help');
    assert.equal(result.is_error, true);
    assert.equal(result.action, undefined);
  }
});

test('tool arguments reject identity injection, invalid types, missing required values and invalid choices', async () => {
  for (const input of [
    { slug: 'infra', new_label: 'Changed', discordUserId: 'attacker' },
    { slug: 123, new_label: 'Changed' },
    { slug: 'infra' },
    { slug: 'infra', new_label: '' },
  ]) {
    const f = fixture('admin');
    const result = await tool(f, 'team_rename', input);
    assert.equal(result.is_error, true);
    assert.equal(f.calls.length, 0);
  }
  const f = fixture('admin');
  const invalid = await tool(f, 'team_add', {
    user: '123456789012345678',
    team: 'infra',
    role: 'owner',
  });
  assert.equal(invalid.is_error, true);
});

test('prepared action pins original options and stops working when tools are disabled', async () => {
  const f = fixture('admin');
  const input = { slug: 'infra', new_label: 'Changed' };
  const result = await tool(f, 'team_rename', input);
  input.new_label = 'Injected';
  await result.action.execute();
  assert.deepEqual(
    f.calls.find((c) => c[0] === 'update'),
    ['update', 't1', { label: 'Changed' }],
  );
  f.ctx.helperToolsEnabled = false;
  await assert.rejects(result.action.check(), /disabled|available/i);
});

test('losing access to the source channel blocks a previously prepared action', async () => {
  const f = fixture('admin');
  let allowed = true;
  f.request.checkSurface = async () => {
    if (!allowed) throw new Error('Channel access lost');
  };
  const result = await tool(f, 'team_rename', { slug: 'infra', new_label: 'Changed' });
  allowed = false;
  await assert.rejects(result.action.execute(), /Channel access/);
  assert.equal(
    f.calls.some((c) => c[0] === 'update'),
    false,
  );
});

test('channel access previews identify the trusted guild and parent channel', async () => {
  const f = fixture('admin');
  const result = await tool(f, 'channel_access_clear');
  assert.match(result.action.summary, /222222222222222222/);
  assert.match(result.action.summary, /333333333333333333/);
  assert.match(result.action.summary, /parent/i);
});

test('member previews escape labels while keeping the exact member, team and thread identifiable', async () => {
  const f = fixture('admin');
  const identify = f.ctx.directory.getPersonByDiscordId;
  f.ctx.directory.getPersonByDiscordId = async (id) =>
    id === '123' ? { id: 'target', display_name: '**Sam** @everyone' } : identify(id);
  const result = await tool(f, 'team_remove', { user: '123', team: 'infra' });
  assert.match(result.action.summary, /<@123>/);
  assert.match(result.action.summary, /Infrastructure/);
  assert.match(result.action.summary, /<#444444444444444444>/);
  assert.ok(result.action.summary.includes('\\*\\*Sam\\*\\*'));
  assert.ok(!result.action.summary.includes('@everyone'));
  assert.doesNotMatch(JSON.stringify(result.content), /Sam|Infrastructure/);
});

test('optional preview labels fall back to the member mention and team slug on lookup failure', async () => {
  const f = fixture('admin');
  const identify = f.ctx.directory.getPersonByDiscordId;
  f.ctx.directory.getPersonByDiscordId = async (id) => {
    if (id === '123') throw new Error('Sensitive person detail');
    return identify(id);
  };
  f.ctx.directory.getTeamBySlug = async () => {
    throw new Error('Sensitive team detail');
  };
  const result = await tool(f, 'team_remove', { user: '123', team: 'infra' });
  assert.ok(result.action);
  assert.match(result.action.summary, /<@123>/);
  assert.match(result.action.summary, /infra/);
  assert.doesNotMatch(result.action.summary, /Sensitive/);
  assert.equal(
    f.calls.some((call) => call[0] === 'update'),
    false,
  );
});

test('the turn deadline still rejects a preview whose optional label lookup stalls', async () => {
  const f = fixture('admin');
  let labels = 0;
  f.ctx.directory.getTeamBySlug = async () => {
    labels++;
    return new Promise(() => {});
  };
  await assert.rejects(
    f.ctx.toolRegistry.handlers.get('team_rename')(
      { slug: 'infra', new_label: 'New' },
      {
        request: f.request,
        principal: f.principal,
        withDeadline: async (read) => {
          const pending = read();
          if (labels) throw new Error('Turn deadline');
          return pending;
        },
      },
    ),
    /Turn deadline/,
  );
  assert.equal(labels, 1);
});

test('guild helper help is a shared guide while DM help remains personalized', async () => {
  for (const input of [{}, { command: 'team' }, { command: 'seed' }]) {
    const member = fixture();
    const admin = fixture('admin');
    const memberReply = await (await tool(member, 'help', input)).action.execute();
    const adminReply = await (await tool(admin, 'help', input)).action.execute();
    assert.deepEqual(adminReply.publicReply, memberReply.publicReply);
    assert.doesNotMatch(JSON.stringify(adminReply), /\/seed|\/team create/);
  }
  const admin = fixture('admin');
  delete admin.request.discordGuildId;
  const reply = await (await tool(admin, 'help', { command: 'team' })).action.execute();
  assert.equal(reply.publicReply, undefined);
  assert.match(JSON.stringify(reply), /create/);
});

test('a delayed authentication cannot execute an expired action', async (t) => {
  const f = fixture('admin');
  const result = await tool(f, 'team_rename', { slug: 'infra', new_label: 'Changed' });
  const later = Date.now() + 120001;
  f.ctx.directory.getPersonByDiscordId = async () => {
    t.mock.method(Date, 'now', () => later);
    return f.principal.person;
  };
  await assert.rejects(result.action.execute(), /expired|permissions|authorize/);
  assert.equal(
    f.calls.some((c) => c[0] === 'update'),
    false,
  );
});

test('verification is excluded and the complete tool catalog fits the LLM service limit', () => {
  const f = fixture();
  const names = f.ctx.toolRegistry.definitions.map((t) => t.name);
  assert.ok(names.includes('record_start'));
  assert.ok(names.includes('qna'));
  assert.ok(names.includes('channel_access_append'));
  assert.ok(names.includes('helper_limits_set'));
  assert.ok(names.includes('doc_add'));
  assert.ok(names.includes('seed'));
  assert.ok(names.length <= 32);
  assert.equal(
    names.some((n) => /verify|email|^link$/.test(n)),
    false,
  );
});

for (const name of ['team_add', 'team_remove']) {
  test(`${name} validates the entire member list before any directory lookup`, async () => {
    for (const user of [[], ['101', 'bad-id'], ['101', '101'], Array(26).fill('101')]) {
      const f = fixture('admin');
      const result = await tool(f, name, { user, team: 'infra' });
      assert.equal(result.is_error, true);
      assert.equal(result.action, undefined);
      assert.equal(f.calls.length, 0);
    }
  });

  test(`${name} prepares every member with pinned arguments and keeps labels out of model results`, async () => {
    const f = fixture('admin');
    const user = ['101', '202', '303', '404', '505'];
    const result = await tool(f, name, { user, team: 'infra' });
    assert.ok(result.action);
    user[0] = '999';
    for (const id of ['101', '202', '303', '404', '505'])
      assert.ok(result.action.summary.includes(id));
    assert.ok(!result.action.summary.includes('999'));
    assert.equal(result.action.confirm, true);
    assert.doesNotMatch(JSON.stringify(result.content), /Alex|Infrastructure/);
  });
}

test('a failure inside a bulk command stops subsequent commands and retains earlier receipts', async () => {
  const f = fixture('admin');
  const ended = [];
  f.ctx.directory.listMemberships = async ({ personId }) => [{ id: personId }];
  const identify = f.ctx.directory.getPersonByDiscordId;
  f.ctx.directory.getPersonByDiscordId = async (id) =>
    id === f.request.discordUserId ? identify(id) : { id, display_name: `Member ${id}` };
  f.ctx.directory.endMembership = async (id) => {
    ended.push(id);
    if (id === '202') throw new Error('Uncertain write');
  };
  f.ctx.llmClient.chat = async () => ({
    stop_reason: 'tool_use',
    usage: { output_tokens: 100 },
    content_blocks: [
      {
        type: 'tool_use',
        id: 'a',
        name: 'team_remove',
        input: { user: ['101', '202'], team: 'infra' },
      },
      { type: 'tool_use', id: 'b', name: 'team_remove', input: { user: '303', team: 'infra' } },
    ],
  });
  const { action } = await f.ctx.toolExecutor.run({
    messages: [{ role: 'user', content: 'Remove all three' }],
    request: f.request,
    principal: f.principal,
  });
  const result = await action.execute();
  assert.deepEqual(ended, ['101', '202']);
  assert.match(result.replies[0].content, /Removed.*Member 101/);
  assert.equal(typeof result.replies[0].check, 'function');
  assert.equal(result.replies[1].safeFailure, true);
  assert.match(result.replies[1].content, /Action 2.*remaining actions were not run/);
  assert.equal(result.replies.length, 2);
});
