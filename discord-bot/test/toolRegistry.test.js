import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildToolRegistry } from '../src/toolRegistry.js';

const toolA = { definition: { name: 'a', input_schema: {} }, run: async () => ({ content: {} }) };
const toolB = { definition: { name: 'b', input_schema: {} }, run: async () => ({ content: {} }) };

test('includes tools only from providers with llmSafe === true', () => {
  const reg = buildToolRegistry([
    { name: 'safe', llmSafe: true, tools: [toolA] },
    { name: 'unsafe', llmSafe: false, tools: [toolB] },
  ]);
  assert.deepEqual(
    reg.definitions.map((d) => d.name),
    ['a'],
  );
  assert.equal(reg.has('a'), true);
  assert.equal(reg.has('b'), false);
});

test('only a literal true is safe — truthy non-true values are excluded', () => {
  for (const value of [1, 'true', {}, [], 'yes']) {
    const reg = buildToolRegistry([{ name: 'p', llmSafe: value, tools: [toolA] }]);
    assert.equal(reg.definitions.length, 0, `llmSafe=${JSON.stringify(value)} must be excluded`);
  }
});

test('throws on a tool with no definition name', () => {
  assert.throws(
    () => buildToolRegistry([{ name: 'p', llmSafe: true, tools: [{ run: async () => ({}) }] }]),
    /no definition name/,
  );
});

test('throws on a tool with no run handler', () => {
  assert.throws(
    () => buildToolRegistry([{ name: 'p', llmSafe: true, tools: [{ definition: { name: 'x' } }] }]),
    /no run handler/,
  );
});

test('throws on duplicate tool names across providers', () => {
  assert.throws(
    () =>
      buildToolRegistry([
        { name: 'p1', llmSafe: true, tools: [toolA] },
        { name: 'p2', llmSafe: true, tools: [toolA] },
      ]),
    /duplicate tool name/,
  );
});

test('empty / missing provider list yields an empty registry', () => {
  assert.equal(buildToolRegistry([]).definitions.length, 0);
  assert.equal(buildToolRegistry(undefined).definitions.length, 0);
});
