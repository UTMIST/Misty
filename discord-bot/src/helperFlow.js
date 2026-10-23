import { resolvePrincipal } from './auth/principal.js';
import { authorize } from './auth/policy.js';
import { DirectoryUnavailable } from './clients/directoryClient.js';
import { OUTCOME } from './toolExecutor.js';
import { renderHelperAnswer } from './messages/helper.js';

export const LINK_PROMPT =
  'You need to link your account first. Run `/link` to identify yourself, then try again.';
export const VERIFY_UNAVAILABLE =
  "I can't verify you right now — the directory is unavailable. Please try again shortly.";
export const LLM_UNAVAILABLE =
  "I'm having trouble reaching the assistant right now — please try again shortly.";
export const EMPTY_ANSWER = "I couldn't come up with an answer that time — please try rephrasing.";
export const COULDNT_COMPLETE =
  "I couldn't finish that one — please try again, or narrow the question.";
export const WITHHELD_ANSWER =
  "I couldn't confirm continued access to the sources for that answer. Please try again.";
export const INCOMPLETE_ACTION =
  "I couldn't prepare the full request, so nothing was changed. Please check the people and other details, then ask again.";
export const MIXED_ACTION_REQUEST =
  'Please ask for document research and commands separately. Nothing was changed.';

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

  if (!authorize('linked', principal).ok || principal.person.active === false)
    return { ok: false, content: LINK_PROMPT };
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
export async function answerHelperTurns(appContext, turns, principal, destination, request) {
  const reply = await prepareHelperReply(appContext, turns, principal, destination, request);
  return reply.content;
}

/** Prepare a reply with a final access check for adapters that send it later. */
export async function prepareHelperReply(appContext, turns, principal, destination, request) {
  const context = retrievalContext(destination, principal);
  // Discord history has no trusted provenance for old bot text (including
  // pre-rollout replies, restarts, and answers sent before the flag was disabled).
  // Retain user questions for follow-up retrieval, never replay old answers or
  // cached document text without a fresh authorized read.
  const safeTurns = turns.filter((turn) => turn.role === 'user');

  if (!toolsAvailable(appContext, context)) {
    let content;
    try {
      ({ content } = await appContext.helperService.answer({ turns: safeTurns, principal }));
    } catch (err) {
      console.error('helper answer failed:', err.message);
      return { content: LLM_UNAVAILABLE };
    }
    return { content: content?.trim() ? content : EMPTY_ANSWER };
  }

  try {
    const { messages, system } = await appContext.helperService.prepare({
      turns: safeTurns,
      principal,
    });
    if (!messages.length) return { content: EMPTY_ANSWER };
    const result = await appContext.toolExecutor.run({
      messages,
      system,
      context,
      principal,
      request,
    });
    switch (result.outcome) {
      case OUTCOME.ACTION:
        return { action: result.action, canPublish: result.canPublish };
      case OUTCOME.ANSWER:
        return {
          content: renderHelperAnswer(result.content, result.citations),
          canPublish: result.canPublish,
        };
      case OUTCOME.EXHAUSTED:
        return { content: COULDNT_COMPLETE };
      case OUTCOME.WITHHELD:
        return { content: WITHHELD_ANSWER };
      case OUTCOME.INCOMPLETE_ACTION:
        return {
          content: result.reason === 'mixed_tools' ? MIXED_ACTION_REQUEST : INCOMPLETE_ACTION,
        };
      default:
        return { content: EMPTY_ANSWER };
    }
  } catch (err) {
    console.error('helper tool answer failed:', err.message);
    return { content: LLM_UNAVAILABLE };
  }
}
