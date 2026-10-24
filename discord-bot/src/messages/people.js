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
