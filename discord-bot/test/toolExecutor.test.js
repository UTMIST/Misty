import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createToolExecutor, OUTCOME } from '../src/toolExecutor.js';
import { buildToolRegistry } from '../src/toolRegistry.js';

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

// --- Dormant scopeCheck seam -------------------------------------------------

test('without a scopeCheck the loop never withholds (seam is dormant)', async () => {
  const llm = scriptedLlm([{ content: 'answer', stop_reason: 'end_turn' }]);
  const exec = createToolExecutor({ llmClient: llm, registry: registryWith(async () => ({})) });
  const res = await exec.run(base); // no scopeCheck
  assert.equal(res.outcome, OUTCOME.ANSWER);
});

test('a scopeCheck returning false before publishing yields WITHHELD, no model text', async () => {
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
