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

test('incomplete command proposals explain that nothing changed without leaking or replaying private data', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => assert.fail('Unexpected network request'));
  for (const failure of ['invalid arguments', 'unknown tool', 'authorization failure']) {
    for (const invalidFirst of [false, true]) {
      const ctx = createAppContext({ helperToolsEnabled: true });
      const admin = { person: { id: 'admin', active: true, access_level: 'admin' } };
      ctx.directory = {
        getPersonByDiscordId: async () => admin.person,
        getTeamBySlug: async (slug) => {
          if (slug === 'missing') throw new Error('Sensitive backend detail');
          return { id: 'team', slug, label: 'Private team name' };
        },
      };
      ctx.teamService.removeMember = async () => assert.fail('Incomplete proposal executed');
      ctx.helperService.prepare = async () => ({
        messages: [{ role: 'user', content: 'Remove both members' }],
        system: '',
      });
      let modelCalls = 0;
      ctx.llmClient.chat = async () => {
        assert.equal(++modelCalls, 1, 'private proposals must not be replayed');
        const valid = {
          type: 'tool_use',
          id: 'valid',
          name: 'team_remove',
          input: { user: '101', team: 'infra' },
        };
        const invalid = {
          ...valid,
          id: 'invalid',
          name:
            failure === 'unknown tool'
              ? 'missing'
              : failure === 'authorization failure'
                ? 'seed'
                : 'team_remove',
          input: failure === 'authorization failure' ? {} : { user: '202' },
        };
        return {
          stop_reason: 'tool_use',
          content_blocks: invalidFirst ? [invalid, valid] : [valid, invalid],
        };
      };
      const result = await prepareHelperReply(
        ctx,
        turns,
        admin,
        { kind: 'person' },
        {
          discordUserId: 'owner',
        },
      );
      assert.match(result.content, /couldn't prepare the full request.*nothing was changed/);
      assert.doesNotMatch(result.content, /sources|Private team name|Sensitive backend detail/);
      assert.equal(result.action, undefined);
      assert.equal(modelCalls, 1);
    }
  }
});

for (const failure of ['invalid arguments', 'authorization failure']) {
  test(`an entirely failed command proposal is terminal (${failure})`, async (t) => {
    t.mock.method(globalThis, 'fetch', async () => assert.fail('Unexpected network request'));
    const ctx = createAppContext({ helperToolsEnabled: true });
    const admin = {
      person: {
        id: 'admin',
        active: true,
        access_level: failure === 'authorization failure' ? 'member' : 'admin',
      },
    };
    ctx.directory = {
      getPersonByDiscordId: async () => admin.person,
      getTeamBySlug: async (slug) => {
        if (slug === 'missing') throw new Error('Sensitive backend detail');
        return { id: 'team', slug, label: 'Infrastructure' };
      },
    };
    ctx.helperService.prepare = async () => ({
      messages: [{ role: 'user', content: 'Remove both' }],
    });
    let modelCalls = 0;
    ctx.llmClient.chat = async () => {
      modelCalls++;
      return {
        stop_reason: 'tool_use',
        content_blocks: (modelCalls === 1 ? ['101', '202'] : ['101']).map((user) => ({
          type: 'tool_use',
          id: user,
          name: 'team_remove',
          input: {
            user,
            ...(modelCalls > 1
              ? { team: 'infra' }
              : failure === 'authorization failure'
                ? { team: 'infra' }
                : {}),
          },
        })),
      };
    };
    const result = await prepareHelperReply(
      ctx,
      turns,
      admin,
      { kind: 'person' },
      { discordUserId: 'owner' },
    );
    assert.equal(modelCalls, 1, 'failed proposals must not be replayed or replaced with a subset');
    assert.match(result.content, /couldn't prepare the full request.*nothing was changed/);
    assert.equal(result.action, undefined);
  });
}

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

test('mixed document and command proposals explain how to retry without implying a change', async () => {
  const ctx = appContext({
    enabled: true,
    run: async () => ({ outcome: OUTCOME.INCOMPLETE_ACTION, reason: 'mixed_tools' }),
  });
  const result = await prepareHelperReply(ctx, turns, principal, { kind: 'person' });
  assert.match(result.content, /document research and commands separately.*Nothing was changed/);
  assert.equal(result.action, undefined);
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
