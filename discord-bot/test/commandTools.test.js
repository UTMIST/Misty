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

test('team lookup prepares a private action, runs as the actual requester, and keeps results out of model input', async () => {
  const f = fixture();
  const result = await tool(f, 'team_list');
  assert.ok(result.action);
  assert.equal(result.action.confirm, false);
  assert.equal(
    f.calls.some((c) => c[0] === 'list'),
    false,
  );
  const reply = await result.action.execute();
  assert.match(reply.content, /Infrastructure/);
  assert.ok(
    f.calls.filter((c) => c[0] === 'identify').every((c) => c[1] === f.request.discordUserId),
  );
  assert.equal(JSON.stringify(result.content).includes('Infrastructure'), false);
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
