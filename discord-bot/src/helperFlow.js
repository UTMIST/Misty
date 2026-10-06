import { resolvePrincipal } from './auth/principal.js';
import { authorize } from './auth/policy.js';
import { DirectoryUnavailable } from './directoryClient.js';

export const LINK_PROMPT =
  'You need to link your account first. Run `/link` to identify yourself, then try again.';
export const VERIFY_UNAVAILABLE =
  "I can't verify you right now — the directory is unavailable. Please try again shortly.";
export const LLM_UNAVAILABLE =
  "I'm having trouble reaching the assistant right now — please try again shortly.";
export const EMPTY_ANSWER = "I couldn't come up with an answer that time — please try rephrasing.";

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

/** Send an attributed helper transcript through the common helper service. */
export async function answerHelperTurns(appContext, turns, principal) {
  let content;
  try {
    ({ content } = await appContext.helperService.answer({ turns, principal }));
  } catch (err) {
    console.error('helper answer failed:', err.message);
    return LLM_UNAVAILABLE;
  }
  return content?.trim() ? content : EMPTY_ANSWER;
}
