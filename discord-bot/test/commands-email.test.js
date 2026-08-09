import { test } from 'node:test';
import assert from 'node:assert/strict';
import addEmail from '../src/commands/add-email.js';
import verifyEmail from '../src/commands/verify-email.js';

test('add-email is linked-only and calls requestEmailCode', async () => {
  assert.equal(addEmail.auth, 'linked');
  let seen;
  const ctx = {
    emailService: {
      requestEmailCode: async (a) => {
        seen = a;
        return { outcome: 'CODE_SENT', email: a.email };
      },
    },
  };
  const res = await addEmail.handler({ options: { email: 'a@b.com' }, ctx, discordUserId: '1' });
  assert.deepEqual(seen, { discordUserId: '1', email: 'a@b.com' });
  assert.match(res.content, /a@b.com/);
  assert.equal(res.ephemeral, true);
});

test('verify-email is linked-only and confirms with principal.person.id', async () => {
  assert.equal(verifyEmail.auth, 'linked');
  let seen;
  const ctx = {
    emailService: {
      confirmAndAddEmail: async (a) => {
        seen = a;
        return { outcome: 'ADDED', email: 'a@b.com' };
      },
    },
  };
  const res = await verifyEmail.handler({
    options: { code: '123456' },
    ctx,
    principal: { person: { id: 'p1' } },
    discordUserId: '1',
  });
  assert.deepEqual(seen, { personId: 'p1', discordUserId: '1', code: '123456' });
  assert.equal(res.ephemeral, true);
});
