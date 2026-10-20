import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDocumentTools, llmSafe } from '../src/helperTools.js';

function tools(docClient) {
  const list = createDocumentTools({ docClient });
  const byName = new Map(list.map((t) => [t.definition.name, t]));
  return {
    search: byName.get('search_documents'),
    get: byName.get('get_document'),
  };
}

const hit = {
  doc_id: 'd1',
  title: 'CPSIF notes',
  url: 'https://x.test/d1',
  source_id: 'gdocs',
  text: 'CPSIF '.repeat(200),
  chunk: { ordinal: 0, start_offset: 0, end_offset: 32, source_content_hash: 'abc' },
  score: 0.9,
};

test('module is llmSafe', () => {
  assert.equal(llmSafe, true);
});

test('search_documents passes person context to docClient, never scope from input', async () => {
  let seen;
  const { search } = tools({
    async searchDocs(args) {
      seen = args;
      return [hit];
    },
  });
  const out = await search.run(
    { query: 'cpsif', onBehalfOf: 'ATTACKER', guildId: '999' }, // injected scope must be ignored
    { context: { onBehalfOf: 'person-1' } },
  );
  assert.equal(seen.onBehalfOf, 'person-1');
  assert.equal(seen.guildId, undefined);
  assert.equal(seen.query, 'cpsif');
  // model-visible content excludes the raw citation object and bounds the snippet
  assert.equal(out.content.results[0].doc_id, 'd1');
  assert.equal(out.content.results[0].citation, undefined);
  assert.ok(out.content.results[0].snippet.length <= 400);
  assert.deepEqual(out.citations[0].chunk, hit.chunk);
});

test('search_documents forwards channel context (guild + channel) unchanged', async () => {
  let seen;
  const { search } = tools({
    async searchDocs(args) {
      seen = args;
      return [];
    },
  });
  await search.run({ query: 'q' }, { context: { guildId: '100', channelId: '200' } });
  assert.equal(seen.guildId, '100');
  assert.equal(seen.channelId, '200');
  assert.equal(seen.onBehalfOf, undefined);
});

test('search_documents rejects an empty query without calling the service', async () => {
  let called = false;
  const { search } = tools({
    async searchDocs() {
      called = true;
      return [];
    },
  });
  const out = await search.run({ query: '   ' }, { context: { onBehalfOf: 'p' } });
  assert.equal(out.is_error, true);
  assert.equal(called, false);
});

test('get_document fetches as the person in a DM context', async () => {
  let seen;
  const { get } = tools({
    async getDoc(id, opts) {
      seen = { id, opts };
      return { id, title: 'T', content: 'body' };
    },
  });
  const out = await get.run({ doc_id: 'd1' }, { context: { onBehalfOf: 'person-1' } });
  assert.equal(seen.opts.onBehalfOf, 'person-1');
  assert.equal(out.content.found, true);
  assert.equal(out.content.content, 'body');
});

test('get_document is unavailable in a channel context and never reads a doc', async () => {
  let called = false;
  const { get } = tools({
    async getDoc() {
      called = true;
      return { id: 'd1' };
    },
  });
  const out = await get.run({ doc_id: 'd1' }, { context: { guildId: '100', channelId: '200' } });
  assert.equal(called, false);
  assert.equal(out.content.available, false);
});

test('get_document reports not-found (never forbidden) for a null doc', async () => {
  const { get } = tools({
    async getDoc() {
      return null;
    },
  });
  const out = await get.run({ doc_id: 'secret' }, { context: { onBehalfOf: 'person-1' } });
  assert.deepEqual(out.content, { found: false });
});
