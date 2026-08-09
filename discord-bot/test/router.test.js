import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import {
  dispatch,
  dispatchAutocomplete,
  PRINCIPAL_AUTOCOMPLETE_TIMEOUT_MS,
} from '../src/router.js';
import { defineCommand } from '../src/defineCommand.js';

const publicCmd = defineCommand({
  name: 'ping',
  description: 'p',
  auth: 'public',
  handler: async () => ({ content: 'pong', ephemeral: true }),
});

const linkedCmd = defineCommand({
  name: 'me',
  description: 'm',
  auth: 'linked',
  handler: async ({ principal }) => ({
    content: `hi ${principal.person.display_name}`,
    ephemeral: true,
  }),
});

test('dispatch: unknown command → null', async () => {
  const out = await dispatch(
    { commandName: 'nope', options: {}, discordUserId: '1' },
    { commands: new Map(), appContext: {} },
  );
  assert.equal(out, null);
});

test('dispatch: public command runs without a principal', async () => {
  const out = await dispatch(
    { commandName: 'ping', options: {}, discordUserId: '1' },
    { commands: new Map([['ping', publicCmd]]), appContext: {} },
  );
  assert.equal(out.content, 'pong');
});

test('dispatch: public command can identify its caller and receives the registry in ctx', async () => {
  const cmd = defineCommand({
    name: 'help',
    description: 'help',
    auth: 'public',
    identifyCaller: true,
    handler: async ({ principal, ctx }) => ({
      content: `${principal.person.display_name}:${ctx.commands.size}`,
      ephemeral: true,
    }),
  });
  const commands = new Map([['help', cmd]]);
  const directory = {
    async getPersonByDiscordId() {
      return { id: 'p1', display_name: 'Alex' };
    },
  };
  const out = await dispatch(
    { commandName: 'help', options: {}, discordUserId: '1' },
    { commands, appContext: { directory } },
  );
  assert.equal(out.content, 'Alex:1');
});

test('dispatch: Discord handler context includes beta commands only in the testing guild', async () => {
  const help = defineCommand({
    name: 'help',
    description: 'help',
    auth: 'public',
    handler: async ({ ctx }) => ({
      content: [...ctx.commands.keys()].sort().join(','),
      ephemeral: true,
    }),
  });
  const doc = defineCommand({
    name: 'doc',
    description: 'doc',
    auth: 'linked',
    beta: true,
    handler: async () => ({ content: 'doc', ephemeral: true }),
  });
  const commands = new Map([
    ['help', help],
    ['doc', doc],
  ]);
  const appContext = { discordGuildId: 'testing' };

  const production = await dispatch(
    { surface: 'discord', discordGuildId: 'production', commandName: 'help' },
    { commands, appContext },
  );
  assert.equal(production.content, 'help');

  const testing = await dispatch(
    { surface: 'discord', discordGuildId: 'testing', commandName: 'help' },
    { commands, appContext },
  );
  assert.equal(testing.content, 'doc,help');

  const web = await dispatch({ surface: 'web', commandName: 'help' }, { commands, appContext });
  assert.equal(web.content, 'doc,help');
});

test('dispatch: optional identification degrades to anonymous when directory is unavailable', async () => {
  const { DirectoryUnavailable } = await import('../src/directoryClient.js');
  const cmd = defineCommand({
    name: 'help',
    description: 'help',
    auth: 'public',
    identifyCaller: true,
    handler: async ({ principal }) => ({
      content: principal ? 'linked' : 'anonymous',
      ephemeral: true,
    }),
  });
  const directory = {
    async getPersonByDiscordId() {
      throw new DirectoryUnavailable('down');
    },
  };
  const out = await dispatch(
    { commandName: 'help', options: {}, discordUserId: '1' },
    { commands: new Map([['help', cmd]]), appContext: { directory } },
  );
  assert.equal(out.content, 'anonymous');
});

test('dispatch: unlinked user hitting a linked command → denied payload', async () => {
  const directory = {
    async getPersonByDiscordId() {
      return null;
    },
  };
  const out = await dispatch(
    { commandName: 'me', options: {}, discordUserId: '1' },
    { commands: new Map([['me', linkedCmd]]), appContext: { directory } },
  );
  assert.ok(out.content.includes('link'));
});

test('dispatch: linked user gets handler payload', async () => {
  const directory = {
    async getPersonByDiscordId() {
      return { id: 'p1', display_name: 'Alex' };
    },
  };
  const out = await dispatch(
    { commandName: 'me', options: {}, discordUserId: '1' },
    { commands: new Map([['me', linkedCmd]]), appContext: { directory } },
  );
  assert.equal(out.content, 'hi Alex');
});

test('dispatch: DirectoryUnavailable → fail-closed payload', async () => {
  const { DirectoryUnavailable } = await import('../src/directoryClient.js');
  const directory = {
    async getPersonByDiscordId() {
      throw new DirectoryUnavailable('down');
    },
  };
  const out = await dispatch(
    { commandName: 'me', options: {}, discordUserId: '1' },
    { commands: new Map([['me', linkedCmd]]), appContext: { directory } },
  );
  assert.ok(out.content.toLowerCase().includes('unavailable'));
});

test('dispatch: subcommand routes to the right handler', async () => {
  const cmd = defineCommand({
    name: 'team',
    description: 't',
    subcommands: [
      { name: 'list', handler: async () => ({ content: 'list', ephemeral: true }) },
      {
        name: 'create',
        auth: 'admin',
        handler: async () => ({ content: 'create', ephemeral: true }),
      },
    ],
    handler: async function (intent) {
      const sub = this.subcommands.find((s) => s.name === intent.subcommand);
      return sub.handler(intent);
    },
  });
  const directory = {
    async getPersonByDiscordId() {
      return { id: 'p1', display_name: 'A' };
    },
  };
  const out = await dispatch(
    { commandName: 'team', options: {}, subcommand: 'list', discordUserId: '1' },
    { commands: new Map([['team', cmd]]), appContext: { directory } },
  );
  assert.equal(out.content, 'list');
});

test('dispatch: unknown/undefined non-DirectoryUnavailable authN error propagates', async () => {
  const directory = {
    async getPersonByDiscordId() {
      throw new Error('boom');
    },
  };
  await assert.rejects(
    () =>
      dispatch(
        { commandName: 'me', options: {}, discordUserId: '1' },
        { commands: new Map([['me', linkedCmd]]), appContext: { directory } },
      ),
    (e) => e instanceof Error && e.message === 'boom',
  );
});

test('dispatch: handler error is caught centrally with a generic reply', async () => {
  const cmd = defineCommand({
    name: 'boom',
    description: 'b',
    auth: 'public',
    handler: async () => {
      throw new Error('boom');
    },
  });
  const out = await dispatch(
    { commandName: 'boom', options: {}, discordUserId: '1' },
    { commands: new Map([['boom', cmd]]), appContext: {} },
  );
  assert.match(out.content, /went wrong/i);
});

test('dispatch: admin subcommand denies a member', async () => {
  const cmd = defineCommand({
    name: 'team',
    description: 't',
    subcommands: [
      {
        name: 'create',
        auth: 'admin',
        handler: async () => ({ content: 'create', ephemeral: true }),
      },
    ],
    handler: async function (intent) {
      const sub = this.subcommands.find((s) => s.name === intent.subcommand);
      return sub.handler(intent);
    },
  });
  const directory = {
    async getPersonByDiscordId() {
      return { id: 'p1', display_name: 'A', access_level: 'member' };
    },
  };
  const out = await dispatch(
    { commandName: 'team', options: {}, subcommand: 'create', discordUserId: '1' },
    { commands: new Map([['team', cmd]]), appContext: { directory } },
  );
  assert.match(out.content, /permission|allowed/i);
});

test('dispatch: auth-as-function is called with the intent and its return is the policy', async () => {
  let seenIntent = null;
  const cmd = defineCommand({
    name: 'team',
    description: 't',
    auth: (intent) => {
      seenIntent = intent;
      return 'public';
    },
    handler: async () => ({ content: 'ran', ephemeral: true }),
  });
  const intent = { commandName: 'team', options: {}, discordUserId: '1' };
  const out = await dispatch(intent, { commands: new Map([['team', cmd]]), appContext: {} });
  assert.equal(seenIntent, intent);
  assert.equal(out.content, 'ran');
});

test('dispatch: auth-as-function returning nullish falls back to linked (fail-secure)', async () => {
  const cmd = defineCommand({
    name: 'team',
    description: 't',
    auth: () => null,
    handler: async () => ({ content: 'ran', ephemeral: true }),
  });
  const directory = {
    async getPersonByDiscordId() {
      return null;
    },
  };
  const out = await dispatch(
    { commandName: 'team', options: {}, discordUserId: '1' },
    { commands: new Map([['team', cmd]]), appContext: { directory } },
  );
  assert.ok(out.content.includes('link'));
});

function cmdWithTeamAutocomplete(resolver) {
  return new Map([
    [
      'doc',
      {
        name: 'doc',
        subcommands: [
          { name: 'list', options: [{ name: 'team', type: 'string', autocomplete: resolver }] },
        ],
        options: [],
      },
    ],
  ]);
}

test('dispatchAutocomplete calls the focused option resolver with principal', async () => {
  const resolver = async ({ typed, principal }) => {
    assert.equal(typed, 'm');
    assert.equal(principal.person.id, 'p1');
    return [{ name: 'ML', value: 'ml' }];
  };
  const commands = cmdWithTeamAutocomplete(resolver);
  const appContext = { directory: { getPersonByDiscordId: async () => ({ id: 'p1' }) } };
  const out = await dispatchAutocomplete(
    {
      commandName: 'doc',
      subcommand: 'list',
      focusedOption: 'team',
      typed: 'm',
      discordUserId: 'u1',
    },
    { commands, appContext },
  );
  assert.deepEqual(out, [{ name: 'ML', value: 'ml' }]);
});

test('dispatchAutocomplete returns [] when resolver throws', async () => {
  const commands = cmdWithTeamAutocomplete(async () => {
    throw new Error('boom');
  });
  const appContext = { directory: { getPersonByDiscordId: async () => ({ id: 'p1' }) } };
  const out = await dispatchAutocomplete(
    {
      commandName: 'doc',
      subcommand: 'list',
      focusedOption: 'team',
      typed: '',
      discordUserId: 'u1',
    },
    { commands, appContext },
  );
  assert.deepEqual(out, []);
});

test('dispatchAutocomplete falls back to a null principal when the lookup exceeds the budget', async () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    let seenPrincipal = 'unset';
    const resolver = async ({ principal }) => {
      seenPrincipal = principal;
      return [];
    };
    const commands = cmdWithTeamAutocomplete(resolver);
    // A hung directory call: never settles. The timeout must let the resolver run anyway.
    const appContext = { directory: { getPersonByDiscordId: () => new Promise(() => {}) } };
    const pending = dispatchAutocomplete(
      {
        commandName: 'doc',
        subcommand: 'list',
        focusedOption: 'team',
        typed: '',
        discordUserId: 'u1',
      },
      { commands, appContext },
    );
    // Tick past the principal budget (whatever it currently is) so the timeout fires.
    mock.timers.tick(PRINCIPAL_AUTOCOMPLETE_TIMEOUT_MS + 100);
    const out = await pending;
    assert.equal(seenPrincipal, null);
    assert.deepEqual(out, []);
  } finally {
    mock.timers.reset();
  }
});

test('dispatchAutocomplete returns [] for unknown option', async () => {
  const commands = cmdWithTeamAutocomplete(async () => [{ name: 'ML', value: 'ml' }]);
  const appContext = { directory: { getPersonByDiscordId: async () => ({ id: 'p1' }) } };
  const out = await dispatchAutocomplete(
    {
      commandName: 'doc',
      subcommand: 'list',
      focusedOption: 'nope',
      typed: '',
      discordUserId: 'u1',
    },
    { commands, appContext },
  );
  assert.deepEqual(out, []);
});

test('dispatchAutocomplete caps at 25', async () => {
  const many = Array.from({ length: 40 }, (_, i) => ({ name: `t${i}`, value: `t${i}` }));
  const commands = cmdWithTeamAutocomplete(async () => many);
  const appContext = { directory: { getPersonByDiscordId: async () => ({ id: 'p1' }) } };
  const out = await dispatchAutocomplete(
    {
      commandName: 'doc',
      subcommand: 'list',
      focusedOption: 'team',
      typed: '',
      discordUserId: 'u1',
    },
    { commands, appContext },
  );
  assert.equal(out.length, 25);
});
