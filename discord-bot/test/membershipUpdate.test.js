import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createAppContext } from '../src/context.js';
import { createTeamService } from '../src/services/teamService.js';
import { createDirectoryClient, DirectoryUnavailable } from '../src/clients/directoryClient.js';

function fixture() {
  const ctx = createAppContext({ helperToolsEnabled: true });
  const actor = { id: 'actor', active: true, access_level: 'admin' };
  const person = { id: 'p', display_name: 'Alex', primary_email: 'alex@example.org', active: true };
  const team = { id: 't', slug: 'research', label: 'Research', active: true };
  let membership = {
    id: 'm',
    person_id: 'p',
    team_id: 't',
    role_kind_id: 'member',
    is_team_admin: true,
    started_at: '2025-01-01',
    ended_at: null,
    updated_at: 'before',
  };
  const writes = [];
  ctx.directory = {
    getPersonByDiscordId: async () => actor,
    getPerson: async () => person,
    getTeam: async () => team,
    getTeamBySlug: async () => team,
    searchPeople: async () => ({ matches: [person], has_more: false, needs_disambiguation: false }),
    listMemberships: async () => (membership ? [membership] : []),
    updateMembership: async (id, changes) => {
      writes.push({ id, changes });
      membership = {
        ...membership,
        updated_at: 'after',
        ...(changes.roleKindId !== undefined && { role_kind_id: changes.roleKindId }),
        ...(changes.isTeamAdmin !== undefined && { is_team_admin: changes.isTeamAdmin }),
      };
      return membership;
    },
    createMembership: async () => assert.fail('must not create a membership'),
    endMembership: async () => assert.fail('must not end a membership'),
  };
  ctx.teamService = createTeamService({ directory: ctx.directory });
  return {
    ctx,
    actor,
    person,
    team,
    writes,
    current: () => membership,
    change: (changes) => {
      membership = { ...membership, ...changes };
    },
    remove: () => {
      membership = null;
    },
    prepare: (options) =>
      ctx.toolRegistry.handlers.get('team_update')(
        { user: { name: 'Alex' }, team: 'research', ...options },
        { principal: { person: actor }, request: { discordUserId: '111' } },
      ),
  };
}

for (const [options, changes] of [
  [{ role: 'lead' }, { roleKindId: 'lead' }],
  [{ team_admin: false }, { isTeamAdmin: false }],
  [
    { role: 'lead', team_admin: false },
    { roleKindId: 'lead', isTeamAdmin: false },
  ],
]) {
  test(`membership update ${JSON.stringify(options)} preserves continuity and allows receipt`, async () => {
    const f = fixture();
    const { action } = await f.prepare(options);
    assert.ok(action.confirm);
    assert.match(action.summary, /Team role: member →/);
    assert.match(action.summary, /Team-admin flag: true →/);
    assert.match(action.summary, /Global access level is unchanged/);
    assert.equal(f.writes.length, 0);
    const reply = await action.execute();
    assert.match(reply.content, /Updated/);
    assert.deepEqual(f.writes, [{ id: 'm', changes }]);
    assert.equal(f.current().started_at, '2025-01-01');
    assert.equal(f.current().ended_at, null);
    await action.check();
  });
}

test('unchanged values do not PATCH and missing or invalid options fail before I/O', async () => {
  const f = fixture();
  const { action } = await f.prepare({ role: 'member', team_admin: true });
  assert.match((await action.execute()).content, /Nothing was changed/);
  assert.equal(f.writes.length, 0);
  for (const options of [{}, { role: 'superuser' }, { team_admin: 'true' }]) {
    assert.equal((await f.prepare(options)).is_error, true);
  }
});

for (const state of [
  { id: 'replacement' },
  { role_kind_id: 'lead' },
  { is_team_admin: false },
  { started_at: '2026-01-01' },
  { ended_at: '2026-11-01' },
  { updated_at: 'changed' },
]) {
  test(`changed membership ${JSON.stringify(state)} is never updated`, async () => {
    const f = fixture();
    const { action } = await f.prepare({ role: 'lead' });
    f.change(state);
    await assert.rejects(action.execute());
    assert.equal(f.writes.length, 0);
  });
}

test('missing, ambiguous, inactive membership targets and authorization loss fail closed', async () => {
  for (const change of [
    (f) => f.remove(),
    (f) => {
      f.actor.access_level = 'member';
    },
    (f) => {
      f.person.active = false;
    },
    (f) => {
      f.team.active = false;
    },
    (f) => {
      f.ctx.directory.listMemberships = async () => [f.current(), f.current()];
    },
  ]) {
    const f = fixture();
    const { action } = await f.prepare({ role: 'lead' });
    change(f);
    await assert.rejects(action.execute());
    assert.equal(f.writes.length, 0);
  }
});

test('backend failure is not retried', async () => {
  const f = fixture();
  let attempts = 0;
  f.ctx.directory.updateMembership = async () => {
    attempts++;
    throw new DirectoryUnavailable('outage');
  };
  const { action } = await f.prepare({ role: 'lead' });
  await assert.rejects(action.execute(), /Helper command failed/);
  assert.equal(attempts, 1);
});

test('directory PATCH sends only requested fields, including explicit false', async () => {
  const client = createDirectoryClient({
    baseUrl: 'http://directory',
    apiKey: 'test',
    fetchImpl: async (url, options) => {
      assert.equal(url, 'http://directory/memberships/m');
      assert.equal(options.method, 'PATCH');
      assert.deepEqual(JSON.parse(options.body), { is_team_admin: false });
      return new Response(JSON.stringify({ id: 'm' }), { status: 200 });
    },
  });
  assert.deepEqual(await client.updateMembership('m', { isTeamAdmin: false }), { id: 'm' });
});

test('future scheduled end date is preserved and global members cannot prepare updates', async () => {
  const f = fixture();
  f.change({ ended_at: '2099-12-31' });
  const { action } = await f.prepare({ role: 'lead' });
  assert.ok(action);
  await action.execute();
  assert.equal(f.current().ended_at, '2099-12-31');
  f.actor.access_level = 'member';
  assert.equal((await f.prepare({ role: 'lead' })).is_error, true);
});

test('real confirmation handler keeps updated receipt private and consumes the action once', async () => {
  const { presentHelperAction, handleHelperActionInteraction } =
    await import('../src/adapters/discord/helperActions.js');
  const f = fixture();
  const { action } = await f.prepare({ role: 'lead' });
  const publicReplies = [];
  await presentHelperAction(
    { guildId: 'guild', author: { id: '111' } },
    async (p) => publicReplies.push(p),
    action,
    f.ctx,
  );
  const id = (messages, label) =>
    messages
      .flatMap((p) => p.components ?? [])
      .flatMap((row) => row.components)
      .find((b) => b.label === label)?.custom_id;
  function button(customId, userId = '111') {
    return {
      customId,
      user: { id: userId },
      replies: [],
      ephemeral: true,
      async deferReply() {
        this.deferred = true;
      },
      async editReply(p) {
        this.replied = true;
        this.replies.push(p);
      },
      async followUp(p) {
        this.replies.push(p);
      },
    };
  }
  assert.doesNotMatch(JSON.stringify(publicReplies), /Alex|lead|Research/);
  const review = button(id(publicReplies, 'Review privately'));
  await handleHelperActionInteraction(review, f.ctx);
  assert.match(JSON.stringify(review.replies), /member → lead/);
  assert.equal(f.writes.length, 0);
  const confirmId = id(review.replies, 'Confirm');
  const intruder = button(confirmId, '222');
  await handleHelperActionInteraction(intruder, f.ctx);
  assert.equal(f.writes.length, 0);
  const confirmation = button(confirmId);
  await Promise.all([
    handleHelperActionInteraction(confirmation, f.ctx),
    handleHelperActionInteraction(button(confirmId), f.ctx),
  ]);
  assert.equal(f.writes.length, 1);
  assert.match(JSON.stringify(confirmation.replies), /Updated the existing team membership/);
});

for (const status of [400, 404, 422, 503]) {
  test(`membership PATCH normalizes HTTP ${status}`, async () => {
    const client = createDirectoryClient({
      baseUrl: 'http://directory',
      apiKey: 'test',
      fetchImpl: async () => new Response('{}', { status }),
    });
    if (status === 404)
      assert.equal(await client.updateMembership('m', { roleKindId: 'lead' }), null);
    else await assert.rejects(client.updateMembership('m', { roleKindId: 'lead' }));
  });
}

test('uncertain PATCH stops later batch actions without retrying', async () => {
  const { combineHelperActions } = await import('../src/helperActions.js');
  const f = fixture();
  let attempts = 0;
  let laterWrites = 0;
  f.ctx.directory.updateMembership = async () => {
    attempts++;
    f.change({ role_kind_id: 'lead', updated_at: 'committed-but-response-lost' });
    throw new DirectoryUnavailable('response lost');
  };
  const { action } = await f.prepare({ role: 'lead' });
  const next = {
    userId: '111',
    summary: 'Later action',
    check: async () => {},
    execute: async () => {
      laterWrites++;
      return {};
    },
  };
  const result = await combineHelperActions([action, next]).execute();
  assert.equal(attempts, 1);
  assert.equal(laterWrites, 0);
  assert.equal(result.replies[0].safeFailure, true);
  assert.match(result.replies[0].content, /Check the current state/);
});

test('slash update reports an uncertain write without inviting a blind retry', async () => {
  const { renderUpdateMemberResult } = await import('../src/messages/team.js');
  const f = fixture();
  f.ctx.directory.updateMembership = async () => {
    throw new DirectoryUnavailable('response lost');
  };
  const result = await f.ctx.teamService.updateMember({
    personId: 'p',
    teamId: 't',
    roleKindId: 'lead',
  });
  assert.equal(result.outcome, 'UPDATE_UNCERTAIN');
  assert.match(renderUpdateMemberResult(result).content, /may have completed/);
});

test('expiration during the final membership read prevents PATCH', async (t) => {
  const f = fixture();
  const { action } = await f.prepare({ role: 'lead' });
  let clock = Date.now();
  t.mock.method(Date, 'now', () => clock);
  action.setExpiresAt(clock + 1000);
  let reads = 0;
  f.ctx.directory.listMemberships = async () => {
    if (++reads === 2) clock += 2000;
    return [f.current()];
  };
  await assert.rejects(action.execute());
  assert.equal(f.writes.length, 0);
});

test('caller revocation during final membership lookup prevents PATCH', async () => {
  const f = fixture();
  const { action } = await f.prepare({ role: 'lead' });
  let reads = 0;
  f.ctx.directory.listMemberships = async () => {
    if (++reads === 2) f.actor.access_level = 'member';
    return [f.current()];
  };
  await assert.rejects(action.execute());
  assert.equal(f.writes.length, 0);
});

test('exhaustive role/flag transitions preserve the update invariants', async () => {
  const roles = ['member', 'lead', 'director', 'executive'];
  let cases = 0;
  for (const initialRole of roles) {
    for (const initialFlag of [false, true]) {
      for (const role of [undefined, ...roles]) {
        for (const teamAdmin of [undefined, false, true]) {
          if (role === undefined && teamAdmin === undefined) continue;
          const f = fixture();
          f.change({ role_kind_id: initialRole, is_team_admin: initialFlag });
          const original = { ...f.current() };
          const { action } = await f.prepare({
            ...(role !== undefined && { role }),
            ...(teamAdmin !== undefined && { team_admin: teamAdmin }),
          });
          assert.ok(action);
          await action.execute();
          const changes = {};
          if (role !== undefined && role !== initialRole) changes.roleKindId = role;
          if (teamAdmin !== undefined && teamAdmin !== initialFlag) changes.isTeamAdmin = teamAdmin;
          assert.deepEqual(
            f.writes,
            Object.keys(changes).length ? [{ id: original.id, changes }] : [],
          );
          assert.equal(f.current().role_kind_id, role ?? initialRole);
          assert.equal(f.current().is_team_admin, teamAdmin ?? initialFlag);
          for (const key of ['id', 'person_id', 'team_id', 'started_at', 'ended_at'])
            assert.equal(f.current()[key], original[key]);
          await action.check();
          cases++;
        }
      }
    }
  }
  assert.equal(cases, 112);
});

for (const accessLevel of ['member', 'admin', 'superuser']) {
  test(`ordinary team update dispatch enforces global ${accessLevel} policy`, async () => {
    const { dispatch } = await import('../src/router.js');
    const { commands } = await import('../src/commands/index.js');
    const f = fixture();
    f.actor.access_level = accessLevel;
    f.ctx.directory.getPersonByDiscordId = async (id) => (id === '111' ? f.actor : f.person);
    const result = await dispatch(
      {
        surface: 'discord',
        commandName: 'team',
        subcommand: 'update',
        discordUserId: '111',
        options: { user: { id: '222' }, team: 'research', role: 'lead', team_admin: null },
      },
      { commands, appContext: f.ctx },
    );
    if (accessLevel === 'member') assert.equal(f.writes.length, 0);
    else {
      assert.match(result.content, /Updated/);
      assert.deepEqual(f.writes, [{ id: 'm', changes: { roleKindId: 'lead' } }]);
    }
    assert.equal(f.actor.access_level, accessLevel);
    assert.equal(f.current().is_team_admin, true);
  });
}

test('a stale final bulk target prevents every update', async () => {
  const { combineHelperActions } = await import('../src/helperActions.js');
  const first = fixture();
  const second = fixture();
  second.person.id = 'p2';
  second.change({ id: 'm2', person_id: 'p2' });
  const a = (await first.prepare({ role: 'lead' })).action;
  const b = (await second.prepare({ role: 'lead' })).action;
  const batch = combineHelperActions([a, b]);
  second.change({ updated_at: 'changed-after-preview' });
  await assert.rejects(batch.execute());
  assert.equal(first.writes.length + second.writes.length, 0);
});
