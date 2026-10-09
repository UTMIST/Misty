import { MessageFlags } from 'discord.js';
import { dispatch, dispatchAutocomplete } from '../../router.js';
import { authMessages } from '../../messages/auth.js';
import {
  interactionToIntent,
  interactionToAutocompleteIntent,
  resolveEphemeral,
  safeReply,
} from './interactions.js';
import { startsWithBotMention, isReplyToBotInOwnedThread, handleMention } from './mentions.js';
import { handleRecordInteraction } from './recording.js';
import { createAutoStop, createMeetingPrompt } from './voice.js';

// Discord event wiring. Conversion, mentions, recording, and voice events live
// in sibling modules; the application router and commands stay surface-neutral.

export function wireDiscordClient(client, { commands, appContext }) {
  client.on('interactionCreate', async (interaction) => {
    // Autocomplete interactions are a separate path: they CANNOT be deferred and
    // must respond within 3s. Best-effort — dispatchAutocomplete never throws.
    if (interaction.isAutocomplete()) {
      const command = commands.get(interaction.commandName);
      if (!command) return;
      try {
        const intent = interactionToAutocompleteIntent(interaction);
        const suggestions = await dispatchAutocomplete(intent, { commands, appContext });
        await interaction.respond(suggestions.slice(0, 25)).catch(() => {});
      } catch (err) {
        console.error('Autocomplete error:', err);
        await interaction.respond([]).catch(() => {});
      }
      return;
    }

    if (!interaction.isChatInputCommand()) return;

    // `/record` is a dedicated adapter path (live voice I/O has no neutral
    // equivalent) — intercept it BEFORE the neutral dispatch below so the
    // command/router contract stays surface-agnostic.
    if (interaction.commandName === 'record') {
      try {
        await handleRecordInteraction(
          interaction,
          appContext,
          commands.get('record'),
          client.user?.id,
        );
      } catch (err) {
        console.error('Unhandled /record error:', err);
      }
      return;
    }

    const command = commands.get(interaction.commandName);
    if (!command) return;
    try {
      const intent = interactionToIntent(interaction, command);
      // Acknowledge within Discord's 3s deadline BEFORE running the handler.
      // The handler may hit a cold-starting (asleep) Neon database that takes
      // several seconds to boot; without an early defer the interaction token
      // expires and the reply fails ("Unknown interaction") even though the DB
      // work succeeds. Deferring extends the response window to 15 minutes.
      const ephemeral = resolveEphemeral(command, intent.subcommand);
      await interaction.deferReply(ephemeral ? { flags: MessageFlags.Ephemeral } : {});
      const payload = await dispatch(intent, { commands, appContext });
      await safeReply(interaction, payload);
    } catch (err) {
      console.error('Unhandled interaction error:', err);
      // We already deferred, so the user is staring at "thinking…". Resolve the
      // interaction with a generic error instead of leaving it hanging until the
      // token expires. safeReply routes this via editReply on the deferred
      // message; its own catch swallows any follow-on failure.
      await safeReply(interaction, authMessages.internalError());
    }
  });

  client.on('messageCreate', async (message) => {
    try {
      if (message.author?.bot) return;
      const botId = client.user?.id;
      if (!botId) return;
      const isMention = startsWithBotMention(message.content, botId);
      if (!isMention && !(await isReplyToBotInOwnedThread(message, botId))) return;
      await handleMention(message, { appContext, botId });
    } catch (err) {
      console.error('Unhandled helper message error:', err);
    }
  });

  // Auto-stop a recording when everyone leaves its voice channel (debounced).
  const onVoiceStateUpdate = createAutoStop({
    meetingSurface: appContext.meetingSurface,
    getBotId: () => client.user?.id,
  });
  const onMeetingPrompt = createMeetingPrompt({
    meetingSurface: appContext.meetingSurface,
    getBotId: () => client.user?.id,
    getUser: (userId) => client.users.fetch(userId),
  });
  client.on('voiceStateUpdate', (oldState, newState) => {
    try {
      // Track bot moves before auto-stop counts occupants. The voice connection
      // follows Discord's move, so the session must follow it too.
      const botId = client.user?.id;
      if (
        botId &&
        newState?.id === botId &&
        newState.channelId &&
        newState.channelId !== oldState?.channelId
      ) {
        const channel =
          newState.channel ?? newState.guild?.channels?.cache?.get(newState.channelId);
        if (channel) {
          appContext.meetingSurface?.updateVoiceChannel?.(newState.guild.id, channel);
        }
      }
      onVoiceStateUpdate(oldState, newState);
      onMeetingPrompt(oldState, newState);
    } catch (err) {
      console.error('voiceStateUpdate handler error:', err?.message ?? err);
    }
  });
}
