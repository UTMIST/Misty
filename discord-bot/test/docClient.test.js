import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDocClient, DocUnavailable, DocBadReference } from '../src/clients/docClient.js';

function fakeFetch(responder) {
  return async (url, options) => responder(url, options);
}
function jsonResponse(status, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  };
}

const searchHit = {
  doc_id: 'd1',
  title: 'CPSIF',
  url: 'https://x.test/d1',
  text: 'CPSIF notes',
  chunk: { ordinal: 0, start_offset: 0, end_offset: 11, source_content_hash: 'h' },
  score: 0.5,
};

test('searchDocs (person) posts to /search with X-On-Behalf-Of and {query,k}', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async (url, options) => {
      assert.equal(url, 'http://doc/search');
      assert.equal(options.method, 'POST');
      assert.equal(options.headers['X-On-Behalf-Of'], 'person-1');
      assert.equal('X-Guild-ID' in options.headers, false);
      assert.deepEqual(JSON.parse(options.body), { query: 'cpsif', k: 5 });
      return jsonResponse(200, { results: [searchHit] });
    }),
  });
  const results = await client.searchDocs({ query: 'cpsif', k: 5, onBehalfOf: 'person-1' });
  assert.deepEqual(results, [searchHit]);
});

test('searchDocs (channel) sends X-Guild-ID and X-Channel-ID, not an actor', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async (url, options) => {
      assert.equal(options.headers['X-Guild-ID'], '100');
      assert.equal(options.headers['X-Channel-ID'], '200');
      assert.equal('X-On-Behalf-Of' in options.headers, false);
      return jsonResponse(200, { results: [] });
    }),
  });
  const results = await client.searchDocs({ query: 'q', guildId: '100', channelId: '200' });
  assert.deepEqual(results, []);
});

test('searchDocs omits k when not given so the service default applies', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async (_url, options) => {
      assert.deepEqual(JSON.parse(options.body), { query: 'q' });
      return jsonResponse(200, { results: [] });
    }),
  });
  await client.searchDocs({ query: 'q', onBehalfOf: 'p' });
});

test('searchDocs fails closed before any request when context is missing', async () => {
  let called = false;
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async () => {
      called = true;
      return jsonResponse(200, { results: [] });
    }),
  });
  await assert.rejects(() => client.searchDocs({ query: 'q' }), DocUnavailable);
  assert.equal(called, false);
});

test('searchDocs fails closed when both person and channel context are given', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async () => jsonResponse(200, { results: [] })),
  });
  await assert.rejects(
    () => client.searchDocs({ query: 'q', onBehalfOf: 'p', guildId: '1', channelId: '2' }),
    DocUnavailable,
  );
});

test('searchDocs fails closed on a partial channel context (guild without channel)', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async () => jsonResponse(200, { results: [] })),
  });
  await assert.rejects(() => client.searchDocs({ query: 'q', guildId: '100' }), DocUnavailable);
});

test('searchDocs throws DocUnavailable on a non-ok status', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async () => jsonResponse(503, { detail: 'scope unavailable' })),
  });
  await assert.rejects(() => client.searchDocs({ query: 'q', onBehalfOf: 'p' }), DocUnavailable);
});

test('searchDocs rejects a body without a results array', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async () => jsonResponse(200, { oops: true })),
  });
  await assert.rejects(() => client.searchDocs({ query: 'q', onBehalfOf: 'p' }), DocUnavailable);
});

test('ingestDoc posts to /docs and returns IngestResult (created)', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async (url, options) => {
      assert.equal(url, 'http://doc/docs');
      assert.equal(options.method, 'POST');
      assert.equal(options.headers['X-API-Key'], 'k');
      const body = JSON.parse(options.body);
      assert.equal(body.url, 'https://x.com');
      assert.deepEqual(body.tags, ['onboarding']);
      assert.equal(body.owning_team_id, 't1');
      return jsonResponse(201, {
        doc: { id: 'd1', url: 'https://x.com' },
        created: true,
        warnings: [],
      });
    }),
  });
  const res = await client.ingestDoc({
    url: 'https://x.com',
    owningTeamId: 't1',
    tags: ['onboarding'],
  });
  assert.equal(res.created, true);
  assert.equal(res.doc.id, 'd1');
});

test('ingestDoc throws DocBadReference on 400', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async () => jsonResponse(400, { detail: 'owning_team_id not found' })),
  });
  await assert.rejects(
    () => client.ingestDoc({ url: 'https://x.com' }),
    (e) => {
      assert.ok(e instanceof DocBadReference);
      assert.equal(e.detail, 'owning_team_id not found');
      return true;
    },
  );
});

test('ingestDoc throws DocUnavailable on network error', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: async () => {
      throw new Error('econnrefused');
    },
  });
  await assert.rejects(
    () => client.ingestDoc({ url: 'https://x.com' }),
    (e) => e instanceof DocUnavailable,
  );
});

test('listDocs builds query string and returns array', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async (url) => {
      assert.ok(url.startsWith('http://doc/docs?'));
      assert.ok(url.includes('owning_team_id=t1'));
      assert.ok(url.includes('tag=onboarding'));
      assert.ok(url.includes('active_only=true'));
      return jsonResponse(200, [{ id: 'd1' }]);
    }),
  });
  const docs = await client.listDocs({ owningTeamId: 't1', tag: 'onboarding', activeOnly: true });
  assert.equal(docs.length, 1);
});

test('getDoc returns null on 404', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async () => jsonResponse(404, { detail: 'doc not found' })),
  });
  assert.equal(await client.getDoc('nope'), null);
});

test('listDocs throws DocUnavailable and warns on 401 (bad API key)', async () => {
  const warnings = [];
  const origWarn = console.warn;
  console.warn = (msg) => warnings.push(msg);
  try {
    const client = createDocClient({
      baseUrl: 'http://doc',
      apiKey: 'bad',
      fetchImpl: async () => ({
        status: 401,
        ok: false,
        json: async () => ({ detail: 'invalid key' }),
      }),
    });
    await assert.rejects(
      () => client.listDocs({}),
      (e) => e instanceof DocUnavailable,
    );
    assert.ok(
      warnings.some((w) => w.includes('401')),
      'a warning includes the 401 status',
    );
  } finally {
    console.warn = origWarn;
  }
});

test('deactivateDoc patches active:false and returns doc', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async (url, options) => {
      assert.equal(url, 'http://doc/docs/d1');
      assert.equal(options.method, 'PATCH');
      assert.deepEqual(JSON.parse(options.body), { active: false });
      return jsonResponse(200, { id: 'd1', active: false });
    }),
  });
  const doc = await client.deactivateDoc('d1');
  assert.equal(doc.active, false);
});

test('listDocs sends X-On-Behalf-Of when onBehalfOf is provided', async () => {
  let seen;
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async (url, options) => {
      seen = options.headers;
      return jsonResponse(200, []);
    }),
  });
  await client.listDocs({ onBehalfOf: 'person-1' });
  assert.equal(seen['X-On-Behalf-Of'], 'person-1');
  assert.equal(seen['X-API-Key'], 'k');
});

test('listDocs omits X-On-Behalf-Of when no actor is given', async () => {
  let seen;
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async (url, options) => {
      seen = options.headers;
      return jsonResponse(200, []);
    }),
  });
  await client.listDocs({});
  assert.equal('X-On-Behalf-Of' in seen, false);
});

test('getDoc sends X-On-Behalf-Of when onBehalfOf is provided', async () => {
  let seen;
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async (url, options) => {
      seen = options.headers;
      return jsonResponse(200, { id: 'd1' });
    }),
  });
  await client.getDoc('d1', { onBehalfOf: 'person-1' });
  assert.equal(seen['X-On-Behalf-Of'], 'person-1');
});

test('getDoc omits X-On-Behalf-Of when no actor is given', async () => {
  let seen;
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async (url, options) => {
      seen = options.headers;
      return jsonResponse(200, { id: 'd1' });
    }),
  });
  await client.getDoc('d1', {});
  assert.equal('X-On-Behalf-Of' in seen, false);
});
