import { test } from 'node:test';
import assert from 'node:assert/strict';
import { authMessages } from '../src/messages/auth.js';
import { renderLinkResult, renderVerifyCodeResult } from '../src/messages/link.js';
import { renderSeedResult, buildWhoamiEmbed } from '../src/messages/people.js';
import {
  renderCreateTeamResult,
  renderListTeamsResult,
  renderRenameTeamResult,
  renderAddMemberResult,
  renderRemoveMemberResult,
  renderRosterResult,
  renderMyTeamsResult,
  renderGoogleGroupStatusResult,
  renderGoogleGroupSyncResult,
} from '../src/messages/team.js';
import {
  renderDocAddResult,
  renderDocListResult,
  renderDocRemoveResult,
} from '../src/messages/doc.js';
import { renderAddEmailResult, renderVerifyEmailResult } from '../src/messages/email.js';

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
  const team = { slug: 'ml', label: 'ML' };
  const needsSetup = renderCreateTeamResult({
    outcome: 'CREATED',
    team,
    google: {
      outcome: 'SYNC_RESULT',
      state: {
        group_email: 'ml@utmist.skule.ca',
        group_name: 'groups/ml',
        status: 'needs_external_members',
        last_error: 'Allow external members',
      },
    },
  }).content;
  assert.match(needsSetup, /Created team.*ML/s);
  assert.match(needsSetup, /infrastructure@utmist\.skule\.ca/);
  assert.match(needsSetup, /https:\/\/groups\.google\.com\/a\/utmist\.skule\.ca\/g\/ml\/settings/);
  assert.match(needsSetup, /Allow external members/);
  assert.match(needsSetup, /Who can add external members/);
  assert.match(needsSetup, /Super Admin/);
  assert.match(needsSetup, /not yet verified/i);

  // Empty/fresh groups often report synced — create must still show manual setup.
  const syncedFresh = renderCreateTeamResult({
    outcome: 'CREATED',
    team,
    google: {
      outcome: 'SYNC_RESULT',
      state: { group_email: 'ml@utmist.skule.ca', group_name: 'groups/ml', status: 'synced' },
    },
  }).content;
  assert.match(syncedFresh, /synced/i);
  assert.match(syncedFresh, /infrastructure@utmist\.skule\.ca/);
  assert.match(syncedFresh, /groups\.google\.com/);
  assert.match(syncedFresh, /Allow external members/);
  assert.match(syncedFresh, /Who can add external members/);

  const unconfigured = renderCreateTeamResult({
    outcome: 'CREATED',
    team,
    google: { outcome: 'NOT_CONFIGURED' },
  }).content;
  assert.match(unconfigured, /Created team/);
  assert.match(unconfigured, /not configured/i);
  assert.doesNotMatch(unconfigured, /groups\.google\.com/);

  assert.match(renderCreateTeamResult({ outcome: 'SLUG_EXISTS', detail: 'x' }).content, /already/i);
  assert.match(
    renderCreateTeamResult({ outcome: 'DIRECTORY_DOWN' }).content,
    /unavailable|try again/i,
  );
});

test('renderGoogleGroupStatusResult covers outcomes', () => {
  const team = { slug: 'ml', label: 'ML' };
  assert.match(
    renderGoogleGroupStatusResult({
      outcome: 'STATUS',
      team,
      state: { group_email: 'ml@utmist.skule.ca', status: 'synced' },
    }).content,
    /synced/i,
  );
  const needs = renderGoogleGroupStatusResult({
    outcome: 'STATUS',
    team,
    state: {
      group_email: 'ml@utmist.skule.ca',
      group_name: 'groups/ml',
      status: 'needs_external_members',
      last_error: 'Allow external members',
    },
  }).content;
  assert.match(needs, /external-member/i);
  assert.match(needs, /Who can add external members/);
  assert.match(needs, /groups\.google\.com/);
  assert.match(
    renderGoogleGroupStatusResult({ outcome: 'NO_MAPPING', team }).content,
    /No Google Group mapping/i,
  );
  assert.match(renderGoogleGroupStatusResult({ outcome: 'TEAM_NOT_FOUND' }).content, /no team/i);
});

test('renderGoogleGroupSyncResult covers outcomes', () => {
  const team = { slug: 'ml', label: 'ML' };
  assert.match(
    renderGoogleGroupSyncResult({
      outcome: 'SYNC_RESULT',
      team,
      state: { group_email: 'ml@utmist.skule.ca', status: 'failed', last_error: 'boom' },
    }).content,
    /failed|boom/i,
  );
  assert.match(
    renderGoogleGroupSyncResult({ outcome: 'NOT_CONFIGURED' }).content,
    /not configured/i,
  );
  assert.match(renderGoogleGroupSyncResult({ outcome: 'RETIRED_NO_GROUP' }).content, /retired/i);
});

test('google-status provides setup for an older empty synced group using its stored address', () => {
  const content = renderGoogleGroupStatusResult({
    outcome: 'STATUS',
    team: { slug: 'renamed', label: 'Renamed' },
    state: {
      group_email: 'original@utmist.skule.ca',
      group_name: 'groups/123',
      status: 'synced',
    },
  }).content;
  assert.match(content, /not proof/i);
  assert.match(content, /infrastructure@utmist\.skule\.ca/);
  assert.match(content, /\/g\/original\/settings/);
  assert.match(content, /Allow external members/);
  assert.match(content, /Who can add external members/);
  assert.match(content, /Super Admin/);
  assert.match(content, /not yet verified/);
  assert.match(content, /google-sync/);
});

test('unconfirmed group creation never offers settings actions on any reply path', () => {
  const team = { slug: 'ml', label: 'ML' };
  const state = {
    group_email: 'ml@utmist.skule.ca',
    group_name: null,
    status: 'failed',
    last_error: 'create group: ml@utmist.skule.ca is already in use by another group',
  };
  const replies = [
    renderCreateTeamResult({ outcome: 'CREATED', team, google: { outcome: 'SYNC_RESULT', state } }),
    renderGoogleGroupStatusResult({ outcome: 'STATUS', team, state }),
    renderGoogleGroupSyncResult({ outcome: 'SYNC_RESULT', team, state }),
  ];
  for (const { content } of replies) {
    assert.match(content, /already in use/);
    assert.match(content, /google-sync/);
    assert.doesNotMatch(content, /groups\.google\.com|Allow external members/);
  }
});

test('a timed-out create probe preserves team success and directs the user to status', () => {
  const { content } = renderCreateTeamResult({
    outcome: 'CREATED',
    team: { slug: 'ml', label: 'ML' },
    google: { outcome: 'PENDING' },
  });
  assert.match(content, /Created team/);
  assert.match(content, /provisioning.*progress/i);
  assert.match(content, /google-status/);
  assert.doesNotMatch(content, /groups\.google\.com|is \*\*synced\*\*/);
});

// #272 Done checklist — offline cases for Google Group setup / recovery replies.
test('Google Group offline cases: in-progress, disabled, setup, empty synced, failed, recovery', () => {
  const team = { slug: 'ml', label: 'ML' };

  // Provisioning still in progress (no mapping yet).
  const inProgress = renderGoogleGroupStatusResult({ outcome: 'NO_MAPPING', team }).content;
  assert.match(inProgress, /No Google Group mapping/i);
  assert.match(inProgress, /google-sync|Provisioning/i);

  // Disabled integration — create must not present a settings link.
  const disabled = renderCreateTeamResult({
    outcome: 'CREATED',
    team,
    google: { outcome: 'NOT_CONFIGURED' },
  }).content;
  assert.match(disabled, /not configured/i);
  assert.doesNotMatch(disabled, /groups\.google\.com/);
  assert.match(
    renderGoogleGroupSyncResult({ outcome: 'NOT_CONFIGURED' }).content,
    /not configured/i,
  );

  // Setup required.
  const setup = renderGoogleGroupSyncResult({
    outcome: 'SYNC_RESULT',
    team,
    state: {
      group_email: 'ml@utmist.skule.ca',
      group_name: 'groups/ml',
      status: 'needs_external_members',
      last_error: 'Allow external members',
    },
  }).content;
  assert.match(setup, /Allow external members/);
  assert.match(setup, /Who can add external members/);
  assert.match(setup, /groups\.google\.com/);

  // Empty-group synced is not proof external membership works.
  const emptySynced = renderGoogleGroupStatusResult({
    outcome: 'STATUS',
    team,
    state: { group_email: 'ml@utmist.skule.ca', status: 'synced' },
  }).content;
  assert.match(emptySynced, /synced/i);
  assert.match(emptySynced, /empty group|not proof|before any external/i);

  // Failed sync (generic + external-members flavored).
  const failed = renderGoogleGroupSyncResult({
    outcome: 'SYNC_RESULT',
    team,
    state: { group_email: 'ml@utmist.skule.ca', status: 'failed', last_error: 'timeout' },
  }).content;
  assert.match(failed, /failed/i);
  assert.match(failed, /timeout/);
  const failedExternal = renderGoogleGroupSyncResult({
    outcome: 'SYNC_RESULT',
    team,
    state: {
      group_email: 'ml@utmist.skule.ca',
      group_name: 'groups/ml',
      status: 'failed',
      last_error: 'group rejects external members',
    },
  }).content;
  assert.match(failedExternal, /Allow external members/);
  assert.match(failedExternal, /groups\.google\.com/);

  // Successful recovery after settings + sync.
  const recovered = renderGoogleGroupSyncResult({
    outcome: 'SYNC_RESULT',
    team,
    state: { group_email: 'ml@utmist.skule.ca', status: 'synced' },
  }).content;
  assert.match(recovered, /synced/i);
  assert.match(recovered, /Google Group for \*\*ML\*\*/);

  // Create-time probe could not reach Google — team still created, no fake ready group.
  const probeDown = renderCreateTeamResult({
    outcome: 'CREATED',
    team,
    google: { outcome: 'DIRECTORY_DOWN' },
  }).content;
  assert.match(probeDown, /Created team/);
  assert.match(probeDown, /could not be checked|google-status/i);
  assert.doesNotMatch(probeDown, /is \*\*synced\*\*/);
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
    renderGoogleGroupStatusResult({ outcome: 'DIRECTORY_DOWN' }),
    renderGoogleGroupSyncResult({ outcome: 'DIRECTORY_DOWN' }),
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

test('renderDocListResult LISTED is public and lists titles+links, not ids', () => {
  const r = renderDocListResult({
    outcome: 'LISTED',
    docs: [{ title: 'Onboarding', id: 'd1', url: 'https://x.com', source_id: 'gdocs' }],
  });
  assert.equal(r.ephemeral, false);
  assert.match(r.content, /Onboarding/);
  assert.match(r.content, /<https:\/\/x\.com>/);
  assert.doesNotMatch(r.content, /d1/);
});

test('renderDocListResult shows ids only when isAdmin is set', () => {
  const docs = [{ title: 'Onboarding', id: 'd1', url: 'https://x.com', source_id: 'gdocs' }];
  const shown = renderDocListResult({ outcome: 'LISTED', docs }, { isAdmin: true });
  assert.match(shown.content, /`d1`/);
  assert.match(shown.content, /<https:\/\/x\.com>/);
  assert.doesNotMatch(renderDocListResult({ outcome: 'LISTED', docs }).content, /d1/);
  assert.doesNotMatch(
    renderDocListResult({ outcome: 'LISTED', docs }, { isAdmin: false }).content,
    /d1/,
  );
});

test('renderDocListResult LISTED empty', () => {
  assert.match(renderDocListResult({ outcome: 'LISTED', docs: [] }).content, /no docs/i);
});

test('renderDocRemoveResult REMOVED is ephemeral', () => {
  const r = renderDocRemoveResult({ outcome: 'REMOVED', doc: { title: 'Onboarding', id: 'd1' } });
  assert.equal(r.ephemeral, true);
  assert.match(r.content, /removed/i);
});

test('doc renderers map DOC_DOWN', () => {
  for (const fn of [renderDocAddResult, renderDocListResult, renderDocRemoveResult]) {
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
