import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  createDocService,
  llmSafe,
  SEARCH_CHUNK_K,
  SEARCH_MAX_DOCS,
} from '../src/services/docService.js';
import { DocUnavailable, DocBadReference } from '../src/clients/docClient.js';
import { DirectoryUnavailable } from '../src/clients/directoryClient.js';

test('llmSafe is true', () => {
  assert.equal(llmSafe, true);
});

test('addDoc resolves team slug and returns ADDED on created', async () => {
  const svc = createDocService({
    directory: {
      getTeamBySlug: async (slug) => (slug === 'ml' ? { id: 't1', slug: 'ml', label: 'ML' } : null),
    },
    docClient: {
      ingestDoc: async (payload) => {
        assert.equal(payload.owningTeamId, 't1');
        assert.equal(payload.url, 'https://x.com');
        return { doc: { id: 'd1' }, created: true, warnings: [] };
      },
    },
  });
  const res = await svc.addDoc({ url: 'https://x.com', teamSlug: 'ml', tags: ['onboarding'] });
  assert.equal(res.outcome, 'ADDED');
  assert.equal(res.doc.id, 'd1');
});

test('addDoc treats null tags as absent and does not throw', async () => {
  const svc = createDocService({
    directory: {},
    docClient: {
      ingestDoc: async (payload) => {
        assert.equal('tags' in payload, false);
        return { doc: { id: 'd1' }, created: true, warnings: [] };
      },
    },
  });
  const res = await svc.addDoc({ url: 'https://x.com', tags: null });
  assert.equal(res.outcome, 'ADDED');
});

test('addDoc returns MERGED when created is false', async () => {
  const svc = createDocService({
    directory: {},
    docClient: { ingestDoc: async () => ({ doc: { id: 'd1' }, created: false, warnings: ['x'] }) },
  });
  const res = await svc.addDoc({ url: 'https://x.com' });
  assert.equal(res.outcome, 'MERGED');
  assert.deepEqual(res.warnings, ['x']);
});

test('addDoc returns TEAM_NOT_FOUND for unknown slug (no ingest call)', async () => {
  let called = false;
  const svc = createDocService({
    directory: { getTeamBySlug: async () => null },
    docClient: {
      ingestDoc: async () => {
        called = true;
        return {};
      },
    },
  });
  const res = await svc.addDoc({ url: 'https://x.com', teamSlug: 'nope' });
  assert.equal(res.outcome, 'TEAM_NOT_FOUND');
  assert.equal(called, false);
});

test('addDoc maps DocBadReference to BAD_REFERENCE', async () => {
  const svc = createDocService({
    directory: {},
    docClient: {
      ingestDoc: async () => {
        throw new DocBadReference('bad');
      },
    },
  });
  const res = await svc.addDoc({ url: 'https://x.com' });
  assert.equal(res.outcome, 'BAD_REFERENCE');
  assert.equal(res.detail, 'bad');
});

test('addDoc maps DocUnavailable to DOC_DOWN and DirectoryUnavailable to DIRECTORY_DOWN', async () => {
  const down = createDocService({
    directory: {},
    docClient: {
      ingestDoc: async () => {
        throw new DocUnavailable('down');
      },
    },
  });
  assert.equal((await down.addDoc({ url: 'https://x.com' })).outcome, 'DOC_DOWN');

  const dirDown = createDocService({
    directory: {
      getTeamBySlug: async () => {
        throw new DirectoryUnavailable('down');
      },
    },
    docClient: {},
  });
  assert.equal(
    (await dirDown.addDoc({ url: 'https://x.com', teamSlug: 'ml' })).outcome,
    'DIRECTORY_DOWN',
  );
});

test('listDocs resolves team slug to id and returns LISTED', async () => {
  const svc = createDocService({
    directory: { getTeamBySlug: async () => ({ id: 't1' }) },
    docClient: {
      listDocs: async (filters) => {
        assert.equal(filters.owningTeamId, 't1');
        assert.equal(filters.tag, 'onboarding');
        assert.equal(filters.activeOnly, true);
        return [{ id: 'd1' }];
      },
    },
  });
  const res = await svc.listDocs({ teamSlug: 'ml', tag: 'onboarding' });
  assert.equal(res.outcome, 'LISTED');
  assert.equal(res.docs.length, 1);
});

test('removeDoc returns REMOVED with doc', async () => {
  const svc = createDocService({
    directory: {},
    docClient: { deactivateDoc: async () => ({ id: 'd1', active: false }) },
  });
  const res = await svc.removeDoc({ id: 'd1' });
  assert.equal(res.outcome, 'REMOVED');
  assert.equal(res.doc.active, false);
});

test('listDocs threads onBehalfOf to the doc client', async () => {
  let received;
  const svc = createDocService({
    directory: {},
    docClient: {
      listDocs: async (args) => {
        received = args;
        return [{ id: 'd1' }];
      },
    },
  });
  const res = await svc.listDocs({ onBehalfOf: 'p1' });
  assert.equal(res.outcome, 'LISTED');
  assert.equal(received.onBehalfOf, 'p1');
});

test('addDoc sets owningPersonId from the caller (visible to the adder)', async () => {
  let payload;
  const svc = createDocService({
    directory: {},
    docClient: {
      ingestDoc: async (p) => {
        payload = p;
        return { doc: { id: 'd1' }, created: true, warnings: [] };
      },
    },
  });
  const res = await svc.addDoc({ url: 'https://x.com', owningPersonId: 'p1' });
  assert.equal(res.outcome, 'ADDED');
  assert.equal(payload.owningPersonId, 'p1');
});

function chunk(docId, ordinal, extra = {}) {
  return {
    doc_id: docId,
    title: `Title ${docId}`,
    url: `https://example.test/${docId}`,
    source_id: 'gdocs',
    text: `chunk ${ordinal} of ${docId}`,
    chunk: { ordinal, start_offset: 0, end_offset: 10, source_content_hash: 'h' },
    score: 0.03,
    ...extra,
  };
}

test('searchDocs asks for SEARCH_CHUNK_K chunks as the caller with a trimmed query', async () => {
  let seen;
  const svc = createDocService({
    directory: {},
    docClient: {
      searchDocs: async (args) => {
        seen = args;
        return [chunk('d1', 0)];
      },
    },
  });
  await svc.searchDocs({ query: '  CPSIF  ', onBehalfOf: 'person-1' });
  assert.deepEqual(seen, { query: 'CPSIF', k: SEARCH_CHUNK_K, onBehalfOf: 'person-1' });
});

test('searchDocs collapses chunks to distinct docs in rank order', async () => {
  const svc = createDocService({
    directory: {},
    docClient: {
      searchDocs: async () => [chunk('d2', 0), chunk('d1', 3), chunk('d2', 1), chunk('d3', 0)],
    },
  });
  const res = await svc.searchDocs({ query: 'CPSIF', onBehalfOf: 'person-1' });
  assert.equal(res.outcome, 'FOUND');
  assert.deepEqual(
    res.docs.map((d) => d.id),
    ['d2', 'd1', 'd3'],
  );
  assert.deepEqual(res.docs[0], {
    id: 'd2',
    title: 'Title d2',
    url: 'https://example.test/d2',
    source_id: 'gdocs',
  });
});

test('searchDocs caps the result at SEARCH_MAX_DOCS docs', async () => {
  const many = Array.from({ length: SEARCH_MAX_DOCS + 3 }, (_, i) => chunk(`d${i}`, 0));
  const svc = createDocService({
    directory: {},
    docClient: { searchDocs: async () => many },
  });
  const res = await svc.searchDocs({ query: 'CPSIF', onBehalfOf: 'person-1' });
  assert.equal(res.docs.length, SEARCH_MAX_DOCS);
  assert.equal(res.docs[0].id, 'd0');
});

test('searchDocs returns NO_MATCH for an empty result', async () => {
  const svc = createDocService({
    directory: {},
    docClient: { searchDocs: async () => [] },
  });
  const res = await svc.searchDocs({ query: 'unrelated galaxies', onBehalfOf: 'person-1' });
  assert.deepEqual(res, { outcome: 'NO_MATCH' });
});

test('searchDocs returns EMPTY_QUERY for a blank query without calling the service', async () => {
  const svc = createDocService({
    directory: {},
    docClient: {
      searchDocs: async () => assert.fail('doc service must not be called'),
    },
  });
  for (const query of ['', '   ', null, undefined]) {
    assert.deepEqual(await svc.searchDocs({ query, onBehalfOf: 'person-1' }), {
      outcome: 'EMPTY_QUERY',
    });
  }
});

test('searchDocs maps DocUnavailable to DOC_DOWN', async () => {
  const svc = createDocService({
    directory: {},
    docClient: {
      searchDocs: async () => {
        throw new DocUnavailable('down');
      },
    },
  });
  const res = await svc.searchDocs({ query: 'CPSIF', onBehalfOf: 'person-1' });
  assert.deepEqual(res, { outcome: 'DOC_DOWN' });
});

test('searchDocs rethrows unexpected errors', async () => {
  const svc = createDocService({
    directory: {},
    docClient: {
      searchDocs: async () => {
        throw new TypeError('bug');
      },
    },
  });
  await assert.rejects(
    () => svc.searchDocs({ query: 'CPSIF', onBehalfOf: 'person-1' }),
    (e) => e instanceof TypeError,
  );
});
