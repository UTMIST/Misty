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
  const windowMs = windowSeconds * 1000;
  const users = new Map();

  return {
    tryConsume(discordUserId) {
      if (typeof discordUserId !== 'string' || !discordUserId) {
        throw new Error('A Discord user ID is required for helper accounting');
      }
      const time = now();
      // Lazy cleanup bounds retained state to users active in the last window.
      for (const [id, usage] of users) {
        if (time >= usage.resetsAt) users.delete(id);
      }
      const usage = users.get(discordUserId) ?? { requests: 0, resetsAt: time + windowMs };
      if (usage.requests >= maxRequests) {
        return { allowed: false, retryAfterSeconds: Math.ceil((usage.resetsAt - time) / 1000) };
      }
      usage.requests += 1;
      users.set(discordUserId, usage);
      return { allowed: true };
    },
  };
}
