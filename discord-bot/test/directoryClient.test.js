import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  createDirectoryClient,
  DirectoryUnavailable,
  AlreadyLinked,
  PersonExists,
  TeamExists,
  TeamNotFound,
  MembershipInvalid,
  EmailAlreadyRegistered,
} from '../src/directoryClient.js';

function fakeFetch(responses) {
  const calls = [];
  let i = 0;
  const fetchImpl = async (url, opts) => {
    calls.push({ url, opts });
    const r = responses[i++];
    return {
      status: r.status,
      ok: r.ok ?? (r.status >= 200 && r.status < 300),
      json: async () => r.body,
    };
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

const BASE = 'http://d';
const KEY = 'k';

test('getSelfKeyScopes returns scopes from GET /api-keys/self', async () => {
  const fetchImpl = fakeFetch([
    { status: 200, body: { name: 'bot', scopes: ['dev:spoof', 'people:read'] } },
  ]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const scopes = await client.getSelfKeyScopes();
  assert.deepEqual(scopes, ['dev:spoof', 'people:read']);
  assert.match(fetchImpl.calls[0].url, /\/api-keys\/self$/);
  assert.equal(fetchImpl.calls[0].opts.headers['X-API-Key'], 'k');
});

test('getPersonByEmail returns person on 200', async () => {
  const fetchImpl = fakeFetch([{ status: 200, body: { id: '1', display_name: 'Alex' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const person = await client.getPersonByEmail('alex@utmist.ca');
  assert.equal(person.display_name, 'Alex');
  assert.match(fetchImpl.calls[0].url, /\/people\/by-email\/alex%40utmist\.ca$/);
  assert.equal(fetchImpl.calls[0].opts.headers['X-API-Key'], 'k');
});

test('getPersonByEmail returns null on 404', async () => {
  const fetchImpl = fakeFetch([{ status: 404, body: {} }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  assert.equal(await client.getPersonByEmail('x@utmist.ca'), null);
});

test('getPersonByEmail throws DirectoryUnavailable on 500', async () => {
  const fetchImpl = fakeFetch([{ status: 500, body: {} }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(() => client.getPersonByEmail('x@utmist.ca'), DirectoryUnavailable);
});

test('getPersonByDiscordId returns null on 404', async () => {
  const fetchImpl = fakeFetch([{ status: 404, body: {} }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  assert.equal(await client.getPersonByDiscordId('123'), null);
});

test('linkDiscord posts provider=discord and returns body on 201', async () => {
  const fetchImpl = fakeFetch([{ status: 201, body: { provider: 'discord', external_id: '123' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const res = await client.linkDiscord('p1', { externalId: '123', handle: 'alex' });
  assert.equal(res.external_id, '123');
  const { url, opts } = fetchImpl.calls[0];
  assert.match(url, /\/people\/p1\/identifiers$/);
  assert.equal(opts.method, 'POST');
  const sent = JSON.parse(opts.body);
  assert.equal(sent.provider, 'discord');
  assert.equal(sent.external_id, '123');
  assert.equal(sent.handle, 'alex');
});

test('linkDiscord throws AlreadyLinked with detail on 409', async () => {
  const fetchImpl = fakeFetch([{ status: 409, body: { detail: 'person already linked' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.linkDiscord('p1', { externalId: '123', handle: 'alex' }),
    (e) => e instanceof AlreadyLinked && e.detail === 'person already linked',
  );
});

test('getPersonByEmail throws DirectoryUnavailable on malformed 200 body', async () => {
  const fetchImpl = async () => ({
    status: 200,
    ok: true,
    json: async () => {
      throw new SyntaxError('bad');
    },
  });
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(() => client.getPersonByEmail('x@utmist.ca'), DirectoryUnavailable);
});

test('network error becomes DirectoryUnavailable', async () => {
  const fetchImpl = async () => {
    throw new Error('econnrefused');
  };
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(() => client.getPersonByEmail('x@utmist.ca'), DirectoryUnavailable);
});

test('createPerson posts fields and returns body on 201', async () => {
  const fetchImpl = fakeFetch([
    { status: 201, body: { id: 'p1', display_name: 'A', access_level: 'admin' } },
  ]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const person = await client.createPerson({
    displayName: 'A',
    primaryEmail: 'a@utmist.ca',
    accessLevel: 'admin',
  });
  assert.equal(person.id, 'p1');
  const { url, opts } = fetchImpl.calls[0];
  assert.match(url, /\/people$/);
  assert.equal(opts.method, 'POST');
  const sent = JSON.parse(opts.body);
  assert.equal(sent.display_name, 'A');
  assert.equal(sent.primary_email, 'a@utmist.ca');
  assert.equal(sent.access_level, 'admin');
});

test('createPerson throws PersonExists on 409', async () => {
  const fetchImpl = fakeFetch([{ status: 409, body: { detail: 'primary_email already exists' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () =>
      client.createPerson({ displayName: 'A', primaryEmail: 'a@utmist.ca', accessLevel: 'member' }),
    (e) => e instanceof PersonExists && e.detail === 'primary_email already exists',
  );
});

test('createPerson throws DirectoryUnavailable on 500', async () => {
  const fetchImpl = fakeFetch([{ status: 500, body: {} }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () =>
      client.createPerson({ displayName: 'A', primaryEmail: 'a@utmist.ca', accessLevel: 'member' }),
    DirectoryUnavailable,
  );
});

test('listIdentifiers returns array on 200 and uses X-API-Key', async () => {
  const rows = [
    { provider: 'discord', external_id: '123', handle: 'alex' },
    { provider: 'github', external_id: '9876', handle: 'eeetan' },
  ];
  const fetchImpl = fakeFetch([{ status: 200, body: rows }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const result = await client.listIdentifiers('p1');
  assert.deepEqual(result, rows);
  const { url, opts } = fetchImpl.calls[0];
  assert.match(url, /\/people\/p1\/identifiers$/);
  assert.equal(opts.headers['X-API-Key'], 'k');
});

test('listIdentifiers returns [] on 404 (defensive)', async () => {
  const fetchImpl = fakeFetch([{ status: 404, body: {} }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  assert.deepEqual(await client.listIdentifiers('p1'), []);
});

test('listIdentifiers throws DirectoryUnavailable on 500', async () => {
  const fetchImpl = fakeFetch([{ status: 500, body: {} }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(() => client.listIdentifiers('p1'), DirectoryUnavailable);
});

test('listIdentifiers network error becomes DirectoryUnavailable', async () => {
  const fetchImpl = async () => {
    throw new Error('econnrefused');
  };
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(() => client.listIdentifiers('p1'), DirectoryUnavailable);
});

test('listTeams returns array on 200 and passes active_only', async () => {
  const fetchImpl = fakeFetch([{ status: 200, body: [{ id: 't1', slug: 'ml', label: 'ML' }] }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const teams = await client.listTeams({ activeOnly: true });
  assert.equal(teams.length, 1);
  assert.match(fetchImpl.calls[0].url, /\/teams\?active_only=true$/);
});

test('listTeams omits query string when activeOnly is false or undefined', async () => {
  const fetchImpl = fakeFetch([{ status: 200, body: [] }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await client.listTeams();
  assert.match(fetchImpl.calls[0].url, /\/teams$/);
});

test('listTeams throws DirectoryUnavailable on 500', async () => {
  const fetchImpl = fakeFetch([{ status: 500, body: {} }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(() => client.listTeams(), DirectoryUnavailable);
});

test('getTeamBySlug returns team on 200', async () => {
  const fetchImpl = fakeFetch([{ status: 200, body: { id: 't1', slug: 'ml', label: 'ML' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const team = await client.getTeamBySlug('ml');
  assert.equal(team.id, 't1');
  assert.match(fetchImpl.calls[0].url, /\/teams\/by-slug\/ml$/);
});

test('getTeamBySlug returns null on 404', async () => {
  const fetchImpl = fakeFetch([{ status: 404, body: {} }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  assert.equal(await client.getTeamBySlug('missing'), null);
});

test('getTeam returns team on 200 and null on 404', async () => {
  const ok = fakeFetch([{ status: 200, body: { id: 't1', slug: 'ml', label: 'ML' } }]);
  const okClient = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl: ok });
  assert.equal((await okClient.getTeam('t1')).id, 't1');
  assert.match(ok.calls[0].url, /\/teams\/t1$/);
  const miss = fakeFetch([{ status: 404, body: {} }]);
  const missClient = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl: miss });
  assert.equal(await missClient.getTeam('t1'), null);
});

test('getPerson returns person on 200 and null on 404', async () => {
  const ok = fakeFetch([{ status: 200, body: { id: 'p1', display_name: 'A' } }]);
  const okClient = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl: ok });
  assert.equal((await okClient.getPerson('p1')).display_name, 'A');
  assert.match(ok.calls[0].url, /\/people\/p1$/);
  const miss = fakeFetch([{ status: 404, body: {} }]);
  const missClient = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl: miss });
  assert.equal(await missClient.getPerson('p1'), null);
});

test('createTeam posts fields and returns body on 201', async () => {
  const fetchImpl = fakeFetch([{ status: 201, body: { id: 't1', slug: 'ml', label: 'ML' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const team = await client.createTeam({
    slug: 'ml',
    label: 'ML',
    description: 'Machine Learning',
  });
  assert.equal(team.id, 't1');
  const { url, opts } = fetchImpl.calls[0];
  assert.match(url, /\/teams$/);
  assert.equal(opts.method, 'POST');
  const sent = JSON.parse(opts.body);
  assert.equal(sent.slug, 'ml');
  assert.equal(sent.label, 'ML');
  assert.equal(sent.description, 'Machine Learning');
});

test('createTeam throws TeamExists on 409', async () => {
  const fetchImpl = fakeFetch([{ status: 409, body: { detail: 'slug already exists' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.createTeam({ slug: 'ml', label: 'ML' }),
    (e) => e instanceof TeamExists && e.detail === 'slug already exists',
  );
});

test('createTeam throws DirectoryUnavailable on 500', async () => {
  const fetchImpl = fakeFetch([{ status: 500, body: {} }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(() => client.createTeam({ slug: 'ml', label: 'ML' }), DirectoryUnavailable);
});

test('updateTeam patches and returns on 200', async () => {
  const fetchImpl = fakeFetch([{ status: 200, body: { id: 't1', label: 'New Label' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const t = await client.updateTeam('t1', { label: 'New Label' });
  assert.equal(t.label, 'New Label');
  const { url, opts } = fetchImpl.calls[0];
  assert.match(url, /\/teams\/t1$/);
  assert.equal(opts.method, 'PATCH');
  assert.deepEqual(JSON.parse(opts.body), { label: 'New Label' });
});

test('updateTeam throws TeamNotFound on 404', async () => {
  const fetchImpl = fakeFetch([{ status: 404, body: { detail: 'team not found' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.updateTeam('t1', { label: 'x' }),
    (e) => e instanceof TeamNotFound,
  );
});

test('updateTeam throws TeamExists on 409', async () => {
  const fetchImpl = fakeFetch([{ status: 409, body: { detail: 'slug already exists' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.updateTeam('t1', { slug: 'other' }),
    (e) => e instanceof TeamExists,
  );
});

test('createMembership posts fields and returns body on 201', async () => {
  const fetchImpl = fakeFetch([
    { status: 201, body: { id: 'm1', person_id: 'p1', team_id: 't1' } },
  ]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const m = await client.createMembership({
    personId: 'p1',
    teamId: 't1',
    roleKindId: 'lead',
    isTeamAdmin: true,
  });
  assert.equal(m.id, 'm1');
  const { url, opts } = fetchImpl.calls[0];
  assert.match(url, /\/memberships$/);
  assert.equal(opts.method, 'POST');
  const sent = JSON.parse(opts.body);
  assert.equal(sent.person_id, 'p1');
  assert.equal(sent.team_id, 't1');
  assert.equal(sent.role_kind_id, 'lead');
  assert.equal(sent.is_team_admin, true);
});

test('createMembership omits optional fields when not provided', async () => {
  const fetchImpl = fakeFetch([{ status: 201, body: { id: 'm1' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await client.createMembership({ personId: 'p1', teamId: 't1' });
  const sent = JSON.parse(fetchImpl.calls[0].opts.body);
  assert.equal('role_kind_id' in sent, false);
  assert.equal('is_team_admin' in sent, false);
});

test('createMembership throws MembershipInvalid on 400', async () => {
  const fetchImpl = fakeFetch([
    { status: 400, body: { detail: 'active membership already exists' } },
  ]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(
    () => client.createMembership({ personId: 'p1', teamId: 't1' }),
    (e) => e instanceof MembershipInvalid && e.detail === 'active membership already exists',
  );
});

test('listMemberships builds query string from provided filters only', async () => {
  const fetchImpl = fakeFetch([{ status: 200, body: [] }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await client.listMemberships({ teamId: 't1', personId: 'p1', activeOnly: true });
  const url = fetchImpl.calls[0].url;
  assert.match(url, /team_id=t1/);
  assert.match(url, /person_id=p1/);
  assert.match(url, /active_only=true/);
});

test('listMemberships with no filters hits base path', async () => {
  const fetchImpl = fakeFetch([{ status: 200, body: [] }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await client.listMemberships();
  assert.match(fetchImpl.calls[0].url, /\/memberships$/);
});

test('listMemberships passes as_of and is_team_admin', async () => {
  const fetchImpl = fakeFetch([{ status: 200, body: [] }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await client.listMemberships({ asOf: '2026-07-01', isTeamAdmin: true });
  assert.match(fetchImpl.calls[0].url, /as_of=2026-07-01/);
  assert.match(fetchImpl.calls[0].url, /is_team_admin=true/);
});

test('endMembership posts ended_at and returns body on 200', async () => {
  const fetchImpl = fakeFetch([{ status: 200, body: { id: 'm1', ended_at: '2026-07-01' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const m = await client.endMembership('m1', '2026-07-01');
  assert.equal(m.ended_at, '2026-07-01');
  const { url, opts } = fetchImpl.calls[0];
  assert.match(url, /\/memberships\/m1\/end$/);
  assert.equal(opts.method, 'POST');
  assert.deepEqual(JSON.parse(opts.body), { ended_at: '2026-07-01' });
});

test('endMembership returns null on 404', async () => {
  const fetchImpl = fakeFetch([{ status: 404, body: {} }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  assert.equal(await client.endMembership('m1', '2026-07-01'), null);
});

test('directoryClient.listPeople returns the /people list', async () => {
  const fetchImpl = fakeFetch([
    {
      status: 200,
      body: [
        {
          id: 'p1',
          display_name: 'Alex',
          primary_email: 'a@x',
          access_level: 'member',
          active: true,
        },
        {
          id: 'p2',
          display_name: 'Bea',
          primary_email: 'b@x',
          access_level: 'admin',
          active: true,
        },
      ],
    },
  ]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const people = await client.listPeople();
  assert.equal(people.length, 2);
  assert.equal(people[0].display_name, 'Alex');
  assert.match(fetchImpl.calls[0].url, /\/people$/);
  assert.equal(fetchImpl.calls[0].opts.headers['X-API-Key'], 'k');
});

test('addEmailIdentifier returns the identifier on 201', async () => {
  const fetchImpl = fakeFetch([
    { status: 201, body: { id: '1', provider: 'email', external_id: 'a@b.com' } },
  ]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  const r = await client.addEmailIdentifier('p1', 'a@b.com');
  assert.equal(r.external_id, 'a@b.com');
  assert.match(fetchImpl.calls[0].url, /\/people\/p1\/emails$/);
  assert.equal(fetchImpl.calls[0].opts.method, 'POST');
  assert.deepEqual(JSON.parse(fetchImpl.calls[0].opts.body), { email: 'a@b.com' });
});

test('addEmailIdentifier throws EmailAlreadyRegistered on 409', async () => {
  const fetchImpl = fakeFetch([{ status: 409, body: { detail: 'email_registered_to_another' } }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(() => client.addEmailIdentifier('p1', 'a@b.com'), EmailAlreadyRegistered);
});

test('addEmailIdentifier throws DirectoryUnavailable on 500', async () => {
  const fetchImpl = fakeFetch([{ status: 500, body: {} }]);
  const client = createDirectoryClient({ baseUrl: BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(() => client.addEmailIdentifier('p1', 'a@b.com'), DirectoryUnavailable);
});
