import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createAppContext } from '../src/context.js';
import { createTeamService } from '../src/services/teamService.js';
import { validatePersonReference, resolvePersonTarget } from '../src/services/personResolver.js';
import { DirectoryUnavailable } from '../src/clients/directoryClient.js';

function fixture() {
  const ctx = createAppContext({ helperToolsEnabled: true });
  const actor = { id: 'actor', display_name: 'Requester', active: true, access_level: 'admin' };
  const alex = {
    id: 'alex-id',
    display_name: 'Alex Chen',
    primary_email: 'alex@example.org',
    active: true,
  };
  const sam = {
    id: 'sam-id',
    display_name: 'Sam Lee',
    primary_email: 'sam@example.org',
    active: true,
  };
  const people = new Map([
    [alex.id, alex],
    [sam.id, sam],
  ]);
  const mentions = new Map([
    ['222', alex.id],
    ['333', sam.id],
  ]);
  const aliases = new Map([['alias@example.org', alex.id]]);
  let team = { id: 'team-id', slug: 'research', label: 'Research', active: true };
  let currentActor = actor;
  const reads = [],
    effects = [];
  ctx.directory = {
    async getPersonByDiscordId(id) {
      reads.push(['mention', id]);
      return id === '111' ? currentActor : (people.get(mentions.get(id)) ?? null);
    },
    async searchPeople(query) {
      reads.push(['search', query]);
      const matches = [...people.values()].filter(
        (p) =>
          p.active &&
          (!query.name || p.display_name.toLowerCase().includes(query.name)) &&
          (!query.email || p.primary_email === query.email || aliases.get(query.email) === p.id),
      );
      return {
        matches: matches.slice(0, 5),
        has_more: matches.length > 5,
        needs_disambiguation: matches.length > 1,
      };
    },
    async getPerson(id) {
      reads.push(['person', id]);
      return people.get(id) ?? null;
    },
    async getTeamBySlug(slug) {
      reads.push(['team-slug', slug]);
      return team?.slug === slug ? team : null;
    },
    async getTeam(id) {
      reads.push(['team', id]);
      return team?.id === id ? team : null;
    },
    async listMemberships({ personId }) {
      return [{ id: personId }];
    },
    async createMembership(args) {
      effects.push(args);
      return args;
    },
    async endMembership(id) {
      effects.push(id);
    },
  };
  ctx.teamService = createTeamService({ directory: ctx.directory });
  const request = { discordUserId: '111', discordGuildId: 'guild', discordChannelId: 'channel' };
  const principal = { person: actor };
  return {
    ctx,
    actor,
    alex,
    sam,
    people,
    mentions,
    aliases,
    reads,
    effects,
    request,
    principal,
    setTeam: (value) => {
      team = value;
    },
    setActor: (value) => {
      currentActor = value;
    },
    prepare: (user, name = 'team_remove') =>
      ctx.toolRegistry.handlers.get(name)({ user, team: 'research' }, { request, principal }),
  };
}

test('person references normalize names/emails and reject forged selections without I/O', () => {
  assert.deepEqual(validatePersonReference({ name: ' ALEX Chen ', email: ' ALIAS@EXAMPLE.ORG ' }), {
    name: 'alex chen',
    email: 'alias@example.org',
  });
  assert.deepEqual(validatePersonReference('222'), { id: '222' });
  for (const value of [
    {},
    null,
    [],
    'Alex',
    { id: '222' },
    { personId: 'alex-id' },
    { name: 'Alex', selected: 'alex-id' },
    { name: ' ' },
    { name: 'a'.repeat(201) },
    { email: 'a'.repeat(321) },
    { email: '\0' },
  ]) {
    assert.throws(() => validatePersonReference(value));
  }
});

for (const user of [
  { name: ' Alex Chen ' },
  { email: ' ALIAS@EXAMPLE.ORG ' },
  { name: 'alex', email: 'alex@example.org' },
  '222',
]) {
  test(`resolves ${JSON.stringify(user)} into a private immutable stable target`, async () => {
    const f = fixture();
    const input = structuredClone(user);
    const result = await f.prepare(input);
    assert.ok(result.action);
    assert.equal(result.action.confirm, true);
    assert.equal(result.action.publicRead, false);
    assert.match(
      result.action.summary.replaceAll('\u200b', ''),
      /Alex Chen.*alex@example.org.*alex-id/,
    );
    assert.match(result.action.summary, /Research.*research/);
    assert.doesNotMatch(JSON.stringify(result.content), /Alex|alex@example|alex-id|Research/);
    assert.equal(f.effects.length, 0);
    if (typeof input === 'object') input.name = 'Sam';
    const searches = f.reads.filter(([kind]) => kind === 'search').length;
    // New matching candidates or a changed Discord link cannot retarget this action.
    f.people.set('another', { ...f.alex, id: 'another' });
    f.mentions.set('222', f.sam.id);
    await result.action.execute();
    assert.deepEqual(f.effects, [f.alex.id]);
    assert.equal(f.reads.filter(([kind]) => kind === 'search').length, searches);
    assert.ok(f.reads.some(([kind, id]) => kind === 'person' && id === f.alex.id));
  });
}

test('team add uses stable IDs for an active person with no Discord link', async () => {
  const f = fixture();
  f.mentions.clear();
  f.ctx.directory.listMemberships = async () => [];
  const result = await f.ctx.toolRegistry.handlers.get('team_add')(
    {
      user: { name: 'Alex', email: 'alias@example.org' },
      team: 'research',
      role: 'lead',
      team_admin: true,
    },
    { request: f.request, principal: f.principal },
  );
  assert.ok(result.action);
  assert.doesNotMatch(result.action.summary, /<@/);
  assert.match(result.action.summary, /role:.*lead/);
  await result.action.execute();
  assert.deepEqual(f.effects, [
    { personId: f.alex.id, teamId: 'team-id', roleKindId: 'lead', isTeamAdmin: true },
  ]);
});

for (const user of [
  { name: 'Nobody' },
  { name: 'Alex', email: 'sam@example.org' },
  { email: 'alex@' },
]) {
  test(`no match or conflicting filters withhold ${JSON.stringify(user)}`, async () => {
    const f = fixture();
    const result = await f.prepare(user);
    assert.equal(result.is_error, true);
    assert.equal(result.action, undefined);
    assert.match(result.content.error, /No active directory member/);
    assert.doesNotMatch(JSON.stringify(result.content), /alex-id|sam-id|example.org/);
    assert.equal(f.effects.length, 0);
  });
}

test('duplicate names and truncated matches never select the first person', async () => {
  const f = fixture();
  f.people.set('duplicate', { ...f.alex, id: 'duplicate' });
  for (const truncated of [false, true]) {
    if (truncated)
      f.ctx.directory.searchPeople = async () => ({
        matches: [f.alex],
        has_more: true,
        needs_disambiguation: false,
      });
    const result = await f.prepare({ name: 'Alex' });
    assert.equal(result.action, undefined);
    assert.match(result.content.error, /More detail/);
    assert.match(result.content.error, /DM/);
    assert.doesNotMatch(JSON.stringify(result.content), /alex-id|example.org|Alex Chen/);
  }
  assert.equal(f.effects.length, 0);
});

test('inactive mention targets and search outages fail without exposing details', async () => {
  const f = fixture();
  f.people.set(f.alex.id, { ...f.alex, active: false });
  assert.match((await f.prepare('222')).content.error, /inactive/);
  f.ctx.directory.searchPeople = async () => {
    throw new DirectoryUnavailable('private@example.org');
  };
  const result = await f.prepare({ name: 'Alex' });
  assert.equal(result.action, undefined);
  assert.doesNotMatch(JSON.stringify(result), /private@example/);
});

test('bulk resolution includes mixed references and rejects aliases selecting the same person', async () => {
  const f = fixture();
  const result = await f.prepare([{ name: 'Alex' }, '333']);
  assert.equal(result.action.actions.length, 2);
  await result.action.execute();
  assert.deepEqual(f.effects, [f.alex.id, f.sam.id]);
  for (const user of [
    ['222', { email: 'alias@example.org' }],
    [{ name: 'Alex' }, { name: 'Nobody' }],
  ]) {
    const result = await f.prepare(user);
    assert.equal(result.is_error, true);
    assert.equal(result.action, undefined);
  }
  assert.equal(f.effects.length, 2);
});

test('all bulk input is validated before requester or target lookup', async () => {
  const f = fixture();
  for (const user of [[{ name: 'Alex' }, { personId: 'sam-id' }], Array(26).fill('222')]) {
    assert.equal((await f.prepare(user)).action, undefined);
    assert.equal(f.reads.length, 0);
  }
});

for (const stale of ['inactive', 'removed', 'renamed', 'email', 'team-retired', 'team-replaced']) {
  test(`confirmation rejects stale ${stale} state before a write`, async () => {
    const f = fixture();
    const { action } = await f.prepare({ name: 'Alex' });
    if (stale === 'inactive') f.people.set(f.alex.id, { ...f.alex, active: false });
    if (stale === 'removed') f.people.delete(f.alex.id);
    if (stale === 'renamed') f.people.set(f.alex.id, { ...f.alex, display_name: 'Changed' });
    if (stale === 'email')
      f.people.set(f.alex.id, { ...f.alex, primary_email: 'changed@example.org' });
    if (stale === 'team-retired') f.setTeam({ id: 'team-id', active: false });
    if (stale === 'team-replaced')
      f.setTeam({ id: 'replacement', slug: 'research', label: 'Research' });
    await assert.rejects(action.execute());
    assert.equal(f.effects.length, 0);
  });
}

test('bulk preflight withholds every write if the final target becomes ineligible', async () => {
  const f = fixture();
  const { action } = await f.prepare(['222', '333']);
  f.people.set(f.sam.id, { ...f.sam, active: false });
  await assert.rejects(action.execute());
  assert.equal(f.effects.length, 0);
});

test('a target cannot authenticate the requester or bypass current admin authorization', async () => {
  const f = fixture();
  f.setActor({ ...f.actor, access_level: 'member' });
  assert.equal((await f.prepare({ name: 'Alex' })).action, undefined);
  assert.equal(
    f.reads.some(([kind]) => kind === 'search'),
    false,
  );
  f.setActor(f.actor);
  const { action } = await f.prepare({ name: 'Alex' });
  f.setActor({ ...f.actor, id: f.alex.id });
  await assert.rejects(action.execute());
  assert.equal(f.effects.length, 0);
});

test('executor rejects duplicate stable targets across distinct tool calls without model replay', async () => {
  const f = fixture();
  let modelCalls = 0;
  f.ctx.llmClient.chat = async () => {
    assert.equal(++modelCalls, 1);
    return {
      stop_reason: 'tool_use',
      usage: { output_tokens: 100 },
      content_blocks: [
        {
          type: 'tool_use',
          id: '1',
          name: 'team_remove',
          input: { user: '222', team: 'research' },
        },
        {
          type: 'tool_use',
          id: '2',
          name: 'team_remove',
          input: { user: { email: 'alias@example.org' }, team: 'research' },
        },
      ],
    };
  };
  const result = await f.ctx.toolExecutor.run({
    messages: [{ role: 'user', content: 'Remove both' }],
    request: f.request,
    principal: f.principal,
  });
  assert.equal(result.outcome, 'incomplete_action');
  assert.equal(result.action, undefined);
  assert.equal(f.effects.length, 0);
});

test('team services reuse the resolver for name/email and recheck pinned person eligibility', async () => {
  const f = fixture();
  const outcome = await f.ctx.teamService.removeMember({
    reference: { name: 'Alex', email: 'alias@example.org' },
    teamSlug: 'research',
  });
  assert.equal(outcome.outcome, 'REMOVED');
  f.people.set(f.alex.id, { ...f.alex, active: false });
  assert.equal(
    (await f.ctx.teamService.removeMember({ personId: f.alex.id, teamId: 'team-id' })).outcome,
    'PERSON_INACTIVE',
  );
  await assert.rejects(
    resolvePersonTarget(f.ctx.directory, { reference: { name: 'Nobody' } }),
    /No active/,
  );
});
