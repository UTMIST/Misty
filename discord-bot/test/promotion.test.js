import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createAppContext } from '../src/context.js';
import { createPromotionService } from '../src/services/promotionService.js';
import { promotionDecision } from '../src/auth/policy.js';
import { combineHelperActions } from '../src/helperActions.js';
import { dispatch } from '../src/router.js';
import { commands } from '../src/commands/index.js';
import { renderPromoteResult } from '../src/messages/people.js';
import {
  createDirectoryClient,
  AccessLevelChangeForbidden,
  DirectoryUnavailable,
} from '../src/clients/directoryClient.js';

const LEVELS = ['member', 'admin', 'superuser'];

// The confirmed #361 table, written out rather than derived from ACCESS_RANK.
const ALLOWED = new Set([
  'admin/member/admin',
  'superuser/member/admin',
  'superuser/member/superuser',
  'superuser/admin/superuser',
]);

function fixture({ callerLevel = 'admin', targetLevel = 'member' } = {}) {
  const ctx = createAppContext({ helperToolsEnabled: true });
  const caller = {
    id: 'caller',
    display_name: 'Casey',
    primary_email: 'casey@example.org',
    active: true,
    access_level: callerLevel,
    updated_at: 'before',
  };
  const target = {
    id: 'p',
    display_name: 'Alex',
    primary_email: 'alex@example.org',
    active: true,
    access_level: targetLevel,
    updated_at: 'before',
  };
  const other = { ...target, id: 'q', display_name: 'Blair', primary_email: 'blair@example.org' };
  const people = { caller, p: target, q: other };
  const links = { 111: 'caller', 222: 'p', 333: 'q' };
  const writes = [];
  let reads = 0;
  ctx.directory = {
    getPersonByDiscordId: async (id) => (reads++, people[links[id]] ?? null),
    getPerson: async (id) => (reads++, people[id] ?? null),
    searchPeople: async ({ name, email }) => {
      reads++;
      const matches = Object.values(people).filter(
        (p) =>
          (!name || p.display_name.toLowerCase() === name) && (!email || p.primary_email === email),
      );
      return { matches, has_more: false, needs_disambiguation: false };
    },
    updatePersonAccessLevel: async (id, level) => {
      writes.push({ id, level });
      Object.assign(people[id], { access_level: level, updated_at: 'after' });
      return { ...people[id] };
    },
    updateMembership: async () => assert.fail('must not change team memberships'),
    createPerson: async () => assert.fail('must not create people'),
  };
  ctx.promotionService = createPromotionService({ directory: ctx.directory });
  return {
    ctx,
    caller,
    target,
    people,
    links,
    writes,
    reads: () => reads,
    prepare: (input, { discordUserId = '111', person = caller } = {}) =>
      ctx.toolRegistry.handlers.get('promote')(
        { user: { name: 'Alex' }, level: 'admin', ...input },
        { principal: { person: { ...person } }, request: { discordUserId } },
      ),
    slash: (options, surface = 'discord') =>
      dispatch(
        {
          surface,
          commandName: 'promote',
          discordUserId: '111',
          options: { user: { id: '222' }, level: 'admin', ...options },
        },
        { commands, appContext: ctx },
      ),
  };
}

test('promotion policy matches the confirmed table for every level combination', () => {
  let cases = 0;
  for (const callerLevel of LEVELS)
    for (const targetLevel of LEVELS)
      for (const requestedLevel of LEVELS) {
        const decision = promotionDecision({ callerLevel, targetLevel, requestedLevel });
        assert.equal(
          decision === 'ALLOWED',
          ALLOWED.has(`${callerLevel}/${targetLevel}/${requestedLevel}`),
          `${callerLevel} promoting ${targetLevel} to ${requestedLevel}: ${decision}`,
        );
        cases++;
      }
  assert.equal(cases, 27);
  assert.equal(
    promotionDecision({ callerLevel: 'superuser', targetLevel: 'admin', requestedLevel: 'admin' }),
    'UNCHANGED',
  );
  assert.equal(
    promotionDecision({ callerLevel: 'admin', targetLevel: 'admin', requestedLevel: 'admin' }),
    'TARGET_NOT_LOWER',
  );
});

test('unknown levels are rejected rather than treated as member', () => {
  for (const bad of [
    'owner',
    '',
    undefined,
    null,
    'constructor',
    '__proto__',
    'toString',
    'Admin',
    ['admin'],
    new String('admin'),
  ]) {
    for (const levels of [
      { callerLevel: bad, targetLevel: 'member', requestedLevel: 'admin' },
      { callerLevel: 'superuser', targetLevel: bad, requestedLevel: 'admin' },
      { callerLevel: 'superuser', targetLevel: 'member', requestedLevel: bad },
    ]) {
      assert.equal(promotionDecision(levels), 'INVALID_LEVEL');
    }
  }
});

for (const callerLevel of LEVELS)
  for (const targetLevel of LEVELS)
    for (const level of ['admin', 'superuser']) {
      const allowed = ALLOWED.has(`${callerLevel}/${targetLevel}/${level}`);
      test(`helper: ${callerLevel} promoting ${targetLevel} to ${level} is ${allowed ? 'proposed' : 'refused'}`, async () => {
        const f = fixture({ callerLevel, targetLevel });
        const result = await f.prepare({ level });
        assert.equal(f.writes.length, 0);
        if (!allowed) {
          assert.equal(result.is_error, true);
          assert.equal(result.action, undefined);
          assert.doesNotMatch(JSON.stringify(result), /alex@example\.org/);
          return;
        }
        assert.ok(result.action.confirm);
        assert.equal(result.content.status, 'confirmation_required');
        assert.match(result.action.summary, new RegExp(`${targetLevel} → ${level}`));
        assert.match(result.action.summary, /Team roles and team-admin flags are unchanged/);
        const reply = await result.action.execute();
        assert.match(reply.content, /Promoted/);
        assert.deepEqual(f.writes, [{ id: 'p', level }]);
      });

      test(`slash: ${callerLevel} promoting ${targetLevel} to ${level} is ${allowed ? 'applied' : 'denied'}`, async () => {
        const f = fixture({ callerLevel, targetLevel });
        const reply = await f.slash({ level });
        assert.equal(reply.ephemeral, true);
        if (allowed) {
          assert.match(reply.content, /Promoted \*\*Alex\*\*/);
          assert.deepEqual(f.writes, [{ id: 'p', level }]);
        } else {
          assert.doesNotMatch(reply.content, /Promoted/);
          assert.equal(f.writes.length, 0);
        }
        assert.equal(f.caller.access_level, callerLevel);
      });
    }

test('self-promotion is refused on both paths', async () => {
  const f = fixture({ callerLevel: 'admin' });
  f.ctx.directory.searchPeople = async () => ({
    matches: [f.caller],
    has_more: false,
    needs_disambiguation: false,
  });
  assert.equal((await f.prepare({ level: 'superuser' })).is_error, true);
  assert.match((await f.slash({ user: { id: '111' }, level: 'superuser' })).content, /your own/);
  assert.equal(f.writes.length, 0);
});

test('missing, ambiguous, unlinked and inactive targets are distinguished without writes', async () => {
  const f = fixture({ callerLevel: 'superuser' });
  assert.match((await f.slash({ user: { id: '999' } })).content, /not linked/);
  f.ctx.directory.searchPeople = async () => ({
    matches: [],
    has_more: false,
    needs_disambiguation: false,
  });
  assert.match((await f.prepare()).content.error, /No active directory member/);
  f.ctx.directory.searchPeople = async () => ({
    matches: [f.target, { ...f.target, id: 'p2' }],
    has_more: false,
    needs_disambiguation: true,
  });
  assert.match((await f.prepare()).content.error, /More detail/);
  f.target.active = false;
  assert.match((await f.slash()).content, /inactive/);
  assert.equal(f.writes.length, 0);
});

test('no-op promotions are reported as unchanged, not denied or successful', async () => {
  const f = fixture({ callerLevel: 'superuser', targetLevel: 'admin' });
  assert.match((await f.prepare({ level: 'admin' })).content.error, /already has/);
  assert.match((await f.slash({ level: 'admin' })).content, /already has/);
  assert.equal(f.writes.length, 0);
});

test('invalid or forged arguments fail before any directory read', async () => {
  const f = fixture({ callerLevel: 'superuser' });
  const reads = f.reads();
  for (const input of [
    { level: 'member' },
    { level: 'owner' },
    { level: 'Admin' },
    { level: ['admin'] },
    { level: 1 },
    { level: undefined },
    { level: 'admin', caller_level: 'superuser' },
    { level: 'admin', actor: 'someone-else' },
    { user: { name: 'Alex', access_level: 'member' } },
    { user: { id: 'p' } },
    { user: { personId: 'p' } },
    { user: [{ name: 'Alex' }, { name: 'Blair' }] },
    { user: { name: '   ' } },
    { user: { name: 'a'.repeat(201) } },
    { user: { name: 'Al\0ex' } },
    { user: '1'.repeat(21) },
    { user: '<@222>' },
    { pad: 'x'.repeat(5000) },
  ]) {
    assert.equal((await f.prepare(input)).is_error, true, JSON.stringify(input));
  }
  assert.equal(f.reads(), reads);
  assert.equal(f.writes.length, 0);
  const definition = f.ctx.toolRegistry.definitions.find((d) => d.name === 'promote');
  assert.deepEqual(definition.input_schema.properties.level.enum, ['admin', 'superuser']);
});

test('a stale principal claiming a higher level is replaced by the fresh one', async () => {
  const f = fixture({ callerLevel: 'admin' });
  const forged = { ...f.caller, access_level: 'superuser' };
  assert.equal((await f.prepare({ level: 'superuser' }, { person: forged })).is_error, true);
  assert.equal(f.writes.length, 0);
});

test('denials do not reveal whether an equal or higher target is admin, superuser or self', async () => {
  const f = fixture({ callerLevel: 'admin', targetLevel: 'admin' });
  const onAdmin = (await f.prepare()).content.error;
  f.target.access_level = 'superuser';
  assert.equal((await f.prepare()).content.error, onAdmin);
  assert.equal((await f.prepare({ user: '111' })).content.error, onAdmin);
  assert.doesNotMatch(onAdmin, /example\.org/);
});

test('forged web options cannot bypass the policy', async () => {
  const f = fixture({ callerLevel: 'admin', targetLevel: 'superuser' });
  const stale = { ...f.target, access_level: 'member' };
  assert.doesNotMatch((await f.slash({ expectedPerson: stale }, 'web')).content, /Promoted/);
  f.target.access_level = 'member';
  assert.match((await f.slash({ user: { personId: 'caller' } }, 'web')).content, /your own/);
  assert.doesNotMatch((await f.slash({ level: ['admin'] }, 'web')).content, /Promoted/);
  assert.equal(f.writes.length, 0);
});

for (const [label, change] of [
  ['target level changed', (f) => (f.target.access_level = 'admin')],
  ['target deactivated', (f) => (f.target.active = false)],
  ['target record edited', (f) => (f.target.updated_at = 'edited')],
  ['target email changed', (f) => (f.target.primary_email = 'other@example.org')],
  ['caller demoted to member', (f) => (f.caller.access_level = 'member')],
  ['caller demoted below the requested level', (f) => (f.caller.access_level = 'admin')],
  ['caller deactivated', (f) => (f.caller.active = false)],
  ['caller Discord link rebound', (f) => (f.links['111'] = 'q')],
  ['helper tools disabled', (f) => (f.ctx.helperToolsEnabled = false)],
  ['target promoted by someone else', (f) => (f.target.access_level = 'superuser')],
]) {
  test(`${label} after preview prevents the PATCH`, async () => {
    const f = fixture({ callerLevel: 'superuser' });
    const { action } = await f.prepare({ level: 'superuser' });
    change(f);
    await assert.rejects(action.execute());
    assert.equal(f.writes.length, 0);
  });
}

// Count the reads before the PATCH in one successful execution, then change
// state on the last of them.
async function readsDuringExecute(method) {
  const f = fixture({ callerLevel: 'superuser' });
  const { action } = await f.prepare();
  let reads = 0;
  let beforeWrite;
  const read = f.ctx.directory[method];
  f.ctx.directory[method] = async (id) => (reads++, read(id));
  const write = f.ctx.directory.updatePersonAccessLevel;
  f.ctx.directory.updatePersonAccessLevel = async (...args) => {
    beforeWrite = reads;
    return write(...args);
  };
  await action.execute();
  assert.equal(f.writes.length, 1);
  return beforeWrite;
}

for (const [method, label, change] of [
  ['getPerson', 'target', (f) => (f.target.access_level = 'admin')],
  ['getPersonByDiscordId', 'caller', (f) => (f.caller.access_level = 'member')],
]) {
  test(`a ${label} change on the final read before PATCH prevents the write`, async () => {
    const total = await readsDuringExecute(method);
    const f = fixture({ callerLevel: 'superuser' });
    const { action } = await f.prepare();
    let reads = 0;
    const read = f.ctx.directory[method];
    f.ctx.directory[method] = async (id) => {
      if (++reads === total) change(f);
      return read(id);
    };
    // Either the action fails closed or the private result reports no change.
    const reply = await action.execute().catch(() => null);
    if (reply) assert.match(reply.content, /Nothing was changed/);
    assert.equal(f.writes.length, 0);
  });
}

test('expiry prevents the PATCH', async (t) => {
  const f = fixture();
  const { action } = await f.prepare();
  let clock = Date.now();
  t.mock.method(Date, 'now', () => clock);
  action.setExpiresAt(clock + 1000);
  clock += 2000;
  await assert.rejects(action.execute());
  assert.equal(f.writes.length, 0);
});

test('two proposals for the same person in one batch are rejected', async () => {
  const f = fixture({ callerLevel: 'superuser' });
  const a = (await f.prepare({ level: 'admin' })).action;
  const b = (await f.prepare({ level: 'superuser' })).action;
  assert.throws(() => combineHelperActions([a, b]), /Duplicate/);
  const byMention = (await f.prepare({ user: '222' })).action;
  const byEmail = (await f.prepare({ user: { email: 'alex@example.org' } })).action;
  assert.throws(() => combineHelperActions([byMention, byEmail]), /Duplicate/);
});

test('a batch promotes different people and delivers each receipt', async () => {
  const f = fixture({ callerLevel: 'superuser' });
  const a = (await f.prepare()).action;
  const b = (await f.prepare({ user: { name: 'Blair' }, level: 'superuser' })).action;
  const result = await combineHelperActions([a, b]).execute();
  assert.deepEqual(f.writes, [
    { id: 'p', level: 'admin' },
    { id: 'q', level: 'superuser' },
  ]);
  assert.equal(result.replies.filter((r) => /Promoted/.test(r.content)).length, 2);
});

test('of two independent proposals for one person, only the first confirmed writes', async () => {
  const f = fixture({ callerLevel: 'superuser' });
  const a = (await f.prepare()).action;
  const b = (await f.prepare({ level: 'superuser' })).action;
  await a.execute();
  await assert.rejects(b.execute());
  await a.execute().catch(() => {});
  assert.deepEqual(f.writes, [{ id: 'p', level: 'admin' }]);
});

test('a missing people:elevate scope is reported without claiming success', async () => {
  const f = fixture();
  f.ctx.directory.updatePersonAccessLevel = async () => {
    throw new AccessLevelChangeForbidden('people:elevate required');
  };
  assert.match((await f.slash()).content, /people:elevate/);
  const reply = await (await f.prepare()).action.execute();
  assert.match(reply.content, /people:elevate/);
  assert.doesNotMatch(reply.content, /Promoted/);
});

test('an outage before the write is retryable; an uncertain write is never retried', async () => {
  const before = fixture();
  before.ctx.directory.getPerson = async () => {
    throw new DirectoryUnavailable('down');
  };
  before.ctx.directory.getPersonByDiscordId = async (id) =>
    id === '111'
      ? before.caller
      : (() => {
          throw new DirectoryUnavailable('down');
        })();
  assert.match((await before.slash()).content, /temporarily unavailable/);

  const f = fixture();
  let attempts = 0;
  f.ctx.directory.updatePersonAccessLevel = async () => {
    attempts++;
    throw new DirectoryUnavailable('response lost');
  };
  assert.match((await f.slash()).content, /may have completed.*not retried/);
  const { action } = await f.prepare();
  const later = { userId: '111', summary: 'Later', check: async () => {}, execute: assert.fail };
  const result = await combineHelperActions([action, later]).execute();
  assert.equal(attempts, 2);
  assert.equal(result.replies.length, 1);
  assert.equal(result.replies[0].safeFailure, true);
});

test('refused keys and rejections are definite; malformed success is uncertain', async () => {
  for (const [status, body, expected] of [
    [401, '{}', /refused Misty's key.*Nothing was changed/],
    [403, '{}', /people:elevate.*Nothing was changed/],
    [409, '{"detail":"x"}', /directory rejected this change/],
    [200, 'not json', /may have completed/],
  ]) {
    const f = fixture();
    let calls = 0;
    f.ctx.directory.updatePersonAccessLevel = createDirectoryClient({
      baseUrl: 'http://directory',
      apiKey: 'test',
      fetchImpl: async () => (calls++, new Response(body, { status })),
    }).updatePersonAccessLevel;
    const reply = await f.slash();
    assert.match(reply.content, expected, String(status));
    assert.equal(calls, 1);
  }
});

test('the success reply escapes markdown and mentions in names', () => {
  const { content } = renderPromoteResult({
    outcome: 'PROMOTED',
    person: { display_name: 'x** @everyone `y`', access_level: 'admin' },
    previousLevel: 'member',
  });
  assert.match(content, /\*\*x\\\*\\\* @\u200beveryone \\`y\\`\*\*/);
});

test('success is reported only from a matching directory response', async () => {
  const f = fixture();
  f.ctx.directory.updatePersonAccessLevel = async (id) => ({ ...f.target, id, active: true });
  assert.match((await f.slash()).content, /may have completed/);
});

test('real confirmation handler previews privately and applies the promotion once', async () => {
  const { presentHelperAction, handleHelperActionInteraction } =
    await import('../src/adapters/discord/helperActions.js');
  const f = fixture();
  const { action } = await f.prepare();
  const publicReplies = [];
  await presentHelperAction(
    { guildId: 'guild', author: { id: '111' } },
    async (p) => publicReplies.push(p),
    action,
    f.ctx,
  );
  const id = (messages, label) =>
    messages
      .flatMap((p) => p.components ?? [])
      .flatMap((row) => row.components)
      .find((b) => b.label === label)?.custom_id;
  function button(customId, userId = '111') {
    return {
      customId,
      user: { id: userId },
      replies: [],
      ephemeral: true,
      async deferReply() {
        this.deferred = true;
      },
      async editReply(p) {
        this.replied = true;
        this.replies.push(p);
      },
      async followUp(p) {
        this.replies.push(p);
      },
    };
  }
  assert.doesNotMatch(JSON.stringify(publicReplies), /Alex|admin|member/);
  const review = button(id(publicReplies, 'Review privately'));
  await handleHelperActionInteraction(review, f.ctx);
  assert.match(JSON.stringify(review.replies), /member → admin/);
  const confirmId = id(review.replies, 'Confirm');
  await handleHelperActionInteraction(button(confirmId, '222'), f.ctx);
  assert.equal(f.writes.length, 0);
  const confirmation = button(confirmId);
  await Promise.all([
    handleHelperActionInteraction(confirmation, f.ctx),
    handleHelperActionInteraction(button(confirmId), f.ctx),
  ]);
  assert.deepEqual(f.writes, [{ id: 'p', level: 'admin' }]);
  assert.match(JSON.stringify(confirmation.replies), /Promoted/);
});

test('directory PATCH sends only access_level and normalizes statuses', async () => {
  let body;
  const client = (status) =>
    createDirectoryClient({
      baseUrl: 'http://directory',
      apiKey: 'test',
      fetchImpl: async (url, options) => {
        assert.equal(url, 'http://directory/people/p');
        assert.equal(options.method, 'PATCH');
        body = JSON.parse(options.body);
        return new Response(JSON.stringify({ id: 'p', access_level: 'admin' }), { status });
      },
    });
  assert.deepEqual(await client(200).updatePersonAccessLevel('p', 'admin'), {
    id: 'p',
    access_level: 'admin',
  });
  assert.deepEqual(body, { access_level: 'admin' });
  assert.equal(await client(404).updatePersonAccessLevel('p', 'admin'), null);
  for (const status of [401, 403])
    await assert.rejects(client(status).updatePersonAccessLevel('p', 'admin'), {
      name: 'AccessLevelChangeForbidden',
    });
  for (const status of [400, 409, 422])
    await assert.rejects(client(status).updatePersonAccessLevel('p', 'admin'), {
      name: 'PersonUpdateInvalid',
    });
  await assert.rejects(client(503).updatePersonAccessLevel('p', 'admin'), DirectoryUnavailable);
});

test('help detail distinguishes global access from team roles', async () => {
  const { buildCommandDetailEmbed } = await import('../src/messages/help.js');
  const fields = buildCommandDetailEmbed(commands.get('promote')).embeds[0].fields;
  assert.match(JSON.stringify(fields), /Global access, not team roles/);
  assert.match(JSON.stringify(fields), /\/team update/);
});
