import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  authMessages,
  renderLinkResult,
  renderVerifyCodeResult,
  renderSeedResult,
  buildWhoamiEmbed,
  renderCreateTeamResult,
  renderListTeamsResult,
  renderRenameTeamResult,
  renderAddMemberResult,
  renderRemoveMemberResult,
  renderRosterResult,
  renderMyTeamsResult,
  renderDocAddResult,
  renderDocListResult,
  renderDocShowResult,
  renderDocRemoveResult,
  renderAddEmailResult,
  renderVerifyEmailResult,
} from '../src/messages.js';

test('authMessages.unavailable returns ReplyPayload', () => {
  const p = authMessages.unavailable();
  assert.equal(typeof p.content, 'string');
  assert.equal(p.ephemeral, true);
  assert.equal(p.embeds, undefined);
});

test('authMessages.denied returns ReplyPayload', () => {
  const p = authMessages.denied('not_linked');
  assert.equal(typeof p.content, 'string');
  assert.equal(p.ephemeral, true);
  assert.match(p.content, /link/i);
});

test('authMessages.internalError returns ReplyPayload', () => {
  const p = authMessages.internalError();
  assert.equal(typeof p.content, 'string');
  assert.equal(p.ephemeral, true);
});

test('renderLinkResult covers every outcome', () => {
  assert.match(
    renderLinkResult({ outcome: 'CODE_SENT', email: 'alex@utmist.ca' }).content,
    /alex@utmist\.ca/,
  );
  assert.match(renderLinkResult({ outcome: 'NOT_A_MEMBER' }).content, /exec/i);
  assert.match(renderLinkResult({ outcome: 'DIRECTORY_DOWN' }).content, /unavailable|try again/i);
  assert.match(
    renderLinkResult({ outcome: 'VERIFICATION_DOWN' }).content,
    /unavailable|try again/i,
  );
  assert.match(renderLinkResult({ outcome: 'RATE_LIMITED' }).content, /too many|wait/i);
  assert.match(renderLinkResult({ outcome: 'SOMETHING_ELSE' }).content, /wrong|try again/i);
});

test('renderLinkResult returns ReplyPayload with content', () => {
  const p = renderLinkResult({ outcome: 'CODE_SENT', email: 'alex@utmist.ca' });
  assert.ok(p.content.includes('alex@utmist.ca'));
  assert.equal(p.ephemeral, true);
});

test('renderVerifyCodeResult covers every outcome', () => {
  assert.match(
    renderVerifyCodeResult({ outcome: 'LINKED', person: { display_name: 'Alex' } }).content,
    /Alex/,
  );
  assert.match(renderVerifyCodeResult({ outcome: 'NOT_A_MEMBER' }).content, /exec/i);
  assert.match(
    renderVerifyCodeResult({ outcome: 'ALREADY_LINKED', detail: 'x' }).content,
    /already|couldn't|could not/i,
  );
  assert.match(renderVerifyCodeResult({ outcome: 'CODE_EXPIRED' }).content, /expired/i);
  assert.match(renderVerifyCodeResult({ outcome: 'TOO_MANY_ATTEMPTS' }).content, /too many/i);
  assert.match(renderVerifyCodeResult({ outcome: 'INVALID_CODE' }).content, /right|invalid|wrong/i);
  assert.match(renderVerifyCodeResult({ outcome: 'NO_PENDING_CODE' }).content, /pending|link/i);
  assert.match(
    renderVerifyCodeResult({ outcome: 'VERIFICATION_DOWN' }).content,
    /unavailable|try again/i,
  );
  assert.match(
    renderVerifyCodeResult({ outcome: 'DIRECTORY_DOWN' }).content,
    /unavailable|try again/i,
  );
  assert.match(renderVerifyCodeResult({ outcome: 'SOMETHING_ELSE' }).content, /wrong|try again/i);
});

test('renderVerifyCodeResult returns ReplyPayload with content', () => {
  const p = renderVerifyCodeResult({ outcome: 'LINKED', person: { display_name: 'Alex' } });
  assert.ok(p.content.includes('Alex'));
  assert.equal(p.ephemeral, true);
});

test('buildWhoamiEmbed returns ReplyPayload with embeds', () => {
  const p = buildWhoamiEmbed(
    { display_name: 'A', primary_email: 'a@x', access_level: 'member', active: true },
    [],
  );
  assert.equal(p.content, undefined);
  assert.equal(p.embeds.length, 1);
  assert.equal(p.embeds[0].title, 'A');
  assert.equal(p.ephemeral, true);
});

test('buildWhoamiEmbed renders the four person fields', () => {
  const embed = buildWhoamiEmbed(
    { display_name: 'Alex', primary_email: 'alex@utmist.ca', access_level: 'admin', active: true },
    [],
  ).embeds[0];
  assert.equal(embed.title, 'Alex');
  const byName = Object.fromEntries(embed.fields.map((f) => [f.name, f.value]));
  assert.equal(byName['Email'], 'alex@utmist.ca');
  assert.equal(byName['Access level'], 'admin');
  assert.equal(byName['Status'], 'Active');
  assert.equal(byName['Identities'], '_(none)_');
});

test('buildWhoamiEmbed renders Inactive when person.active is false', () => {
  const embed = buildWhoamiEmbed(
    { display_name: 'A', primary_email: 'a@x', access_level: 'member', active: false },
    [],
  ).embeds[0];
  const byName = Object.fromEntries(embed.fields.map((f) => [f.name, f.value]));
  assert.equal(byName['Status'], 'Inactive');
});

test('buildWhoamiEmbed sorts identifiers alphabetically and formats handle vs no-handle', () => {
  const embed = buildWhoamiEmbed(
    { display_name: 'A', primary_email: 'a@x', access_level: 'member', active: true },
    [
      { provider: 'github', external_id: '9876', handle: 'eeetan' },
      { provider: 'discord', external_id: '123', handle: 'alex' },
      { provider: 'notion', external_id: 'notion-uuid', handle: null },
    ],
  ).embeds[0];
  const byName = Object.fromEntries(embed.fields.map((f) => [f.name, f.value]));
  assert.equal(byName['Identities'], 'discord: <@123>\ngithub: eeetan (9876)\nnotion: notion-uuid');
});

test('buildWhoamiEmbed renders discord identity as a mention (drops stored handle)', () => {
  const embed = buildWhoamiEmbed(
    { display_name: 'A', primary_email: 'a@x', access_level: 'member', active: true },
    [{ provider: 'discord', external_id: '987654321', handle: 'stale-handle' }],
  ).embeds[0];
  const byName = Object.fromEntries(embed.fields.map((f) => [f.name, f.value]));
  assert.equal(byName['Identities'], 'discord: <@987654321>');
});

test('buildWhoamiEmbed shows _(unavailable)_ when identifiers is null', () => {
  const embed = buildWhoamiEmbed(
    { display_name: 'A', primary_email: 'a@x', access_level: 'member', active: true },
    null,
  ).embeds[0];
  const byName = Object.fromEntries(embed.fields.map((f) => [f.name, f.value]));
  assert.equal(byName['Identities'], '_(unavailable)_');
});

test('renderSeedResult covers outcomes', () => {
  assert.match(
    renderSeedResult({
      outcome: 'SEEDED',
      person: { display_name: 'A', primary_email: 'a@x', access_level: 'member' },
    }).content,
    /A/,
  );
  assert.match(renderSeedResult({ outcome: 'EXISTS', detail: 'x' }).content, /already/i);
  assert.match(renderSeedResult({ outcome: 'DIRECTORY_DOWN' }).content, /unavailable|try again/i);
});

test('renderCreateTeamResult covers outcomes', () => {
  assert.match(
    renderCreateTeamResult({ outcome: 'CREATED', team: { slug: 'ml', label: 'ML' } }).content,
    /ML|ml/,
  );
  assert.match(renderCreateTeamResult({ outcome: 'SLUG_EXISTS', detail: 'x' }).content, /already/i);
  assert.match(
    renderCreateTeamResult({ outcome: 'DIRECTORY_DOWN' }).content,
    /unavailable|try again/i,
  );
});

test('renderListTeamsResult covers outcomes', () => {
  assert.match(
    renderListTeamsResult({
      outcome: 'LISTED',
      teams: [
        { slug: 'ml', label: 'ML' },
        { slug: 'ops', label: 'Ops' },
      ],
    }).content,
    /ML.*Ops|ml.*ops/is,
  );
  assert.match(renderListTeamsResult({ outcome: 'LISTED', teams: [] }).content, /no teams/i);
  assert.match(
    renderListTeamsResult({ outcome: 'DIRECTORY_DOWN' }).content,
    /unavailable|try again/i,
  );
});

test('renderRenameTeamResult covers outcomes', () => {
  assert.match(
    renderRenameTeamResult({ outcome: 'RENAMED', team: { slug: 'ml', label: 'New' } }).content,
    /New/,
  );
  assert.match(renderRenameTeamResult({ outcome: 'TEAM_NOT_FOUND' }).content, /no team|not found/i);
  assert.match(
    renderRenameTeamResult({ outcome: 'DIRECTORY_DOWN' }).content,
    /unavailable|try again/i,
  );
});

test('renderAddMemberResult covers outcomes', () => {
  assert.match(
    renderAddMemberResult({
      outcome: 'ADDED',
      person: { display_name: 'Alex' },
      team: { label: 'ML' },
    }).content,
    /Alex.*ML/s,
  );
  assert.match(renderAddMemberResult({ outcome: 'USER_NOT_LINKED' }).content, /link/i);
  assert.match(renderAddMemberResult({ outcome: 'TEAM_NOT_FOUND' }).content, /no team|not found/i);
  assert.match(
    renderAddMemberResult({
      outcome: 'ALREADY_ON_TEAM',
      person: { display_name: 'Alex' },
      team: { label: 'ML' },
    }).content,
    /already/i,
  );
  const invalid = renderAddMemberResult({
    outcome: 'MEMBERSHIP_INVALID',
    detail: 'role_kind_id not found: lead',
  }).content;
  assert.match(invalid, /rejected/i);
  assert.match(invalid, /role_kind_id not found: lead/);
  assert.match(
    renderAddMemberResult({ outcome: 'DIRECTORY_DOWN' }).content,
    /unavailable|try again/i,
  );
});

test('renderRemoveMemberResult covers outcomes', () => {
  assert.match(
    renderRemoveMemberResult({
      outcome: 'REMOVED',
      person: { display_name: 'Alex' },
      team: { label: 'ML' },
    }).content,
    /Alex.*ML/s,
  );
  assert.match(
    renderRemoveMemberResult({ outcome: 'USER_NOT_LINKED' }).content,
    /aren't on any team|not on any team/i,
  );
  assert.match(
    renderRemoveMemberResult({ outcome: 'TEAM_NOT_FOUND' }).content,
    /no team|not found/i,
  );
  assert.match(
    renderRemoveMemberResult({
      outcome: 'NOT_ON_TEAM',
      person: { display_name: 'Alex' },
      team: { label: 'ML' },
    }).content,
    /not on/i,
  );
  assert.match(
    renderRemoveMemberResult({ outcome: 'DIRECTORY_DOWN' }).content,
    /unavailable|try again/i,
  );
});

test('renderRosterResult covers outcomes', () => {
  const roster = renderRosterResult({
    outcome: 'ROSTER',
    team: { slug: 'ml', label: 'ML' },
    members: [
      { person: { display_name: 'One' }, role_kind_id: 'lead', is_team_admin: true },
      { person: { display_name: 'Two' }, role_kind_id: 'member', is_team_admin: false },
    ],
  }).content;
  assert.match(roster, /One/);
  assert.match(roster, /Two/);
  assert.match(roster, /lead/);
  assert.match(roster, /ML|ml/);
  assert.match(
    renderRosterResult({ outcome: 'ROSTER', team: { slug: 'ml', label: 'ML' }, members: [] })
      .content,
    /no members|empty/i,
  );
  assert.match(renderRosterResult({ outcome: 'TEAM_NOT_FOUND' }).content, /no team|not found/i);
  assert.match(renderRosterResult({ outcome: 'DIRECTORY_DOWN' }).content, /unavailable|try again/i);
});

test('renderMyTeamsResult covers outcomes', () => {
  const out = renderMyTeamsResult({
    outcome: 'MY_TEAMS',
    memberships: [
      { team: { slug: 'ml', label: 'ML' }, role_kind_id: 'lead', is_team_admin: true },
      { team: { slug: 'ops', label: 'Ops' }, role_kind_id: 'member', is_team_admin: false },
    ],
  }).content;
  assert.match(out, /ML/);
  assert.match(out, /Ops/);
  assert.match(
    renderMyTeamsResult({ outcome: 'MY_TEAMS', memberships: [] }).content,
    /not on any/i,
  );
  assert.match(
    renderMyTeamsResult({ outcome: 'DIRECTORY_DOWN' }).content,
    /unavailable|try again/i,
  );
});

test('personal/admin render functions return ReplyPayload with ephemeral: true', () => {
  const payloads = [
    renderLinkResult({ outcome: 'DIRECTORY_DOWN' }),
    renderSeedResult({ outcome: 'DIRECTORY_DOWN' }),
    renderCreateTeamResult({ outcome: 'DIRECTORY_DOWN' }),
    renderRenameTeamResult({ outcome: 'DIRECTORY_DOWN' }),
    renderAddMemberResult({ outcome: 'DIRECTORY_DOWN' }),
    renderRemoveMemberResult({ outcome: 'DIRECTORY_DOWN' }),
    renderMyTeamsResult({ outcome: 'DIRECTORY_DOWN' }),
  ];
  for (const p of payloads) {
    assert.equal(typeof p.content, 'string');
    assert.equal(p.ephemeral, true);
    assert.equal(p.embeds, undefined);
  }
});

test('shared-reference render functions (list, roster) are public (ephemeral: false)', () => {
  const payloads = [
    renderListTeamsResult({ outcome: 'DIRECTORY_DOWN' }),
    renderRosterResult({ outcome: 'DIRECTORY_DOWN' }),
  ];
  for (const p of payloads) {
    assert.equal(typeof p.content, 'string');
    assert.equal(p.ephemeral, false);
    assert.equal(p.embeds, undefined);
  }
});

test('renderDocAddResult ADDED shows title and url, ephemeral', () => {
  const r = renderDocAddResult({
    outcome: 'ADDED',
    doc: { title: 'Onboarding', url: 'https://x.com', id: 'd1' },
    warnings: [],
  });
  assert.match(r.content, /Onboarding/);
  assert.match(r.content, /https:\/\/x\.com/);
  assert.equal(r.ephemeral, true);
});

test('renderDocAddResult MERGED says already catalogued', () => {
  const r = renderDocAddResult({
    outcome: 'MERGED',
    doc: { url: 'https://x.com', id: 'd1' },
    warnings: [],
  });
  assert.match(r.content, /already catalogued/i);
});

test('renderDocAddResult surfaces warnings', () => {
  const r = renderDocAddResult({
    outcome: 'ADDED',
    doc: { url: 'https://x.com', id: 'd1' },
    warnings: ['owner label deferred'],
  });
  assert.match(r.content, /owner label deferred/);
});

test('renderDocAddResult TEAM_NOT_FOUND', () => {
  assert.match(renderDocAddResult({ outcome: 'TEAM_NOT_FOUND' }).content, /no team/i);
});

test('renderDocListResult LISTED is public and lists titles+ids', () => {
  const r = renderDocListResult({
    outcome: 'LISTED',
    docs: [{ title: 'Onboarding', id: 'd1', source_id: 'gdocs' }],
  });
  assert.equal(r.ephemeral, false);
  assert.match(r.content, /Onboarding/);
  assert.match(r.content, /d1/);
});

test('renderDocListResult LISTED empty', () => {
  assert.match(renderDocListResult({ outcome: 'LISTED', docs: [] }).content, /no docs/i);
});

test('renderDocShowResult SHOWN includes url and id', () => {
  const r = renderDocShowResult({
    outcome: 'SHOWN',
    doc: {
      title: 'Onboarding',
      url: 'https://x.com',
      id: 'd1',
      source_id: 'gdocs',
      tags: ['onboarding'],
      owning_team_label: 'ML',
    },
  });
  assert.match(r.content, /https:\/\/x\.com/);
  assert.match(r.content, /ML/);
});

test('renderDocShowResult NOT_FOUND', () => {
  assert.match(renderDocShowResult({ outcome: 'NOT_FOUND' }).content, /no doc/i);
});

test('renderDocRemoveResult REMOVED is ephemeral', () => {
  const r = renderDocRemoveResult({ outcome: 'REMOVED', doc: { title: 'Onboarding', id: 'd1' } });
  assert.equal(r.ephemeral, true);
  assert.match(r.content, /removed/i);
});

test('doc renderers map DOC_DOWN', () => {
  for (const fn of [
    renderDocAddResult,
    renderDocListResult,
    renderDocShowResult,
    renderDocRemoveResult,
  ]) {
    assert.match(fn({ outcome: 'DOC_DOWN' }).content, /unavailable/i);
  }
});

test('renderAddEmailResult CODE_SENT mentions the email', () => {
  const r = renderAddEmailResult({ outcome: 'CODE_SENT', email: 'a@b.com' });
  assert.match(r.content, /a@b.com/);
  assert.equal(r.ephemeral, true);
});

test('renderVerifyEmailResult covers ADDED and EMAIL_TAKEN', () => {
  const added = renderVerifyEmailResult({ outcome: 'ADDED', email: 'a@b.com' });
  assert.match(added.content, /a@b.com/);
  assert.equal(added.ephemeral, true);
  const taken = renderVerifyEmailResult({ outcome: 'EMAIL_TAKEN' });
  assert.match(taken.content, /already/i);
  assert.equal(taken.ephemeral, true);
});

test('renderAddEmailResult covers RATE_LIMITED, VERIFICATION_DOWN, and unknown outcomes', () => {
  for (const outcome of ['RATE_LIMITED', 'VERIFICATION_DOWN', 'SOMETHING_ELSE']) {
    const r = renderAddEmailResult({ outcome });
    assert.equal(typeof r.content, 'string');
    assert.ok(r.content.length > 0);
    assert.equal(r.ephemeral, true);
  }
});

test('renderVerifyEmailResult covers CODE_EXPIRED, TOO_MANY_ATTEMPTS, INVALID_CODE, NO_PENDING_CODE, DIRECTORY_DOWN, and unknown outcomes', () => {
  for (const outcome of [
    'CODE_EXPIRED',
    'TOO_MANY_ATTEMPTS',
    'INVALID_CODE',
    'NO_PENDING_CODE',
    'DIRECTORY_DOWN',
    'SOMETHING_ELSE',
  ]) {
    const r = renderVerifyEmailResult({ outcome });
    assert.equal(typeof r.content, 'string');
    assert.ok(r.content.length > 0);
    assert.equal(r.ephemeral, true);
  }
});
