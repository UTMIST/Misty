import { resolvePrincipal } from './auth/principal.js';
import { authorize } from './auth/policy.js';
import { DirectoryUnavailable } from './clients/directoryClient.js';
import { OUTCOME } from './toolExecutor.js';

export const LINK_PROMPT =
  'You need to link your account first. Run `/link` to identify yourself, then try again.';
export const VERIFY_UNAVAILABLE =
  "I can't verify you right now — the directory is unavailable. Please try again shortly.";
export const LLM_UNAVAILABLE =
  "I'm having trouble reaching the assistant right now — please try again shortly.";
export const EMPTY_ANSWER = "I couldn't come up with an answer that time — please try rephrasing.";
export const COULDNT_COMPLETE =
  "I couldn't finish that one — please try again, or narrow the question.";

// Derive the trusted retrieval context from the answer's destination. This is
// the single place scope is decided, from the surface, never from the model:
//   DM      -> the asking person (their own authorized documents).
//   channel -> the configured channel (the doc service resolves its team union
//              from guild + channel; an unconfigured channel returns nothing).
function retrievalContext(destination, principal) {
  if (destination?.kind === 'person') return { onBehalfOf: principal.person.id };
  if (destination?.kind === 'channel') {
    if (!destination.guildId || !destination.channelId) return null;
    return { guildId: destination.guildId, channelId: destination.channelId };
  }
  return null;
}

// Route through the tool loop only when it is enabled, wired, and the context
// could actually retrieve something. No usable context -> answer text-only
// rather than spend tool rounds that can only fail closed.
function toolsAvailable(appContext, context) {
  return Boolean(appContext.helperToolsEnabled && appContext.toolExecutor && context);
}

/** Resolve and authorize a helper-bot caller without depending on a surface. */
export async function resolveHelperCaller(appContext, discordUserId) {
  let principal;
  try {
    principal = await resolvePrincipal(appContext.directory, discordUserId);
  } catch (e) {
    if (e instanceof DirectoryUnavailable) return { ok: false, content: VERIFY_UNAVAILABLE };
    throw e;
  }

  if (!authorize('linked', principal).ok) return { ok: false, content: LINK_PROMPT };
  return { ok: true, principal };
}

/**
 * Answer an attributed helper transcript. With no `destination` (or when the
 * tool loop is disabled/unconfigured, or the context can't retrieve) this is the
 * original text-only path. When the destination supplies a retrievable context
 * and tools are enabled, it runs the authorized tool-execution loop (#71) and
 * maps its outcome to a reply.
 *
 * @param destination {kind:'person'} for a DM, or
 *   {kind:'channel', guildId, channelId} for a shared channel. Omit for the
 *   text-only path (backward compatible).
 */
export async function answerHelperTurns(appContext, turns, principal, destination) {
  const context = retrievalContext(destination, principal);

  if (!toolsAvailable(appContext, context)) {
    let content;
    try {
      ({ content } = await appContext.helperService.answer({ turns, principal }));
    } catch (err) {
      console.error('helper answer failed:', err.message);
      return LLM_UNAVAILABLE;
    }
    return content?.trim() ? content : EMPTY_ANSWER;
  }

  try {
    const { messages, system } = await appContext.helperService.prepare({ turns, principal });
    if (!messages.length) return EMPTY_ANSWER;
    const result = await appContext.toolExecutor.run({ messages, system, context, principal });
    switch (result.outcome) {
      case OUTCOME.ANSWER:
        // result.citations is available for a future surface to render sources.
        return result.content;
      case OUTCOME.EXHAUSTED:
        return COULDNT_COMPLETE;
      default:
        return EMPTY_ANSWER;
    }
  } catch (err) {
    console.error('helper tool answer failed:', err.message);
    return LLM_UNAVAILABLE;
  }
}
