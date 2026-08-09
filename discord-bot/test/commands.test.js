import { test } from 'node:test';
import assert from 'node:assert/strict';
import link from '../src/commands/link.js';
import verifyCode from '../src/commands/verify-code.js';
import whoami from '../src/commands/whoami.js';
import seed from '../src/commands/seed.js';
import teamCmd from '../src/commands/team.js';
import myTeamsCmd from '../src/commands/my-teams.js';
import { commands, partitionCommands } from '../src/commands/index.js';
import { DirectoryUnavailable } from '../src/directoryClient.js';
import { buildDiscordData } from '../src/registerCommands.js';
import { defineCommand } from '../src/defineCommand.js';

test('link is public and requests a verification code via linkService', async () => {
  assert.equal(link.auth, 'public');
  const ctx = {
    linkService: {
      linkByEmail: async (args) => {
        assert.equal(args.email, 'alex@utmist.ca');
        assert.equal(args.discordUserId, '123');
        assert.equal(args.discordHandle, 'alex');
        return { outcome: 'CODE_SENT', email: 'alex@utmist.ca' };
      },
    },
  };
  const payload = await link.handler({
    options: { email: 'alex@utmist.ca' },
    principal: null,
    ctx,
    discordUserId: '123',
    discordHandle: 'alex',
  });
  assert.equal(payload.ephemeral, true);
  assert.match(payload.content, /alex@utmist\.ca/);
  assert.match(payload.content, /verify-code/);
});

test('verify-code is public and confirms + links via linkService', async () => {
  assert.equal(verifyCode.auth, 'public');
  const ctx = {
    linkService: {
      confirmAndLink: async (args) => {
        assert.equal(args.discordUserId, '123');
        assert.equal(args.discordHandle, 'alex');
        assert.equal(args.code, '123456');
        return { outcome: 'LINKED', person: { display_name: 'Alex' } };
      },
    },
  };
  const payload = await verifyCode.handler({
    options: { code: '123456' },
    principal: null,
    ctx,
    discordUserId: '123',
    discordHandle: 'alex',
  });
  assert.equal(payload.ephemeral, true);
  assert.match(payload.content, /Alex/);
});

test('whoami is linked-gated and stable', () => {
  assert.equal(whoami.auth, 'linked');
  assert.equal(whoami.beta, false);
});

test('whoami replies with an ephemeral embed carrying person + identifiers', async () => {
  const person = {
    id: 'p1',
    display_name: 'Alex',
    primary_email: 'alex@utmist.ca',
    access_level: 'admin',
    active: true,
  };
  const identifiers = [{ provider: 'discord', external_id: '123', handle: 'alex' }];
  const ctx = {
    directory: {
      listIdentifiers: async (id) => {
        assert.equal(id, 'p1');
        return identifiers;
      },
    },
  };
  const payload = await whoami.handler({ principal: { person }, ctx });
  assert.equal(payload.ephemeral, true);
  assert.equal(payload.embeds.length, 1);
  const embed = payload.embeds[0];
  assert.equal(embed.title, 'Alex');
  const byName = Object.fromEntries(embed.fields.map((f) => [f.name, f.value]));
  assert.equal(byName['Email'], 'alex@utmist.ca');
  assert.equal(byName['Access level'], 'admin');
  assert.equal(byName['Status'], 'Active');
  assert.match(byName['Identities'], /discord: <@123>/);
});

test('whoami degrades gracefully when identifiers fetch fails', async () => {
  const person = {
    id: 'p1',
    display_name: 'Alex',
    primary_email: 'alex@utmist.ca',
    access_level: 'member',
    active: true,
  };
  const ctx = {
    directory: {
      listIdentifiers: async () => {
        throw new DirectoryUnavailable('down');
      },
    },
  };
  const payload = await whoami.handler({ principal: { person }, ctx });
  assert.equal(payload.ephemeral, true);
  const byName = Object.fromEntries(payload.embeds[0].fields.map((f) => [f.name, f.value]));
  assert.equal(byName['Identities'], '_(unavailable)_');
});

test('whoami rethrows non-DirectoryUnavailable errors from listIdentifiers', async () => {
  const person = {
    id: 'p1',
    display_name: 'Alex',
    primary_email: 'alex@utmist.ca',
    access_level: 'member',
    active: true,
  };
  const ctx = {
    directory: {
      listIdentifiers: async () => {
        throw new Error('boom');
      },
    },
  };
  await assert.rejects(() => whoami.handler({ principal: { person }, ctx }), /boom/);
});

test('registry contains both commands keyed by name', () => {
  assert.equal(commands.get('link'), link);
  assert.equal(commands.get('whoami'), whoami);
});

test('partitionCommands splits stable (global) from beta (test-guild only)', () => {
  const a = { name: 'a' }; // no beta flag → stable
  const b = { name: 'b', beta: false }; // explicit stable
  const c = { name: 'c', beta: true }; // beta
  const { stable, beta } = partitionCommands([a, b, c]);
  assert.deepEqual(stable, [a, b]);
  assert.deepEqual(beta, [c]);
});

test('link and whoami are stable (registered globally, not beta-exclusive)', () => {
  const { stable } = partitionCommands([...commands.values()]);
  assert.ok(stable.includes(link));
  assert.ok(stable.includes(whoami));
  assert.equal(link.beta, false);
  assert.equal(whoami.beta, false);
});

test('seed is admin-gated and stable', () => {
  assert.equal(seed.auth, 'admin');
  assert.equal(seed.beta, false);
});

test('seed adapter passes options + caller into seedService and renders outcome', async () => {
  const ctx = {
    seedService: {
      seedPerson: async (args, opts) => {
        assert.deepEqual(args, {
          email: 'new@utmist.ca',
          displayName: 'New Person',
          level: 'admin',
        });
        assert.equal(opts.caller.access_level, 'admin');
        return {
          outcome: 'SEEDED',
          person: {
            display_name: 'New Person',
            primary_email: 'new@utmist.ca',
            access_level: 'admin',
          },
        };
      },
    },
  };
  const payload = await seed.handler({
    options: { email: 'new@utmist.ca', name: 'New Person', level: 'admin' },
    principal: { person: { access_level: 'admin' } },
    ctx,
  });
  assert.equal(payload.ephemeral, true);
  assert.match(payload.content, /New Person/);
});

test('seed adapter defaults level to member when option missing', async () => {
  const ctx = {
    seedService: {
      seedPerson: async (args) => {
        assert.equal(args.level, 'member');
        return {
          outcome: 'SEEDED',
          person: { display_name: 'A', primary_email: 'a@utmist.ca', access_level: 'member' },
        };
      },
    },
  };
  const payload = await seed.handler({
    options: { email: 'a@utmist.ca', name: 'A', level: null },
    principal: { person: { access_level: 'admin' } },
    ctx,
  });
  assert.match(payload.content, /A/);
});

test('registry includes seed', () => {
  assert.equal(commands.get('seed'), seed);
});

test('/team is stable and its subcommands carry per-subcommand auth', () => {
  assert.equal(teamCmd.beta, false);
  const authFor = (name) => teamCmd.subcommands.find((s) => s.name === name).auth;
  assert.equal(authFor('create'), 'admin');
  assert.equal(authFor('add'), 'admin');
  assert.equal(authFor('remove'), 'admin');
  assert.equal(authFor('rename'), 'admin');
  assert.equal(authFor('list'), 'linked');
  assert.equal(authFor('roster'), 'linked');
});

test('/team create dispatches teamService.createTeam with options and caller', async () => {
  let seen = null;
  const ctx = {
    teamService: {
      createTeam: async (args, opts) => {
        seen = { args, opts };
        return { outcome: 'CREATED', team: { slug: 'ml', label: 'ML' } };
      },
    },
  };
  const payload = await teamCmd.handler({
    subcommand: 'create',
    options: { slug: 'ml', label: 'ML', description: 'Machine Learning' },
    principal: { person: { access_level: 'admin' } },
    ctx,
  });
  assert.deepEqual(seen.args, { slug: 'ml', label: 'ML', description: 'Machine Learning' });
  assert.equal(seen.opts.caller.access_level, 'admin');
  assert.match(payload.content, /ML|ml/);
  assert.equal(payload.ephemeral, true);
});

test('/team list defaults active_only to true and calls teamService.listTeams', async () => {
  let seen = null;
  const ctx = {
    teamService: {
      listTeams: async (args, opts) => {
        seen = { args, opts };
        return { outcome: 'LISTED', teams: [] };
      },
    },
  };
  const payload = await teamCmd.handler({
    subcommand: 'list',
    options: { active_only: null },
    principal: { person: { access_level: 'member' } },
    ctx,
  });
  assert.equal(seen.args.activeOnly, true);
  assert.match(payload.content, /no teams/i);
});

test('/team list forwards active_only=false when explicitly provided', async () => {
  let seen = null;
  const ctx = {
    teamService: {
      listTeams: async (args) => {
        seen = args;
        return { outcome: 'LISTED', teams: [] };
      },
    },
  };
  await teamCmd.handler({
    subcommand: 'list',
    options: { active_only: false },
    principal: { person: { access_level: 'member' } },
    ctx,
  });
  assert.equal(seen.activeOnly, false);
});

test('/team rename dispatches renameTeam', async () => {
  let seen = null;
  const ctx = {
    teamService: {
      renameTeam: async (args) => {
        seen = args;
        return { outcome: 'RENAMED', team: { slug: 'ml', label: 'New ML' } };
      },
    },
  };
  const payload = await teamCmd.handler({
    subcommand: 'rename',
    options: { slug: 'ml', new_label: 'New ML' },
    principal: { person: { access_level: 'admin' } },
    ctx,
  });
  assert.deepEqual(seen, { slug: 'ml', newLabel: 'New ML' });
  assert.match(payload.content, /New ML/);
});

test('/team add dispatches addMember using the mentioned user snowflake', async () => {
  let seen = null;
  const ctx = {
    teamService: {
      addMember: async (args) => {
        seen = args;
        return {
          outcome: 'ADDED',
          person: { display_name: 'Alex' },
          team: { label: 'ML' },
          membership: { id: 'm1' },
        };
      },
    },
  };
  const payload = await teamCmd.handler({
    subcommand: 'add',
    options: { user: { id: '555' }, team: 'ml', role: 'lead', team_admin: true },
    principal: { person: { access_level: 'admin' } },
    ctx,
  });
  assert.deepEqual(seen, {
    discordSnowflake: '555',
    teamSlug: 'ml',
    roleKindId: 'lead',
    isTeamAdmin: true,
  });
  assert.match(payload.content, /Alex/);
});

test('/team add omits role/team_admin when not provided', async () => {
  let seen = null;
  const ctx = {
    teamService: {
      addMember: async (args) => {
        seen = args;
        return {
          outcome: 'ADDED',
          person: { display_name: 'Alex' },
          team: { label: 'ML' },
          membership: { id: 'm1' },
        };
      },
    },
  };
  await teamCmd.handler({
    subcommand: 'add',
    options: { user: { id: '555' }, team: 'ml', role: null, team_admin: null },
    principal: { person: { access_level: 'admin' } },
    ctx,
  });
  assert.equal(seen.roleKindId, undefined);
  assert.equal(seen.isTeamAdmin, undefined);
});

test('/team remove dispatches removeMember using the mentioned user snowflake', async () => {
  let seen = null;
  const ctx = {
    teamService: {
      removeMember: async (args) => {
        seen = args;
        return { outcome: 'REMOVED', person: { display_name: 'Alex' }, team: { label: 'ML' } };
      },
    },
  };
  const payload = await teamCmd.handler({
    subcommand: 'remove',
    options: { user: { id: '555' }, team: 'ml' },
    principal: { person: { access_level: 'admin' } },
    ctx,
  });
  assert.deepEqual(seen, { discordSnowflake: '555', teamSlug: 'ml' });
  assert.match(payload.content, /Alex/);
});

test('/team roster dispatches getRoster', async () => {
  let seen = null;
  const ctx = {
    teamService: {
      getRoster: async (args) => {
        seen = args;
        return {
          outcome: 'ROSTER',
          team: { slug: 'ml', label: 'ML' },
          members: [
            { person: { display_name: 'Alex' }, role_kind_id: 'member', is_team_admin: false },
          ],
        };
      },
    },
  };
  const payload = await teamCmd.handler({
    subcommand: 'roster',
    options: { team: 'ml', as_of: null },
    principal: { person: { access_level: 'member' } },
    ctx,
  });
  assert.equal(seen.teamSlug, 'ml');
  assert.match(payload.content, /Alex/);
});

test('/team roster forwards as_of when provided', async () => {
  let seen = null;
  const ctx = {
    teamService: {
      getRoster: async (args) => {
        seen = args;
        return { outcome: 'ROSTER', team: { slug: 'ml', label: 'ML' }, members: [] };
      },
    },
  };
  await teamCmd.handler({
    subcommand: 'roster',
    options: { team: 'ml', as_of: '2026-01-01' },
    principal: { person: { access_level: 'member' } },
    ctx,
  });
  assert.equal(seen.asOf, '2026-01-01');
});

test('/team handler falls back to a generic reply for an unknown subcommand', async () => {
  const payload = await teamCmd.handler({
    subcommand: 'nonsense',
    options: {},
    principal: null,
    ctx: {},
  });
  assert.equal(payload.ephemeral, true);
  assert.match(payload.content, /went wrong/i);
});

test('/my-teams is linked-gated, stable, and dispatches getMyTeams with caller person id', async () => {
  assert.equal(myTeamsCmd.auth, 'linked');
  assert.equal(myTeamsCmd.beta, false);
  let seen = null;
  const ctx = {
    teamService: {
      getMyTeams: async (args) => {
        seen = args;
        return { outcome: 'MY_TEAMS', memberships: [] };
      },
    },
  };
  const payload = await myTeamsCmd.handler({
    principal: { person: { id: 'p1', display_name: 'Alex', access_level: 'member' } },
    ctx,
  });
  assert.equal(seen.personId, 'p1');
  assert.match(payload.content, /not on any/i);
});

test('registry includes team and my-teams', () => {
  assert.equal(commands.get('team'), teamCmd);
  assert.equal(commands.get('my-teams'), myTeamsCmd);
});

test('every command is stable (registered globally); none are beta', () => {
  const { stable, beta } = partitionCommands([...commands.values()]);
  assert.ok(stable.some((c) => c.name === 'doc'));
  // `record` was promoted off the beta channel in the staging → main release, so
  // the beta partition is now empty. registerCommands still sends the (empty)
  // beta body to the testing guild, which clears the stale guild-scoped copy.
  assert.deepEqual(
    beta.map((c) => c.name),
    [],
  );
  assert.equal(stable.length, commands.size);
});

test('buildDiscordData marks autocomplete string options', () => {
  const cmd = defineCommand({
    name: 'doc',
    description: 'd',
    handler: async () => ({ content: 'x' }),
    subcommands: [
      {
        name: 'list',
        description: 'l',
        handler: async () => ({ content: 'y' }),
        options: [{ name: 'team', type: 'string', description: 't', autocomplete: async () => [] }],
      },
    ],
  });
  const json = buildDiscordData(cmd);
  const teamOpt = json.options[0].options.find((o) => o.name === 'team');
  assert.equal(teamOpt.autocomplete, true);
});
