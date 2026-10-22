import { prepareHelperReply, resolveHelperCaller, WITHHELD_ANSWER } from '../../helperFlow.js';
import { sendDiscordMessage } from './messages.js';
import { presentHelperAction } from './helperActions.js';
import { runRecordAction } from './recording.js';
import { runQnaAction } from './qna.js';
import recordCommand from '../../commands/record.js';
import { ChannelType, PermissionFlagsBits } from 'discord.js';

/** Bind Discord metadata outside model input. Meetings keep the exact source channel. */
export function createHelperRequest(message, appContext, botId) {
  const userId = message.author.id;
  const guildId = message.guild?.id ?? message.guildId ?? null;
  const sourceChannel = message.channel;
  const parent = sourceChannel.isThread?.() ? sourceChannel.parentId : null;
  const recording = guildId ? appContext.meetingSurface?.activeSession?.(guildId) : null;
  async function sourceAccess() {
    if (!guildId) return {};
    const member = await message.guild.members.fetch({ user: userId, force: true });
    const channel = await message.guild.channels.fetch(sourceChannel.id, { force: true });
    if (channel?.isThread?.() && channel.parentId) {
      await message.guild.channels.fetch(channel.parentId, { force: true });
    }
    const permissions = channel?.permissionsFor(member);
    if (!permissions?.has(PermissionFlagsBits.ViewChannel))
      throw new Error('Channel access changed.');
    if (
      channel.type === ChannelType.PrivateThread &&
      !permissions.has(PermissionFlagsBits.ManageThreads)
    ) {
      const membership = await channel.members.fetch({ member: userId, force: true });
      if (!membership) throw new Error('Thread access changed.');
    }
    return { member, channel };
  }
  return Object.freeze({
    discordUserId: userId,
    discordHandle: message.author.username,
    discordGuildId: guildId,
    discordChannelId: parent ?? sourceChannel.id,
    sourceChannelId: sourceChannel.id,
    channelUsedParent: Boolean(parent),
    guildLabel: message.guild?.name,
    recordingVoiceChannelId: recording?.voiceChannel?.id,
    checkSurface: sourceAccess,
    async runMeeting({ commandName, subcommand, options }) {
      if (!guildId) return { content: 'Use this action in a server channel.' };
      // Refresh the caller, never use voice state captured during model generation.
      const { member, channel } = await sourceAccess();
      if (
        (commandName === 'qna' || subcommand === 'stop') &&
        (!recording ||
          appContext.meetingSurface?.activeSession?.(guildId)?.sessionId !== recording.sessionId)
      ) {
        return {
          content:
            'The recording ended or changed since your request. Ask again for the current meeting.',
        };
      }
      if (commandName === 'qna') {
        return runQnaAction(
          { guildId, channelId: sourceChannel.id, question: options.question },
          appContext,
        );
      }
      return runRecordAction(
        {
          guildId,
          requesterId: userId,
          voiceChannel: member.voice?.channel,
          textChannel: channel,
          subcommand,
          name: options.name ?? null,
        },
        appContext,
        recordCommand,
        botId,
      );
    },
  });
}

// Recheck immediately before each send, including later parts of a long reply.
// A check failure is terminal for this reply; no remaining source text is sent.
export async function sendHelperReply(send, reply, { message, appContext } = {}) {
  if (reply.action) return presentHelperAction(message, send, reply.action, appContext);
  let withheld = false;
  await sendDiscordMessage(async (part) => {
    if (withheld) return;
    if (reply.canPublish) {
      let allowed = false;
      try {
        allowed = (await reply.canPublish()) === true;
      } catch {
        // Access lookup outages fail closed.
      }
      if (!allowed) {
        withheld = true;
        return send(WITHHELD_ANSWER);
      }
    }
    return send(part);
  }, reply.content);
}

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

// A reply is a helper request only when its reply ping mentions Misty, it is
// inside a thread Misty owns, and the referenced message was authored by
// Misty. Fetching the message avoids trusting reference metadata that does not
// carry an author.
export async function isReplyToBotInOwnedThread(message, botId) {
  if (!message.reference || !message.channel?.isThread?.()) return false;
  if (message.channel.ownerId !== botId) return false;
  if (
    !message.mentions?.has?.(botId, {
      ignoreDirect: true,
      ignoreRoles: true,
      ignoreEveryone: true,
    })
  ) {
    return false;
  }

  try {
    const referenced = await message.fetchReference();
    return referenced?.author?.id === botId;
  } catch (e) {
    console.error('referenced message fetch failed:', e.message);
    return false;
  }
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

const HISTORY_LIMIT = 100; // Discord's messages.fetch maximum; no pagination
const THREAD_UNAVAILABLE =
  "I couldn't open a thread for that — please try again. (I may be missing the 'Create Public Threads' or 'Read Message History' permission.)";
const DM_HISTORY_UNAVAILABLE =
  "I couldn't read this conversation right now — please try again shortly.";

export function isDirectMessage(message) {
  return Boolean(
    message.channel?.isDMBased?.() || message.channel?.type === 'DM' || message.channel?.type === 1,
  );
}

export function directMessageHistoryToTurns(fetched, currentUserId, botId) {
  return threadHistoryToTurns(
    fetched.filter((message) => {
      const authorId = message.author?.id;
      return authorId === botId || authorId === currentUserId;
    }),
    botId,
  );
}

function messageIsAtOrBefore(message, boundary) {
  if (/^\d+$/.test(message.id ?? '') && /^\d+$/.test(boundary.id ?? '')) {
    return BigInt(message.id) <= BigInt(boundary.id);
  }
  return (message.createdTimestamp ?? 0) <= (boundary.createdTimestamp ?? 0);
}

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

// Handle a helper request: either a leading mention or a reply that pings Misty
// in a Misty-owned thread. Linked-only. In a channel it opens a thread; in a
// thread it fetches recent history, including the starter message. The flow
// retains user questions and removes unverified bot answers before model use.
// Never throws to discord.js.
export async function handleMention(message, { appContext, botId }) {
  const question = stripLeadingMention(message.content, botId);
  if (!question) return; // bare ping, nothing to answer

  const caller = await resolveHelperCaller(appContext, message.author.id);
  if (!caller.ok) {
    await sendDiscordMessage((part) => message.reply(part), caller.content).catch(() => {});
    return;
  }

  // Charge the authenticated requester, never an author found in thread history.
  // Reserve before any more awaits; even concurrent mentions share this limit.
  const allowance = appContext.helperRequestLimiter.tryConsume(message.author.id);
  if (!allowance.allowed) {
    await sendDiscordMessage(
      (part) => message.reply(part),
      `Please slow down a little so everyone can use the helper. Try again in ${allowance.retryAfterSeconds} seconds.`,
    ).catch(() => {});
    return;
  }

  let target;
  let turns;
  try {
    if (message.channel.isThread()) {
      target = message.channel;
      const fetched = await target.messages.fetch({ limit: HISTORY_LIMIT });
      const ordered = [...fetched.values()]
        .filter((candidate) => messageIsAtOrBefore(candidate, message))
        .sort((a, b) => (a.createdTimestamp ?? 0) - (b.createdTimestamp ?? 0));
      await prependStarterMessage(target, ordered);
      if (!ordered.some((candidate) => candidate.id && candidate.id === message.id) && message.id)
        ordered.push(message);
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
    await sendDiscordMessage((part) => message.reply(part), THREAD_UNAVAILABLE).catch(() => {});
    return;
  }
  if (!turns.length) return;

  await target.sendTyping().catch(() => {});
  // Shared-channel scope keys on the configured channel, not the thread Misty
  // opens inside it: in a thread, that's the parent channel. A tool-enabled
  // answer is scoped to that channel's configured team union (resolved by the
  // doc service); the asking person's own privileges never widen it.
  const channelId = message.channel.isThread() ? message.channel.parentId : message.channel.id;
  const reply = await prepareHelperReply(
    appContext,
    turns,
    caller.principal,
    {
      kind: 'channel',
      guildId: message.guild?.id ?? message.guildId ?? null,
      channelId: channelId ?? null,
    },
    createHelperRequest(message, appContext, botId),
  );
  await sendHelperReply((part) => target.send(part), reply, { message, appContext }).catch(() =>
    console.error('Could not deliver helper reply'),
  );
}

export async function handleDirectMessage(message, { appContext, botId }) {
  const question = (message.content ?? '').trim();
  if (!question) return;

  const caller = await resolveHelperCaller(appContext, message.author.id);
  if (!caller.ok) {
    await sendDiscordMessage((part) => message.reply(part), caller.content).catch(() => {});
    return;
  }
  const allowance = appContext.helperRequestLimiter.tryConsume(message.author.id);
  if (!allowance.allowed) {
    await sendDiscordMessage(
      (part) => message.reply(part),
      `Please slow down a little so everyone can use the helper. Try again in ${allowance.retryAfterSeconds} seconds.`,
    ).catch(() => {});
    return;
  }

  let turns;
  try {
    const fetched = await message.channel.messages.fetch({ limit: HISTORY_LIMIT });
    const ordered = [...fetched.values()]
      .filter((candidate) => messageIsAtOrBefore(candidate, message))
      .sort((a, b) => (a.createdTimestamp ?? 0) - (b.createdTimestamp ?? 0));
    if (!ordered.some((candidate) => candidate.id && candidate.id === message.id)) {
      ordered.push(message);
    }
    turns = directMessageHistoryToTurns(ordered, message.author.id, botId);
  } catch (e) {
    console.error('DM history fetch failed:', e.message);
    await sendDiscordMessage((part) => message.reply(part), DM_HISTORY_UNAVAILABLE).catch(() => {});
    return;
  }
  if (!turns.length) return;

  await message.channel.sendTyping().catch(() => {});
  // DM: a tool-enabled answer is scoped to the asking person — their own
  // authorized documents, including anything privately shared with them.
  const reply = await prepareHelperReply(
    appContext,
    turns,
    caller.principal,
    { kind: 'person' },
    createHelperRequest(message, appContext, botId),
  );
  await sendHelperReply((part) => message.channel.send(part), reply, { message, appContext }).catch(
    () => console.error('Could not deliver helper DM reply'),
  );
}
