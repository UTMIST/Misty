import { test } from 'node:test';
import assert from 'node:assert/strict';
import bug, { BUG_REPORT_URL } from '../src/commands/bug.js';
import { commands } from '../src/commands/index.js';

test('/bug is public, stable, and registered', () => {
  assert.equal(bug.auth, 'public');
  assert.equal(bug.beta, false);
  assert.equal(commands.get('bug'), bug);
});

test('/bug returns the GitHub issue link and configured infrastructure username', async () => {
  const payload = await bug.handler({
    ctx: { infrastructureDiscordUsername: 'infra-user' },
  });

  assert.equal(payload.ephemeral, true);
  assert.ok(payload.content.includes(BUG_REPORT_URL));
  assert.match(payload.content, /@infra-user/);
});
