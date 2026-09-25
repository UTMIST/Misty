import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildHealthServer } from '../src/healthServer.js';

test('Discord health follows gateway readiness through connect and disconnect', async () => {
  let ready = false;
  const server = buildHealthServer({ isReady: () => ready });
  try {
    const before = await server.inject({ method: 'GET', url: '/health/ready' });
    assert.equal(before.statusCode, 503);
    assert.deepEqual(before.json(), { status: 'discord bot unavailable' });

    ready = true;
    const connected = await server.inject({ method: 'GET', url: '/health/ready' });
    assert.equal(connected.statusCode, 200);
    assert.deepEqual(connected.json(), { status: 'ok' });

    ready = false;
    const disconnected = await server.inject({ method: 'GET', url: '/health/ready' });
    assert.equal(disconnected.statusCode, 503);
    assert.deepEqual(disconnected.json(), { status: 'discord bot unavailable' });
  } finally {
    await server.close();
  }
});
