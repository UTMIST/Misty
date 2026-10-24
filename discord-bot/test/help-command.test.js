import { test } from 'node:test';
import assert from 'node:assert/strict';
import { defineCommand } from '../src/defineCommand.js';
import help from '../src/commands/help.js';
import { commands } from '../src/commands/index.js';

function command(name, auth = 'public', description = `${name} description`) {
  return defineCommand({
    name,
    description,
    auth,
    handler: async () => ({ content: 'ok' }),
  });
}

const fakeCommands = new Map([
  ['help', help],
  ['link', command('link')],
  ['profile', command('profile', 'linked')],
  ['seed', command('seed', 'admin')],
  ['root', command('root', 'superuser')],
]);

test('/help is public, stable, and registered', () => {
  assert.equal(help.auth, 'public');
  assert.equal(help.beta, false);
  assert.equal(help.identifyCaller, true);
  assert.equal(commands.get('help'), help);
});

test('/help lists current registry commands, descriptions, and excludes itself', async () => {
  const payload = await help.handler({
    options: {},
    principal: { person: { access_level: 'member' } },
    ctx: { commands: fakeCommands },
  });
  const description = payload.embeds[0].description;
  assert.match(description, /\/link.*link description/);
  assert.match(description, /\/profile.*profile description/);
  assert.doesNotMatch(description, /\/help/);
  assert.doesNotMatch(description, /\/seed/);
  assert.equal(payload.ephemeral, true);
});

test('/help filters commands by caller access level', async () => {
  const anonymous = await help.handler({
    options: {},
    principal: null,
    ctx: { commands: fakeCommands },
  });
  assert.match(anonymous.embeds[0].description, /\/link/);
  assert.doesNotMatch(anonymous.embeds[0].description, /\/profile|\/seed|\/root/);

  const admin = await help.handler({
    options: {},
    principal: { person: { access_level: 'admin' } },
    ctx: { commands: fakeCommands },
  });
  assert.match(admin.embeds[0].description, /\/link|\/profile/);
  assert.match(admin.embeds[0].description, /\/seed/);
  assert.doesNotMatch(admin.embeds[0].description, /\/root/);
});

test('/help command detail shows options and accepts a leading slash', async () => {
  const detailed = defineCommand({
    name: 'search',
    description: 'Search things',
    auth: 'public',
    options: [{ name: 'query', type: 'string', required: true, description: 'Search query' }],
    handler: async () => ({ content: 'ok' }),
  });
  const payload = await help.handler({
    options: { command: '/SEARCH' },
    principal: null,
    ctx: {
      commands: new Map([
        ['help', help],
        ['search', detailed],
      ]),
    },
  });
  assert.equal(payload.embeds[0].title, '/search');
  assert.match(payload.embeds[0].fields[0].value, /query.*required.*Search query/);
});

test('/help command detail filters subcommands by caller access level', async () => {
  const parent = defineCommand({
    name: 'team',
    description: 'Manage teams',
    auth: 'linked',
    subcommands: [
      {
        name: 'list',
        description: 'List teams',
        auth: 'linked',
        handler: async () => ({ content: 'ok' }),
      },
      {
        name: 'create',
        description: 'Create a team',
        auth: 'admin',
        handler: async () => ({ content: 'ok' }),
      },
    ],
    handler: async () => ({ content: 'ok' }),
  });
  const payload = await help.handler({
    options: { command: 'team' },
    principal: { person: { access_level: 'member' } },
    ctx: {
      commands: new Map([
        ['help', help],
        ['team', parent],
      ]),
    },
  });
  const subcommands = payload.embeds[0].fields[0].value;
  assert.match(subcommands, /list/);
  assert.doesNotMatch(subcommands, /create/);
});

test('/help reports unknown and unauthorized command names without leaking them', async () => {
  for (const requested of ['nope', 'seed']) {
    const payload = await help.handler({
      options: { command: requested },
      principal: null,
      ctx: { commands: fakeCommands },
    });
    assert.match(payload.content, /couldn't find/i);
    assert.equal(payload.ephemeral, true);
  }
});
