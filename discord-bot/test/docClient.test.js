import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
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

// --- searchDocs: replays the shared retrieval contract fixture (#176) so the
// client is tested against the documented request/response shapes rather than
// a bot-specific invention. Channel cases belong to the helper (#71).
const retrievalFixture = JSON.parse(
  readFileSync(
    fileURLToPath(
      new URL('../../services/documentation-system/docs/fixtures/retrieval.json', import.meta.url),
    ),
    'utf8',
  ),
);

function fixtureCase(name) {
  const found = retrievalFixture.cases.find((c) => c.name === name);
  assert.ok(found, `fixture case ${name} exists`);
  return found;
}

// A client whose fetch asserts the request matches the fixture case exactly,
// then answers with that case's recorded response.
function replayClient(fixture) {
  const { request, response } = fixture;
  return createDocClient({
    baseUrl: 'http://doc',
    apiKey: request.headers['X-API-Key'],
    fetchImpl: fakeFetch(async (url, options) => {
      assert.equal(url, `http://doc${request.path}`);
      assert.equal(options.method, request.method);
      assert.deepEqual(JSON.parse(options.body), request.json);
      for (const [name, value] of Object.entries(request.headers)) {
        assert.equal(options.headers[name], value, `header ${name}`);
      }
      if (!('X-On-Behalf-Of' in request.headers)) {
        assert.equal('X-On-Behalf-Of' in options.headers, false);
      }
      return jsonResponse(response.status, response.json);
    }),
  });
}

function argsFor(fixture) {
  const { headers, json } = fixture.request;
  return { query: json.query, k: json.k, onBehalfOf: headers['X-On-Behalf-Of'] };
}

async function withSilencedWarn(fn) {
  const warnings = [];
  const origWarn = console.warn;
  console.warn = (msg) => warnings.push(msg);
  try {
    await fn();
  } finally {
    console.warn = origWarn;
  }
  return warnings;
}

for (const name of ['alice', 'bob', 'filter-before-rank', 'empty-relevance']) {
  test(`searchDocs replays fixture case ${name} and returns its results`, async () => {
    const fixture = fixtureCase(name);
    const results = await replayClient(fixture).searchDocs(argsFor(fixture));
    assert.deepEqual(results, fixture.response.json.results);
  });
}

test('searchDocs returns different results for people with different access', async () => {
  const alice = await replayClient(fixtureCase('alice')).searchDocs(argsFor(fixtureCase('alice')));
  const bob = await replayClient(fixtureCase('bob')).searchDocs(argsFor(fixtureCase('bob')));
  const aliceIds = new Set(alice.map((r) => r.doc_id));
  assert.ok(alice.some((r) => r.title === 'Personal CPSIF notes'));
  assert.ok(bob.every((r) => r.title !== 'Personal CPSIF notes'));
  assert.ok(bob.some((r) => !aliceIds.has(r.doc_id)));
});

for (const name of ['scope-unavailable', 'embedding-error', 'denied-person', 'missing-context']) {
  test(`searchDocs throws DocUnavailable and warns on fixture case ${name}`, async () => {
    const fixture = fixtureCase(name);
    const warnings = await withSilencedWarn(async () => {
      await assert.rejects(
        () => replayClient(fixture).searchDocs(argsFor(fixture)),
        (e) => e instanceof DocUnavailable,
      );
    });
    assert.ok(
      warnings.some((w) => w.includes(String(fixture.response.status))),
      'a warning includes the HTTP status',
    );
  });
}

test('searchDocs omits k when not given so the service default applies', async () => {
  let body;
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async (url, options) => {
      body = JSON.parse(options.body);
      return jsonResponse(200, { results: [] });
    }),
  });
  await client.searchDocs({ query: 'CPSIF', onBehalfOf: 'person-1' });
  assert.deepEqual(body, { query: 'CPSIF' });
});

test('searchDocs throws DocUnavailable on network error', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: async () => {
      throw new Error('econnrefused');
    },
  });
  await assert.rejects(
    () => client.searchDocs({ query: 'CPSIF', onBehalfOf: 'person-1' }),
    (e) => e instanceof DocUnavailable,
  );
});

test('searchDocs throws DocUnavailable when the body has no results array', async () => {
  const client = createDocClient({
    baseUrl: 'http://doc',
    apiKey: 'k',
    fetchImpl: fakeFetch(async () => jsonResponse(200, { hits: [] })),
  });
  await assert.rejects(
    () => client.searchDocs({ query: 'CPSIF', onBehalfOf: 'person-1' }),
    (e) => e instanceof DocUnavailable,
  );
});
