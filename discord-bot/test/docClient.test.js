import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDocClient, DocUnavailable, DocBadReference } from '../src/docClient.js';

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
