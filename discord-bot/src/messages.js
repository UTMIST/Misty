export const authMessages = {
  unavailable: () => ({
    content:
      "I can't verify you right now — the directory is unavailable. Please try again shortly.",
    ephemeral: true,
  }),
  denied: (reason) => ({
    content:
      reason === 'not_linked'
        ? 'You need to link your account first. Run `/link` to identify yourself, then try again.'
        : reason === 'forbidden'
          ? "You don't have permission to do that."
          : "You're not allowed to do that.",
    ephemeral: true,
  }),
  internalError: () => ({
    content: 'Something went wrong. Please try again.',
    ephemeral: true,
  }),
};

/** Reply payloads for help-command errors. */
export const helpMessages = {
  unknownCommand: (name) => ({
    content: `I couldn't find a command named \`/${name}\` that you can use. Run \`/help\` to see available commands.`,
    ephemeral: true,
  }),
};

/**
 * Format one neutral command option for a Discord embed field.
 *
 * @param {object} option Command option metadata.
 * @returns {string} Human-readable option summary.
 */
function formatOption(option) {
  const required = option.required ? 'required' : 'optional';
  return `\`${option.name}\` (${required}) — ${option.description || 'No description'}`;
}

/**
 * Build an ephemeral embed listing commands visible to the caller.
 *
 * @param {Iterable<object>} commands Visible command definitions.
 * @returns {object} Surface-neutral reply payload.
 */
export function buildHelpEmbed(commands) {
  const sorted = [...commands].sort((a, b) => a.name.localeCompare(b.name));
  return {
    embeds: [
      {
        title: 'Available commands',
        description:
          sorted.length === 0
            ? 'No commands are available.'
            : sorted.map((command) => `**/${command.name}** — ${command.description}`).join('\n'),
        footer: { text: 'Use /help command:<name> for details.' },
      },
    ],
    ephemeral: true,
  };
}

/**
 * Build an ephemeral detail embed for a permission-filtered command.
 *
 * @param {object} command Visible command metadata and subcommands.
 * @returns {object} Surface-neutral reply payload.
 */
export function buildCommandDetailEmbed(command) {
  const fields = [];
  if (command.options.length > 0) {
    fields.push({ name: 'Options', value: command.options.map(formatOption).join('\n') });
  }
  if (command.subcommands.length > 0) {
    fields.push({
      name: 'Subcommands',
      value: command.subcommands
        .map((sub) => `**${sub.name}** — ${sub.description || 'No description'}`)
        .join('\n'),
    });
  }
  return {
    embeds: [
      {
        title: `/${command.name}`,
        description: command.description,
        ...(fields.length > 0 ? { fields } : {}),
      },
    ],
    ephemeral: true,
  };
}

export function renderLinkResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'CODE_SENT':
        return `📧 I've emailed a code to **${result.email}** — run \`/verify-code <code>\` to finish linking.`;
      case 'NOT_A_MEMBER':
        return "I couldn't find that email in the directory. Ask an exec to add you, then run `/link` again.";
      case 'DIRECTORY_DOWN':
        return 'The directory is temporarily unavailable. Please try again shortly.';
      case 'VERIFICATION_DOWN':
        return 'The verification service is temporarily unavailable. Please try again shortly.';
      case 'RATE_LIMITED':
        return "You've requested too many codes recently. Please wait a bit before trying again.";
      default:
        return 'Something went wrong. Please try again.';
    }
  })();
  return { content, ephemeral: true };
}

export function renderVerifyCodeResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'LINKED':
        return `✅ Linked! You're now identified as **${result.person.display_name}**.`;
      case 'NOT_A_MEMBER':
        return "I couldn't find that email in the directory. Ask an exec to add you, then run `/link` again.";
      case 'ALREADY_LINKED':
        return `That link couldn't be created: ${result.detail}`;
      case 'CODE_EXPIRED':
        return 'That code has expired. Run `/link` again to get a new one.';
      case 'TOO_MANY_ATTEMPTS':
        return 'Too many incorrect attempts. Run `/link` again to get a new code.';
      case 'INVALID_CODE':
        return "That code isn't right. Double check it and try again.";
      case 'NO_PENDING_CODE':
        return "I don't have a pending code for you. Run `/link` first.";
      case 'VERIFICATION_DOWN':
        return 'The verification service is temporarily unavailable. Please try again shortly.';
      case 'DIRECTORY_DOWN':
        return 'The directory is temporarily unavailable. Please try again shortly.';
      default:
        return 'Something went wrong. Please try again.';
    }
  })();
  return { content, ephemeral: true };
}

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

const FALLBACK = 'Something went wrong. Please try again.';
const DIRECTORY_DOWN_MSG = 'The directory is temporarily unavailable. Please try again shortly.';
const DOC_DOWN_MSG =
  'The documentation service is temporarily unavailable. Please try again shortly.';
const USER_NOT_LINKED_MSG =
  "That user hasn't linked their directory account yet. Ask them to run `/link` first, then try again.";

export function renderCreateTeamResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'CREATED':
        return `✅ Created team **${result.team.label}** (\`${result.team.slug}\`).`;
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

function docLine(doc) {
  const title = doc.title || doc.url;
  const src = doc.source_id ? ` · ${doc.source_id}` : '';
  return `• **${title}**${src} — \`${doc.id}\``;
}

export function renderDocAddResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'ADDED':
      case 'MERGED': {
        const verb =
          result.outcome === 'ADDED' ? '✅ Catalogued' : '✅ Already catalogued (tags merged)';
        const title = result.doc.title || result.doc.url;
        const lines = [`${verb}: **${title}**`, result.doc.url, `id: \`${result.doc.id}\``];
        if (result.warnings && result.warnings.length > 0) {
          lines.push(`⚠️ ${result.warnings.join('; ')}`);
        }
        return lines.join('\n');
      }
      case 'TEAM_NOT_FOUND':
        return "There's no team with that slug.";
      case 'BAD_REFERENCE':
        return `The doc service rejected that: ${result.detail}`;
      case 'DOC_DOWN':
        return DOC_DOWN_MSG;
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  return { content, ephemeral: true };
}

export function renderDocListResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'LISTED':
        if (result.docs.length === 0) return 'There are no docs matching that.';
        return result.docs.map(docLine).join('\n');
      case 'TEAM_NOT_FOUND':
        return "There's no team with that slug.";
      case 'DOC_DOWN':
        return DOC_DOWN_MSG;
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  // Public: shared reference. Visibility is locked at defer time in the Discord
  // adapter (see doc.js `list` subcommand); this keeps the neutral payload consistent.
  return { content, ephemeral: false };
}

export function renderDocShowResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'SHOWN': {
        const d = result.doc;
        const lines = [`**${d.title || d.url}**`, d.url];
        if (d.description) lines.push(d.description);
        if (d.owning_team_label) lines.push(`Owner: ${d.owning_team_label}`);
        if (d.tags && d.tags.length > 0) lines.push(`Tags: ${d.tags.join(', ')}`);
        if (d.source_id) lines.push(`Source: ${d.source_id}`);
        lines.push(`id: \`${d.id}\``);
        return lines.join('\n');
      }
      case 'NOT_FOUND':
        return "There's no doc with that id.";
      case 'DOC_DOWN':
        return DOC_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  return { content, ephemeral: false };
}

export function renderAddEmailResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'CODE_SENT':
        return `📧 I've emailed a code to **${result.email}** — run \`/verify-email <code>\` to add it.`;
      case 'RATE_LIMITED':
        return 'That email was just sent a code. Please wait a moment and try again.';
      case 'VERIFICATION_DOWN':
        return 'The verification service is unavailable right now. Please try again shortly.';
      default:
        return 'Something went wrong. Please try again.';
    }
  })();
  return { content, ephemeral: true };
}

export function renderVerifyEmailResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'ADDED':
        return `✅ Added **${result.email}** to your account.`;
      case 'EMAIL_TAKEN':
        return "That email is already registered to a member, so I can't add it to your account.";
      case 'CODE_EXPIRED':
        return 'That code has expired. Run `/add-email` again to get a new one.';
      case 'TOO_MANY_ATTEMPTS':
        return 'Too many incorrect attempts. Run `/add-email` again to get a new code.';
      case 'INVALID_CODE':
        return "That code doesn't match. Double-check it and try again.";
      case 'NO_PENDING_CODE':
        return 'I have no pending code for you. Run `/add-email <email>` first.';
      case 'VERIFICATION_DOWN':
        return 'The verification service is unavailable right now. Please try again shortly.';
      case 'DIRECTORY_DOWN':
        return 'The directory is temporarily unavailable. Please try again shortly.';
      default:
        return 'Something went wrong. Please try again.';
    }
  })();
  return { content, ephemeral: true };
}

export function renderDocRemoveResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'REMOVED':
        return `✅ Removed **${result.doc.title || result.doc.id}** from the catalog.`;
      case 'NOT_FOUND':
        return "There's no doc with that id.";
      case 'DOC_DOWN':
        return DOC_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  return { content, ephemeral: true };
}
