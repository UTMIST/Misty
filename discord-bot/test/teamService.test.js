import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createTeamService, llmSafe } from '../src/teamService.js';
import { TeamExists, MembershipInvalid, DirectoryUnavailable } from '../src/directoryClient.js';

const admin = { id: 'a', display_name: 'A', access_level: 'admin' };

test('llmSafe is true', () => {
  assert.equal(llmSafe, true);
});

test('createTeam returns CREATED on success', async () => {
  const svc = createTeamService({
    directory: {
      createTeam: async (args) => {
        assert.equal(args.slug, 'ml');
        assert.equal(args.label, 'ML');
        assert.equal(args.description, 'Machine Learning');
        return { id: 't1', slug: 'ml', label: 'ML' };
      },
    },
  });
  const res = await svc.createTeam(
    { slug: 'ml', label: 'ML', description: 'Machine Learning' },
    { caller: admin },
  );
  assert.equal(res.outcome, 'CREATED');
  assert.equal(res.team.id, 't1');
});

test('createTeam returns SLUG_EXISTS on TeamExists', async () => {
  const svc = createTeamService({
    directory: {
      createTeam: async () => {
        throw new TeamExists('slug already exists');
      },
    },
  });
  const res = await svc.createTeam({ slug: 'ml', label: 'ML' }, { caller: admin });
  assert.equal(res.outcome, 'SLUG_EXISTS');
  assert.equal(res.detail, 'slug already exists');
});

test('createTeam returns DIRECTORY_DOWN on DirectoryUnavailable', async () => {
  const svc = createTeamService({
    directory: {
      createTeam: async () => {
        throw new DirectoryUnavailable('down');
      },
    },
  });
  const res = await svc.createTeam({ slug: 'ml', label: 'ML' }, { caller: admin });
  assert.equal(res.outcome, 'DIRECTORY_DOWN');
});

test('listTeams returns LISTED with teams array', async () => {
  const svc = createTeamService({
    directory: {
      listTeams: async ({ activeOnly }) => {
        assert.equal(activeOnly, true);
        return [{ id: 't1', slug: 'ml', label: 'ML' }];
      },
    },
  });
  const res = await svc.listTeams({ activeOnly: true }, { caller: admin });
  assert.equal(res.outcome, 'LISTED');
  assert.equal(res.teams.length, 1);
});

test('listTeams returns DIRECTORY_DOWN when directory is unavailable', async () => {
  const svc = createTeamService({
    directory: {
      listTeams: async () => {
        throw new DirectoryUnavailable('down');
      },
    },
  });
  const res = await svc.listTeams({ activeOnly: true }, { caller: admin });
  assert.equal(res.outcome, 'DIRECTORY_DOWN');
});

test('renameTeam returns RENAMED after slug lookup + updateTeam', async () => {
  const svc = createTeamService({
    directory: {
      getTeamBySlug: async (slug) => {
        assert.equal(slug, 'ml');
        return { id: 't1', slug: 'ml', label: 'ML' };
      },
      updateTeam: async (teamId, patch) => {
        assert.equal(teamId, 't1');
        assert.deepEqual(patch, { label: 'Machine Learning Group' });
        return { id: 't1', slug: 'ml', label: 'Machine Learning Group' };
      },
    },
  });
  const res = await svc.renameTeam(
    { slug: 'ml', newLabel: 'Machine Learning Group' },
    { caller: admin },
  );
  assert.equal(res.outcome, 'RENAMED');
  assert.equal(res.team.label, 'Machine Learning Group');
});

test('renameTeam returns TEAM_NOT_FOUND when slug does not resolve', async () => {
  const svc = createTeamService({
    directory: {
      getTeamBySlug: async () => null,
      updateTeam: async () => {
        throw new Error('should not be called');
      },
    },
  });
  const res = await svc.renameTeam({ slug: 'missing', newLabel: 'X' }, { caller: admin });
  assert.equal(res.outcome, 'TEAM_NOT_FOUND');
});

test('renameTeam returns DIRECTORY_DOWN on outage during lookup', async () => {
  const svc = createTeamService({
    directory: {
      getTeamBySlug: async () => {
        throw new DirectoryUnavailable('down');
      },
      updateTeam: async () => ({}),
    },
  });
  const res = await svc.renameTeam({ slug: 'ml', newLabel: 'X' }, { caller: admin });
  assert.equal(res.outcome, 'DIRECTORY_DOWN');
});

function mkDir(overrides = {}) {
  return {
    getPersonByDiscordId: async () => null,
    getTeamBySlug: async () => null,
    listMemberships: async () => [],
    createMembership: async () => ({ id: 'm1' }),
    endMembership: async () => ({ id: 'm1', ended_at: '2026-07-01' }),
    ...overrides,
  };
}

test('addMember returns USER_NOT_LINKED when snowflake does not resolve', async () => {
  const svc = createTeamService({ directory: mkDir() });
  const res = await svc.addMember({ discordSnowflake: '999', teamSlug: 'ml' }, { caller: admin });
  assert.equal(res.outcome, 'USER_NOT_LINKED');
});

test('addMember returns TEAM_NOT_FOUND when slug does not resolve', async () => {
  const svc = createTeamService({
    directory: mkDir({
      getPersonByDiscordId: async () => ({ id: 'p1', display_name: 'A' }),
      getTeamBySlug: async () => null,
    }),
  });
  const res = await svc.addMember(
    { discordSnowflake: '123', teamSlug: 'missing' },
    { caller: admin },
  );
  assert.equal(res.outcome, 'TEAM_NOT_FOUND');
});

test('addMember returns ALREADY_ON_TEAM when active membership exists', async () => {
  const person = { id: 'p1', display_name: 'A' };
  const team = { id: 't1', slug: 'ml', label: 'ML' };
  let created = false;
  const svc = createTeamService({
    directory: mkDir({
      getPersonByDiscordId: async () => person,
      getTeamBySlug: async () => team,
      listMemberships: async (args) => {
        assert.equal(args.teamId, 't1');
        assert.equal(args.personId, 'p1');
        assert.equal(args.activeOnly, true);
        return [{ id: 'm-existing' }];
      },
      createMembership: async () => {
        created = true;
        return {};
      },
    }),
  });
  const res = await svc.addMember({ discordSnowflake: '123', teamSlug: 'ml' }, { caller: admin });
  assert.equal(res.outcome, 'ALREADY_ON_TEAM');
  assert.equal(res.person, person);
  assert.equal(res.team, team);
  assert.equal(created, false);
});

test('addMember returns ADDED with role and admin flag forwarded', async () => {
  const person = { id: 'p1', display_name: 'A' };
  const team = { id: 't1', slug: 'ml', label: 'ML' };
  const svc = createTeamService({
    directory: mkDir({
      getPersonByDiscordId: async () => person,
      getTeamBySlug: async () => team,
      listMemberships: async () => [],
      createMembership: async (args) => {
        assert.equal(args.personId, 'p1');
        assert.equal(args.teamId, 't1');
        assert.equal(args.roleKindId, 'lead');
        assert.equal(args.isTeamAdmin, true);
        return { id: 'm1', person_id: 'p1', team_id: 't1' };
      },
    }),
  });
  const res = await svc.addMember(
    { discordSnowflake: '123', teamSlug: 'ml', roleKindId: 'lead', isTeamAdmin: true },
    { caller: admin },
  );
  assert.equal(res.outcome, 'ADDED');
  assert.equal(res.membership.id, 'm1');
  assert.equal(res.person, person);
  assert.equal(res.team, team);
});

test('addMember returns DIRECTORY_DOWN on outage during person lookup', async () => {
  const svc = createTeamService({
    directory: mkDir({
      getPersonByDiscordId: async () => {
        throw new DirectoryUnavailable('down');
      },
    }),
  });
  const res = await svc.addMember({ discordSnowflake: '123', teamSlug: 'ml' }, { caller: admin });
  assert.equal(res.outcome, 'DIRECTORY_DOWN');
});

test('addMember maps MembershipInvalid with "already" detail to ALREADY_ON_TEAM (race defense)', async () => {
  const person = { id: 'p1', display_name: 'A' };
  const team = { id: 't1', slug: 'ml', label: 'ML' };
  const svc = createTeamService({
    directory: mkDir({
      getPersonByDiscordId: async () => person,
      getTeamBySlug: async () => team,
      listMemberships: async () => [],
      createMembership: async () => {
        throw new MembershipInvalid('active membership already exists');
      },
    }),
  });
  const res = await svc.addMember({ discordSnowflake: '123', teamSlug: 'ml' }, { caller: admin });
  assert.equal(res.outcome, 'ALREADY_ON_TEAM');
});

test('addMember surfaces other MembershipInvalid detail as MEMBERSHIP_INVALID', async () => {
  const person = { id: 'p1', display_name: 'A' };
  const team = { id: 't1', slug: 'ml', label: 'ML' };
  const svc = createTeamService({
    directory: mkDir({
      getPersonByDiscordId: async () => person,
      getTeamBySlug: async () => team,
      listMemberships: async () => [],
      createMembership: async () => {
        throw new MembershipInvalid('role_kind_id not found: lead');
      },
    }),
  });
  const res = await svc.addMember(
    { discordSnowflake: '123', teamSlug: 'ml', roleKindId: 'lead' },
    { caller: admin },
  );
  assert.equal(res.outcome, 'MEMBERSHIP_INVALID');
  assert.equal(res.detail, 'role_kind_id not found: lead');
  assert.equal(res.person, person);
  assert.equal(res.team, team);
});

test('removeMember returns USER_NOT_LINKED when snowflake does not resolve', async () => {
  const svc = createTeamService({ directory: mkDir() });
  const res = await svc.removeMember(
    { discordSnowflake: '999', teamSlug: 'ml' },
    { caller: admin },
  );
  assert.equal(res.outcome, 'USER_NOT_LINKED');
});

test('removeMember returns TEAM_NOT_FOUND when slug does not resolve', async () => {
  const svc = createTeamService({
    directory: mkDir({
      getPersonByDiscordId: async () => ({ id: 'p1', display_name: 'A' }),
      getTeamBySlug: async () => null,
    }),
  });
  const res = await svc.removeMember(
    { discordSnowflake: '123', teamSlug: 'missing' },
    { caller: admin },
  );
  assert.equal(res.outcome, 'TEAM_NOT_FOUND');
});

test('removeMember returns NOT_ON_TEAM when there is no active membership', async () => {
  const svc = createTeamService({
    directory: mkDir({
      getPersonByDiscordId: async () => ({ id: 'p1', display_name: 'A' }),
      getTeamBySlug: async () => ({ id: 't1', slug: 'ml', label: 'ML' }),
      listMemberships: async () => [],
    }),
  });
  const res = await svc.removeMember(
    { discordSnowflake: '123', teamSlug: 'ml' },
    { caller: admin },
  );
  assert.equal(res.outcome, 'NOT_ON_TEAM');
});

test('removeMember ends the active membership with injected now() and returns REMOVED', async () => {
  const person = { id: 'p1', display_name: 'A' };
  const team = { id: 't1', slug: 'ml', label: 'ML' };
  let endedWith = null;
  const svc = createTeamService({
    directory: mkDir({
      getPersonByDiscordId: async () => person,
      getTeamBySlug: async () => team,
      listMemberships: async () => [{ id: 'm1', person_id: 'p1', team_id: 't1' }],
      endMembership: async (id, endedAt) => {
        endedWith = { id, endedAt };
        return { id, ended_at: endedAt };
      },
    }),
    now: () => '2026-07-01',
  });
  const res = await svc.removeMember(
    { discordSnowflake: '123', teamSlug: 'ml' },
    { caller: admin },
  );
  assert.equal(res.outcome, 'REMOVED');
  assert.equal(res.person, person);
  assert.equal(res.team, team);
  assert.deepEqual(endedWith, { id: 'm1', endedAt: '2026-07-01' });
});

test('removeMember returns DIRECTORY_DOWN on outage during team lookup', async () => {
  const svc = createTeamService({
    directory: mkDir({
      getPersonByDiscordId: async () => ({ id: 'p1', display_name: 'A' }),
      getTeamBySlug: async () => {
        throw new DirectoryUnavailable('down');
      },
    }),
  });
  const res = await svc.removeMember(
    { discordSnowflake: '123', teamSlug: 'ml' },
    { caller: admin },
  );
  assert.equal(res.outcome, 'DIRECTORY_DOWN');
});

test('getRoster returns TEAM_NOT_FOUND when slug does not resolve', async () => {
  const svc = createTeamService({
    directory: mkDir({ getTeamBySlug: async () => null }),
  });
  const res = await svc.getRoster({ teamSlug: 'missing' }, { caller: admin });
  assert.equal(res.outcome, 'TEAM_NOT_FOUND');
});

test('getRoster returns ROSTER with resolved persons using injected now() as as_of', async () => {
  const team = { id: 't1', slug: 'ml', label: 'ML' };
  const memberships = [
    { id: 'm1', person_id: 'p1', team_id: 't1', role_kind_id: 'lead', is_team_admin: true },
    { id: 'm2', person_id: 'p2', team_id: 't1', role_kind_id: 'member', is_team_admin: false },
  ];
  const people = {
    p1: { id: 'p1', display_name: 'One' },
    p2: { id: 'p2', display_name: 'Two' },
  };
  let listedWith = null;
  const svc = createTeamService({
    directory: {
      ...mkDir(),
      getTeamBySlug: async () => team,
      listMemberships: async (args) => {
        listedWith = args;
        return memberships;
      },
      getPerson: async (id) => people[id] ?? null,
    },
    now: () => '2026-07-01',
  });
  const res = await svc.getRoster({ teamSlug: 'ml' }, { caller: admin });
  assert.equal(res.outcome, 'ROSTER');
  assert.equal(res.team, team);
  assert.equal(res.members.length, 2);
  assert.equal(res.members[0].person.display_name, 'One');
  assert.equal(res.members[0].role_kind_id, 'lead');
  assert.equal(res.members[0].is_team_admin, true);
  assert.deepEqual(listedWith, {
    teamId: 't1',
    activeOnly: true,
    asOf: '2026-07-01',
  });
});

test('getRoster uses explicit asOf when provided', async () => {
  let listedWith = null;
  const svc = createTeamService({
    directory: {
      ...mkDir(),
      getTeamBySlug: async () => ({ id: 't1' }),
      listMemberships: async (args) => {
        listedWith = args;
        return [];
      },
    },
    now: () => 'IGNORED',
  });
  await svc.getRoster({ teamSlug: 'ml', asOf: '2026-01-01' }, { caller: admin });
  assert.equal(listedWith.asOf, '2026-01-01');
});

test('getRoster filters out memberships whose person cannot be resolved', async () => {
  const team = { id: 't1', slug: 'ml', label: 'ML' };
  const svc = createTeamService({
    directory: {
      ...mkDir(),
      getTeamBySlug: async () => team,
      listMemberships: async () => [
        { id: 'm1', person_id: 'p1', team_id: 't1', role_kind_id: 'member', is_team_admin: false },
        { id: 'm2', person_id: 'p2', team_id: 't1', role_kind_id: 'member', is_team_admin: false },
      ],
      getPerson: async (id) => (id === 'p1' ? { id: 'p1', display_name: 'One' } : null),
    },
  });
  const res = await svc.getRoster({ teamSlug: 'ml' }, { caller: admin });
  assert.equal(res.members.length, 1);
  assert.equal(res.members[0].person.id, 'p1');
});

test('getRoster returns DIRECTORY_DOWN on outage', async () => {
  const svc = createTeamService({
    directory: {
      ...mkDir(),
      getTeamBySlug: async () => {
        throw new DirectoryUnavailable('down');
      },
    },
  });
  const res = await svc.getRoster({ teamSlug: 'ml' }, { caller: admin });
  assert.equal(res.outcome, 'DIRECTORY_DOWN');
});

test('getMyTeams returns MY_TEAMS with resolved team records', async () => {
  const memberships = [
    { id: 'm1', person_id: 'p1', team_id: 't1', role_kind_id: 'lead', is_team_admin: true },
    { id: 'm2', person_id: 'p1', team_id: 't2', role_kind_id: 'member', is_team_admin: false },
  ];
  const teams = {
    t1: { id: 't1', slug: 'ml', label: 'ML' },
    t2: { id: 't2', slug: 'ops', label: 'Ops' },
  };
  const svc = createTeamService({
    directory: {
      ...mkDir(),
      listMemberships: async (args) => {
        assert.equal(args.personId, 'p1');
        assert.equal(args.activeOnly, true);
        return memberships;
      },
      getTeam: async (id) => teams[id] ?? null,
    },
  });
  const res = await svc.getMyTeams({ personId: 'p1' }, { caller: admin });
  assert.equal(res.outcome, 'MY_TEAMS');
  assert.equal(res.memberships.length, 2);
  assert.equal(res.memberships[0].team.slug, 'ml');
  assert.equal(res.memberships[0].role_kind_id, 'lead');
  assert.equal(res.memberships[0].is_team_admin, true);
});

test('getMyTeams filters out memberships whose team cannot be resolved', async () => {
  const svc = createTeamService({
    directory: {
      ...mkDir(),
      listMemberships: async () => [
        { id: 'm1', person_id: 'p1', team_id: 't1', role_kind_id: 'member', is_team_admin: false },
        { id: 'm2', person_id: 'p1', team_id: 't2', role_kind_id: 'member', is_team_admin: false },
      ],
      getTeam: async (id) => (id === 't1' ? { id: 't1', slug: 'ml', label: 'ML' } : null),
    },
  });
  const res = await svc.getMyTeams({ personId: 'p1' }, { caller: admin });
  assert.equal(res.memberships.length, 1);
  assert.equal(res.memberships[0].team.id, 't1');
});

test('getMyTeams returns DIRECTORY_DOWN on outage', async () => {
  const svc = createTeamService({
    directory: {
      ...mkDir(),
      listMemberships: async () => {
        throw new DirectoryUnavailable('down');
      },
    },
  });
  const res = await svc.getMyTeams({ personId: 'p1' }, { caller: admin });
  assert.equal(res.outcome, 'DIRECTORY_DOWN');
});
