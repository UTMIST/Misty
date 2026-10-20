import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  answerHelperTurns,
  EMPTY_ANSWER,
  COULDNT_COMPLETE,
  LLM_UNAVAILABLE,
} from '../src/helperFlow.js';
import { OUTCOME } from '../src/toolExecutor.js';

const principal = { person: { id: 'person-1', display_name: 'Pat' } };
const turns = [{ role: 'user', text: 'hi', authorId: 'u1', authorName: 'Pat' }];

function appContext({ enabled = false, answer, run } = {}) {
  return {
    helperToolsEnabled: enabled,
    toolExecutor: run ? { run } : undefined,
    helperService: {
      answer: answer ?? (async () => ({ content: 'text-only answer' })),
      prepare: async () => ({ messages: [{ role: 'user', content: 'hi' }], system: 's' }),
    },
  };
}

test('text-only path when tools are disabled (backward compatible, no destination)', async () => {
  const ctx = appContext({ enabled: false });
  const out = await answerHelperTurns(ctx, turns, principal);
  assert.equal(out, 'text-only answer');
});

test('text-only path when enabled but channel context is incomplete', async () => {
  let ran = false;
  const ctx = appContext({
    enabled: true,
    run: async () => {
      ran = true;
      return { outcome: OUTCOME.ANSWER, content: 'x' };
    },
  });
  const out = await answerHelperTurns(ctx, turns, principal, { kind: 'channel' }); // no ids
  assert.equal(ran, false);
  assert.equal(out, 'text-only answer');
});

test('person destination runs the tool loop with an onBehalfOf context', async () => {
  let seenContext;
  const ctx = appContext({
    enabled: true,
    run: async ({ context }) => {
      seenContext = context;
      return { outcome: OUTCOME.ANSWER, content: 'tool answer' };
    },
  });
  const out = await answerHelperTurns(ctx, turns, principal, { kind: 'person' });
  assert.equal(out, 'tool answer');
  assert.deepEqual(seenContext, { onBehalfOf: 'person-1' });
});

test('channel destination runs the tool loop with a guild+channel context', async () => {
  let seenContext;
  const ctx = appContext({
    enabled: true,
    run: async ({ context }) => {
      seenContext = context;
      return { outcome: OUTCOME.ANSWER, content: 'a' };
    },
  });
  await answerHelperTurns(ctx, turns, principal, {
    kind: 'channel',
    guildId: '100',
    channelId: '200',
  });
  assert.deepEqual(seenContext, { guildId: '100', channelId: '200' });
});

test('EXHAUSTED maps to the could-not-complete message', async () => {
  const ctx = appContext({ enabled: true, run: async () => ({ outcome: OUTCOME.EXHAUSTED }) });
  const out = await answerHelperTurns(ctx, turns, principal, { kind: 'person' });
  assert.equal(out, COULDNT_COMPLETE);
});

test('EMPTY and WITHHELD both map to the empty-answer message', async () => {
  for (const outcome of [OUTCOME.EMPTY, OUTCOME.WITHHELD]) {
    const ctx = appContext({ enabled: true, run: async () => ({ outcome }) });
    const out = await answerHelperTurns(ctx, turns, principal, { kind: 'person' });
    assert.equal(out, EMPTY_ANSWER);
  }
});

test('a thrown executor error surfaces as LLM_UNAVAILABLE', async () => {
  const ctx = appContext({
    enabled: true,
    run: async () => {
      throw new Error('kaboom');
    },
  });
  const out = await answerHelperTurns(ctx, turns, principal, { kind: 'person' });
  assert.equal(out, LLM_UNAVAILABLE);
});
