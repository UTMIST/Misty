export const DEFAULT_HELPER_USER_MAX_REQUESTS = 10;
export const DEFAULT_HELPER_USER_WINDOW_SECONDS = 3600;

/**
 * In-process, per-Discord-user fixed windows. tryConsume reserves synchronously
 * so concurrent mentions cannot spend the same slot. No timers or persistence.
 */
export function createHelperRequestLimiter({
  maxRequests = DEFAULT_HELPER_USER_MAX_REQUESTS,
  windowSeconds = DEFAULT_HELPER_USER_WINDOW_SECONDS,
  now = Date.now,
} = {}) {
  if (!Number.isSafeInteger(maxRequests) || maxRequests <= 0) {
    throw new Error('HELPER_USER_MAX_REQUESTS must be a positive safe integer');
  }
  if (
    !Number.isSafeInteger(windowSeconds) ||
    windowSeconds <= 0 ||
    !Number.isSafeInteger(windowSeconds * 1000)
  ) {
    throw new Error('HELPER_USER_WINDOW_SECONDS must be a positive safe integer in milliseconds');
  }
  const users = new Map();
  let limits = { maxRequests, windowSeconds };

  function validate(next) {
    if (!Number.isSafeInteger(next.maxRequests) || next.maxRequests <= 0) {
      throw new Error('maxRequests must be a positive safe integer');
    }
    if (
      !Number.isSafeInteger(next.windowSeconds) ||
      next.windowSeconds <= 0 ||
      !Number.isSafeInteger(next.windowSeconds * 1000)
    ) {
      throw new Error('windowSeconds must be a positive safe integer in milliseconds');
    }
  }

  return {
    getLimits() {
      return { ...limits };
    },
    updateLimits(updates) {
      const next = { ...limits, ...updates };
      validate(next);
      limits = next;
      return { ...limits };
    },
    tryConsume(discordUserId) {
      if (typeof discordUserId !== 'string' || !discordUserId) {
        throw new Error('A Discord user ID is required for helper accounting');
      }
      const time = now();
      // Lazy cleanup bounds retained state to users active in the last window.
      for (const [id, usage] of users) {
        if (time >= usage.startedAt + limits.windowSeconds * 1000) users.delete(id);
      }
      const usage = users.get(discordUserId) ?? { requests: 0, startedAt: time };
      if (usage.requests >= limits.maxRequests) {
        const resetsAt = usage.startedAt + limits.windowSeconds * 1000;
        return { allowed: false, retryAfterSeconds: Math.ceil((resetsAt - time) / 1000) };
      }
      usage.requests += 1;
      users.set(discordUserId, usage);
      return { allowed: true };
    },
  };
}
