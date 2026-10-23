import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  answerHelperTurns,
  prepareHelperReply,
  EMPTY_ANSWER,
  WITHHELD_ANSWER,
  COULDNT_COMPLETE,
  LLM_UNAVAILABLE,
} from '../src/helperFlow.js';
import { OUTCOME } from '../src/toolExecutor.js';
import { createAppContext } from '../src/context.js';

const principal = { person: { id: 'person-1', display_name: 'Pat' } };
const turns = [{ role: 'user', text: 'hi', authorId: 'u1', authorName: 'Pat' }];

test('production tool wiring withholds destination-only callers before any provider request', async (t) => {
  t.mock.method(globalThis, 'fetch', async () =>
    assert.fail('No provider request without identity'),
  );
  const ctx = createAppContext({ helperToolsEnabled: true });
  ctx.directory.listTeams = async () => [];
  ctx.directory.listMemberships = async () => [];
  let modelCalls = 0;
  ctx.llmClient.chat = async (args) => {
    modelCalls++;
    assert.equal(args.tools, undefined);
    return { content: 'Text-only playground answer' };
  };
  for (const destination of [
    { kind: 'person' },
    { kind: 'channel', guildId: '100', channelId: '200' },
  ]) {
    const result = await prepareHelperReply(ctx, turns, principal, destination);
    assert.equal(result.content, WITHHELD_ANSWER);
    assert.equal(result.action, undefined);
  }
  assert.equal(modelCalls, 0);
  assert.equal(await answerHelperTurns(ctx, turns, principal), 'Text-only playground answer');
  assert.equal(modelCalls, 1);
});

test('the flow passes trusted request metadata and preserves a terminal action for private delivery', async () => {
  const request = { discordUserId: 'real-user', sourceChannelId: 'thread' };
  const action = { userId: 'real-user' };
  let seen;
  const ctx = appContext({
    enabled: true,
    run: async (args) => {
      seen = args.request;
      return { outcome: 'action', action };
    },
  });
  const result = await prepareHelperReply(ctx, turns, principal, { kind: 'person' }, request);
  assert.equal(seen, request);
  assert.equal(result.action, action);
});

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

test('EMPTY and WITHHELD map to distinct safe messages', async () => {
  for (const outcome of [OUTCOME.EMPTY, OUTCOME.WITHHELD]) {
    const ctx = appContext({ enabled: true, run: async () => ({ outcome }) });
    const out = await answerHelperTurns(ctx, turns, principal, { kind: 'person' });
    assert.equal(out, outcome === OUTCOME.WITHHELD ? WITHHELD_ANSWER : EMPTY_ANSWER);
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
