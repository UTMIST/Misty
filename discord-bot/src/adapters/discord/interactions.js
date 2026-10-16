import { MessageFlags } from 'discord.js';

function extractOptions(interaction, activeOptions) {
  const options = {};
  for (const o of activeOptions) {
    if (o.type === 'string') options[o.name] = interaction.options.getString(o.name);
    else if (o.type === 'boolean') options[o.name] = interaction.options.getBoolean(o.name);
    else if (o.type === 'user') options[o.name] = interaction.options.getUser(o.name);
    // extend for other types as they appear
  }
  return options;
}

/**
 * Convert a Discord chat-input interaction into a surface-neutral intent.
 *
 * @param {object} interaction Discord interaction.
 * @param {object} command Neutral command definition.
 * @returns {object} Intent consumed by the application router.
 */
export function interactionToIntent(interaction, command) {
  const subcommand = command.subcommands.length ? interaction.options.getSubcommand(false) : null;
  const activeOptions = subcommand
    ? (command.subcommands.find((s) => s.name === subcommand)?.options ?? [])
    : command.options;
  return {
    surface: 'discord',
    discordGuildId: interaction.guildId ?? null,
    commandName: interaction.commandName,
    options: extractOptions(interaction, activeOptions),
    subcommand,
    discordUserId: interaction.user.id,
    discordHandle: interaction.user.username,
  };
}

export function interactionToAutocompleteIntent(interaction) {
  const focused = interaction.options.getFocused(true); // { name, value, ... }
  return {
    commandName: interaction.commandName,
    subcommand: interaction.options.getSubcommand(false),
    focusedOption: focused.name,
    typed: focused.value ?? '',
    discordUserId: interaction.user.id,
  };
}

export function payloadToDiscordReply(payload) {
  if (!payload) return null;
  const out = {};
  if (payload.content !== undefined) out.content = payload.content;
  if (payload.embeds) out.embeds = payload.embeds;
  if (payload.ephemeral) out.flags = MessageFlags.Ephemeral;
  return out;
}

// Resolve the visibility hint for an interaction BEFORE the handler runs, so we
// can defer with the right ephemerality. Mirrors the router's auth resolution:
// an active subcommand's hint wins, otherwise the command-level hint, otherwise
// fail-safe to ephemeral (private).
export function resolveEphemeral(command, subcommandName) {
  const sub = subcommandName ? command.subcommands.find((s) => s.name === subcommandName) : null;
  return sub?.ephemeral ?? command.ephemeral ?? true;
}

export async function safeReply(interaction, payload) {
  const dpayload = payloadToDiscordReply(payload);
  if (!dpayload) {
    // Nothing to say. If we deferred, clear the "thinking…" state so the user
    // isn't left staring at a spinner.
    if (interaction.deferred && !interaction.replied) {
      await interaction.deleteReply().catch((e) => console.error('deleteReply failed:', e.message));
    }
    return;
  }

  // After deferReply(), the first response must edit the deferred message.
  // Ephemerality was already locked in at defer time, so editReply ignores the
  // ephemeral flag — strip it to avoid passing an unsupported option.
  if (interaction.deferred && !interaction.replied) {
    const { flags, ...editable } = dpayload;
    await interaction.editReply(editable).catch((e) => console.error('reply failed:', e.message));
    return;
  }

  const method = interaction.replied ? 'followUp' : 'reply';
  await interaction[method](dpayload).catch((e) => console.error('reply failed:', e.message));
}
