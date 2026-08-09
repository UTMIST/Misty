import { test } from 'node:test';
import assert from 'node:assert/strict';
import doc, { AUTOCOMPLETE_TIMEOUT_MS } from '../src/commands/doc.js';
import { PRINCIPAL_AUTOCOMPLETE_TIMEOUT_MS } from '../src/router.js';

function findSub(name) {
  return doc.subcommands.find((s) => s.name === name);
}

test('doc command is stable with four subcommands', () => {
  assert.equal(doc.name, 'doc');
  assert.equal(doc.beta, false);
  assert.deepEqual(doc.subcommands.map((s) => s.name).sort(), ['add', 'list', 'remove', 'show']);
});

test('add and list have an autocomplete team option; remove is admin', () => {
  const teamOptAdd = findSub('add').options.find((o) => o.name === 'team');
  const teamOptList = findSub('list').options.find((o) => o.name === 'team');
  assert.equal(typeof teamOptAdd.autocomplete, 'function');
  assert.equal(typeof teamOptList.autocomplete, 'function');
  assert.equal(findSub('remove').auth, 'admin');
  assert.equal(findSub('add').auth, 'linked');
});

test('add handler calls docService.addDoc with parsed tags and returns rendered payload', async () => {
  let received;
  const ctx = {
    docService: {
      addDoc: async (args) => {
        received = args;
        return {
          outcome: 'ADDED',
          doc: { title: 'X', url: 'https://x.com', id: 'd1' },
          warnings: [],
        };
      },
    },
  };
  const payload = await findSub('add').handler({
    options: { url: 'https://x.com', title: null, team: 'ml', tags: 'a, b' },
    principal: { person: { id: 'p1' } },
    ctx,
  });
  assert.equal(received.url, 'https://x.com');
  assert.equal(received.teamSlug, 'ml');
  assert.deepEqual(received.tags, ['a', 'b']);
  assert.match(payload.content, /X/);
  assert.equal(payload.ephemeral, true);
});

test('team autocomplete resolver returns caller teams filtered by typed', async () => {
  const resolver = findSub('list').options.find((o) => o.name === 'team').autocomplete;
  const ctx = {
    directory: {
      listMemberships: async ({ personId, activeOnly }) => {
        assert.equal(personId, 'p1');
        assert.equal(activeOnly, true);
        return [{ team_id: 't1' }, { team_id: 't2' }];
      },
      listTeams: async () => [
        { id: 't1', slug: 'ml', label: 'Machine Learning' },
        { id: 't2', slug: 'ops', label: 'Operations' },
        { id: 't3', slug: 'design', label: 'Design' },
      ],
    },
  };
  const out = await resolver({ typed: 'mach', principal: { person: { id: 'p1' } }, ctx });
  assert.deepEqual(out, [{ name: 'Machine Learning', value: 'ml' }]);
});

test('team autocomplete returns [] when principal is null', async () => {
  const resolver = findSub('list').options.find((o) => o.name === 'team').autocomplete;
  const out = await resolver({ typed: '', principal: null, ctx: { directory: {} } });
  assert.deepEqual(out, []);
});

test('teamAutocomplete drops teams with no usable label without crashing', async () => {
  const resolver = findSub('list').options.find((o) => o.name === 'team').autocomplete;
  const ctx = {
    directory: {
      listMemberships: async () => [{ team_id: 't1' }, { team_id: 't2' }],
      listTeams: async () => [
        { id: 't1', slug: 'ml', label: 'Machine Learning' },
        { id: 't2', slug: 'ghost', label: null },
      ],
    },
  };
  const out = await resolver({ typed: '', principal: { person: { id: 'p1' } }, ctx });
  assert.deepEqual(out, [{ name: 'Machine Learning', value: 'ml' }]);
});

test('teamAutocomplete returns [] when directory lookups exceed the budget', async () => {
  const resolver = findSub('list').options.find((o) => o.name === 'team').autocomplete;
  // The directory calls resolve, but only AFTER the (tiny) budget elapses, so
  // the timeout wins the race and the resolver yields []. They still SETTLE
  // (rather than hanging forever) so the test leaves no pending promise for the
  // runner to flag as "Promise resolution is still pending".
  const slow = () => new Promise((r) => setTimeout(() => r([]), 80));
  const ctx = {
    _autocompleteTimeoutMs: 10,
    directory: { listMemberships: slow, listTeams: slow },
  };
  const out = await resolver({ typed: '', principal: { person: { id: 'p1' } }, ctx });
  assert.deepEqual(out, []);
});

test("stacked autocomplete timeouts stay under Discord's 3s window", () => {
  assert.ok(
    PRINCIPAL_AUTOCOMPLETE_TIMEOUT_MS + AUTOCOMPLETE_TIMEOUT_MS <= 2800,
    `principal (${PRINCIPAL_AUTOCOMPLETE_TIMEOUT_MS}) + lookup (${AUTOCOMPLETE_TIMEOUT_MS}) must stay well under 3000ms`,
  );
});

test('list handler passes the caller person id as onBehalfOf', async () => {
  let received;
  const ctx = {
    docService: {
      listDocs: async (args) => {
        received = args;
        return { outcome: 'LISTED', docs: [] };
      },
    },
  };
  await findSub('list').handler({
    options: { team: null, tag: null, source: null },
    principal: { person: { id: 'p1' } },
    ctx,
  });
  assert.equal(received.onBehalfOf, 'p1');
});

test('show handler passes the caller person id as onBehalfOf', async () => {
  let received;
  const ctx = {
    docService: {
      showDoc: async (args) => {
        received = args;
        return { outcome: 'NOT_FOUND' };
      },
    },
  };
  await findSub('show').handler({
    options: { id: 'd1' },
    principal: { person: { id: 'p1' } },
    ctx,
  });
  assert.equal(received.id, 'd1');
  assert.equal(received.onBehalfOf, 'p1');
});

test('list handler omits onBehalfOf when the caller is not linked (fail-closed)', async () => {
  let received;
  const ctx = {
    docService: {
      listDocs: async (args) => {
        received = args;
        return { outcome: 'LISTED', docs: [] };
      },
    },
  };
  await findSub('list').handler({
    options: { team: null, tag: null, source: null },
    principal: null,
    ctx,
  });
  assert.equal(received.onBehalfOf, undefined);
});

test('show handler omits onBehalfOf when the caller is not linked (fail-closed)', async () => {
  let received;
  const ctx = {
    docService: {
      showDoc: async (args) => {
        received = args;
        return { outcome: 'NOT_FOUND' };
      },
    },
  };
  await findSub('show').handler({
    options: { id: 'd1' },
    principal: null,
    ctx,
  });
  assert.equal(received.id, 'd1');
  assert.equal(received.onBehalfOf, undefined);
});

test('add handler owns the doc as the caller (owningPersonId)', async () => {
  let received;
  const ctx = {
    docService: {
      addDoc: async (args) => {
        received = args;
        return {
          outcome: 'ADDED',
          doc: { title: 'X', url: 'https://x.com', id: 'd1' },
          warnings: [],
        };
      },
    },
  };
  await findSub('add').handler({
    options: { url: 'https://x.com', title: null, team: null, tags: null },
    principal: { person: { id: 'p1' } },
    ctx,
  });
  assert.equal(received.owningPersonId, 'p1');
});
