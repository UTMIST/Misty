/** Reply payloads for help-command errors. */
export const helpMessages = {
  unknownCommand: (name, { shared = false } = {}) => ({
    content: shared
      ? 'That command is not in the shared guide. Use `/help` privately for commands available to your account.'
      : `I couldn't find a command named \`/${name}\` that you can use. Run \`/help\` to see available commands.`,
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
export function buildHelpEmbed(commands, { shared = false } = {}) {
  const sorted = [...commands].sort((a, b) => a.name.localeCompare(b.name));
  return {
    embeds: [
      {
        title: shared ? 'Shared command guide' : 'Available commands',
        description:
          sorted.length === 0
            ? 'No commands are available.'
            : sorted.map((command) => `**/${command.name}** — ${command.description}`).join('\n'),
        footer: {
          text: shared
            ? 'Use /help privately for commands available to your account.'
            : 'Use /help command:<name> for details.',
        },
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
export function buildCommandDetailEmbed(command, { shared = false } = {}) {
  const fields = [];
  if (command.name === 'team') {
    fields.push({
      name: 'Member changes in chat',
      value:
        'With helper tools enabled, ask Misty to add/remove members or update their team role and team-admin flag by mention, full directory name, exact email, or name and email together. Unlinked directory members are supported. Ambiguous names need more detail. All targets must resolve before a private preview and confirmation; slash commands still use user mentions.',
    });
  }
  if (command.name === 'promote') {
    fields.push({
      name: 'Global access, not team roles',
      value:
        'Raises the directory-wide level (member < admin < superuser). You can promote only another active person below your level, up to your own level; no demotions. With helper tools enabled, ask Misty by mention, full name or exact email and confirm the private preview. Team roles use /team update.',
    });
  }
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
        ...(shared && {
          footer: { text: 'Use /help privately for commands available to your account.' },
        }),
        ...(fields.length > 0 ? { fields } : {}),
      },
    ],
    ephemeral: true,
  };
}
