import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHelperRequestLimiter } from '../src/helperRequestLimiter.js';

test('each Discord user has an independent fixed window, including the exact reset boundary', () => {
  let time = 0;
  const limiter = createHelperRequestLimiter({
    maxRequests: 1,
    windowSeconds: 10,
    now: () => time,
  });
  assert.deepEqual(limiter.tryConsume('a'), { allowed: true });
  time = 1001;
  assert.deepEqual(limiter.tryConsume('a'), { allowed: false, retryAfterSeconds: 9 });
  assert.deepEqual(limiter.tryConsume('b'), { allowed: true });
  time = 9999;
  assert.equal(limiter.tryConsume('a').allowed, false);
  time = 10000;
  assert.equal(limiter.tryConsume('a').allowed, true);
  assert.equal(limiter.tryConsume('b').allowed, false);
  time = 11001;
  assert.equal(limiter.tryConsume('b').allowed, true);
});

test('denials do not extend the window and expired inactive users get a fresh allowance', () => {
  let time = 0;
  const limiter = createHelperRequestLimiter({ maxRequests: 1, windowSeconds: 1, now: () => time });
  limiter.tryConsume('a');
  time = 900;
  assert.equal(limiter.tryConsume('a').allowed, false);
  time = 1000;
  limiter.tryConsume('b');
  assert.equal(limiter.tryConsume('a').allowed, true);
});

test('new limiter instances reset usage and defaults allow ten requests', () => {
  const limiter = createHelperRequestLimiter({ now: () => 0 });
  for (let i = 0; i < 10; i++) assert.equal(limiter.tryConsume('a').allowed, true);
  assert.deepEqual(limiter.tryConsume('a'), { allowed: false, retryAfterSeconds: 3600 });
  assert.equal(createHelperRequestLimiter().tryConsume('a').allowed, true);
});

test('invalid limits and missing identities fail closed', () => {
  for (const value of [0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(
      () => createHelperRequestLimiter({ maxRequests: value }),
      /HELPER_USER_MAX_REQUESTS/,
    );
    assert.throws(
      () => createHelperRequestLimiter({ windowSeconds: value }),
      /HELPER_USER_WINDOW_SECONDS/,
    );
  }
  assert.throws(
    () => createHelperRequestLimiter({ windowSeconds: Number.MAX_SAFE_INTEGER }),
    /HELPER_USER_WINDOW_SECONDS/,
  );
  const limiter = createHelperRequestLimiter();
  for (const id of [undefined, null, '', 123]) {
    assert.throws(() => limiter.tryConsume(id), /Discord user ID/);
  }
});
