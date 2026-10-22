import { test } from 'node:test';
import assert from 'node:assert/strict';
import { dispatch } from '../src/router.js';
import { commands } from '../src/commands/index.js';
import channelAccess from '../src/commands/channel-access.js';
import { createChannelAccessService } from '../src/services/channelAccessService.js';
import { ChannelTeamsInvalid, DirectoryUnavailable } from '../src/clients/directoryClient.js';
import {
  channelScopeFromInteraction,
  interactionToIntent,
} from '../src/adapters/discord/interactions.js';
import {
  renderAppendResult,
  renderClearResult,
  renderSetResult,
  renderShowResult,
} from '../src/messages/channelAccess.js';
import { buildDiscordData } from '../src/registerCommands.js';

const admin = { id: 'a', display_name: 'A', access_level: 'admin' };
const member = { id: 'm', display_name: 'M', access_level: 'member' };
const ml = { id: 't1', slug: 'ml', label: 'ML', active: true };
const ops = { id: 't2', slug: 'ops', label: 'Ops', active: true };

test('channel-access is registered and admin-gated', () => {
  assert.equal(commands.get('channel-access'), channelAccess);
  assert.equal(channelAccess.auth, 'admin');
  for (const sub of channelAccess.subcommands) assert.equal(sub.auth, 'admin');
  const data = buildDiscordData(channelAccess);
  assert.equal(data.name, 'channel-access');
  assert.equal(data.options.length, 4);
  assert.ok(data.options.some((o) => o.name === 'append'));
});

test('channelScopeFromInteraction uses parent channel in a thread', () => {
  const scope = channelScopeFromInteraction({
    guildId: 'g1',
    channelId: 'thread-9',
    channel: { id: 'thread-9', parentId: 'parent-1', isThread: () => true },
  });
  assert.deepEqual(scope, {
    discordGuildId: 'g1',
    discordChannelId: 'parent-1',
    channelUsedParent: true,
  });
});

test('channelScopeFromInteraction uses the channel itself outside threads', () => {
  const scope = channelScopeFromInteraction({
    guildId: 'g1',
    channelId: 'c1',
    channel: { id: 'c1', isThread: () => false },
  });
  assert.deepEqual(scope, {
    discordGuildId: 'g1',
    discordChannelId: 'c1',
    channelUsedParent: false,
  });
});

test('channelScopeFromInteraction rejects DMs', () => {
  assert.deepEqual(channelScopeFromInteraction({ guildId: null, channelId: 'dm' }), {
    discordGuildId: null,
    discordChannelId: null,
    channelUsedParent: false,
  });
});

test('interactionToIntent carries channel scope', () => {
  const intent = interactionToIntent(
    {
      commandName: 'channel-access',
      guildId: 'g1',
      channelId: 'c1',
      channel: { id: 'c1', isThread: () => false },
      user: { id: '1', username: 'alex' },
      options: { getString: () => null, getSubcommand: () => 'show' },
    },
    channelAccess,
  );
  assert.equal(intent.discordGuildId, 'g1');
  assert.equal(intent.discordChannelId, 'c1');
  assert.equal(intent.subcommand, 'show');
});

test('service show resolves team labels', async () => {
  const directory = {
    getChannelTeams: async (g, c) => {
      assert.equal(g, 'g1');
      assert.equal(c, 'c1');
      return { team_ids: ['t1', 't2'], version: 'v1' };
    },
    getTeam: async (id) => (id === 't1' ? ml : id === 't2' ? ops : null),
  };
  const svc = createChannelAccessService({ directory });
  const result = await svc.show({ guildId: 'g1', channelId: 'c1' });
  assert.equal(result.outcome, 'SHOWN');
  assert.deepEqual(
    result.teams.map((t) => t.slug),
    ['ml', 'ops'],
  );
});

test('service set resolves slugs and replaces', async () => {
  const calls = [];
  const directory = {
    getTeamBySlug: async (slug) => (slug === 'ml' ? ml : slug === 'ops' ? ops : null),
    replaceChannelTeams: async (g, c, ids) => {
      calls.push({ g, c, ids });
      return { team_ids: ids, version: 'v2' };
    },
  };
  const svc = createChannelAccessService({ directory });
  const result = await svc.set({ guildId: 'g1', channelId: 'c1', slugs: ['ML', 'ops', 'ml'] });
  assert.equal(result.outcome, 'SET');
  assert.deepEqual(calls[0].ids, ['t1', 't2']);
  assert.equal(result.teams.length, 2);
});

test('service set reports unknown slug before writing', async () => {
  let wrote = false;
  const directory = {
    getTeamBySlug: async () => null,
    replaceChannelTeams: async () => {
      wrote = true;
    },
  };
  const svc = createChannelAccessService({ directory });
  const result = await svc.set({ guildId: 'g1', channelId: 'c1', slugs: ['nope'] });
  assert.equal(result.outcome, 'UNKNOWN_TEAM');
  assert.equal(result.slug, 'nope');
  assert.equal(wrote, false);
});

test('service append uses the atomic directory append', async () => {
  const calls = [];
  const directory = {
    getTeamBySlug: async (slug) => (slug === 'ops' ? ops : null),
    getTeam: async (id) => (id === 't1' ? ml : id === 't2' ? ops : null),
    getChannelTeams: async () => assert.fail('must not GET-then-PUT'),
    replaceChannelTeams: async () => assert.fail('must not replace'),
    appendChannelTeams: async (g, c, ids) => {
      calls.push({ g, c, ids });
      return { team_ids: ['t1', 't2'], version: 'v2' };
    },
  };
  const svc = createChannelAccessService({ directory });
  const result = await svc.append({ guildId: 'g1', channelId: 'c1', slugs: ['ops'] });
  assert.equal(result.outcome, 'APPENDED');
  assert.deepEqual(calls[0], { g: 'g1', c: 'c1', ids: ['t2'] });
  assert.deepEqual(
    result.teams.map((t) => t.slug),
    ['ml', 'ops'],
  );
});

test('service set maps ChannelTeamsInvalid', async () => {
  const directory = {
    getTeamBySlug: async () => ml,
    replaceChannelTeams: async () => {
      throw new ChannelTeamsInvalid('retired');
    },
  };
  const svc = createChannelAccessService({ directory });
  assert.equal(
    (await svc.set({ guildId: 'g1', channelId: 'c1', slugs: ['ml'] })).outcome,
    'INVALID_TEAMS',
  );
});

test('service clear and directory-down', async () => {
  const ok = createChannelAccessService({
    directory: { clearChannelTeams: async () => undefined },
  });
  assert.equal((await ok.clear({ guildId: 'g1', channelId: 'c1' })).outcome, 'CLEARED');
  const down = createChannelAccessService({
    directory: {
      clearChannelTeams: async () => {
        throw new DirectoryUnavailable('down');
      },
    },
  });
  assert.equal((await down.clear({ guildId: 'g1', channelId: 'c1' })).outcome, 'DIRECTORY_DOWN');
});

test('renderers name teams and keep Drive personal', () => {
  const shown = renderShowResult(
    { outcome: 'SHOWN', teams: [ml] },
    { channelId: 'c1', usedParent: false },
  );
  assert.match(shown.content, /ML/);
  assert.match(shown.content, /Drive access stays personal/i);
  assert.match(shown.content, /<#c1>/);

  const set = renderSetResult(
    { outcome: 'SET', teams: [ml, ops] },
    { channelId: 'parent', usedParent: true },
  );
  assert.match(set.content, /Updated/);
  assert.match(set.content, /parent channel/i);
  assert.match(set.content, /Ops/);

  const appended = renderAppendResult(
    { outcome: 'APPENDED', teams: [ml, ops] },
    { channelId: 'c1' },
  );
  assert.match(appended.content, /Appended/);
  assert.match(appended.content, /Ops/);

  const cleared = renderClearResult({ outcome: 'CLEARED' }, { channelId: 'c1' });
  assert.match(cleared.content, /Cleared/);
});

test('handler set/show/append/clear against a fake service', async () => {
  const calls = [];
  const ctx = {
    channelAccessService: {
      show: async (args) => {
        calls.push(['show', args]);
        return { outcome: 'SHOWN', teams: [] };
      },
      set: async (args) => {
        calls.push(['set', args]);
        return { outcome: 'SET', teams: [ml] };
      },
      append: async (args) => {
        calls.push(['append', args]);
        return { outcome: 'APPENDED', teams: [ml, ops] };
      },
      clear: async (args) => {
        calls.push(['clear', args]);
        return { outcome: 'CLEARED' };
      },
    },
  };
  const scope = {
    discordGuildId: 'g1',
    discordChannelId: 'c1',
    channelUsedParent: false,
  };
  const show = await channelAccess.handler({
    ...scope,
    subcommand: 'show',
    options: {},
    ctx,
    principal: { person: admin },
  });
  assert.match(show.content, /No teams configured/);

  const set = await channelAccess.handler({
    ...scope,
    subcommand: 'set',
    options: { team: 'ml', team2: 'ops' },
    ctx,
    principal: { person: admin },
  });
  assert.match(set.content, /Updated/);
  assert.deepEqual(calls[1][1].slugs, ['ml', 'ops']);

  const append = await channelAccess.handler({
    ...scope,
    subcommand: 'append',
    options: { team: 'ops' },
    ctx,
    principal: { person: admin },
  });
  assert.match(append.content, /Appended/);
  assert.deepEqual(calls[2][1].slugs, ['ops']);

  const clear = await channelAccess.handler({
    ...scope,
    subcommand: 'clear',
    options: {},
    ctx,
    principal: { person: admin },
  });
  assert.match(clear.content, /Cleared/);
});

test('handler refuses DMs', async () => {
  const payload = await channelAccess.handler({
    subcommand: 'show',
    options: {},
    discordGuildId: null,
    discordChannelId: null,
    ctx: { channelAccessService: { show: async () => assert.fail('no call') } },
    principal: { person: admin },
  });
  assert.match(payload.content, /server channel/i);
});

test('dispatch denies a member changing channel access', async () => {
  const directory = {
    async getPersonByDiscordId() {
      return member;
    },
  };
  const out = await dispatch(
    {
      commandName: 'channel-access',
      subcommand: 'set',
      options: { team: 'ml' },
      discordUserId: '1',
      discordGuildId: 'g1',
      discordChannelId: 'c1',
    },
    {
      commands,
      appContext: {
        directory,
        channelAccessService: {
          set: async () => assert.fail('must not write'),
        },
      },
    },
  );
  assert.match(out.content, /permission|allowed/i);
});

test('dispatch lets an admin set channel access', async () => {
  const directory = {
    async getPersonByDiscordId() {
      return admin;
    },
  };
  const out = await dispatch(
    {
      commandName: 'channel-access',
      subcommand: 'set',
      options: { team: 'ml' },
      discordUserId: '1',
      discordGuildId: 'g1',
      discordChannelId: 'c1',
    },
    {
      commands,
      appContext: {
        directory,
        channelAccessService: {
          set: async ({ slugs }) => {
            assert.deepEqual(slugs, ['ml']);
            return { outcome: 'SET', teams: [ml] };
          },
        },
      },
    },
  );
  assert.match(out.content, /Updated/);
  assert.match(out.content, /Drive access stays personal/i);
});
