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
  // Model-visible content excludes the raw citation object and keeps the passage.
  assert.equal(out.content.results[0].doc_id, 'd1');
  assert.equal(out.content.results[0].citation, undefined);
  assert.equal(out.content.results[0].snippet, hit.text);
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

test('channel search preserves answer evidence after the first 400 characters', async () => {
  const text = 'Background context. '.repeat(80) + 'The restore target is four hours.';
  const { search } = tools({
    async searchDocs(args) {
      assert.equal(args.guildId, '100');
      assert.equal(args.channelId, '200');
      return [{ ...hit, text }];
    },
  });
  const out = await search.run(
    { query: 'What is the restore target?' },
    { context: { guildId: '100', channelId: '200' } },
  );
  assert.equal(out.content.results[0].snippet, text);
  assert.equal('truncated' in out.content.results[0], false);
});

test('search revalidation detects changed evidence after the first 400 characters', async () => {
  let changed = false;
  const prefix = 'Background context. '.repeat(80);
  const { search } = tools({
    async searchDocs() {
      return [{ ...hit, text: prefix + (changed ? 'eight hours' : 'four hours') }];
    },
  });
  const out = await search.run({ query: 'restore' }, { context: { onBehalfOf: 'p' } });
  assert.equal(await out.revalidate(), true);
  changed = true;
  assert.equal(await out.revalidate(), false);
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
const MAX_SEARCH_CHUNK_BYTES = 49152; // independent search-passage contract
const MAX_SEARCH_RESULT_BYTES = 61440;
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

test('oversized search passage is byte-capped without splitting a character', async () => {
  const text = 'a'.repeat(MAX_SEARCH_CHUNK_BYTES - 1) + '😀';
  const { search } = tools({
    async searchDocs() {
      return [{ doc_id: 'd1', title: 'T', url: 'u', text, chunk: {} }];
    },
  });
  const out = await search.run({ query: 'q' }, { context: { onBehalfOf: 'p' } });
  const snippet = out.content.results[0].snippet;
  assert.equal(snippet, 'a'.repeat(MAX_SEARCH_CHUNK_BYTES - 1));
  assert.ok(u8(snippet) <= MAX_SEARCH_CHUNK_BYTES);
  assert.equal(snippet.isWellFormed(), true);
  assert.equal(out.content.results[0].truncated, true);
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

// The service's per-block MAX_JSON_BYTES is 65_536. Per-field caps bound one
// hit; this guards the combined block, which ten fat multibyte hits can push
// past the limit even when every field is individually within its cap.
// Ten hits, each a fat multibyte (3-byte) title/url/text well over the per-field
// caps, so each projected hit is near its max and the ten together overflow.
const fatHits = () =>
  Array.from({ length: 10 }, (_, i) => ({
    doc_id: `d${i}`,
    title: '界'.repeat(1000),
    url: 'https://x.test/' + '界'.repeat(2000),
    text: '界'.repeat(1000),
    chunk: { ordinal: i },
  }));

test('search caps the combined result block under the per-block byte limit', async () => {
  const { search } = tools({
    async searchDocs() {
      return fatHits();
    },
  });
  const out = await search.run({ query: 'q' }, { context: { onBehalfOf: 'p' } });
  // The whole serialized block (what rides in the tool_result) stays under the
  // service limit, not merely each field.
  assert.ok(u8(JSON.stringify(out.content)) <= MAX_SEARCH_RESULT_BYTES);
  // The aggregate budget actually bit: the overflow tail was dropped.
  assert.ok(out.content.results.length < 10);
  assert.ok(out.content.results.length >= 1);
  // Citations match the kept hits, not the raw ten.
  assert.equal(out.citations.length, out.content.results.length);
  // Every kept string is still well-formed UTF-8.
  for (const hit of out.content.results) {
    assert.equal(hit.title.isWellFormed(), true);
    assert.equal(hit.url.isWellFormed(), true);
    assert.equal(hit.snippet.isWellFormed(), true);
  }
});

test('revalidation covers kept hits and ignores changes to dropped hits', async () => {
  let current = fatHits();
  const { search } = tools({
    async searchDocs() {
      return current;
    },
  });
  const out = await search.run({ query: 'q' }, { context: { onBehalfOf: 'p' } });
  // Same hits still visible -> every kept fingerprint is found.
  assert.equal(await out.revalidate(), true);
  const keptIds = new Set(out.content.results.map((r) => r.doc_id));
  assert.ok(keptIds.size < current.length);
  current = current.map((r) => (keptIds.has(r.doc_id) ? r : { ...r, text: 'Changed unseen text' }));
  assert.equal(await out.revalidate(), true);
});

test('search skips an overflow hit and retains later small passages in rank order', async () => {
  let current = Array.from({ length: 10 }, (_, i) => ({
    ...hit,
    doc_id: `d${i}`,
    text: i < 4 ? 'a'.repeat(16384) : `Small passage ${i}`,
  }));
  const { search } = tools({ searchDocs: async () => current });
  const out = await search.run({ query: 'q' }, { context: { onBehalfOf: 'p' } });
  const expectedIds = ['d0', 'd1', 'd2', 'd4', 'd5', 'd6', 'd7', 'd8', 'd9'];
  assert.deepEqual(
    out.content.results.map((r) => r.doc_id),
    expectedIds,
  );
  assert.deepEqual(
    out.citations.map((r) => r.doc_id),
    expectedIds,
  );
  assert.equal(out.content.results[0].snippet, current[0].text);
  assert.ok(u8(JSON.stringify(out.content)) <= MAX_SEARCH_RESULT_BYTES);
  // Revalidation compares original evidence, independently of new rank order.
  current = current.filter((r) => r.doc_id !== 'd3').reverse();
  assert.equal(await out.revalidate(), true);
  current = current.filter((r) => r.doc_id !== 'd9');
  assert.equal(await out.revalidate(), false);
});

for (const text of [
  '\u0001'.repeat(16000),
  '"\\\n\r\t\b\f'.repeat(6000),
  ('😀中' + '\u001f'.repeat(20)).repeat(1600),
]) {
  test(`search retains escaped evidence within both byte caps (${u8(text)} raw bytes)`, async () => {
    let current = [{ ...hit, title: '界'.repeat(300), url: 'u'.repeat(2048), text }];
    const { search } = tools({ searchDocs: async () => current });
    const out = await search.run({ query: 'q' }, { context: { guildId: '100', channelId: '200' } });
    assert.equal(out.content.results.length, 1);
    const result = out.content.results[0];
    assert.equal(result.truncated, true);
    assert.ok(result.snippet.length > 400);
    assert.ok(text.startsWith(result.snippet));
    assert.equal(result.snippet.isWellFormed(), true);
    assert.ok(u8(result.snippet) <= MAX_SEARCH_CHUNK_BYTES);
    assert.ok(u8(JSON.stringify(out.content)) <= MAX_SEARCH_RESULT_BYTES);
    assert.deepEqual(out.citations[0].chunk, hit.chunk);
    assert.equal(await out.revalidate(), true);
    // Even a change past the old preview boundary in a JSON-capped hit matters.
    current = [{ ...current[0], text: text.slice(0, 500) + 'replacement' + text.slice(511) }];
    assert.equal(await out.revalidate(), false);
  });
}

test('search fills the aggregate budget exactly, including array commas', async () => {
  const row = { doc_id: 'd', title: 't', url: 'u', text: 'a', chunk: {} };
  const modelRow = { doc_id: 'd', title: 't', url: 'u', snippet: '' };
  const overhead = u8(JSON.stringify({ results: [modelRow, modelRow] }));
  const firstText = 'a'.repeat(32768);
  const secondText = 'b'.repeat(MAX_SEARCH_RESULT_BYTES - overhead - firstText.length);
  const { search } = tools({
    searchDocs: async () => [
      { ...row, text: firstText },
      { ...row, text: secondText },
    ],
  });
  const out = await search.run({ query: 'q' }, { context: { onBehalfOf: 'p' } });
  assert.equal(out.content.results.length, 2);
  assert.equal(out.content.results[1].snippet, secondText);
  assert.equal(u8(JSON.stringify(out.content)), MAX_SEARCH_RESULT_BYTES);
});

test('JSON-budget truncation stops before an emoji and still revalidates source revision', async () => {
  const fields = { doc_id: hit.doc_id, title: hit.title, url: hit.url };
  const overhead = u8(JSON.stringify({ results: [{ ...fields, snippet: '', truncated: true }] }));
  const prefixBytes = MAX_SEARCH_RESULT_BYTES - overhead - 3;
  const prefix = '\u0001'.repeat(Math.floor(prefixBytes / 6)) + 'a'.repeat(prefixBytes % 6);
  let current = { ...hit, text: prefix + '😀' + 'tail'.repeat(10) };
  const { search } = tools({ searchDocs: async () => [current] });
  const out = await search.run({ query: 'q' }, { context: { onBehalfOf: 'p' } });
  assert.equal(out.content.results[0].snippet, prefix);
  assert.equal(out.content.results[0].snippet.isWellFormed(), true);
  assert.equal(out.content.results[0].truncated, true);
  assert.ok(u8(JSON.stringify(out.content)) <= MAX_SEARCH_RESULT_BYTES);
  assert.equal(await out.revalidate(), true);
  // Even if the visible prefix stays identical, a new source revision is stale.
  current = { ...current, chunk: { ...hit.chunk, source_content_hash: 'replacement-hash' } };
  assert.equal(await out.revalidate(), false);
});

for (const missing of [null, undefined, []]) {
  test(`search revalidation fails closed for missing results (${String(missing)})`, async () => {
    let current = [hit];
    const { search } = tools({ searchDocs: async () => current });
    const out = await search.run({ query: 'q' }, { context: { onBehalfOf: 'p' } });
    current = missing;
    assert.equal(await out.revalidate(), false);
  });

  test(`initial empty search stays empty without fabricated evidence (${String(missing)})`, async () => {
    const { search } = tools({ searchDocs: async () => missing });
    const out = await search.run({ query: 'q' }, { context: { onBehalfOf: 'p' } });
    assert.deepEqual(out.content, { results: [] });
    assert.deepEqual(out.citations, []);
    assert.equal(await out.revalidate(), true);
  });
}

test('get_document bounds JSON escaping and revalidates the retained body', async () => {
  const text = '\u0001'.repeat(20000) + '😀';
  let doc = { id: 'd', title: '界'.repeat(300), content_snapshot: text };
  const { get } = tools({ getDoc: async () => doc });
  const out = await get.run({ doc_id: 'd' }, { context: { onBehalfOf: 'p' } });
  assert.equal(out.content.truncated, true);
  assert.ok(out.content.content.length > 400);
  assert.ok(text.startsWith(out.content.content));
  assert.ok(u8(out.content.content) <= MAX_DOC_CONTENT_BYTES);
  assert.ok(u8(JSON.stringify(out.content)) <= 65536);
  assert.equal(out.content.content.isWellFormed(), true);
  assert.equal(await out.revalidate(), true);
  doc = { ...doc, content_snapshot: text.slice(0, 500) + 'changed' + text.slice(507) };
  assert.equal(await out.revalidate(), false);
});

test('a pathological doc_id is bounded and does not suppress other hits', async () => {
  const { search } = tools({
    async searchDocs() {
      return [
        { doc_id: 'z'.repeat(200000), title: 't', url: 'u', text: 'x', chunk: {} },
        { doc_id: 'd1', title: 't', url: 'u', text: 'x', chunk: {} },
      ];
    },
  });
  const out = await search.run({ query: 'q' }, { context: { onBehalfOf: 'p' } });
  // The oversized id is capped, so its hit fits and the good hit below survives.
  assert.equal(out.content.results.length, 2);
  assert.ok(out.content.results[0].doc_id.length <= 128);
  assert.ok(u8(JSON.stringify(out.content)) <= 65536);
});

test('revalidation fails when a kept hit disappears', async () => {
  let calls = 0;
  const { search } = tools({
    async searchDocs() {
      calls += 1;
      // Second pass drops the top-ranked (kept) hit.
      return calls === 1 ? fatHits() : fatHits().slice(1);
    },
  });
  const out = await search.run({ query: 'q' }, { context: { onBehalfOf: 'p' } });
  assert.equal(await out.revalidate(), false);
});
