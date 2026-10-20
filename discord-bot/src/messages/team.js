import { FALLBACK, DIRECTORY_DOWN_MSG } from './common.js';

const USER_NOT_LINKED_MSG =
  "That user hasn't linked their directory account yet. Ask them to run `/link` first, then try again.";

function googleGroupSettingsUrl(email) {
  if (typeof email !== 'string') return null;
  const parts = email.split('@');
  if (parts.length !== 2 || !parts[0] || !parts[1]) return null;
  const [local, domain] = parts;
  return `https://groups.google.com/a/${encodeURIComponent(domain)}/g/${encodeURIComponent(local)}/settings`;
}

function externalMembersSetupHint(settingsUrl) {
  return [
    'Misty configures external membership automatically. Retry with `/team google-sync`.',
    "If setup still fails, ask UTMIST's Google administrator to check API authorization and organization policy.",
    `For manual recovery, check **Allow external members** and **Who can add external members** in [Group settings](${settingsUrl}) as this environment's Misty group owner, then retry sync.`,
  ].join('\n');
}

function renderGoogleGroupState(team, state) {
  const email = state.group_email;
  const settingsUrl = googleGroupSettingsUrl(email);
  const header = `Google Group for **${team.label}**${email ? ` (\`${email}\`)` : ''}`;
  // Keep recovery instructions visible even when several membership errors accumulate.
  const error = state.last_error?.slice(0, 600);
  let content;
  switch (state.status) {
    case 'synced':
      content = `✅ ${header} is **synced**.`;
      break;
    case 'needs_external_members':
      content = [
        `⚠️ ${header} needs external-member recovery.`,
        error ? `_Directory note:_ ${error}` : null,
      ]
        .filter(Boolean)
        .join('\n');
      break;
    case 'failed':
      content = [
        `❌ ${header} sync **failed**.`,
        error ? `_${error}_` : null,
        'Re-run `/team google-sync` after fixing the issue, or ask infrastructure for help.',
      ]
        .filter(Boolean)
        .join('\n');
      break;
    default:
      content = `${header}: status \`${state.status}\`.`;
  }
  // A recorded email alone can be an attempted address belonging to another group.
  if (
    state.group_name &&
    settingsUrl &&
    (state.status === 'needs_external_members' ||
      (state.status === 'failed' &&
        /group settings|external members/i.test(state.last_error ?? '')))
  ) {
    return [content, '', externalMembersSetupHint(settingsUrl)].join('\n');
  }
  return content;
}

function renderGoogleAfterCreate(team, google) {
  switch (google?.outcome) {
    case 'SYNC_RESULT':
      return renderGoogleGroupState(team, google.state);
    case 'PENDING':
      return 'Google Group provisioning may still be in progress. Run `/team google-status` in a moment to check automatic setup.';
    case 'NOT_CONFIGURED':
      return 'Google Groups is not configured on the directory, so no group was created. The team exists in Misty only.';
    case 'RETIRED_NO_GROUP':
      return 'Google Group sync was skipped (retired team with no group).';
    case 'DIRECTORY_DOWN':
      return 'The team was created, but Google Group status could not be checked. Try `/team google-status` in a moment.';
    default:
      return 'The team was created, but Google Group status was not returned. Try `/team google-status`.';
  }
}

export function renderCreateTeamResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'CREATED': {
        return [
          `✅ Created team **${result.team.label}** (\`${result.team.slug}\`).`,
          '',
          renderGoogleAfterCreate(result.team, result.google),
        ].join('\n');
      }
      case 'SLUG_EXISTS':
        return `A team with that slug already exists: ${result.detail}`;
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  return { content, ephemeral: true };
}

export function renderGoogleGroupStatusResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'STATUS':
        return renderGoogleGroupState(result.team, result.state);
      case 'NO_MAPPING':
        return [
          `No Google Group mapping yet for **${result.team.label}** (\`${result.team.slug}\`).`,
          'Provisioning runs after team creation — wait a moment, then try `/team google-sync`, or ask infrastructure if it stays missing.',
        ].join('\n');
      case 'TEAM_NOT_FOUND':
        return "There's no team with that slug.";
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  return { content, ephemeral: true };
}

export function renderGoogleGroupSyncResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'SYNC_RESULT':
        return renderGoogleGroupState(result.team, result.state);
      case 'NOT_CONFIGURED':
        return 'Google Groups integration is not configured on the directory service.';
      case 'RETIRED_NO_GROUP':
        return 'That team is retired and never had a Google Group, so there is nothing to sync.';
      case 'TEAM_NOT_FOUND':
        return "There's no team with that slug.";
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  return { content, ephemeral: true };
}

export function renderListTeamsResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'LISTED':
        if (result.teams.length === 0) return 'There are no teams yet.';
        return result.teams.map((t) => `• **${t.label}** (\`${t.slug}\`)`).join('\n');
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  // Public: shared reference. Visibility is locked at defer time in the Discord
  // adapter (see team.js `list` subcommand); this keeps the neutral payload consistent.
  return { content, ephemeral: false };
}

export function renderRenameTeamResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'RENAMED':
        return `✅ Renamed to **${result.team.label}** (\`${result.team.slug}\`).`;
      case 'TEAM_NOT_FOUND':
        return "There's no team with that slug.";
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  return { content, ephemeral: true };
}

export function renderAddMemberResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'ADDED':
        return `✅ Added **${result.person.display_name}** to **${result.team.label}**.`;
      case 'USER_NOT_LINKED':
        return USER_NOT_LINKED_MSG;
      case 'TEAM_NOT_FOUND':
        return "There's no team with that slug.";
      case 'ALREADY_ON_TEAM':
        return `**${result.person.display_name}** is already on **${result.team.label}**.`;
      case 'MEMBERSHIP_INVALID':
        return `The directory rejected that add: ${result.detail}`;
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  return { content, ephemeral: true };
}

export function renderRemoveMemberResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'REMOVED':
        return `✅ Removed **${result.person.display_name}** from **${result.team.label}**.`;
      case 'USER_NOT_LINKED':
        return "That user hasn't linked their directory account, so they aren't on any team.";
      case 'TEAM_NOT_FOUND':
        return "There's no team with that slug.";
      case 'NOT_ON_TEAM':
        return `**${result.person.display_name}** is not on **${result.team.label}**.`;
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  return { content, ephemeral: true };
}

export function renderRosterResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'ROSTER': {
        const header = `**${result.team.label}** (\`${result.team.slug}\`)`;
        if (result.members.length === 0) return `${header}\n_No members yet._`;
        const lines = result.members.map((m) => {
          const adminTag = m.is_team_admin ? ' — team admin' : '';
          return `• **${m.person.display_name}** — ${m.role_kind_id}${adminTag}`;
        });
        return [header, ...lines].join('\n');
      }
      case 'TEAM_NOT_FOUND':
        return "There's no team with that slug.";
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  // Public: shared reference. Visibility is locked at defer time in the Discord
  // adapter (see team.js `roster` subcommand); this keeps the neutral payload consistent.
  return { content, ephemeral: false };
}

export function renderMyTeamsResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'MY_TEAMS':
        if (result.memberships.length === 0) return "You're not on any team yet.";
        return result.memberships
          .map((m) => {
            const adminTag = m.is_team_admin ? ' — team admin' : '';
            return `• **${m.team.label}** (\`${m.team.slug}\`) — ${m.role_kind_id}${adminTag}`;
          })
          .join('\n');
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  return { content, ephemeral: true };
}
