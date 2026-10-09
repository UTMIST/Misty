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
