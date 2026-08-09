import { test, describe, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { buildServer } from '../src/web/server.js';
import { defineCommand } from '../src/defineCommand.js';

const linkCmd = defineCommand({
  name: 'link',
  description: 'link',
  auth: 'public',
  options: [{ name: 'email', type: 'string', required: true, description: 'e' }],
  handler: async ({ options, discordUserId }) => ({
    content: `linked ${options.email} as ${discordUserId}`,
    ephemeral: true,
  }),
});

const echoCmd = defineCommand({
  name: 'echo',
  description: 'echo options back',
  auth: 'public',
  options: [
    { name: 'user', type: 'user', required: true, description: 'a user' },
    { name: 'active_only', type: 'boolean', required: false, description: 'flag' },
  ],
  handler: async ({ options }) => ({
    content: JSON.stringify(options),
  }),
});

const commands = new Map([
  [linkCmd.name, linkCmd],
  [echoCmd.name, echoCmd],
]);
const appContext = { directory: {} };

describe('web server', () => {
  let server;
  before(async () => {
    server = await buildServer({ commands, appContext });
    await server.ready();
  });
  after(async () => {
    await server.close();
  });

  test('GET /api/commands returns registry shape', async () => {
    const resp = await server.inject({ method: 'GET', url: '/api/commands' });
    assert.equal(resp.statusCode, 200);
    const body = resp.json();
    assert.equal(body[0].name, 'link');
    assert.equal(body[0].options[0].name, 'email');
    assert.equal(body[0].subcommands.length, 0);
  });

  test('POST /api/commands/:name/run returns the ReplyPayload', async () => {
    const resp = await server.inject({
      method: 'POST',
      url: '/api/commands/link/run',
      payload: { options: { email: 'x@y' }, actingAs: '999' },
    });
    assert.equal(resp.statusCode, 200);
    const body = resp.json();
    assert.ok(body.content.includes('x@y'));
    assert.ok(body.content.includes('999'));
  });

  test('POST /api/commands/:name/run: unknown command → 404', async () => {
    const resp = await server.inject({
      method: 'POST',
      url: '/api/commands/nope/run',
      payload: { options: {}, actingAs: '1' },
    });
    assert.equal(resp.statusCode, 404);
  });

  test('POST /api/commands/:name/run coerces `user` option string into { id }', async () => {
    const resp = await server.inject({
      method: 'POST',
      url: '/api/commands/echo/run',
      payload: {
        options: { user: '123456789012345678', active_only: 'false' },
        actingAs: '999',
      },
    });
    assert.equal(resp.statusCode, 200);
    const body = JSON.parse(resp.json().content);
    assert.deepEqual(body.user, { id: '123456789012345678' });
  });

  test('POST /api/commands/:name/run coerces `boolean` option string "false" into false', async () => {
    const resp = await server.inject({
      method: 'POST',
      url: '/api/commands/echo/run',
      payload: {
        options: { user: '1', active_only: 'false' },
        actingAs: '999',
      },
    });
    assert.equal(resp.statusCode, 200);
    const body = JSON.parse(resp.json().content);
    assert.equal(body.active_only, false);
  });

  test('POST /api/commands/:name/run without actingAs → 400', async () => {
    const resp = await server.inject({
      method: 'POST',
      url: '/api/commands/link/run',
      payload: { options: { email: 'x@y' } },
    });
    assert.equal(resp.statusCode, 400);
    assert.equal(resp.json().error, 'actingAs is required');
  });

  test('POST /api/commands/:name/run with whitespace-only actingAs → 400', async () => {
    const resp = await server.inject({
      method: 'POST',
      url: '/api/commands/link/run',
      payload: { options: { email: 'x@y' }, actingAs: '   ' },
    });
    assert.equal(resp.statusCode, 400);
    assert.equal(resp.json().error, 'actingAs is required');
  });
});

test('GET /api/people returns array with discord_id resolved', async () => {
  const directory = {
    listPeople: async () => [
      {
        id: 'p1',
        display_name: 'Alex',
        primary_email: 'a@x',
        access_level: 'member',
        active: true,
      },
      { id: 'p2', display_name: 'Bea', primary_email: 'b@x', access_level: 'admin', active: true },
    ],
    listIdentifiers: async (personId) => {
      if (personId === 'p1') return [{ provider: 'discord', external_id: '111', handle: 'alex' }];
      return [{ provider: 'github', external_id: 'bea-gh', handle: 'bea' }];
    },
  };
  const commands = new Map();
  const appContext = { directory };
  const server = await buildServer({ commands, appContext });
  await server.ready();
  try {
    const resp = await server.inject({ method: 'GET', url: '/api/people' });
    assert.equal(resp.statusCode, 200);
    const body = resp.json();
    assert.deepEqual(body, [
      { id: 'p1', discord_id: '111', display_name: 'Alex' },
      { id: 'p2', discord_id: null, display_name: 'Bea' },
    ]);
  } finally {
    await server.close();
  }
});

test('GET /api/people tolerates a per-person listIdentifiers failure', async () => {
  const directory = {
    listPeople: async () => [
      {
        id: 'p1',
        display_name: 'Alex',
        primary_email: 'a@x',
        access_level: 'member',
        active: true,
      },
      { id: 'p2', display_name: 'Bea', primary_email: 'b@x', access_level: 'admin', active: true },
    ],
    listIdentifiers: async (personId) => {
      if (personId === 'p1') throw new Error('boom');
      return [{ provider: 'discord', external_id: '222', handle: 'bea' }];
    },
  };
  const commands = new Map();
  const appContext = { directory };
  const server = await buildServer({ commands, appContext });
  await server.ready();
  try {
    const resp = await server.inject({ method: 'GET', url: '/api/people' });
    assert.equal(resp.statusCode, 200);
    const body = resp.json();
    assert.deepEqual(body, [
      { id: 'p1', discord_id: null, display_name: 'Alex' },
      { id: 'p2', discord_id: '222', display_name: 'Bea' },
    ]);
  } finally {
    await server.close();
  }
});

test('POST /api/reset with onReset callback returns { ok: true }', async () => {
  let called = 0;
  const server = await buildServer({
    commands: new Map(),
    appContext: {},
    onReset: async () => {
      called++;
    },
  });
  await server.ready();
  try {
    const resp = await server.inject({ method: 'POST', url: '/api/reset' });
    assert.equal(resp.statusCode, 200);
    assert.deepEqual(resp.json(), { ok: true });
    assert.equal(called, 1);
  } finally {
    await server.close();
  }
});

test('POST /api/reset without onReset returns 501', async () => {
  const server = await buildServer({ commands: new Map(), appContext: {} });
  await server.ready();
  try {
    const resp = await server.inject({ method: 'POST', url: '/api/reset' });
    assert.equal(resp.statusCode, 501);
    assert.equal(resp.json().error, 'reset not available');
  } finally {
    await server.close();
  }
});

test('POST /api/reset propagates errors from onReset as 500', async () => {
  const server = await buildServer({
    commands: new Map(),
    appContext: {},
    onReset: async () => {
      throw new Error('pg_dump failed');
    },
  });
  await server.ready();
  try {
    const resp = await server.inject({ method: 'POST', url: '/api/reset' });
    assert.equal(resp.statusCode, 500);
    assert.ok(String(resp.json().content).includes('pg_dump failed'));
  } finally {
    await server.close();
  }
});
