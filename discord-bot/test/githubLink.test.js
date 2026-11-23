import assert from 'node:assert/strict';
import test from 'node:test';
import command from '../src/commands/github.js';
import { createGithubLinkClient } from '../src/clients/githubLinkClient.js';

const principal = { person: { id: 'person-id', active: true } };

test('GitHub command binds the trusted requester and always replies privately', async () => {
  let args;
  const result = await command.handler({
    options: { action: 'connect', person_id: 'attacker' },
    principal,
    discordUserId: '123',
    ctx: {
      githubLinks: {
        async request(...values) {
          args = values;
          return { authorize_url: 'https://github.com/login/oauth/authorize?state=secret' };
        },
      },
    },
  });
  assert.deepEqual(args, ['start', 'person-id', '123']);
  assert.equal(result.ephemeral, true);
  assert.equal(command.auth, 'linked');
});

test('link client rejects a redirected credential-bearing or non-GitHub URL', async () => {
  for (const url of [
    'https://evil.example/link',
    'https://user:secret@github.com/login/oauth/authorize',
  ]) {
    const client = createGithubLinkClient({
      baseUrl: 'https://connector.example',
      apiKey: 'key',
      fetchImpl: async () => ({ ok: true, json: async () => ({ authorize_url: url }) }),
    });
    await assert.rejects(client.request('start', 'person-id', '123'));
  }
});

test('disabled and failed linking do not expose provider errors', async () => {
  assert.equal((await command.handler({ options: {}, principal, ctx: {} })).ephemeral, true);
  const result = await command.handler({
    options: { action: 'status' },
    principal,
    discordUserId: '123',
    ctx: {
      githubLinks: {
        request() {
          throw new Error('SECRET');
        },
      },
    },
  });
  assert.equal(result.ephemeral, true);
  assert.ok(!result.content.includes('SECRET'));
});
