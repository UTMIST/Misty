import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDocService, llmSafe } from '../src/docService.js';
import { DocUnavailable, DocBadReference } from '../src/docClient.js';
import { DirectoryUnavailable } from '../src/directoryClient.js';

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

test('showDoc returns NOT_FOUND when null', async () => {
  const svc = createDocService({ directory: {}, docClient: { getDoc: async () => null } });
  assert.equal((await svc.showDoc({ id: 'd1' })).outcome, 'NOT_FOUND');
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

test('showDoc threads onBehalfOf to the doc client', async () => {
  let received;
  const svc = createDocService({
    directory: {},
    docClient: {
      getDoc: async (id, opts) => {
        received = { id, opts };
        return { id };
      },
    },
  });
  const res = await svc.showDoc({ id: 'd1', onBehalfOf: 'p1' });
  assert.equal(res.outcome, 'SHOWN');
  assert.equal(received.id, 'd1');
  assert.equal(received.opts.onBehalfOf, 'p1');
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
