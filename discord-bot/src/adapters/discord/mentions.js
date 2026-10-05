import { resolvePrincipal } from '../../auth/principal.js';
import { authorize } from '../../auth/policy.js';
import { DirectoryUnavailable } from '../../clients/directoryClient.js';

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
