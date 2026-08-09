import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createSeedService, llmSafe } from '../src/seedService.js';
import { PersonExists, DirectoryUnavailable } from '../src/directoryClient.js';

const adminCaller = { id: 'a', display_name: 'Admin', access_level: 'admin' };
const memberCaller = { id: 'm', display_name: 'Member', access_level: 'member' };

test('llmSafe is true', () => {
  assert.equal(llmSafe, true);
});

test('SEEDED when directory createPerson succeeds', async () => {
  const svc = createSeedService({
    directory: {
      createPerson: async (args) => {
        assert.equal(args.primaryEmail, 'new@utmist.ca');
        assert.equal(args.accessLevel, 'member');
        return { display_name: 'New', primary_email: 'new@utmist.ca', access_level: 'member' };
      },
    },
  });
  const res = await svc.seedPerson(
    { email: 'new@utmist.ca', displayName: 'New', level: 'member' },
    { caller: adminCaller },
  );
  assert.equal(res.outcome, 'SEEDED');
  assert.equal(res.person.display_name, 'New');
});

test('ESCALATION_DENIED when requested level exceeds caller and no create is attempted', async () => {
  let called = false;
  const svc = createSeedService({
    directory: {
      createPerson: async () => {
        called = true;
        return {};
      },
    },
  });
  const res = await svc.seedPerson(
    { email: 'x@utmist.ca', displayName: 'X', level: 'superuser' },
    { caller: adminCaller },
  );
  assert.equal(res.outcome, 'ESCALATION_DENIED');
  assert.equal(res.callerLevel, 'admin');
  assert.equal(res.requestedLevel, 'superuser');
  assert.equal(called, false);
});

test('admin can grant admin (equal rank allowed)', async () => {
  const svc = createSeedService({
    directory: {
      createPerson: async () => ({
        display_name: 'A',
        primary_email: 'a@utmist.ca',
        access_level: 'admin',
      }),
    },
  });
  const res = await svc.seedPerson(
    { email: 'a@utmist.ca', displayName: 'A', level: 'admin' },
    { caller: adminCaller },
  );
  assert.equal(res.outcome, 'SEEDED');
});

test('member caller cannot grant admin', async () => {
  const svc = createSeedService({
    directory: { createPerson: async () => ({}) },
  });
  const res = await svc.seedPerson(
    { email: 'x@utmist.ca', displayName: 'X', level: 'admin' },
    { caller: memberCaller },
  );
  assert.equal(res.outcome, 'ESCALATION_DENIED');
});

test('EXISTS surfaces directory detail', async () => {
  const svc = createSeedService({
    directory: {
      createPerson: async () => {
        throw new PersonExists('primary_email already exists');
      },
    },
  });
  const res = await svc.seedPerson(
    { email: 'dup@utmist.ca', displayName: 'Dup', level: 'member' },
    { caller: adminCaller },
  );
  assert.equal(res.outcome, 'EXISTS');
  assert.equal(res.detail, 'primary_email already exists');
});

test('DIRECTORY_DOWN when directory is unavailable', async () => {
  const svc = createSeedService({
    directory: {
      createPerson: async () => {
        throw new DirectoryUnavailable('down');
      },
    },
  });
  const res = await svc.seedPerson(
    { email: 'd@utmist.ca', displayName: 'D', level: 'member' },
    { caller: adminCaller },
  );
  assert.equal(res.outcome, 'DIRECTORY_DOWN');
});
