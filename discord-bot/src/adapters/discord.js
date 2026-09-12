import { MessageFlags, AttachmentBuilder } from 'discord.js';
import { dispatch, dispatchAutocomplete } from '../router.js';
import { authMessages } from '../messages.js';
import { resolvePrincipal } from '../auth/principal.js';
import { authorize } from '../auth/policy.js';
import { DirectoryUnavailable } from '../directoryClient.js';

// The ONLY module (aside from src/index.js and src/registerCommands.js) that
// imports from discord.js. Everything else — router, commands, services —
// stays surface-agnostic.

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

async function safeReply(interaction, payload) {
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

const DISCORD_MAX_MESSAGE = 2000;

export function startsWithBotMention(content, botId) {
  const trimmed = (content ?? '').trimStart();
  return trimmed.startsWith(`<@${botId}>`) || trimmed.startsWith(`<@!${botId}>`);
}

export function stripLeadingMention(content, botId) {
  const trimmed = (content ?? '').trimStart();
  for (const tag of [`<@${botId}>`, `<@!${botId}>`]) {
    if (trimmed.startsWith(tag)) return trimmed.slice(tag.length).trimStart();
  }
  return trimmed;
}

// Nickname when the member is resolved, else the global/user name — the label a
// human would recognize, and the fallback when the directory can't identify them.
function authorLabel(message) {
  return (
    message.member?.displayName ?? message.author?.globalName ?? message.author?.username ?? ''
  );
}

// Chronological array of fetched messages -> neutral turns carrying author
// metadata. bot -> assistant, everyone else -> user; strip a leading mention
// from user turns; drop empties; drop leading assistant turns.
//
// Same-role turns are deliberately NOT collapsed here: adjacent user messages
// can come from different people, and each needs its own identity tag. The
// service merges them once they're rendered.
export function threadHistoryToTurns(fetched, botId) {
  return fetched
    .map((m) => {
      const isBot = m.author?.id === botId;
      const raw = m.content ?? '';
      const text = (isBot ? raw : stripLeadingMention(raw, botId)).trim();
      if (isBot) return { role: 'assistant', text };
      return { role: 'user', text, authorId: m.author?.id, authorName: authorLabel(m) };
    })
    .filter((t) => t.text.length > 0)
    .reduce((turns, t) => {
      // Leading assistant turns carry no question to answer.
      if (!turns.length && t.role === 'assistant') return turns;
      turns.push(t);
      return turns;
    }, []);
}

export function chunkForDiscord(text) {
  const chunks = [];
  let remaining = text ?? '';
  while (remaining.length > DISCORD_MAX_MESSAGE) {
    let cut = remaining.lastIndexOf('\n', DISCORD_MAX_MESSAGE);
    if (cut <= 0) cut = DISCORD_MAX_MESSAGE; // no newline → hard split
    chunks.push(remaining.slice(0, cut));
    remaining = remaining.slice(cut).replace(/^\n/, '');
  }
  if (remaining.length > 0) chunks.push(remaining);
  return chunks;
}

const HISTORY_LIMIT = 100; // Discord's messages.fetch maximum; no pagination
const LINK_PROMPT =
  'You need to link your account first. Run `/link` to identify yourself, then try again.';
const VERIFY_UNAVAILABLE =
  "I can't verify you right now — the directory is unavailable. Please try again shortly.";
const LLM_UNAVAILABLE =
  "I'm having trouble reaching the assistant right now — please try again shortly.";
const EMPTY_ANSWER = "I couldn't come up with an answer that time — please try rephrasing.";
const THREAD_UNAVAILABLE =
  "I couldn't open a thread for that — please try again. (I may be missing the 'Create Public Threads' or 'Read Message History' permission.)";

// The message a thread was started from lives in the PARENT channel, not the
// thread — ThreadChannel#fetchStarterMessage reads it from `parent` unless the
// parent is thread-only (a forum), where the starter really is the thread's
// first message. So a thread's own fetch omits the opening question, and since
// the bot's first answer then leads the history, the leading-assistant shave
// dropped that too: the founding exchange was missing from every replay.
//
// Mutates `ordered` in place, prepending the starter when it isn't already
// there. Best-effort — a deleted or unreachable starter costs context, never
// the answer, so this never throws into handleMention's THREAD_UNAVAILABLE path.
async function prependStarterMessage(thread, ordered) {
  let starter;
  try {
    starter = await thread.fetchStarterMessage?.();
  } catch (e) {
    console.error('starter message fetch failed:', e.message);
    return;
  }
  if (!starter) return;
  // Forum threads return a starter already present in the fetch. Compare only
  // when both ids exist, so two id-less messages don't collapse into one.
  if (starter.id && ordered.some((m) => m.id && m.id === starter.id)) return;
  ordered.unshift(starter);
}

// Handle a message that starts with the bot's mention. Linked-only. In a
// channel it opens a thread; in a thread it replays the thread's history —
// including the starter message — as memory. Never throws to discord.js.
export async function handleMention(message, { appContext, botId }) {
  const question = stripLeadingMention(message.content, botId);
  if (!question) return; // bare ping, nothing to answer

  let principal;
  try {
    principal = await resolvePrincipal(appContext.directory, message.author.id);
  } catch (e) {
    if (e instanceof DirectoryUnavailable) {
      await message.reply(VERIFY_UNAVAILABLE).catch(() => {});
      return;
    }
    throw e;
  }
  if (!authorize('linked', principal).ok) {
    await message.reply(LINK_PROMPT).catch(() => {});
    return;
  }

  let target;
  let turns;
  try {
    if (message.channel.isThread()) {
      target = message.channel;
      const fetched = await target.messages.fetch({ limit: HISTORY_LIMIT });
      const ordered = [...fetched.values()].sort(
        (a, b) => (a.createdTimestamp ?? 0) - (b.createdTimestamp ?? 0),
      );
      await prependStarterMessage(target, ordered);
      turns = threadHistoryToTurns(ordered, botId);
    } else {
      target = await message.startThread({ name: question.slice(0, 100) });
      turns = [
        {
          role: 'user',
          text: question,
          authorId: message.author?.id,
          authorName: authorLabel(message),
        },
      ];
    }
  } catch (e) {
    console.error('thread create/fetch failed:', e.message);
    await message.reply(THREAD_UNAVAILABLE).catch(() => {});
    return;
  }
  if (!turns.length) return;

  await target.sendTyping().catch(() => {});
  let content;
  try {
    ({ content } = await appContext.helperService.answer({ turns, principal }));
  } catch (err) {
    console.error('helper answer failed:', err.message);
    await target.send(LLM_UNAVAILABLE).catch(() => {});
    return;
  }
  if (!content || !content.trim()) {
    await target.send(EMPTY_ANSWER).catch((e) => console.error('helper reply failed:', e.message));
    return;
  }
  for (const chunk of chunkForDiscord(content)) {
    await target.send(chunk).catch((e) => console.error('helper reply failed:', e.message));
  }
}

// Base64-decodes the report produced by the meeting service into a Discord
// attachment and posts it to the meeting's text channel, @-mentioning whoever
// started the recording so they're notified the minutes are ready. Never
// throws — meetingSurface.stop() awaits this and a poster failure must not
// prevent the session from being torn down.
//
// Only the minutes PDF is posted. The meeting service no longer returns mixed
// audio at all (see services/meeting's StopResponse), so there's no second
// attachment and no oversized-payload fallback to make.
// Tells a meeting's text channel that its recording died on its own. Never
// throws -- it runs inside teardown, which must complete regardless.
export function makeChannelNotifier() {
  return async ({ channel, content }) => {
    try {
      await channel?.send({ content });
    } catch (e) {
      console.error('meeting notice failed:', e.message);
    }
  };
}

export function makeAttachmentPoster() {
  return async ({ channel, report, requesterId }) => {
    try {
      const pdfFile = new AttachmentBuilder(Buffer.from(report.pdf_b64, 'base64'), {
        name: 'meeting-minutes.pdf',
      });
      // No requesterId (e.g. a recording started before this field existed, or
      // any path that couldn't resolve one) => post unaddressed rather than
      // dropping the minutes.
      const content = requesterId ? `<@${requesterId}> 📄 Meeting minutes` : '📄 Meeting minutes';
      // Deliberately NOT swallowed. The service session is destroyed the moment
      // it responds, so if this send fails the minutes are gone for good --
      // reporting success would tell the user "minutes will post here shortly"
      // for a meeting that no longer exists anywhere. Let it propagate so
      // /record stop reports an error and they know to look.
      await channel.send({ content, files: [pdfFile] });
    } catch (e) {
      console.error('meeting minutes post failed:', e.message);
      throw e;
    }
  };
}

// `/record` is a DEDICATED adapter path, not a neutral command: it drives
// live voice I/O (joining a voice channel, streaming Opus) that has no
// equivalent on other surfaces, so it bypasses dispatch()/the router entirely
// and talks straight to appContext.meetingSurface. commands/record.js exists
// solely for slash-command registration metadata.
async function handleRecordInteraction(interaction, appContext, recordCommand) {
  const subcommand = interaction.options.getSubcommand(false);
  await interaction.deferReply({ flags: MessageFlags.Ephemeral }).catch(() => {});

  const reply = (content) =>
    interaction
      .editReply({ content })
      .catch((e) => console.error('record reply failed:', e.message));

  // `/record` bypasses the neutral dispatch, which is where the Policy
  // Enforcement Point normally lives -- so re-run authenticate -> authorize
  // here. Resolve the SAME policy the router would (per-subcommand auth, else
  // command auth, fail-secure to 'linked') from the command metadata rather
  // than hardcoding it, so the two can't drift if record.js is ever retightened.
  // `start` inherits 'linked' (recording consumes resources); `status`/`stop`
  // are declared 'public' so a directory outage can't strand a live recording.
  const activeSub = recordCommand?.subcommands?.find((s) => s.name === subcommand);
  const rawAuth = activeSub?.auth ?? recordCommand?.auth;
  const policy = (typeof rawAuth === 'function' ? rawAuth(interaction) : rawAuth) ?? 'linked';

  if (policy !== 'public') {
    let principal;
    try {
      principal = await resolvePrincipal(appContext.directory, interaction.user.id);
    } catch (e) {
      if (e instanceof DirectoryUnavailable) {
        await reply(authMessages.unavailable().content);
        return;
      }
      console.error('record auth lookup failed:', e.message);
      await reply(authMessages.internalError().content);
      return;
    }
    const decision = authorize(policy, principal);
    if (!decision.ok) {
      await reply(authMessages.denied(decision.reason).content);
      return;
    }
  }

  if (subcommand === 'start') {
    const voiceChannel = interaction.member?.voice?.channel;
    if (!voiceChannel) {
      await reply('Join a voice channel first.');
      return;
    }
    let result;
    try {
      result = await appContext.meetingSurface.start({
        guildId: interaction.guildId,
        voiceChannel,
        textChannel: interaction.channel,
        // Remembered for the whole session so the minutes @-mention whoever
        // started the recording, even when auto-stop ends it.
        requesterId: interaction.user.id,
      });
    } catch (e) {
      console.error('meetingSurface.start failed:', e.message);
      await reply("Couldn't start recording — the meeting service may be unavailable.");
      return;
    }
    if (result.status === 'already-recording') await reply('Already recording.');
    else if (result.status === 'error') {
      // The join failed (missing Connect permission, full channel, timeout).
      // Without this branch the failure fell through to "🔴 Recording…" --
      // the exact false positive this change exists to remove.
      await reply("Couldn't start recording — I couldn't join that voice channel.");
    } else if (result.status === 'unconfigured') await reply("Meeting recording isn't configured.");
    else await reply('🔴 Recording…');
    return;
  }

  if (subcommand === 'status') {
    const result = appContext.meetingSurface.status(interaction.guildId);
    if (result.status === 'not-recording') await reply('No recording in progress.');
    else await reply(`🔴 Recording (${Math.round((result.elapsedMs ?? 0) / 1000)}s elapsed).`);
    return;
  }

  if (subcommand === 'stop') {
    let result;
    try {
      result = await appContext.meetingSurface.stop(interaction.guildId);
    } catch (e) {
      console.error('meetingSurface.stop failed:', e.message);
      await reply("Couldn't stop recording — please try again.");
      return;
    }
    if (result.status === 'not-recording') await reply('No recording in progress.');
    else if (result.status === 'error') await reply('Something went wrong stopping the recording.');
    else await reply('⏳ Processing — minutes will post here shortly.');
    return;
  }

  await reply('Unknown /record subcommand.');
}

export const AUTO_STOP_GRACE_MS = 20_000;

// Count the human (non-bot) occupants of a recorded voice channel.
//
// We count from `guild.voiceStates.cache`, NOT `channel.members`. `channel.members`
// resolves each voice state to a `GuildMember` via `guild.members.cache`, which is
// only kept populated by the privileged `GuildMembers` intent — which the bot does
// not request (see index.js). Without it that member resolution is unreliable, so a
// still-present member (including the bot itself, whose member often isn't cached)
// can be miscounted, which is why auto-stop wasn't firing. `voiceStates.cache` is
// maintained by `GuildVoiceStates` (which we DO have — it's what powers voice
// receive) and carries each occupant's user id + channel id directly, with no
// member-cache dependency. We exclude the recorder bot by its own user id (the
// reliable signal) and other bots best-effort via any resolved member.
function humansIn(voiceChannel, botId) {
  const guild = voiceChannel?.guild;
  if (!guild) return 0;
  let count = 0;
  for (const state of guild.voiceStates.cache.values()) {
    if (state.channelId !== voiceChannel.id) continue;
    if (botId && state.id === botId) continue; // the recorder bot itself
    if (state.member?.user?.bot) continue; // other bots (best-effort; may be uncached)
    count += 1;
  }
  return count;
}

function findMeetingAnnouncementChannel(guild) {
  if (guild?.systemChannel?.send) return guild.systemChannel;
  return guild?.channels?.cache?.find(
    (channel) => channel?.send && channel.isTextBased?.() && !channel.isThread?.(),
  );
}

const NEW_MEETING_PROMPT =
  'A new meeting is starting. Run `/record start` to begin recording the voice channel.';

// Prompt the first human to enter an otherwise empty voice channel. Starting
// remains an explicit slash command because it is linked-only and consumes a
// voice connection plus a live meeting session.
export function createMeetingPrompt({ meetingSurface, getBotId = () => undefined } = {}) {
  return function onVoiceStateUpdate(oldState, newState) {
    const guild = newState?.guild;
    const guildId = guild?.id;
    const channelId = newState?.channelId;
    if (!guildId || !channelId || oldState?.channelId === channelId) return;

    const botId = getBotId();
    const userId = newState.id ?? newState.member?.id;
    if (!userId || userId === botId || newState.member?.user?.bot) return;
    // The no-meeting-service fallback has no activeSession method. Do not
    // advertise a recording flow that cannot start in that configuration.
    if (typeof meetingSurface?.activeSession !== 'function') return;
    if (meetingSurface.activeSession(guildId)) return;

    const voiceChannel = newState.channel ?? guild.channels?.cache?.get?.(channelId);
    if (humansIn(voiceChannel, botId) !== 1) return;

    const announcementChannel = findMeetingAnnouncementChannel(guild);
    if (!announcementChannel) return;

    try {
      Promise.resolve(
        announcementChannel.send({ content: `<@${userId}> ${NEW_MEETING_PROMPT}` }),
      ).catch((err) => console.error(`meeting prompt failed for guild ${guildId}:`, err));
    } catch (err) {
      console.error(`meeting prompt failed for guild ${guildId}:`, err);
    }
  };
}

// Auto-stop: end a recording when everyone leaves its voice channel. Rather than
// finalizing on the raw "last member left" event (which a transient client blip
// or a voice-region failover would trigger, irreversibly terminating a live
// meeting), we DEBOUNCE: when the recorded channel goes empty we schedule a stop
// after a grace period, and cancel it if a human is back when any later event
// arrives OR if they're back at fire time (re-check).
//
// Each pending timer is bound to the SPECIFIC recording (its `sessionId`) that
// scheduled it. That matters because a guild can record again immediately: if a
// timer scheduled for session A were keyed only by guild, a manual /record stop
// of A followed by a new recording B could let A's stale timer terminate B early
// (and A's still-pending timer would suppress scheduling B's own). Binding to
// sessionId means B always schedules its own full-grace timer, and a timer from
// an ended session no-ops at fire time. Idempotent with `/record stop`.
//
// Injectable timers keep it unit-testable. Returns the voiceStateUpdate handler.
export function createAutoStop({
  meetingSurface,
  getBotId = () => undefined,
  graceMs = AUTO_STOP_GRACE_MS,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
} = {}) {
  const pending = new Map(); // guildId -> { handle, sessionId }

  const cancel = (guildId) => {
    const entry = pending.get(guildId);
    if (entry) {
      clearTimer(entry.handle);
      pending.delete(guildId);
    }
  };

  return function onVoiceStateUpdate(oldState, newState) {
    const guildId = (oldState?.guild ?? newState?.guild)?.id;
    if (!guildId) return;

    const botId = getBotId();
    const session = meetingSurface?.activeSession?.(guildId);
    // Not recording (or session already torn down): drop any pending stop.
    if (!session) return cancel(guildId);
    // Someone is (still/again) present: cancel a pending stop, nothing to do.
    if (humansIn(session.voiceChannel, botId) > 0) return cancel(guildId);

    const existing = pending.get(guildId);
    if (existing) {
      // Already scheduled for THIS recording -> don't stack a second timer.
      if (existing.sessionId === session.sessionId) return;
      // Stale timer from a previous recording in this guild -> replace it.
      clearTimer(existing.handle);
      pending.delete(guildId);
    }

    const { sessionId } = session;
    const handle = setTimer(() => {
      pending.delete(guildId);
      // Re-check at fire time: only stop if it's STILL the same recording and
      // STILL empty (the meeting may have ended, or a human returned).
      const current = meetingSurface?.activeSession?.(guildId);
      if (
        !current ||
        current.sessionId !== sessionId ||
        humansIn(current.voiceChannel, getBotId()) > 0
      ) {
        return;
      }
      Promise.resolve(meetingSurface.stop(guildId)).catch((err) =>
        console.error(`auto-stop failed for guild ${guildId}:`, err?.message ?? err),
      );
    }, graceMs);
    handle?.unref?.(); // don't keep the process alive on the grace timer alone
    pending.set(guildId, { handle, sessionId });
  };
}

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
        await handleRecordInteraction(interaction, appContext, commands.get('record'));
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
      if (!botId || !startsWithBotMention(message.content, botId)) return;
      await handleMention(message, { appContext, botId });
    } catch (err) {
      console.error('Unhandled mention error:', err);
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
  });
  client.on('voiceStateUpdate', (oldState, newState) => {
    try {
      onVoiceStateUpdate(oldState, newState);
      onMeetingPrompt(oldState, newState);
    } catch (err) {
      console.error('voiceStateUpdate handler error:', err?.message ?? err);
    }
  });
}
