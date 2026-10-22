import { MessageFlags } from 'discord.js';
import { DELIVERY_ERROR, discordMessageParts } from './messages.js';

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
 * Guild + scope channel for directory channel settings (#238 / #239).
 * Threads inherit the parent channel's scope; DMs have no guild and no scope.
 *
 * @param {object} interaction Discord interaction.
 * @returns {{ discordGuildId: string|null, discordChannelId: string|null, channelUsedParent: boolean }}
 */
export function channelScopeFromInteraction(interaction) {
  const discordGuildId = interaction.guildId ?? null;
  if (!discordGuildId) {
    return { discordGuildId: null, discordChannelId: null, channelUsedParent: false };
  }
  const channel = interaction.channel;
  if (channel && typeof channel.isThread === 'function' && channel.isThread()) {
    return {
      discordGuildId,
      discordChannelId: channel.parentId ?? null,
      channelUsedParent: Boolean(channel.parentId),
    };
  }
  return {
    discordGuildId,
    discordChannelId: interaction.channelId ?? channel?.id ?? null,
    channelUsedParent: false,
  };
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
  const scope = channelScopeFromInteraction(interaction);
  return {
    surface: 'discord',
    ...scope,
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
  if (payload.allowedMentions) out.allowedMentions = payload.allowedMentions;
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

  // Discord locks visibility at defer time. Follow-ups must explicitly keep it.
  const privateReply = interaction.ephemeral ?? Boolean(dpayload.flags & MessageFlags.Ephemeral);
  const flags = privateReply ? MessageFlags.Ephemeral : undefined;
  let first = true;
  const send = async (part) => {
    const { flags: _flags, ...editable } = part;
    if (first && interaction.deferred && !interaction.replied) {
      await interaction.editReply(editable);
    } else {
      const method = first && !interaction.replied ? 'reply' : 'followUp';
      await interaction[method]({ ...editable, ...(flags !== undefined && { flags }) });
    }
    first = false;
  };
  try {
    for (const part of discordMessageParts(dpayload)) await send(part);
  } catch {
    // Discord errors can contain the rejected payload. Never log reply content.
    console.error('Could not deliver Discord reply');
    try {
      await send({ content: DELIVERY_ERROR, embeds: [], allowedMentions: { parse: [] } });
    } catch {
      // If editing the deferred reply also fails, try clearing its spinner.
      if (first && interaction.deferred && !interaction.replied) {
        await interaction.deleteReply?.().catch(() => {});
      }
    }
  }
}
