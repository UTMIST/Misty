import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createToolExecutor, OUTCOME } from '../src/toolExecutor.js';
import { buildToolRegistry } from '../src/toolRegistry.js';
import { createHelperActions } from '../src/helperActions.js';

// A scripted llmClient: each call shifts the next response off `script`.
function scriptedLlm(script) {
  const calls = [];
  return {
    calls,
    async chat(args) {
      calls.push(args);
      const next = script.shift();
      if (!next) throw new Error('llm called more times than scripted');
      return next;
    },
  };
}

function toolUse(id, name, input) {
  return { type: 'tool_use', id, name, input };
}

function registryWith(run, name = 'search_documents') {
  return buildToolRegistry([
    { name: 'docs', llmSafe: true, tools: [{ definition: { name, input_schema: {} }, run }] },
  ]);
}

const base = {
  messages: [{ role: 'user', content: 'hi' }],
  system: 's',
  context: { onBehalfOf: 'p' },
};

for (const failure of ['revocation', 'read budget']) {
  test(`presentation deadline preserves earlier document checks and ${failure}`, async (t) => {
    const epoch = Date.now();
    let clock = 0,
      reads = 0,
      allowed = true;
    t.mock.method(Date, 'now', () => epoch + clock);
    t.mock.method(performance, 'now', () => clock);
    const action = { userId: 'u', check: async () => {} };
    const registry = registryWith(async (_input, { readDocument }) => {
      await readDocument('doc', async () => {
        reads++;
      });
      return {
        revalidate: async (read) =>
          read('doc', async () => {
            reads++;
            return allowed;
          }),
      };
    });
    registry.handlers.set('command', async () => ({ action }));
    const executor = createToolExecutor({
      registry,
      maxDocumentRequests: 4,
      llmClient: scriptedLlm([
        { stop_reason: 'tool_use', content_blocks: [toolUse('a', 'search_documents', {})] },
        { stop_reason: 'tool_use', content_blocks: [toolUse('b', 'command', {})] },
      ]),
    });
    const result = await executor.run(base);
    assert.equal(result.outcome, 'action');
    assert.equal(reads, 3);
    const pending = createHelperActions();
    const token = pending.add(action);
    clock = 70000;
    pending.presented(token, 'u');
    clock = 150000; // past the generation deadline, inside the confirmation window
    if (failure === 'revocation') allowed = false;
    else await action.check();
    await assert.rejects(action.check(), /expired|access/);
    assert.equal(reads, 4); // no refunded/restarted document request budget
  });
}

test('a command action ends the turn without sending its private result back to the model', async () => {
  const action = { userId: 'u1', execute: async () => ({ content: 'private email' }) };
  const llm = scriptedLlm([
    { stop_reason: 'tool_use', content_blocks: [toolUse('t', 'whoami', {})] },
  ]);
  const exec = createToolExecutor({
    llmClient: llm,
    registry: registryWith(async () => ({ action }), 'whoami'),
  });
  const result = await exec.run(base);
  assert.equal(result.outcome, 'action');
  assert.equal(result.action, action);
  assert.equal(llm.calls.length, 1);
});

test('document tools and terminal commands in one response fail before any tool work', async () => {
  for (const commandFirst of [true, false]) {
    const calls = [toolUse('read', 'search_documents', {}), toolUse('write', 'team_rename', {})];
    const registry = buildToolRegistry([
      {
        name: 'mixed',
        llmSafe: true,
        tools: ['search_documents', 'team_rename'].map((name) => ({
          terminal: name === 'team_rename',
          definition: { name, input_schema: {} },
          run: async () => assert.fail('Mixed batch performed provider work'),
        })),
      },
    ]);
    const llm = scriptedLlm([
      {
        stop_reason: 'tool_use',
        content_blocks: commandFirst ? calls.reverse() : calls,
      },
    ]);
    const result = await createToolExecutor({ registry, llmClient: llm }).run(base);
    assert.deepEqual(result, { outcome: OUTCOME.INCOMPLETE_ACTION, reason: 'mixed_tools' });
    assert.equal(llm.calls.length, 1);
  }
});

test('preparation clarification uses the remaining output budget and stops after one response', async () => {
  const registry = registryWith(
    async () => ({ is_error: true, content: { error: 'Invalid user.' } }),
    'command',
  );
  registry.terminalTools.add('command');
  const llm = scriptedLlm([
    {
      stop_reason: 'tool_use',
      usage: { output_tokens: 2 },
      content_blocks: [toolUse('a', 'command', {})],
    },
    { stop_reason: 'end_turn', usage: { output_tokens: 1 }, content: 'Please mention the member.' },
  ]);
  const result = await createToolExecutor({
    registry,
    llmClient: llm,
    maxOutputTokens: 3,
    perCallMaxTokens: 2,
  }).run(base);
  assert.equal(result.outcome, OUTCOME.ANSWER);
  assert.deepEqual(
    llm.calls.map((call) => call.maxTokens),
    [2, 1],
  );
});

for (const revokeAt of ['generation', 'delivery']) {
  test(`preparation clarification still checks earlier document evidence at ${revokeAt}`, async () => {
    let allowed = true;
    const registry = registryWith(async () => ({ revalidate: async () => allowed }));
    registry.handlers.set('command', async () => ({
      is_error: true,
      content: { error: 'Invalid user.' },
    }));
    registry.terminalTools.add('command');
    let modelCalls = 0;
    const executor = createToolExecutor({
      registry,
      llmClient: {
        chat: async () => {
          modelCalls++;
          if (modelCalls < 3)
            return {
              stop_reason: 'tool_use',
              content_blocks: [
                toolUse(String(modelCalls), modelCalls === 1 ? 'search_documents' : 'command', {}),
              ],
            };
          if (revokeAt === 'generation') allowed = false;
          return { stop_reason: 'end_turn', content: 'Please mention the member.' };
        },
      },
    });
    const result = await executor.run(base);
    assert.equal(modelCalls, 3);
    if (revokeAt === 'generation') assert.equal(result.outcome, OUTCOME.WITHHELD);
    else {
      assert.equal(result.outcome, OUTCOME.ANSWER);
      allowed = false;
      assert.equal(await result.canPublish(), false);
    }
  });
}

test('all actions in one model response are prepared and confirmed together', async () => {
  const executed = [];
  const llm = scriptedLlm([
    {
      stop_reason: 'tool_use',
      content_blocks: [
        toolUse('a', 'team_remove', { user: 'Alex' }),
        toolUse('b', 'team_remove', { user: 'Sam' }),
      ],
    },
  ]);
  const executor = createToolExecutor({
    llmClient: llm,
    registry: registryWith(
      async ({ user }) => ({
        action: {
          userId: 'requester',
          confirm: true,
          summary: `Remove ${user}`,
          check: async () => {},
          execute: async () => {
            executed.push(user);
            return { content: `Removed ${user}` };
          },
        },
      }),
      'team_remove',
    ),
  });
  const result = await executor.run(base);
  assert.match(result.action.summary, /Alex/);
  assert.match(result.action.summary, /Sam/);
  assert.deepEqual(executed, []);
  await result.action.execute();
  assert.deepEqual(executed, ['Alex', 'Sam']);
  assert.equal(llm.calls.length, 1);
});

test('a failed call in an action batch rejects the whole incomplete proposal', async () => {
  for (const invalidFirst of [true, false]) {
    const calls = [toolUse('a', 'command', {}), toolUse('b', 'missing', {})];
    const executor = createToolExecutor({
      llmClient: scriptedLlm([
        { stop_reason: 'tool_use', content_blocks: invalidFirst ? calls.reverse() : calls },
      ]),
      registry: registryWith(
        async () => ({ action: { userId: 'u', check: async () => {} } }),
        'command',
      ),
    });
    const result = await executor.run(base);
    assert.equal(result.outcome, 'incomplete_action');
    assert.equal(result.action, undefined);
  }
});

test('a batched confirmation rechecks every action before the first write', async () => {
  let allowed = true;
  const executed = [];
  const executor = createToolExecutor({
    llmClient: scriptedLlm([
      {
        stop_reason: 'tool_use',
        content_blocks: [toolUse('a', 'command', { id: 1 }), toolUse('b', 'command', { id: 2 })],
      },
    ]),
    registry: registryWith(
      async ({ id }) => ({
        action: {
          userId: 'u',
          confirm: true,
          summary: `Action ${id}`,
          check: async () => {
            if (id === 2 && !allowed) throw new Error('Permission revoked');
          },
          execute: async () => {
            executed.push(id);
            return { content: 'Done' };
          },
        },
      }),
      'command',
    ),
  });
  const { action } = await executor.run(base);
  allowed = false;
  await assert.rejects(action.execute(), /Permission revoked/);
  assert.deepEqual(executed, []);
});

test('a batch reports a completed action and stops after an uncertain execution failure', async () => {
  const executed = [];
  const executor = createToolExecutor({
    llmClient: scriptedLlm([
      {
        stop_reason: 'tool_use',
        content_blocks: [1, 2, 3].map((id) => toolUse(String(id), 'command', { id })),
      },
    ]),
    registry: registryWith(
      async ({ id }) => ({
        action: {
          userId: 'u',
          confirm: true,
          summary: `Action ${id}`,
          check: async () => {},
          execute: async () => {
            executed.push(id);
            if (id === 2) throw new Error('Ambiguous backend failure');
            return { content: 'First completed', embeds: [{ title: 'Result' }] };
          },
        },
      }),
      'command',
    ),
  });
  const { action } = await executor.run(base);
  const result = await action.execute();
  assert.deepEqual(executed, [1, 2]);
  const { check, ...receipt } = result.replies[0];
  assert.equal(typeof check, 'function');
  assert.deepEqual(receipt, {
    content: 'First completed',
    embeds: [{ title: 'Result' }],
  });
  assert.match(result.replies[1].content, /Action 2.*remaining actions were not run/);
});

test('fresh caller authorization runs before each tool and prevents execution after revocation', async () => {
  let allowed = true;
  let executed = 0;
  const llm = {
    async chat() {
      allowed = false;
      return { stop_reason: 'tool_use', content_blocks: [toolUse('t', 'whoami', {})] };
    },
  };
  const exec = createToolExecutor({
    llmClient: llm,
    authenticate: async (request) => request.discordUserId === 'actual' && allowed,
    registry: registryWith(async () => {
      executed++;
      return {};
    }, 'whoami'),
  });
  const result = await exec.run({ ...base, request: { discordUserId: 'actual' } });
  assert.equal(result.outcome, OUTCOME.WITHHELD);
  assert.equal(executed, 0);
});

test('returns the final answer when the model stops without tools', async () => {
  const llm = scriptedLlm([{ content: 'the answer', stop_reason: 'end_turn' }]);
  const exec = createToolExecutor({ llmClient: llm, registry: registryWith(async () => ({})) });
  const res = await exec.run(base);
  assert.equal(res.outcome, OUTCOME.ANSWER);
  assert.equal(res.content, 'the answer');
});

test('runs a tool round, binds the executor context, replays blocks verbatim', async () => {
  const blocks = [toolUse('t1', 'search_documents', { query: 'q' })];
  const llm = scriptedLlm([
    { content: '', stop_reason: 'tool_use', content_blocks: blocks },
    { content: 'final', stop_reason: 'end_turn' },
  ]);
  let handlerCtx;
  const run = async (input, ctx) => {
    handlerCtx = ctx;
    return { content: { results: [] }, citations: [{ doc_id: 'd1' }] };
  };
  const exec = createToolExecutor({ llmClient: llm, registry: registryWith(run) });
  const res = await exec.run(base);

  assert.equal(res.outcome, OUTCOME.ANSWER);
  assert.deepEqual(res.citations, [{ doc_id: 'd1' }]);
  // context is the bound one, not model input
  assert.deepEqual(handlerCtx.context, { onBehalfOf: 'p' });
  // second call's history replays the assistant blocks verbatim then a tool_result
  const history = llm.calls[1].messages;
  assert.deepEqual(history[history.length - 2], { role: 'assistant', content: blocks });
  const toolMsg = history[history.length - 1];
  assert.equal(toolMsg.role, 'user');
  assert.equal(toolMsg.content[0].type, 'tool_result');
  assert.equal(toolMsg.content[0].tool_use_id, 't1');
});

test('an unknown tool is refused with an error result and never executed', async () => {
  const blocks = [toolUse('t1', 'delete_everything', {})];
  const llm = scriptedLlm([
    { content: '', stop_reason: 'tool_use', content_blocks: blocks },
    { content: 'ok', stop_reason: 'end_turn' },
  ]);
  const exec = createToolExecutor({ llmClient: llm, registry: registryWith(async () => ({})) });
  const res = await exec.run(base);
  assert.equal(res.outcome, OUTCOME.ANSWER);
  const toolMsg = llm.calls[1].messages.at(-1);
  assert.equal(toolMsg.content[0].is_error, true);
});

test('a throwing tool becomes an error result, loop continues', async () => {
  const blocks = [toolUse('t1', 'search_documents', {})];
  const llm = scriptedLlm([
    { content: '', stop_reason: 'tool_use', content_blocks: blocks },
    { content: 'recovered', stop_reason: 'end_turn' },
  ]);
  const run = async () => {
    throw new Error('boom');
  };
  const exec = createToolExecutor({ llmClient: llm, registry: registryWith(run) });
  const res = await exec.run(base);
  assert.equal(res.outcome, OUTCOME.ANSWER);
  assert.equal(llm.calls[1].messages.at(-1).content[0].is_error, true);
});

test('EXHAUSTED when the model keeps asking for tools past the round limit', async () => {
  const wantTool = {
    content: '',
    stop_reason: 'tool_use',
    content_blocks: [toolUse('t', 'search_documents', {})],
  };
  const llm = scriptedLlm([wantTool, wantTool, wantTool, wantTool, wantTool]);
  const exec = createToolExecutor({
    llmClient: llm,
    registry: registryWith(async () => ({ content: {} })),
    maxToolRounds: 2,
  });
  const res = await exec.run(base);
  assert.equal(res.outcome, OUTCOME.EXHAUSTED);
});

test('EXHAUSTED when cumulative output tokens exceed the cost bound', async () => {
  const wantTool = {
    content: '',
    stop_reason: 'tool_use',
    content_blocks: [toolUse('t', 'search_documents', {})],
    usage: { output_tokens: 10000 },
  };
  const llm = scriptedLlm([wantTool, wantTool]);
  const exec = createToolExecutor({
    llmClient: llm,
    registry: registryWith(async () => ({ content: {} })),
    maxOutputTokens: 5000,
  });
  const res = await exec.run(base);
  assert.equal(res.outcome, OUTCOME.EXHAUSTED);
});

test('EMPTY when the final answer has no usable text', async () => {
  const llm = scriptedLlm([{ content: '   ', stop_reason: 'end_turn' }]);
  const exec = createToolExecutor({ llmClient: llm, registry: registryWith(async () => ({})) });
  assert.equal((await exec.run(base)).outcome, OUTCOME.EMPTY);
});

test('citations dedupe across rounds by doc_id', async () => {
  const blocks = [toolUse('t1', 'search_documents', {})];
  const llm = scriptedLlm([
    { content: '', stop_reason: 'tool_use', content_blocks: blocks },
    {
      content: '',
      stop_reason: 'tool_use',
      content_blocks: [toolUse('t2', 'search_documents', {})],
    },
    { content: 'done', stop_reason: 'end_turn' },
  ]);
  const run = async () => ({ content: {}, citations: [{ doc_id: 'd1' }, { doc_id: 'd2' }] });
  const exec = createToolExecutor({ llmClient: llm, registry: registryWith(run) });
  const res = await exec.run(base);
  assert.deepEqual(res.citations.map((c) => c.doc_id).sort(), ['d1', 'd2']);
});

// --- Additional caller checks -------------------------------------------------

test('a text-only response needs no document revalidation', async () => {
  const llm = scriptedLlm([{ content: 'answer', stop_reason: 'end_turn' }]);
  const exec = createToolExecutor({ llmClient: llm, registry: registryWith(async () => ({})) });
  const res = await exec.run(base);
  assert.equal(res.outcome, OUTCOME.ANSWER);
});

test('a caller check denying before generation yields WITHHELD without a model call', async () => {
  const llm = scriptedLlm([{ content: 'secret answer', stop_reason: 'end_turn' }]);
  const exec = createToolExecutor({ llmClient: llm, registry: registryWith(async () => ({})) });
  const res = await exec.run({ ...base, scopeCheck: async () => false });
  assert.equal(res.outcome, OUTCOME.WITHHELD);
  assert.equal(res.content, undefined);
});

test('a throwing scopeCheck fails closed (WITHHELD)', async () => {
  const llm = scriptedLlm([{ content: 'x', stop_reason: 'end_turn' }]);
  const exec = createToolExecutor({ llmClient: llm, registry: registryWith(async () => ({})) });
  const res = await exec.run({
    ...base,
    scopeCheck: async () => {
      throw new Error('revoked');
    },
  });
  assert.equal(res.outcome, OUTCOME.WITHHELD);
});

for (const usage of [
  { output_tokens: 8 },
  undefined,
  { output_tokens: -1 },
  { output_tokens: NaN },
]) {
  test(`remaining budget caps the next request, including missing/invalid usage: ${JSON.stringify(usage)}`, async () => {
    const llm = scriptedLlm([
      { stop_reason: 'tool_use', content_blocks: [toolUse('t', 'search_documents', {})], usage },
      { content: 'ok', stop_reason: 'end_turn', usage: { output_tokens: 2 } },
    ]);
    const exec = createToolExecutor({
      llmClient: llm,
      registry: registryWith(async () => ({})),
      maxOutputTokens: 10,
      perCallMaxTokens: 8,
    });
    assert.equal((await exec.run(base)).outcome, OUTCOME.ANSWER);
    assert.deepEqual(
      llm.calls.map((call) => call.maxTokens),
      [8, 2],
    );
  });
}

test('an exhausted budget stops before tools or another model call', async () => {
  const llm = scriptedLlm([
    {
      stop_reason: 'tool_use',
      content_blocks: [toolUse('t', 'search_documents', {})],
      usage: { output_tokens: 8 },
    },
  ]);
  const exec = createToolExecutor({
    llmClient: llm,
    registry: registryWith(async () => assert.fail('budget exhausted')),
    maxOutputTokens: 8,
    perCallMaxTokens: 8,
  });
  assert.equal((await exec.run(base)).outcome, OUTCOME.EXHAUSTED);
  assert.equal(llm.calls.length, 1);
});

test('an over-budget final answer is not published', async () => {
  const llm = scriptedLlm([
    { content: 'over budget', stop_reason: 'end_turn', usage: { output_tokens: 11 } },
  ]);
  const exec = createToolExecutor({
    llmClient: llm,
    registry: registryWith(async () => ({})),
    maxOutputTokens: 10,
  });
  const result = await exec.run(base);
  assert.equal(result.outcome, OUTCOME.EXHAUSTED);
  assert.equal(result.content, undefined);
  assert.equal(llm.calls[0].maxTokens, 10);
});

test('zero output budget fails before a paid call', async () => {
  const llm = scriptedLlm([]);
  const exec = createToolExecutor({
    llmClient: llm,
    registry: registryWith(async () => ({})),
    maxOutputTokens: 0,
  });
  assert.equal((await exec.run(base)).outcome, OUTCOME.EXHAUSTED);
  assert.equal(llm.calls.length, 0);
});

for (const [stopReason, outcome] of [
  ['max_tokens', OUTCOME.EXHAUSTED],
  ['incomplete', OUTCOME.EMPTY],
  ['content_filtered', OUTCOME.EMPTY],
  ['unknown', OUTCOME.EMPTY],
  [undefined, OUTCOME.EMPTY],
]) {
  test(`a ${stopReason} completion cannot publish partial text or execute its tools`, async () => {
    const llm = scriptedLlm([
      {
        content: 'Partial instructions: first you',
        stop_reason: stopReason,
        content_blocks: [toolUse('t', 'search_documents', { query: 'q' })],
      },
    ]);
    const exec = createToolExecutor({
      llmClient: llm,
      registry: registryWith(async () => assert.fail('incomplete tool call executed')),
    });
    assert.deepEqual(await exec.run(base), { outcome });
    assert.equal(llm.calls.length, 1);
  });
}

test('a tool_use response without any calls is not treated as a final answer', async () => {
  const exec = createToolExecutor({
    llmClient: scriptedLlm([{ stop_reason: 'tool_use', content: 'Let me search.' }]),
    registry: registryWith(async () => assert.fail('no calls')),
  });
  assert.deepEqual(await exec.run(base), { outcome: OUTCOME.EMPTY });
});

for (const batchSize of [5, 10, 40, 128]) {
  test(`an oversized batch of ${batchSize} calls is rejected before any tool work`, async () => {
    const llm = scriptedLlm([
      {
        stop_reason: 'tool_use',
        content_blocks: Array.from({ length: batchSize }, (_, i) =>
          toolUse(`t${i}`, 'search_documents', { query: 'q' }),
        ),
      },
    ]);
    const exec = createToolExecutor({
      llmClient: llm,
      registry: registryWith(async () => assert.fail('oversized batch executed')),
    });
    assert.deepEqual(await exec.run(base), { outcome: OUTCOME.EXHAUSTED });
    assert.equal(llm.calls.length, 1);
  });
}

test('tool-call capacity is shared across rounds and counts unavailable calls', async () => {
  const llm = scriptedLlm([
    {
      stop_reason: 'tool_use',
      content_blocks: [
        toolUse('t1', 'unknown', {}),
        toolUse('t2', 'search_documents', {}),
        toolUse('t3', 'search_documents', {}),
      ],
    },
    {
      stop_reason: 'tool_use',
      content_blocks: [
        toolUse('t4', 'search_documents', {}),
        toolUse('t5', 'search_documents', {}),
      ],
    },
  ]);
  let tools = 0;
  const exec = createToolExecutor({
    llmClient: llm,
    registry: registryWith(async () => {
      tools++;
      return {};
    }),
  });
  assert.deepEqual(await exec.run(base), { outcome: OUTCOME.EXHAUSTED });
  assert.equal(tools, 2); // the second batch is rejected as a whole
  assert.equal(llm.calls.length, 2);
});

test('the turn deadline aborts a stalled model request even if it ignores cancellation', async () => {
  let requestSignal;
  const exec = createToolExecutor({
    llmClient: {
      chat: ({ signal }) => {
        requestSignal = signal;
        return new Promise(() => {});
      },
    },
    registry: registryWith(async () => assert.fail('no tools after deadline')),
    maxDurationMs: 50,
  });
  assert.deepEqual(await exec.run(base), { outcome: OUTCOME.EXHAUSTED });
  assert.equal(requestSignal.aborted, true);
});

for (const limits of [
  { maxToolCalls: -1 },
  { maxToolCalls: 1.5 },
  { maxDocumentRequests: Infinity },
  { maxDocumentRequests: -1 },
  { maxDurationMs: 0 },
  { maxDurationMs: 2147483648 },
]) {
  test(`invalid work limits are rejected: ${JSON.stringify(limits)}`, () => {
    assert.throws(() => createToolExecutor({ llmClient: {}, registry: {}, ...limits }), RangeError);
  });
}
