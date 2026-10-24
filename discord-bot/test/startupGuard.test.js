import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ensureDevSpoofScope } from '../src/startupGuard.js';

test('ensureDevSpoofScope: passes when dev:spoof present', async () => {
  const ctx = {
    directory: {
      async getSelfKeyScopes() {
        return ['dev:spoof', 'people:read'];
      },
    },
  };
  await assert.doesNotReject(ensureDevSpoofScope(ctx));
});

test('ensureDevSpoofScope: throws when dev:spoof absent', async () => {
  const ctx = {
    directory: {
      async getSelfKeyScopes() {
        return ['people:read', 'people:write'];
      },
    },
  };
  await assert.rejects(ensureDevSpoofScope(ctx), /dev:spoof/);
});

test('ensureDevSpoofScope: admin scope does NOT satisfy the check', async () => {
  const ctx = {
    directory: {
      async getSelfKeyScopes() {
        return ['admin'];
      },
    },
  };
  await assert.rejects(ensureDevSpoofScope(ctx), /dev:spoof/);
});
