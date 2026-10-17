import { FALLBACK, DIRECTORY_DOWN_MSG } from './common.js';

const USER_NOT_LINKED_MSG =
  "That user hasn't linked their directory account yet. Ask them to run `/link` first, then try again.";

// ponytail: domain matches production GOOGLE_GROUPS_DOMAIN; prefer group_email from the status API when present.
const GOOGLE_GROUPS_DOMAIN = 'utmist.skule.ca';
const INFRA_OWNER_EMAIL = 'infrastructure@utmist.skule.ca';

function googleGroupSettingsUrl(slugOrEmail) {
  const local = String(slugOrEmail).includes('@')
    ? String(slugOrEmail).split('@')[0]
    : String(slugOrEmail);
  return `https://groups.google.com/a/${GOOGLE_GROUPS_DOMAIN}/g/${encodeURIComponent(local)}/settings`;
}

function externalMembersSetupHint(slugOrEmail) {
  const settingsUrl = googleGroupSettingsUrl(slugOrEmail);
  // Both Groups Settings fields matter. Owner ability to change the second on a
  // fresh group is unverified (#272); if it is admin-only, escalate instead of
  // looping on Allow external members.
  return [
    `Ask someone with access to \`${INFRA_OWNER_EMAIL}\` to open [Group settings](${settingsUrl}) and set:`,
    `1. **Allow external members** — on`,
    `2. **Who can add external members** — end users / members can add (not “only admins”)`,
    `Save, then run \`/team google-sync\` for this team.`,
    `If (1) is already on but sync still needs external members and (2) is locked to administrators only, stop toggling (1) — ask infrastructure to escalate to a Workspace Super Admin.`,
    `_Note: whether a group owner can change (2) on every new group is not yet verified._`,
  ].join('\n');
}

function renderGoogleGroupState(team, state, { includeSetup = false } = {}) {
  const email = state.group_email ?? `${team.slug}@${GOOGLE_GROUPS_DOMAIN}`;
  const header = `Google Group for **${team.label}** (\`${email}\`)`;
  let content;
  switch (state.status) {
    case 'synced':
      content = [
        `✅ ${header} is **synced**.`,
        '_Note: an empty group can show synced before any external member has been tried. That is not proof external membership is enabled. Run `/team google-status` for setup instructions._',
      ].join('\n');
      break;
    case 'needs_external_members':
      content = [
        `⚠️ ${header} needs external-member settings.`,
        state.last_error ? `_Directory note:_ ${state.last_error}` : null,
      ]
        .filter(Boolean)
        .join('\n');
      break;
    case 'failed':
      content = [
        `❌ ${header} sync **failed**.`,
        state.last_error ? `_${state.last_error}_` : null,
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
    (includeSetup ||
      state.status === 'needs_external_members' ||
      /external members/i.test(state.last_error ?? ''))
  ) {
    return [content, '', externalMembersSetupHint(email)].join('\n');
  }
  return content;
}

function renderGoogleAfterCreate(team, google) {
  switch (google?.outcome) {
    case 'SYNC_RESULT':
      return renderGoogleGroupState(team, google.state, { includeSetup: true });
    case 'PENDING':
      return 'Google Group provisioning may still be in progress. Run `/team google-status` in a moment for status and setup instructions.';
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
        return renderGoogleGroupState(result.team, result.state, { includeSetup: true });
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
