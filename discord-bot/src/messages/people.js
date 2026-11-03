import { FALLBACK, DIRECTORY_DOWN_MSG, escapeInline } from './common.js';

export function buildWhoamiEmbed(person, identifiers) {
  return {
    embeds: [
      {
        title: person.display_name,
        fields: [
          { name: 'Email', value: person.primary_email },
          { name: 'Access level', value: person.access_level },
          { name: 'Status', value: person.active ? 'Active' : 'Inactive' },
          { name: 'Identities', value: formatIdentities(identifiers) },
        ],
      },
    ],
    ephemeral: true,
  };
}

function formatIdentities(identifiers) {
  if (identifiers === null) return '_(unavailable)_';
  if (identifiers.length === 0) return '_(none)_';
  const sorted = [...identifiers].sort((a, b) => a.provider.localeCompare(b.provider));
  return sorted.map(formatIdentityLine).join('\n');
}

// Discord identities render as a mention (<@snowflake>) — Discord renders it as
// a clickable name chip that always reflects the user's current display name.
// Mentions inside embed fields do NOT ping (Discord suppresses notifications
// from embeds), so this is purely a UX improvement, not an accidental ping.
// The stored `handle` is a snapshot from link time and can drift; the mention is
// always current, so we drop `handle` from the discord line entirely.
function formatIdentityLine(i) {
  if (i.provider === 'discord') return `discord: <@${i.external_id}>`;
  return i.handle
    ? `${i.provider}: ${i.handle} (${i.external_id})`
    : `${i.provider}: ${i.external_id}`;
}

export function renderSeedResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'SEEDED':
        return `✅ Added **${result.person.display_name}** (${result.person.primary_email}) as ${result.person.access_level}. They can now \`/link\`.`;
      case 'EXISTS':
        return `That email is already in the directory: ${result.detail}`;
      case 'ESCALATION_DENIED':
        return `You can only grant levels at or below your own (${result.callerLevel}).`;
      case 'DIRECTORY_DOWN':
        return 'The directory is temporarily unavailable. Please try again shortly.';
      default:
        return 'Something went wrong. Please try again.';
    }
  })();
  return { content, ephemeral: true };
}

const PROMOTION_DENIED =
  'You can promote only another active member whose current global level is below yours, up to your own level. Nothing was changed.';

export function renderPromoteResult(result) {
  const messages = {
    PROMOTED: () =>
      `✅ Promoted **${escapeInline(result.person.display_name)}** from ${escapeInline(result.previousLevel)} to ${escapeInline(result.person.access_level)} (global access level). Team roles are unchanged.`,
    UNCHANGED: () => 'That member already has the requested access level. Nothing was changed.',
    SELF: () => 'You cannot change your own access level. Nothing was changed.',
    FORBIDDEN: () => PROMOTION_DENIED,
    TARGET_NOT_LOWER: () => PROMOTION_DENIED,
    ESCALATION_DENIED: () => PROMOTION_DENIED,
    DEMOTION_UNSUPPORTED: () => 'Demotions are not supported. Nothing was changed.',
    INVALID_LEVEL: () =>
      'That access-level change is not valid: only admin or superuser can be granted, from a known current level. Nothing was changed.',
    USER_NOT_LINKED: () =>
      'That mentioned member is not linked. Ask them to `/link`, or ask Misty in chat by name or email. Nothing was changed.',
    PERSON_NOT_FOUND: () => 'No active directory member matched. Nothing was changed.',
    PERSON_AMBIGUOUS: () => 'More detail is needed to identify that member. Nothing was changed.',
    PERSON_INACTIVE: () => 'That directory member is inactive. Nothing was changed.',
    PERSON_CHANGED: () =>
      'That member changed since the preview. Nothing was changed; ask again for a fresh preview.',
    SCOPE_MISSING: () =>
      "The directory refused Misty's key for this change (it needs a valid key with `people:write` and `people:elevate`). Nothing was changed; ask a maintainer.",
    UPDATE_REJECTED: () =>
      'The directory rejected this change. Nothing was changed; check the member with a maintainer.',
    UPDATE_UNCERTAIN: () =>
      'The promotion may have completed, but its result could not be confirmed. Check the member with a maintainer before asking again. It was not retried.',
    DIRECTORY_DOWN: () => DIRECTORY_DOWN_MSG,
  };
  return { content: messages[result.outcome]?.() ?? FALLBACK, ephemeral: true };
}
