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

const MAX_DOC_CONTENT_BYTES = 49152; // mirrors helperTools.js
const u8 = (s) => Buffer.byteLength(s, 'utf8');

test('get_document caps an oversized body by bytes and flags it truncated', async () => {
  const { get } = tools({
    async getDoc(id) {
      return { id, title: 'T', content: 'x'.repeat(50000) };
    },
  });
  const out = await get.run({ doc_id: 'big' }, { context: { onBehalfOf: 'person-1' } });
  assert.equal(out.content.truncated, true);
  assert.ok(u8(out.content.content) <= MAX_DOC_CONTENT_BYTES);
});

// The real governing limit is the service's 64 KiB per-block cap counted in
// UTF-8 bytes, so a multibyte body well under 20k characters can still blow it.
test('get_document bounds a multibyte body by UTF-8 bytes, not units', async () => {
  // 20k CJK chars = 3 bytes each = 60 KB > 48 KiB budget, but only 20k units.
  const { get } = tools({
    async getDoc(id) {
      return { id, title: 'T', content: '中'.repeat(20000) };
    },
  });
  const out = await get.run({ doc_id: 'cjk' }, { context: { onBehalfOf: 'person-1' } });
  assert.equal(out.content.truncated, true);
  assert.ok(u8(out.content.content) <= MAX_DOC_CONTENT_BYTES);
  assert.equal(out.content.content.isWellFormed(), true);
});

test('get_document also bounds the content_snapshot fallback path', async () => {
  const { get } = tools({
    async getDoc(id) {
      return { id, title: 'T', content_snapshot: '中'.repeat(20000) };
    },
  });
  const out = await get.run({ doc_id: 'snap' }, { context: { onBehalfOf: 'person-1' } });
  assert.equal(out.content.truncated, true);
  assert.ok(u8(out.content.content) <= MAX_DOC_CONTENT_BYTES);
  assert.equal(out.content.content.isWellFormed(), true);
});

test('get_document bounds a fat title so it cannot overflow the block', async () => {
  const { get } = tools({
    async getDoc(id) {
      return { id, title: 'T'.repeat(5000), content: 'body' };
    },
  });
  const out = await get.run({ doc_id: 'd1' }, { context: { onBehalfOf: 'person-1' } });
  assert.ok(out.content.title.length <= 300);
});

test('get_document leaves a within-limit body whole and unflagged', async () => {
  const { get } = tools({
    async getDoc(id) {
      return { id, title: 'T', content: 'short body' };
    },
  });
  const out = await get.run({ doc_id: 'd1' }, { context: { onBehalfOf: 'person-1' } });
  assert.equal(out.content.content, 'short body');
  assert.equal('truncated' in out.content, false);
});

// A byte cap landing mid-emoji must not leave a lone surrogate: that is invalid
// UTF-8 and the /chat service rejects the whole replay (422) before the model runs.
test('get_document byte truncation never splits a character', async () => {
  // 13,000 emoji = 52,000 bytes (4 each) > 48 KiB; the cut must land on a boundary.
  const { get } = tools({
    async getDoc(id) {
      return { id, title: 'T', content: '😀'.repeat(13000) };
    },
  });
  const out = await get.run({ doc_id: 'emoji' }, { context: { onBehalfOf: 'person-1' } });
  assert.equal(out.content.truncated, true);
  assert.ok(u8(out.content.content) <= MAX_DOC_CONTENT_BYTES);
  assert.equal(out.content.content.isWellFormed(), true);
});

test('search snippet truncation never splits a surrogate pair', async () => {
  const text = 'a'.repeat(399) + '😀'; // cut at SNIPPET_LEN (400) splits the emoji
  const { search } = tools({
    async searchDocs() {
      return [{ doc_id: 'd1', title: 'T', url: 'u', text, chunk: {} }];
    },
  });
  const out = await search.run({ query: 'q' }, { context: { onBehalfOf: 'p' } });
  const snippet = out.content.results[0].snippet;
  assert.ok(snippet.length <= 400);
  assert.equal(snippet.isWellFormed(), true);
});

test('query normalization never splits a surrogate pair at the cap', async () => {
  let seen;
  const { search } = tools({
    async searchDocs(args) {
      seen = args;
      return [];
    },
  });
  // 1,999 ASCII + emoji: the 2,000-unit query cap would split the pair.
  await search.run({ query: 'a'.repeat(1999) + '😀' }, { context: { onBehalfOf: 'p' } });
  assert.ok(seen.query.length <= 2000);
  assert.equal(seen.query.isWellFormed(), true);
});

test('search bounds service-controlled title and url in the model content', async () => {
  const { search } = tools({
    async searchDocs() {
      return [
        { doc_id: 'd1', title: 'T'.repeat(5000), url: 'u'.repeat(5000), text: 'x', chunk: {} },
      ];
    },
  });
  const out = await search.run({ query: 'q' }, { context: { onBehalfOf: 'p' } });
  const hit = out.content.results[0];
  assert.ok(hit.title.length <= 300);
  assert.ok(hit.url.length <= 2048);
});
