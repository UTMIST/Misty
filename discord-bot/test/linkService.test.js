import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createLinkService } from '../src/linkService.js';
import { AlreadyLinked, DirectoryUnavailable } from '../src/directoryClient.js';
import {
  VerificationUnavailable,
  RateLimited,
  CodeExpired,
  TooManyAttempts,
  InvalidCode,
  NoPendingCode,
} from '../src/verificationClient.js';

const ARGS = { email: 'alex@utmist.ca', discordUserId: '123', discordHandle: 'alex' };
const CONFIRM_ARGS = { discordUserId: '123', discordHandle: 'alex', code: '123456' };

test('linkByEmail: CODE_SENT when email resolves and code request succeeds', async () => {
  const person = { id: 'p1', display_name: 'Alex' };
  const requested = [];
  const svc = createLinkService({
    directory: { getPersonByEmail: async () => person },
    verification: {
      requestCode: async (args) => {
        requested.push(args);
      },
    },
  });
  const res = await svc.linkByEmail(ARGS);
  assert.equal(res.outcome, 'CODE_SENT');
  assert.equal(res.email, 'alex@utmist.ca');
  assert.deepEqual(requested[0], { subject: 'discord:123', email: 'alex@utmist.ca' });
});

test('linkByEmail: NOT_A_MEMBER when email does not resolve, and no code requested', async () => {
  let requestCalled = false;
  const svc = createLinkService({
    directory: { getPersonByEmail: async () => null },
    verification: {
      requestCode: async () => {
        requestCalled = true;
      },
    },
  });
  const res = await svc.linkByEmail(ARGS);
  assert.equal(res.outcome, 'NOT_A_MEMBER');
  assert.equal(requestCalled, false);
});

test('linkByEmail: DIRECTORY_DOWN when directory is unavailable', async () => {
  const svc = createLinkService({
    directory: {
      getPersonByEmail: async () => {
        throw new DirectoryUnavailable('nope');
      },
    },
    verification: { requestCode: async () => {} },
  });
  const res = await svc.linkByEmail(ARGS);
  assert.equal(res.outcome, 'DIRECTORY_DOWN');
});

test('linkByEmail: VERIFICATION_DOWN when verification service is unavailable', async () => {
  const svc = createLinkService({
    directory: { getPersonByEmail: async () => ({ id: 'p1', display_name: 'Alex' }) },
    verification: {
      requestCode: async () => {
        throw new VerificationUnavailable('down');
      },
    },
  });
  const res = await svc.linkByEmail(ARGS);
  assert.equal(res.outcome, 'VERIFICATION_DOWN');
});

test('linkByEmail: RATE_LIMITED when verification service rate limits', async () => {
  const svc = createLinkService({
    directory: { getPersonByEmail: async () => ({ id: 'p1', display_name: 'Alex' }) },
    verification: {
      requestCode: async () => {
        throw new RateLimited('rate_limited');
      },
    },
  });
  const res = await svc.linkByEmail(ARGS);
  assert.equal(res.outcome, 'RATE_LIMITED');
});

test('confirmAndLink: LINKED when code confirms and link succeeds', async () => {
  const person = { id: 'p1', display_name: 'Alex' };
  const linked = [];
  const svc = createLinkService({
    directory: {
      getPersonByEmail: async (email) => {
        assert.equal(email, 'alex@utmist.ca');
        return person;
      },
      linkDiscord: async (pid, payload) => {
        linked.push({ pid, payload });
        return {};
      },
    },
    verification: {
      confirmCode: async (args) => {
        assert.deepEqual(args, { subject: 'discord:123', code: '123456' });
        return { verified: true, subject: 'discord:123', email: 'alex@utmist.ca' };
      },
    },
  });
  const res = await svc.confirmAndLink(CONFIRM_ARGS);
  assert.equal(res.outcome, 'LINKED');
  assert.equal(res.person, person);
  assert.deepEqual(linked[0], { pid: 'p1', payload: { externalId: '123', handle: 'alex' } });
});

test('confirmAndLink: NOT_A_MEMBER when verified email no longer resolves', async () => {
  let linkCalled = false;
  const svc = createLinkService({
    directory: {
      getPersonByEmail: async () => null,
      linkDiscord: async () => {
        linkCalled = true;
        return {};
      },
    },
    verification: {
      confirmCode: async () => ({
        verified: true,
        subject: 'discord:123',
        email: 'alex@utmist.ca',
      }),
    },
  });
  const res = await svc.confirmAndLink(CONFIRM_ARGS);
  assert.equal(res.outcome, 'NOT_A_MEMBER');
  assert.equal(linkCalled, false);
});

test('confirmAndLink: ALREADY_LINKED surfaces directory detail', async () => {
  const svc = createLinkService({
    directory: {
      getPersonByEmail: async () => ({ id: 'p1', display_name: 'Alex' }),
      linkDiscord: async () => {
        throw new AlreadyLinked('this discord already belongs to another person');
      },
    },
    verification: {
      confirmCode: async () => ({
        verified: true,
        subject: 'discord:123',
        email: 'alex@utmist.ca',
      }),
    },
  });
  const res = await svc.confirmAndLink(CONFIRM_ARGS);
  assert.equal(res.outcome, 'ALREADY_LINKED');
  assert.equal(res.detail, 'this discord already belongs to another person');
});

test('confirmAndLink: CODE_EXPIRED', async () => {
  const svc = createLinkService({
    directory: {},
    verification: {
      confirmCode: async () => {
        throw new CodeExpired('expired');
      },
    },
  });
  const res = await svc.confirmAndLink(CONFIRM_ARGS);
  assert.equal(res.outcome, 'CODE_EXPIRED');
});

test('confirmAndLink: TOO_MANY_ATTEMPTS', async () => {
  const svc = createLinkService({
    directory: {},
    verification: {
      confirmCode: async () => {
        throw new TooManyAttempts('too_many_attempts');
      },
    },
  });
  const res = await svc.confirmAndLink(CONFIRM_ARGS);
  assert.equal(res.outcome, 'TOO_MANY_ATTEMPTS');
});

test('confirmAndLink: INVALID_CODE', async () => {
  const svc = createLinkService({
    directory: {},
    verification: {
      confirmCode: async () => {
        throw new InvalidCode('invalid_code');
      },
    },
  });
  const res = await svc.confirmAndLink(CONFIRM_ARGS);
  assert.equal(res.outcome, 'INVALID_CODE');
});

test('confirmAndLink: NO_PENDING_CODE', async () => {
  const svc = createLinkService({
    directory: {},
    verification: {
      confirmCode: async () => {
        throw new NoPendingCode('no_pending_code');
      },
    },
  });
  const res = await svc.confirmAndLink(CONFIRM_ARGS);
  assert.equal(res.outcome, 'NO_PENDING_CODE');
});

test('confirmAndLink: VERIFICATION_DOWN', async () => {
  const svc = createLinkService({
    directory: {},
    verification: {
      confirmCode: async () => {
        throw new VerificationUnavailable('down');
      },
    },
  });
  const res = await svc.confirmAndLink(CONFIRM_ARGS);
  assert.equal(res.outcome, 'VERIFICATION_DOWN');
});

test('confirmAndLink: DIRECTORY_DOWN when directory unavailable after confirm', async () => {
  const svc = createLinkService({
    directory: {
      getPersonByEmail: async () => {
        throw new DirectoryUnavailable('nope');
      },
    },
    verification: {
      confirmCode: async () => ({
        verified: true,
        subject: 'discord:123',
        email: 'alex@utmist.ca',
      }),
    },
  });
  const res = await svc.confirmAndLink(CONFIRM_ARGS);
  assert.equal(res.outcome, 'DIRECTORY_DOWN');
});
