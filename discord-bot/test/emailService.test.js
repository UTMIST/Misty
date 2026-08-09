import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createEmailService } from '../src/emailService.js';
import {
  RateLimited,
  CodeExpired,
  InvalidCode,
  TooManyAttempts,
  NoPendingCode,
  VerificationUnavailable,
} from '../src/verificationClient.js';
import { EmailAlreadyRegistered, DirectoryUnavailable } from '../src/directoryClient.js';

const okVerification = {
  requestCode: async () => undefined,
  confirmCode: async () => ({ verified: true, subject: 'email:1', email: 'a@b.com' }),
};

test('requestEmailCode returns CODE_SENT and uses email:<id> subject', async () => {
  let seen;
  const svc = createEmailService({
    directory: {},
    verification: {
      requestCode: async (args) => {
        seen = args;
      },
    },
  });
  const r = await svc.requestEmailCode({ discordUserId: '1', email: 'a@b.com' });
  assert.deepEqual(r, { outcome: 'CODE_SENT', email: 'a@b.com' });
  assert.equal(seen.subject, 'email:1');
});

test('requestEmailCode maps RateLimited', async () => {
  const svc = createEmailService({
    directory: {},
    verification: {
      requestCode: async () => {
        throw new RateLimited('rate_limited');
      },
    },
  });
  assert.deepEqual(await svc.requestEmailCode({ discordUserId: '1', email: 'a@b.com' }), {
    outcome: 'RATE_LIMITED',
  });
});

test('confirmAndAddEmail attaches and returns ADDED', async () => {
  let added;
  const svc = createEmailService({
    directory: {
      addEmailIdentifier: async (pid, email) => {
        added = { pid, email };
        return { id: 'x' };
      },
    },
    verification: okVerification,
  });
  const r = await svc.confirmAndAddEmail({ personId: 'p1', discordUserId: '1', code: '123456' });
  assert.deepEqual(r, { outcome: 'ADDED', email: 'a@b.com' });
  assert.deepEqual(added, { pid: 'p1', email: 'a@b.com' });
});

test('confirmAndAddEmail maps EmailAlreadyRegistered -> EMAIL_TAKEN', async () => {
  const svc = createEmailService({
    directory: {
      addEmailIdentifier: async () => {
        throw new EmailAlreadyRegistered('x');
      },
    },
    verification: okVerification,
  });
  assert.deepEqual(
    await svc.confirmAndAddEmail({ personId: 'p1', discordUserId: '1', code: 'c' }),
    { outcome: 'EMAIL_TAKEN' },
  );
});

test('confirmAndAddEmail maps CodeExpired -> CODE_EXPIRED', async () => {
  const svc = createEmailService({
    directory: {},
    verification: {
      confirmCode: async () => {
        throw new CodeExpired('expired');
      },
    },
  });
  assert.deepEqual(
    await svc.confirmAndAddEmail({ personId: 'p1', discordUserId: '1', code: 'c' }),
    { outcome: 'CODE_EXPIRED' },
  );
});

test('confirmAndAddEmail maps TooManyAttempts -> TOO_MANY_ATTEMPTS', async () => {
  const svc = createEmailService({
    directory: {},
    verification: {
      confirmCode: async () => {
        throw new TooManyAttempts('too_many');
      },
    },
  });
  assert.deepEqual(
    await svc.confirmAndAddEmail({ personId: 'p1', discordUserId: '1', code: 'c' }),
    { outcome: 'TOO_MANY_ATTEMPTS' },
  );
});

test('confirmAndAddEmail maps InvalidCode -> INVALID_CODE', async () => {
  const svc = createEmailService({
    directory: {},
    verification: {
      confirmCode: async () => {
        throw new InvalidCode('invalid');
      },
    },
  });
  assert.deepEqual(
    await svc.confirmAndAddEmail({ personId: 'p1', discordUserId: '1', code: 'c' }),
    { outcome: 'INVALID_CODE' },
  );
});

test('confirmAndAddEmail maps NoPendingCode -> NO_PENDING_CODE', async () => {
  const svc = createEmailService({
    directory: {},
    verification: {
      confirmCode: async () => {
        throw new NoPendingCode('no_pending');
      },
    },
  });
  assert.deepEqual(
    await svc.confirmAndAddEmail({ personId: 'p1', discordUserId: '1', code: 'c' }),
    { outcome: 'NO_PENDING_CODE' },
  );
});

test('confirmAndAddEmail maps VerificationUnavailable -> VERIFICATION_DOWN', async () => {
  const svc = createEmailService({
    directory: {},
    verification: {
      confirmCode: async () => {
        throw new VerificationUnavailable('down');
      },
    },
  });
  assert.deepEqual(
    await svc.confirmAndAddEmail({ personId: 'p1', discordUserId: '1', code: 'c' }),
    { outcome: 'VERIFICATION_DOWN' },
  );
});

test('confirmAndAddEmail maps DirectoryUnavailable -> DIRECTORY_DOWN', async () => {
  const svc = createEmailService({
    directory: {
      addEmailIdentifier: async () => {
        throw new DirectoryUnavailable('down');
      },
    },
    verification: okVerification,
  });
  assert.deepEqual(
    await svc.confirmAndAddEmail({ personId: 'p1', discordUserId: '1', code: 'c' }),
    { outcome: 'DIRECTORY_DOWN' },
  );
});

test('confirmAndAddEmail rethrows unknown errors', async () => {
  const svc = createEmailService({
    directory: {},
    verification: {
      confirmCode: async () => {
        throw new Error('boom');
      },
    },
  });
  await assert.rejects(
    svc.confirmAndAddEmail({ personId: 'p1', discordUserId: '1', code: 'c' }),
    /boom/,
  );
});
